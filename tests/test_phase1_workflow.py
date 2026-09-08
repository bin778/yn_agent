import datetime as dt
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bot.db import get_conn
from bot.agents.content_editor import process_backlog
from bot.workflow import (
    ActorContext,
    AuthorizationError,
    InvalidTransitionError,
    StaleActionError,
    acknowledge,
    cancel_request,
    claim_handoffs,
    complete_handoff,
    create_revision,
    create_work_item,
    decide,
    expire_pending,
    get_approval_state,
    policy_for,
)


class Phase1WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "workflow.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def actor(self, actor_id, *roles, admin=False):
        return ActorContext.verified_actor(actor_id, roles, is_admin=admin, platform="test")

    def keyword(self, risk="low", now=None):
        return create_work_item(
            agent_id="keyword_analyst",
            output={"keyword": "경찰조사 준비", "compliance_risk": risk},
            routed_to="compliance_review.queue" if risk == "high" else "content_editor.inbox",
            model_version="test-model",
            input_summary="경찰조사 준비",
            now=now,
            db_path=self.db_path,
        )

    def test_role_policy_matrix(self):
        self.assertEqual(("keyword_approver",), policy_for("keyword_analyst", {"compliance_risk": "low"}).roles)
        self.assertEqual(
            ("keyword_approver", "legal_compliance"),
            policy_for("keyword_analyst", {"compliance_risk": "high"}).roles,
        )
        self.assertEqual(
            ("content_approver", "legal_compliance"),
            policy_for("content_editor", {"flagged_claims": ["claim"], "sensitive_flags": []}).roles,
        )
        self.assertEqual(
            ("performance_approver", "operations_owner", "legal_compliance"),
            policy_for(
                "performance_analyst",
                {"action": "PAUSE", "adjustment_percentage": 0, "sensitive_keyword": True},
            ).roles,
        )
        self.assertEqual(
            ("data_owner", "privacy_officer"),
            policy_for(
                "data_assistant", {"status_summary": "Warning", "privacy_flags": ["pii"]}
            ).acknowledgement_roles,
        )

    def test_single_role_approval_queues_handoff_only_after_approval(self):
        ref = self.keyword()
        self.assertEqual([], claim_handoffs("draft_content", db_path=self.db_path))
        state = decide(
            request_id=ref["request_id"], expected_version=1,
            actor=self.actor("keyword-owner", "keyword_approver"),
            acting_role="keyword_approver", decision="approved", db_path=self.db_path,
        )
        self.assertEqual("approved", state["status"])
        jobs = claim_handoffs("draft_content", db_path=self.db_path)
        self.assertEqual(1, len(jobs))
        complete_handoff(jobs[0]["handoff_id"], jobs[0]["claim_token"], db_path=self.db_path)
        self.assertEqual([], claim_handoffs("draft_content", db_path=self.db_path))

    def test_high_risk_needs_two_distinct_people(self):
        ref = self.keyword("high")
        first = decide(
            request_id=ref["request_id"], expected_version=1,
            actor=self.actor("person-a", "keyword_approver", "legal_compliance"),
            acting_role="keyword_approver", decision="approved", db_path=self.db_path,
        )
        self.assertEqual("pending", first["status"])
        with self.assertRaises(InvalidTransitionError):
            decide(
                request_id=ref["request_id"], expected_version=1,
                actor=self.actor("person-a", "keyword_approver", "legal_compliance"),
                acting_role="legal_compliance", decision="approved", db_path=self.db_path,
            )
        final = decide(
            request_id=ref["request_id"], expected_version=1,
            actor=self.actor("person-b", "legal_compliance"),
            acting_role="legal_compliance", decision="approved", db_path=self.db_path,
        )
        self.assertEqual("approved", final["status"])

    def test_content_entrypoint_rejects_unapproved_keyword(self):
        self.keyword()
        self.assertEqual([], process_backlog(db_path=self.db_path))

    def test_performance_handoff_waits_for_every_risk_role(self):
        ref = create_work_item(
            agent_id="performance_analyst",
            output={"action": "PAUSE", "adjustment_percentage": 0,
                    "sensitive_keyword": True, "category": "성범죄"},
            routed_to="performance_review.queue,compliance_review.queue",
            model_version="test", input_summary="sensitive pause", db_path=self.db_path,
        )
        for actor_id, role in (("performance", "performance_approver"),
                               ("operations", "operations_owner")):
            state = decide(request_id=ref["request_id"], expected_version=1,
                           actor=self.actor(actor_id, role), acting_role=role,
                           decision="approved", db_path=self.db_path)
            self.assertEqual("pending", state["status"])
            self.assertEqual([], claim_handoffs("queue_for_execution_review", db_path=self.db_path))
        state = decide(request_id=ref["request_id"], expected_version=1,
                       actor=self.actor("legal", "legal_compliance"), acting_role="legal_compliance",
                       decision="approved", db_path=self.db_path)
        self.assertEqual("approved", state["status"])
        self.assertEqual(1, len(claim_handoffs("queue_for_execution_review", db_path=self.db_path)))

    def test_expected_version_blocks_stale_action(self):
        ref = self.keyword()
        with self.assertRaises(StaleActionError):
            decide(request_id=ref["request_id"], expected_version=99,
                   actor=self.actor("owner", "keyword_approver"),
                   acting_role="keyword_approver", decision="approved", db_path=self.db_path)

    def test_permissions_fail_closed(self):
        ref = self.keyword()
        with self.assertRaises(AuthorizationError):
            decide(
                request_id=ref["request_id"], expected_version=1,
                actor=ActorContext("unknown", frozenset({"keyword_approver"}), False),
                acting_role="keyword_approver", decision="approved", db_path=self.db_path,
            )
        with self.assertRaises(AuthorizationError):
            decide(
                request_id=ref["request_id"], expected_version=1,
                actor=self.actor("lawyer", "legal_compliance"),
                acting_role="keyword_approver", decision="approved", db_path=self.db_path,
            )

    def test_rejection_is_terminal_and_requires_reason(self):
        ref = self.keyword()
        actor = self.actor("keyword-owner", "keyword_approver")
        with self.assertRaises(ValueError):
            decide(request_id=ref["request_id"], expected_version=1, actor=actor,
                   acting_role="keyword_approver", decision="rejected", db_path=self.db_path)
        state = decide(request_id=ref["request_id"], expected_version=1, actor=actor,
                       acting_role="keyword_approver", decision="rejected", reason="근거 부족",
                       db_path=self.db_path)
        self.assertEqual("rejected", state["status"])
        with self.assertRaises(StaleActionError):
            decide(request_id=ref["request_id"], expected_version=1,
                   actor=self.actor("other", "keyword_approver"), acting_role="keyword_approver",
                   decision="approved", db_path=self.db_path)

    def test_revision_creates_fresh_version_and_stales_old_request(self):
        ref = self.keyword()
        decide(request_id=ref["request_id"], expected_version=1,
               actor=self.actor("reviewer", "keyword_approver"), acting_role="keyword_approver",
               decision="revision_requested", reason="표현 수정", db_path=self.db_path)
        revised = create_revision(
            source_request_id=ref["request_id"],
            revised_output={"keyword": "경찰조사 대응 절차", "compliance_risk": "low"},
            created_by="agent:keyword_analyst", reason="표현 수정", db_path=self.db_path,
        )
        self.assertEqual(2, revised["version"])
        self.assertEqual("pending", get_approval_state(revised["request_id"], self.db_path)["status"])
        with self.assertRaises(StaleActionError):
            decide(request_id=ref["request_id"], expected_version=1,
                   actor=self.actor("other", "keyword_approver"), acting_role="keyword_approver",
                   decision="approved", db_path=self.db_path)

    def test_expiration_and_cancellation(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        expired = self.keyword(now=start)
        self.assertEqual(1, expire_pending(now=start + dt.timedelta(days=8), db_path=self.db_path))
        self.assertEqual("expired", get_approval_state(expired["request_id"], self.db_path)["status"])
        cancelled = self.keyword()
        state = cancel_request(
            cancelled["request_id"], self.actor("admin", "workflow_admin", admin=True),
            reason="중복 요청", db_path=self.db_path,
        )
        self.assertEqual("cancelled", state["status"])

    def test_data_acknowledgement_never_creates_handoff(self):
        normal = create_work_item(
            agent_id="data_assistant", output={"status_summary": "Normal", "privacy_flags": []},
            routed_to="data_assistant.tracking_status", model_version="test", input_summary="normal",
            db_path=self.db_path,
        )
        self.assertEqual("recorded", normal["status"])
        warning = create_work_item(
            agent_id="data_assistant", output={"status_summary": "Warning", "privacy_flags": ["pii"]},
            routed_to="dev_team.queue", model_version="test", input_summary="warning",
            db_path=self.db_path,
        )
        first = acknowledge(work_item_id=warning["work_item_id"], expected_version=1,
                            actor=self.actor("data", "data_owner"), acting_role="data_owner",
                            db_path=self.db_path)
        self.assertEqual("acknowledgement_pending", first["status"])
        second = acknowledge(work_item_id=warning["work_item_id"], expected_version=1,
                             actor=self.actor("privacy", "privacy_officer"), acting_role="privacy_officer",
                             db_path=self.db_path)
        self.assertEqual("acknowledged", second["status"])
        self.assertEqual([], claim_handoffs("draft_content", db_path=self.db_path))

    def test_duplicate_concurrent_decision_is_idempotent(self):
        ref = self.keyword()
        actor = self.actor("keyword-owner", "keyword_approver")

        def approve():
            return decide(request_id=ref["request_id"], expected_version=1, actor=actor,
                          acting_role="keyword_approver", decision="approved", db_path=self.db_path)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: approve(), range(2)))
        self.assertTrue(any(result.get("duplicate") for result in results))
        conn = get_conn(self.db_path)
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0])
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM handoff_jobs").fetchone()[0])
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE approval_decisions SET reason='changed'")
        conn.close()

    def test_audit_contains_trace_fields(self):
        ref = self.keyword()
        decide(request_id=ref["request_id"], expected_version=1,
               actor=self.actor("owner", "keyword_approver"), acting_role="keyword_approver",
               decision="approved", reason="ok", db_path=self.db_path)
        conn = get_conn(self.db_path)
        rows = conn.execute("SELECT * FROM audit_events ORDER BY occurred_at").fetchall()
        conn.close()
        self.assertGreaterEqual(len(rows), 4)
        self.assertTrue(all(row["work_item_id"] and row["work_item_version"] and row["correlation_id"] for row in rows))


if __name__ == "__main__":
    unittest.main()
