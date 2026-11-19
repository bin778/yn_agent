import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from bot.db import get_conn, utc_iso, utc_now
from bot.slack_adapter.app import build_dependencies, register_handlers
from bot.slack_adapter.config import SlackSettings
from bot.slack_approval_demo import ACTION_ID, REASON_MODAL, ApprovalDemo
from bot.workflow import get_approval_state


class SlackApprovalDemoTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.db_path = root / "phase4.db"
        self.phase3_path = root / "phase3.db"
        mapping_path = root / "mapping.yml"
        mapping_path.write_text("""workspace_id: TDEMO
allowed_users:
  UDEMO:
    roles: [keyword_approver]
  UOTHER:
    roles: [content_approver]
queue_channels:
  compliance_review.queue: CDEMO
  human_review.queue: CDEMO
  performance_review.queue: CDEMO
  data_assistant.tracking_status: CDEMO
  dev_team.queue: CDEMO
""", encoding="utf-8")
        deps = build_dependencies(SlackSettings("", "", mapping_path), db_path=self.phase3_path)
        self.slash_handlers = {}
        self.actions = {}
        self.views = {}
        app = Mock()
        app.command.side_effect = lambda key: lambda handler: self.slash_handlers.setdefault(key, handler)
        app.action.side_effect = lambda key: lambda handler: self.actions.setdefault(key, handler)
        app.view.side_effect = lambda key: lambda handler: self.views.setdefault(key, handler)
        register_handlers(app, deps, ApprovalDemo(deps.mapping, self.db_path))
        self.client = Mock()
        self.client.chat_postMessage.return_value = {"ts": "111.222"}
        self.respond = Mock()
        self.ack = Mock()

    def command(self, text="approval-demo"):
        self.slash_handlers["/hermes-test"](
            ack=self.ack, command={
                "text": text, "team_id": "TDEMO", "channel_id": "CDEMO",
                "user_id": "UDEMO", "trigger_id": "same-delivery",
            }, respond=self.respond, client=self.client,
        )

    def button(self):
        return self.client.chat_update.call_args.kwargs["blocks"][-1]["elements"][0]["value"]

    def approve(self, value, *, channel="CDEMO", user="UDEMO", ts="111.222", action_id=ACTION_ID):
        self.actions[action_id](
            ack=self.ack, body={
                "team": {"id": "TDEMO"}, "channel": {"id": channel},
                "message": {"ts": ts}, "user": {"id": user}, "trigger_id": "modal-trigger",
            }, action={"value": value, "action_id": action_id}, respond=self.respond, client=self.client,
        )

    def test_approval_creates_one_decision_audit_and_handoff(self):
        self.command()
        value = self.button()
        self.command()
        self.client.chat_postMessage.assert_called_once()
        self.approve(value)
        self.approve(value)
        self.assertIn("콘텐츠 초안 작업 대기열 등록", self.client.chat_update.call_args.kwargs["text"])
        with closing(get_conn(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0], 1)
            handoffs = conn.execute("SELECT handoff_type, target, status FROM handoff_jobs").fetchall()
            self.assertEqual([tuple(row) for row in handoffs], [("draft_content", "content_editor.inbox", "pending")])
            events = conn.execute("SELECT event_type FROM audit_events").fetchall()
            self.assertEqual([row[0] for row in events].count("approval.decision.approved"), 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_requests").fetchone()[0], 0)
        self.assertFalse(self.phase3_path.exists())

    def test_wrong_message_role_channel_and_version_cannot_approve(self):
        self.command()
        value = self.button()
        request_id, _ = value.split(":")
        self.approve(value, ts="333.444")
        self.approve(value, channel="COTHER")
        self.approve(value, user="UOTHER")
        self.approve(f"{request_id}:2")
        state = get_approval_state(request_id, self.db_path)
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["decisions"], [])
        self.assertEqual(self.respond.call_count, 4)

    def test_dispatch_still_accepts_phase3_status(self):
        self.command("status")
        self.assertIn("PHASE 3 TEST 연결 정상", self.respond.call_args.kwargs["text"])
        self.assertFalse(self.db_path.exists())

    def test_approval_persists_when_slack_update_fails_and_can_refresh(self):
        self.command()
        value = self.button()
        request_id, _ = value.split(":")
        self.client.chat_update.side_effect = RuntimeError("Slack unavailable")
        with self.assertLogs("hermes.slack.approval_demo", level="WARNING"):
            self.approve(value)
        self.assertIn(request_id, self.respond.call_args.kwargs["text"])
        self.assertEqual(get_approval_state(request_id, self.db_path)["status"], "approved")
        self.client.chat_update.side_effect = None
        self.command(f"approval-demo {request_id}")
        self.client.chat_postMessage.assert_called_once()
        self.assertIn("콘텐츠 초안 작업 대기열 등록", self.client.chat_update.call_args.kwargs["text"])

    def test_expired_request_refreshes_without_creating_a_handoff(self):
        self.command()
        value = self.button()
        request_id, _ = value.split(":")
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("UPDATE approval_requests SET expires_at=? WHERE request_id=?", (utc_iso(utc_now()), request_id))
        self.approve(value)
        self.assertIn("만료", self.client.chat_update.call_args.kwargs["text"])
        with closing(get_conn(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM handoff_jobs").fetchone()[0], 0)

    def test_reject_and_revision_require_reason_and_create_no_handoff(self):
        for slug, expected in (("reject", "rejected"), ("revision", "revision_requested")):
            with self.subTest(slug=slug):
                self.client.chat_postMessage.return_value = {"ts": "111.222" if slug == "reject" else "111.333"}
                self.slash_handlers["/hermes-test"](
                    ack=self.ack, command={
                        "text": "approval-demo", "team_id": "TDEMO", "channel_id": "CDEMO",
                        "user_id": "UDEMO", "trigger_id": slug,
                    }, respond=self.respond, client=self.client,
                )
                value = self.button()
                request_id, _ = value.split(":")
                self.approve(
                    value, ts=self.client.chat_postMessage.return_value["ts"],
                    action_id=f"phase4_demo_{slug}",
                )
                view = self.client.views_open.call_args.kwargs["view"]
                body = {"team": {"id": "TDEMO"}, "user": {"id": "UDEMO"}, "view": view}
                view["state"] = {"values": {"reason": {"reason": {"value": "   "}}}}
                self.views[REASON_MODAL](ack=self.ack, body=body, client=self.client)
                self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
                self.assertEqual(get_approval_state(request_id, self.db_path)["status"], "pending")
                view["state"]["values"]["reason"]["reason"]["value"] = "테스트 사유"
                self.views[REASON_MODAL](ack=self.ack, body=body, client=self.client)
                state = get_approval_state(request_id, self.db_path)
                self.assertEqual(state["status"], expected)
                self.assertEqual(state["decisions"][0]["reason"], "테스트 사유")
        with closing(get_conn(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM handoff_jobs").fetchone()[0], 0)
