from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.db import get_conn, get_work_item, upsert_tracking_status
from bot.execution import (
    DryRunAdapter,
    ExecutionRequestSpec,
    ExecutionSettings,
    FakeReadOnlyAdapter,
    ServiceContext,
    execute_request,
)
from bot.workflow import ActorContext, create_work_item, decide


POLICY_VERSION = "phase0-implementation-baseline"


class ExternalAdapter(DryRunAdapter):
    name = "unsafe_external_test"
    external_network = True


class Phase2ExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "phase2.db"
        get_conn(self.db).close()
        self.service = ServiceContext.verified_service(
            "execution-orchestrator", ["execution:request"]
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def settings(self, **overrides) -> ExecutionSettings:
        values = {
            "app_env": "test",
            "enabled": True,
            "mode": "dry_run",
            "kill_switch": False,
            "allowed_targets": frozenset({"sandbox-cms", "sandbox-ads"}),
            "max_bid_change_percent": 20.0,
            "max_budget_change_percent": 20.0,
            "allow_campaign_pause": False,
            "compliance_policy_version": POLICY_VERSION,
        }
        values.update(overrides)
        return ExecutionSettings(**values)

    def content_item(self, *, approve: bool = True, output: dict | None = None) -> dict:
        item = create_work_item(
            agent_id="content_editor",
            output=output or {"draft": "safe draft", "flagged_claims": []},
            routed_to="content_approver",
            model_version="test-model",
            input_summary="phase2 test",
            db_path=self.db,
        )
        if approve:
            decide(
                request_id=item["request_id"],
                expected_version=item["version"],
                actor=ActorContext.verified_actor("content-owner", ["content_approver"]),
                acting_role="content_approver",
                decision="approved",
                db_path=self.db,
            )
        return item

    def performance_item(self, *, adjustment: int = 10, action: str = "INCREASE") -> dict:
        item = create_work_item(
            agent_id="performance_analyst",
            output={"action": action, "adjustment_percentage": adjustment},
            routed_to="performance_approver",
            model_version="test-model",
            input_summary="phase2 performance test",
            db_path=self.db,
        )
        for index, role in enumerate(item["required_roles"], start=1):
            decide(
                request_id=item["request_id"],
                expected_version=1,
                actor=ActorContext.verified_actor(f"performance-owner-{index}", [role]),
                acting_role=role,
                decision="approved",
                db_path=self.db,
            )
        return item

    def spec(self, item: dict, **overrides) -> ExecutionRequestSpec:
        work = get_work_item(item["work_item_id"], item["version"], self.db)
        values = {
            "work_item_id": item["work_item_id"],
            "expected_version": item["version"],
            "approval_request_id": item["request_id"],
            "execution_type": "cms_publish",
            "target": "sandbox-cms",
            "payload": work["output"],
            "compliance_policy_version": POLICY_VERSION,
            "correlation_id": item["correlation_id"],
        }
        values.update(overrides)
        return ExecutionRequestSpec(**values)

    def test_environment_defaults_are_fail_closed(self):
        settings = ExecutionSettings.from_env({})
        self.assertEqual(settings.app_env, "development")
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.mode, "disabled")
        self.assertTrue(settings.kill_switch)
        self.assertEqual(settings.allowed_targets, frozenset())

    def test_invalid_environment_values_are_configuration_errors(self):
        settings = ExecutionSettings.from_env(
            {"EXECUTION_ENABLED": "yes", "EXECUTION_MODE": "live"}
        )
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.mode, "disabled")
        self.assertGreaterEqual(len(settings.configuration_errors), 2)

        item = self.content_item()
        adapter = DryRunAdapter()
        result = execute_request(
            self.spec(item), self.service, settings, adapter=adapter, db_path=self.db
        )
        self.assertEqual(result.reason, "configuration_invalid")
        self.assertEqual(adapter.calls, [])

    def test_disabled_execution_blocks_before_adapter(self):
        item = self.content_item()
        adapter = DryRunAdapter()
        result = execute_request(
            self.spec(item), self.service,
            self.settings(enabled=False), adapter=adapter, db_path=self.db,
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "execution_disabled")
        self.assertEqual(adapter.calls, [])

    def test_kill_switch_blocks_before_adapter(self):
        item = self.content_item()
        adapter = DryRunAdapter()
        result = execute_request(
            self.spec(item), self.service,
            self.settings(kill_switch=True), adapter=adapter, db_path=self.db,
        )
        self.assertEqual(result.reason, "kill_switch_active")
        self.assertEqual(adapter.calls, [])

    def test_unverified_or_unauthorized_service_is_denied(self):
        item = self.content_item()
        unverified = ServiceContext("unknown", frozenset({"execution:request"}), False)
        result = execute_request(
            self.spec(item, payload={"case": 1}), unverified,
            self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(result.reason, "service_identity_unverified")

        unauthorized = ServiceContext.verified_service("reader", ["execution:read"])
        result = execute_request(
            self.spec(item, payload={"case": 2}), unauthorized,
            self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(result.reason, "service_permission_denied")

    def test_unapproved_work_never_reaches_adapter(self):
        item = self.content_item(approve=False)
        adapter = DryRunAdapter()
        result = execute_request(
            self.spec(item), self.service, self.settings(), adapter=adapter, db_path=self.db
        )
        self.assertEqual(result.reason, "work_item_not_approved")
        self.assertEqual(adapter.calls, [])

    def test_old_version_is_stale_even_if_previously_approved(self):
        first = self.content_item()
        create_work_item(
            agent_id="content_editor",
            output={"draft": "version two", "flagged_claims": []},
            routed_to="content_approver",
            model_version="test-model",
            input_summary="revision",
            work_item_id=first["work_item_id"],
            version=2,
            parent_version=1,
            db_path=self.db,
        )
        result = execute_request(
            self.spec(first), self.service, self.settings(),
            adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(result.reason, "stale_work_item_version")

    def test_approval_request_must_match_exact_work_version(self):
        item = self.content_item()
        result = execute_request(
            self.spec(item, approval_request_id="forged-request"),
            self.service, self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(result.reason, "approval_request_mismatch")

    def test_write_payload_must_be_the_exact_approved_output(self):
        item = self.content_item()
        adapter = DryRunAdapter()
        result = execute_request(
            self.spec(item, payload={"draft": "changed after approval"}),
            self.service,
            self.settings(),
            adapter=adapter,
            db_path=self.db,
        )
        self.assertEqual(result.reason, "payload_not_approved")
        self.assertEqual(adapter.calls, [])

    def test_execution_type_must_match_ai_employee_and_correlation(self):
        item = self.content_item()
        wrong_agent_type = execute_request(
            self.spec(
                item,
                execution_type="ads_report_read",
                target="sandbox-ads",
                payload={"scope": "test"},
            ),
            self.service,
            self.settings(mode="read_only"),
            adapter=FakeReadOnlyAdapter(),
            db_path=self.db,
        )
        self.assertEqual(
            wrong_agent_type.reason, "agent_not_authorized_for_execution_type"
        )

        correlation_mismatch = execute_request(
            self.spec(item, payload={"case": "correlation"}, correlation_id="forged"),
            self.service,
            self.settings(),
            adapter=DryRunAdapter(),
            db_path=self.db,
        )
        self.assertEqual(correlation_mismatch.reason, "correlation_id_mismatch")
        conn = get_conn(self.db)
        row = conn.execute(
            """SELECT correlation_id FROM execution_requests
               WHERE execution_request_id=?""",
            (correlation_mismatch.execution_request_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row["correlation_id"], item["correlation_id"])

    def test_allowlist_and_policy_version_are_mandatory(self):
        item = self.content_item()
        denied = execute_request(
            self.spec(item), self.service,
            self.settings(allowed_targets=frozenset()),
            adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(denied.reason, "target_not_allowed")
        mismatch = execute_request(
            self.spec(item, payload={"case": "policy"}, compliance_policy_version="old"),
            self.service, self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(mismatch.reason, "compliance_policy_version_mismatch")

    def test_read_only_mode_rejects_write_and_can_simulate_read(self):
        item = self.content_item()
        read_settings = self.settings(mode="read_only")
        blocked = execute_request(
            self.spec(item), self.service, read_settings,
            adapter=FakeReadOnlyAdapter(), db_path=self.db,
        )
        self.assertEqual(blocked.reason, "mode_disallows_operation")

        adapter = FakeReadOnlyAdapter()
        succeeded = execute_request(
            self.spec(
                item, execution_type="cms_read", payload={"query": "preview"}
            ),
            self.service, read_settings, adapter=adapter, db_path=self.db,
        )
        self.assertEqual(succeeded.status, "read_only_succeeded")
        self.assertFalse(succeeded.response_summary["transmitted"])
        self.assertEqual(len(adapter.calls), 1)

    def test_ads_guard_requires_noncritical_tracking_and_limits(self):
        item = self.performance_item()
        spec = self.spec(
            item,
            execution_type="ads_bid_update",
            target="sandbox-ads",
        )
        missing = execute_request(
            spec, self.service, self.settings(), adapter=DryRunAdapter(), db_path=self.db
        )
        self.assertEqual(missing.reason, "tracking_status_missing")

        stale_item = self.performance_item(adjustment=9)
        upsert_tracking_status("Normal", {}, stale_item["work_item_id"], 1, self.db)
        conn = get_conn(self.db)
        conn.execute(
            "UPDATE tracking_status SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=1"
        )
        conn.commit()
        conn.close()
        stale = execute_request(
            self.spec(
                stale_item, execution_type="ads_bid_update", target="sandbox-ads"
            ),
            self.service,
            self.settings(),
            adapter=DryRunAdapter(),
            db_path=self.db,
        )
        self.assertEqual(stale.reason, "tracking_status_stale")

        critical_item = self.performance_item(adjustment=11)
        upsert_tracking_status(
            "Critical", {}, critical_item["work_item_id"], 1, self.db
        )
        critical = execute_request(
            self.spec(
                critical_item, execution_type="ads_bid_update", target="sandbox-ads",
            ),
            self.service, self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(critical.reason, "tracking_status_critical")

        over_limit_item = self.performance_item(adjustment=21)
        upsert_tracking_status(
            "Normal", {}, over_limit_item["work_item_id"], 1, self.db
        )
        over_limit = execute_request(
            self.spec(
                over_limit_item, execution_type="ads_bid_update", target="sandbox-ads",
            ),
            self.service, self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(over_limit.reason, "bid_change_limit_exceeded")

    def test_campaign_pause_is_denied_by_default(self):
        item = self.performance_item(adjustment=0, action="PAUSE")
        upsert_tracking_status("Normal", {}, item["work_item_id"], 1, self.db)
        result = execute_request(
            self.spec(
                item, execution_type="ads_campaign_pause", target="sandbox-ads",
            ),
            self.service, self.settings(), adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(result.reason, "campaign_pause_not_allowed")

    def test_dry_run_succeeds_once_and_duplicate_is_idempotent(self):
        item = self.content_item()
        adapter = DryRunAdapter()
        first = execute_request(
            self.spec(item), self.service, self.settings(), adapter=adapter, db_path=self.db
        )
        second = execute_request(
            self.spec(item), self.service, self.settings(), adapter=adapter, db_path=self.db
        )
        self.assertEqual(first.status, "dry_run_succeeded")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.execution_request_id, second.execution_request_id)
        self.assertEqual(len(adapter.calls), 1)

    def test_timeout_becomes_unknown_and_is_never_retried(self):
        item = self.content_item()
        adapter = DryRunAdapter(simulate_timeout=True)
        first = execute_request(
            self.spec(item), self.service, self.settings(), adapter=adapter, db_path=self.db
        )
        second = execute_request(
            self.spec(item), self.service, self.settings(), adapter=adapter, db_path=self.db
        )
        self.assertEqual(first.status, "unknown")
        self.assertEqual(first.reason, "adapter_timeout_no_retry")
        self.assertTrue(second.duplicate)
        self.assertEqual(len(adapter.calls), 1)

    def test_external_adapter_is_forbidden_in_phase2(self):
        item = self.content_item()
        result = execute_request(
            self.spec(item), self.service, self.settings(),
            adapter=ExternalAdapter(), db_path=self.db,
        )
        self.assertEqual(result.reason, "untrusted_adapter_forbidden_phase2")

    def test_persisted_payloads_and_results_are_redacted(self):
        payload = {
            "api_key": "super-secret-value",
            "contact": "person@example.com / 010-1234-5678 / 900101-1234567",
            "note": "client private narrative",
            "flagged_claims": [],
        }
        item = self.content_item(output=payload)
        result = execute_request(
            self.spec(item), self.service, self.settings(),
            adapter=DryRunAdapter(), db_path=self.db,
        )
        self.assertEqual(result.status, "dry_run_succeeded")
        conn = get_conn(self.db)
        row = conn.execute(
            """SELECT payload_summary_json, response_summary_json
               FROM execution_requests WHERE execution_request_id=?""",
            (result.execution_request_id,),
        ).fetchone()
        persisted = row["payload_summary_json"] + row["response_summary_json"]
        conn.close()
        self.assertNotIn("super-secret-value", persisted)
        self.assertNotIn("person@example.com", persisted)
        self.assertNotIn("010-1234-5678", persisted)
        self.assertNotIn("900101-1234567", persisted)
        self.assertNotIn("client private narrative", persisted)
        self.assertIn("[REDACTED]", persisted)

    def test_every_attempt_has_audit_trace_and_attempts_are_append_only(self):
        item = self.content_item()
        result = execute_request(
            self.spec(item), self.service, self.settings(),
            adapter=DryRunAdapter(), db_path=self.db,
        )
        conn = get_conn(self.db)
        request_row = conn.execute(
            "SELECT * FROM execution_requests WHERE execution_request_id=?",
            (result.execution_request_id,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM execution_attempts WHERE execution_request_id=?",
            (result.execution_request_id,),
        ).fetchone()
        audit = conn.execute(
            """SELECT * FROM audit_events
               WHERE event_type='execution.dry_run_succeeded'
                 AND work_item_id=? AND work_item_version=1""",
            (item["work_item_id"],),
        ).fetchone()
        self.assertEqual(request_row["correlation_id"], item["correlation_id"])
        self.assertEqual(attempt["correlation_id"], item["correlation_id"])
        self.assertEqual(audit["actor_id"], "execution-orchestrator")
        self.assertEqual(audit["correlation_id"], item["correlation_id"])
        requested_audit = conn.execute(
            """SELECT * FROM audit_events
               WHERE event_type='execution.requested'
                 AND work_item_id=? AND work_item_version=1""",
            (item["work_item_id"],),
        ).fetchone()
        self.assertIsNotNone(requested_audit)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE execution_attempts SET reason='changed' WHERE attempt_id=?",
                (attempt["attempt_id"],),
            )
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    unittest.main()
