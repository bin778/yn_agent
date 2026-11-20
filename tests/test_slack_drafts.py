import copy
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from bot.agents.content_editor import ContentRiskAssessment, SlackContentEditorOutput
from bot.db import get_conn, get_work_item
from bot.schemas import ContentEditorOutput
from bot.slack_adapter.app import build_dependencies, register_handlers
from bot.slack_adapter.blocks import seoul_time_label
from bot.slack_adapter.config import SlackConfigurationError, SlackMapping, SlackSettings
from bot.slack_adapter.drafts import (
    COMMAND, EDIT_ACTION, EDIT_MODAL, SlackDrafts, approval_mode_from_env,
)
from bot.slack_adapter.legal_evidence import NAVIGATE, OPEN, SAVE
from bot.workflow import ActorContext, ContentApprovalMode, get_approval_state, read_legal_reviews, revise_pending_content


class SlackDraftsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "hermes.db"
        example_mapping = Path(__file__).resolve().parents[1] / "slack" / "mapping.prod.example.yaml"
        deps = build_dependencies(SlackSettings("", "", example_mapping), db_path=self.db)
        self.mapping = SlackMapping(
            "TDEMO",
            {"UAUTHOR": frozenset({"content_approver", "legal_compliance"}),
            "ULEGAL": frozenset({"legal_compliance"}),
            "UEDITOR": frozenset({"content_approver"})},
            deps.mapping.queue_channels,
        )
        self.drafts = SlackDrafts(self.mapping, self.db, "CDRAFT")
        self.slash, self.actions, self.views = {}, {}, {}
        app = Mock()
        app.command.side_effect = lambda key: lambda handler: self.slash.setdefault(key, handler)
        app.action.side_effect = lambda key: lambda handler: self.actions.setdefault(key, handler)
        app.view.side_effect = lambda key: lambda handler: self.views.setdefault(key, handler)
        register_handlers(app, deps, drafts=self.drafts)
        self.ack = Mock()
        self.respond = Mock()
        self.client = Mock()
        self.client.chat_postMessage.return_value = {"ts": "1.2"}
        self.model = ContentEditorOutput(
            target_keyword="상담 준비", title="상담 전 준비하기", hook="질문을 정리합니다.",
            body="상담 일정과 필요한 자료를 확인합니다.", reason="일반 정보",
        )
        patcher = patch("bot.agents.content_editor.call_structured", return_value=self.model)
        self.call_model = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("bot.agents.content_editor.get_model_identity", return_value="mock-model")
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, **changes):
        command = {
            "team_id": "TDEMO", "channel_id": "CDRAFT", "user_id": "UAUTHOR",
            "trigger_id": "delivery", "text": "상담 준비",
        }
        self.slash[COMMAND](ack=self.ack, command={**command, **changes}, respond=self.respond)

    def approve(self, user="UAUTHOR", value=None, ts="1.2"):
        self.actions["hermes_draft_approve"](
            ack=self.ack, body={"team": {"id": "TDEMO"}, "channel": {"id": "CDRAFT"},
                                "user": {"id": user}, "message": {"ts": ts}},
            action={"action_id": "hermes_draft_approve", "value": value or self.approval_button()},
            respond=self.respond, client=self.client,
        )

    def approval_button(self):
        return self.client.chat_update.call_args.kwargs["blocks"][-1]["elements"][0]["value"]

    def test_command_worker_and_real_approval_engine(self):
        self.submit()
        self.submit()
        self.ack.assert_called_with()
        self.call_model.assert_not_called()
        self.assertTrue(self.drafts.process_next(self.client))
        self.assertFalse(self.drafts.process_next(self.client))
        self.call_model.assert_called_once()
        self.assertIn("상담 준비", self.call_model.call_args.kwargs["user_prompt"])
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs["channel"], "CDRAFT")
        self.assertIn("상담 전 준비하기", self.client.chat_update.call_args.kwargs["text"])
        self.assertIn("승인 진행: 0/1명", self.client.chat_update.call_args.kwargs["text"])
        self.client.chat_postMessage.assert_called_once()
        request_id = self.approval_button().split(":")[0]
        self.approve()
        self.assertEqual(get_approval_state(request_id, self.db)["status"], "approved")
        self.assertIn("발행 검토 대기열 등록", self.client.chat_update.call_args.kwargs["text"])
        self.assertIn("승인 진행: 1/1명", self.client.chat_update.call_args.kwargs["text"])
        self.respond.assert_called_with(
            response_type="ephemeral",
            replace_original=False,
            text=("승인 완료 (1/1명) · 발행 검토 대기열에 등록했습니다. "
                  "발행 담당자는 /hermes-publish에서 확인할 수 있습니다."),
        )
        with closing(get_conn(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT handoff_type FROM handoff_jobs").fetchone()[0],
                             "queue_for_publish")

    def test_publish_retry_loads_saved_draft_and_updates_same_message(self):
        self.submit()
        self.client.chat_update.side_effect = RuntimeError("temporary Slack problem")
        self.assertTrue(self.drafts.process_next(self.client))
        self.assertFalse(self.drafts.process_next(self.client))
        with closing(get_conn(self.db)) as conn, conn:
            conn.execute("UPDATE slack_draft_jobs SET retry_at=0")
        restarted = SlackDrafts(self.mapping, self.db, "CDRAFT")
        self.client.chat_update.side_effect = None
        self.assertTrue(restarted.process_next(self.client))
        self.call_model.assert_called_once()
        self.client.chat_postMessage.assert_called_once()
        self.assertEqual(self.client.chat_update.call_args.kwargs["ts"], "1.2")

    def test_flagged_draft_requires_distinct_content_and_legal_approvals(self):
        self.model.flagged_claims = ["outcome_guarantee"]
        self.submit()
        self.drafts.process_next(self.client)
        request_id = self.approval_button().split(":")[0]
        self.approve()
        self.approve()
        self.assertEqual(get_approval_state(request_id, self.db)["status"], "pending")
        self.assertIn("승인 권한: 법무·컴플라이언스 승인자", self.client.chat_update.call_args.kwargs["text"])
        self.assertIn("승인 진행: 1/2명", self.client.chat_update.call_args.kwargs["text"])
        self.respond.assert_called_with(
            response_type="ephemeral", replace_original=False, text="승인 진행 1/2명 · 남은 승인 권한: 법무·컴플라이언스 승인자",
        )
        self.approve("ULEGAL")
        self.assertIn("승인 진행: 2/2명", self.client.chat_update.call_args.kwargs["text"])
        decisions = get_approval_state(request_id, self.db)["decisions"]
        self.assertEqual({item["actor_role"] for item in decisions}, {"content_approver", "legal_compliance"})
        self.assertEqual(get_approval_state(request_id, self.db)["status"], "approved")

    def test_denies_wrong_workspace_channel_or_role(self):
        for fields in (
            {"team_id": "TOTHER"}, {"channel_id": "COTHER"}, {"user_id": "ULEGAL"},
            {"user_id": "UUNKNOWN"}, {"text": "  "},
        ):
            self.submit(**fields)
        self.assertFalse(self.drafts.process_next(self.client))
        self.call_model.assert_not_called()

    def test_generation_failure_reports_to_requester_without_publishing(self):
        self.submit()
        self.call_model.side_effect = RuntimeError("sensitive provider diagnostic")
        self.assertTrue(self.drafts.process_next(self.client))
        self.assertFalse(self.drafts.process_next(self.client))
        self.client.chat_postMessage.assert_not_called()
        self.assertEqual(self.client.chat_postEphemeral.call_args.kwargs["user"], "UAUTHOR")
        self.assertNotIn("diagnostic", self.client.chat_postEphemeral.call_args.kwargs["text"])

    def edit_view(self, *, user="UAUTHOR"):
        self.actions[EDIT_ACTION](
            ack=self.ack,
            body={"team": {"id": "TDEMO"}, "channel": {"id": "CDRAFT"},
                  "user": {"id": user}, "message": {"ts": "1.2"}, "trigger_id": "edit"},
            action={"action_id": EDIT_ACTION, "value": self.approval_button()},
            respond=self.respond, client=self.client,
        )
        view = copy.deepcopy(self.client.views_open.call_args.kwargs["view"])
        view["state"] = {"values": {
            block["block_id"]: {"value": {"value": block["element"].get("initial_value")}}
            for block in view["blocks"]
        }}
        return view

    def save_view(self, view, user="UAUTHOR"):
        self.views[EDIT_MODAL](
            ack=self.ack, body={"team": {"id": "TDEMO"}, "user": {"id": user}, "view": view},
        )

    def test_edit_saves_new_version_and_reuses_card_after_retry(self):
        self.model.body = "가" * 2000
        self.submit()
        self.drafts.process_next(self.client)
        old_button = self.approval_button()
        old_request = old_button.split(":")[0]
        self.assertIn("승인 권한: 콘텐츠 승인자", self.client.chat_update.call_args.kwargs["text"])
        self.assertIn("(한국 시간)", self.client.chat_update.call_args.kwargs["text"])
        view = self.edit_view()
        values = view["state"]["values"]
        self.assertEqual(values["title_1"]["value"]["value"], self.model.title)
        self.assertIn("body_2", values)
        values["body_2"]["value"]["value"] = "새 문단"
        self.save_view(view)
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "update")
        self.save_view(view)
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")

        risk = ContentRiskAssessment(flagged_claims=["검토 필요"], sensitive_flags=[])
        with patch("bot.slack_adapter.drafts.assess_edited_content", return_value=risk) as assess:
            self.client.chat_update.side_effect = RuntimeError("delivery unavailable")
            self.assertTrue(self.drafts.process_next(self.client))
            old = get_approval_state(old_request, self.db)
            self.assertEqual(old["status"], "revision_requested")
            self.assertEqual(get_work_item(old["work_item_id"], 1, self.db)["output"]["body"], "가" * 2000)
            self.assertEqual(get_work_item(old["work_item_id"], 2, self.db)["output"]["body"], "가" * 1400 + "새 문단")
            with closing(get_conn(self.db)) as conn, conn:
                conn.execute("UPDATE slack_draft_edits SET retry_at=0")
            self.client.chat_update.side_effect = None
            self.assertTrue(SlackDrafts(self.mapping, self.db, "CDRAFT").process_next(self.client))
            assess.assert_called_once()

        self.assertEqual(self.client.chat_update.call_args.kwargs["ts"], "1.2")
        self.client.chat_postMessage.assert_called_once()
        current = self.approval_button().split(":")[0]
        self.approve(value=old_button)
        self.assertEqual(get_approval_state(current, self.db)["decisions"], [])
        self.approve()
        self.assertEqual(get_approval_state(current, self.db)["status"], "pending")
        self.approve("ULEGAL")
        self.assertEqual(get_approval_state(current, self.db)["status"], "approved")
        with closing(get_conn(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT work_item_version FROM handoff_jobs").fetchone()[0], 2)

    def test_edit_checks_permission_and_rejects_stale_submission(self):
        self.submit()
        self.drafts.process_next(self.client)
        view = self.edit_view()
        self.save_view(view, user="ULEGAL")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        self.save_view(view)
        self.assertIn("바뀐 내용이 없습니다.", self.ack.call_args.kwargs["errors"]["title_1"])
        self.approve()
        view["state"]["values"]["body_1"]["value"]["value"] = "뒤늦은 변경"
        self.save_view(view)
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        self.assertFalse(self.drafts.process_next(self.client))

    def test_expiry_label_formats_seoul_noon(self):
        self.assertEqual(seoul_time_label("2026-11-27T03:38:12.339579+00:00"),
                         "2026년 11월 27일(금) 오후 12:38 (한국 시간)")

    def test_single_mode_applies_only_to_new_drafts_and_preserves_risk_flags(self):
        self.model.flagged_claims = ["outcome_guarantee"]
        self.submit(trigger_id="risk-based")
        self.assertTrue(self.drafts.process_next(self.client))
        old_button = self.approval_button()
        self.approve()
        self.assertIn("승인 진행: 1/2명", self.client.chat_update.call_args.kwargs["text"])

        single = SlackDrafts(self.mapping, self.db, "CDRAFT", approval_mode=ContentApprovalMode.SINGLE)
        self.client.chat_postMessage.return_value = {"ts": "1.3"}
        self.submit(trigger_id="single")
        self.assertTrue(single.process_next(self.client))
        request_id = self.approval_button().split(":")[0]
        state = get_approval_state(request_id, self.db)
        work = get_work_item(state["work_item_id"], state["version"], self.db)
        self.assertEqual(work["risk_level"], "high")
        self.assertEqual(work["output"]["flagged_claims"], ["outcome_guarantee"])
        self.assertIn("승인 진행: 0/1명", self.client.chat_update.call_args.kwargs["text"])
        self.approve(ts="1.3")
        self.assertEqual(get_approval_state(request_id, self.db)["status"], "approved")
        self.assertIn("승인 진행: 1/1명", self.client.chat_update.call_args.kwargs["text"])

        self.approve(value=old_button)
        self.assertEqual(get_approval_state(old_button.split(":")[0], self.db)["status"], "pending")
        self.assertIn("승인 진행: 1/2명", self.client.chat_update.call_args.kwargs["text"])

    def test_revision_inherits_single_policy_even_when_risk_is_added(self):
        self.submit()
        single = SlackDrafts(self.mapping, self.db, "CDRAFT", approval_mode=ContentApprovalMode.SINGLE)
        self.assertTrue(single.process_next(self.client))
        original_id = self.approval_button().split(":")[0]
        revision = revise_pending_content(
            request_id=original_id, expected_version=1, db_path=self.db,
            actor=ActorContext.verified_actor("slack:TDEMO:UAUTHOR", ["content_approver"], platform="slack"),
            revised_output={**self.model.model_dump(), "sensitive_flags": ["legal review"]},
        )
        self.assertEqual(revision["policy_key"], "content_single")
        self.assertEqual(revision["required_roles"], ["content_approver"])
        self.assertEqual(revision["version"], 2)

    def test_single_mode_requires_staging_with_external_execution_disabled(self):
        self.assertIs(approval_mode_from_env({}), ContentApprovalMode.RISK_BASED)
        env = {
            "SLACK_CONTENT_APPROVAL_MODE": "single", "APP_ENV": "staging",
            "EXECUTION_ENABLED": "false", "EXECUTION_MODE": "disabled",
        }
        self.assertIs(approval_mode_from_env(env), ContentApprovalMode.SINGLE)
        for invalid in (
            {"APP_ENV": "production"}, {"EXECUTION_ENABLED": "true"},
            {"EXECUTION_MODE": "dry_run"}, {"SLACK_CONTENT_APPROVAL_MODE": "unknown"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SlackConfigurationError):
                approval_mode_from_env({**env, **invalid})

    def review_view(self, *, user="UAUTHOR", index=0):
        identity = {"team": {"id": "TDEMO"}, "user": {"id": user}}
        self.actions[OPEN](
            ack=self.ack, client=self.client, respond=self.respond,
            body={**identity, "channel": {"id": "CDRAFT"}, "message": {"ts": "1.2"}, "trigger_id": "review"},
            action={"value": self.approval_button()},
        )
        view = self.client.views_open.call_args.kwargs["view"]
        self.actions[NAVIGATE](
            ack=self.ack, client=self.client, action={"value": f"claim:{index}"},
            body={**identity, "view": {**view, "id": "VREVIEW", "hash": "latest"}},
        )
        return copy.deepcopy(self.client.views_update.call_args.kwargs["view"])

    def save_review(self, view, *, user="UAUTHOR", status="confirmed",
                    source="https://www.law.go.kr/법령/테스트", reference="테스트 조문과 시행일"):
        fields = {
            "result": {"selected_option": {"value": status}},
            "source_url": {"value": source},
            "source_reference": {"value": reference},
            "note": {"value": "테스트 검토 의견"},
        }
        self.views[SAVE](ack=self.ack, client=self.client, body={
            "team": {"id": "TDEMO"}, "user": {"id": user},
            "view": {**view, "state": {"values": {key: {"value": value} for key, value in fields.items()}}},
        })

    def test_single_approver_verifies_each_claim_before_approval(self):
        self.drafts.approval_mode = ContentApprovalMode.SINGLE
        claims = ["테스트 기한 문장", "테스트 요건 문장"]
        self.call_model.return_value = SlackContentEditorOutput(
            **{**self.model.model_dump(), "body": "\n".join(claims)}, legal_claims=claims,
        )
        self.submit()
        self.drafts.process_next(self.client)
        self.assertEqual(self.call_model.call_args.kwargs["schema_model"], SlackContentEditorOutput)
        request_id = self.approval_button().split(":")[0]
        self.assertIn("법률 근거 확인 0/2건", self.client.chat_update.call_args.kwargs["text"])
        self.approve()
        self.assertIn("모든 대상 문장", self.respond.call_args.kwargs["text"])
        self.assertFalse(self.respond.call_args.kwargs["replace_original"])

        first = self.review_view()
        self.save_review(first, source="https://www.law.go.kr.example.com/pretend")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        self.save_review(first)
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "update")
        self.assertIn("법률 근거 확인 1/2건", self.client.chat_update.call_args.kwargs["text"])
        second = self.review_view(index=1)
        self.save_review(second, status="needs_revision", source="", reference="")
        self.approve()
        self.assertEqual(get_approval_state(request_id, self.db)["decisions"], [])
        with closing(get_conn(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM handoff_jobs").fetchone()[0], 0)

        self.save_review(second)
        self.approve()
        self.assertEqual(get_approval_state(request_id, self.db)["status"], "approved")
        self.assertEqual({row["reviewed_by"] for row in read_legal_reviews(request_id, self.db).values()},
                         {"slack:TDEMO:UAUTHOR"})
        with closing(get_conn(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM handoff_jobs").fetchone()[0], 1)

    def test_legal_review_requires_legal_role_in_risk_based_and_resets_on_revision(self):
        claim = "테스트 법정 기한 문장"
        self.call_model.return_value = SlackContentEditorOutput(
            **{**self.model.model_dump(), "body": claim}, legal_claims=[claim],
        )
        self.submit()
        self.drafts.process_next(self.client)
        old_request = self.approval_button().split(":")[0]
        self.assertEqual([item["role"] for item in get_approval_state(old_request, self.db)["requirements"]],
                         ["content_approver", "legal_compliance"])

        # A content approver without the legal role can inspect evidence but cannot save it.
        viewer = self.review_view(user="UEDITOR")
        self.assertNotIn("submit", viewer)
        self.save_review(viewer, user="UEDITOR")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        reviewer = self.review_view(user="ULEGAL")
        self.save_review(reviewer, user="ULEGAL")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "update")

        edit = self.edit_view()
        replacement = "수정된 테스트 법정 기한 문장"
        edit["state"]["values"]["body_1"]["value"]["value"] = replacement
        self.save_view(edit)
        risk = ContentRiskAssessment(flagged_claims=[], sensitive_flags=[], legal_claims=[replacement])
        with patch("bot.slack_adapter.drafts.assess_edited_content", return_value=risk):
            self.drafts.process_next(self.client)
        new_request = self.approval_button().split(":")[0]
        self.assertNotEqual(old_request, new_request)
        self.assertEqual(len(read_legal_reviews(old_request, self.db)), 1)
        self.assertEqual(read_legal_reviews(new_request, self.db), {})
        self.save_review(reviewer, user="ULEGAL")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        self.approve()
        self.assertEqual(get_approval_state(new_request, self.db)["decisions"], [])
