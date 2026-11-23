import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from bot import workflow
from bot.agents.content_editor import SlackContentEditorOutput
from bot.db import get_conn, get_tracking_status, upsert_tracking_status
from bot.schemas import DataAssistantOutput, KeywordAnalystOutput, PerformanceAnalystOutput
from bot.slack_adapter.app import build_dependencies, register_handlers
from bot.slack_adapter.config import SlackMapping, SlackSettings
from bot.slack_adapter.team import COMMAND, SlackTeamBriefs


class SlackTeamTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "hermes.db"
        sample = Path(__file__).resolve().parents[1] / "slack" / "mapping.prod.example.yaml"
        deps = build_dependencies(SlackSettings("", "", sample), db_path=self.db)
        self.mapping = SlackMapping(
            "TTEAM", {"UKEYWORD": frozenset({"keyword_approver", "legal_compliance"}),
                    "ULEGAL": frozenset({"legal_compliance"}),
                    "UCONTENT": frozenset({"content_approver"})}, deps.mapping.queue_channels,
        )
        self.briefs = self.new_worker()
        self.slash, self.actions = {}, {}
        app = Mock()
        app.command.side_effect = lambda name: lambda fn: self.slash.setdefault(name, fn)
        app.action.side_effect = lambda name: lambda fn: self.actions.setdefault(name, fn)
        register_handlers(app, deps, team_briefs=self.briefs)
        self.client = Mock()
        self.client.chat_postMessage.side_effect = (
            lambda **kwargs: {"ts": f"1.{self.client.chat_postMessage.call_count}"}
        )
        fixtures = {
            "keyword_analyst": KeywordAnalystOutput(items=[{
                "keyword": "상담 준비", "estimated_search_volume": 80, "intent_type": "Information",
                "urgency_level": 2, "compliance_risk": "high", "reason": "검색 의도 확인",
            }]),
            "data_assistant": DataAssistantOutput(status_summary="Normal", reason="전환 수집 정상"),
            "performance_analyst": PerformanceAnalystOutput(recommendations=[{
                "keyword": "상담", "action": "HOLD", "adjustment_percentage": 0, "rationale": "유입 유지",
            }]),
            "content_editor": SlackContentEditorOutput(
                title="상담 전 준비", target_keyword="상담 준비", hook="절차 안내",
                body="자료를 정리합니다.", reason="검색 의도를 반영한 초안",
            ),
        }
        self.models = {
            name: self.enterContext(patch(f"bot.agents.{name}.call_structured", return_value=result))
            for name, result in fixtures.items()
        }
        for name in fixtures:
            self.enterContext(patch(f"bot.agents.{name}.get_model_identity", return_value="test/model"))

    def new_worker(self):
        return SlackTeamBriefs(
            self.mapping, self.db, "CCHANNEL", approval_mode=workflow.ContentApprovalMode.RISK_BASED,
        )

    def submit(self, **changes):
        command = {
            "team_id": "TTEAM", "channel_id": "CCHANNEL", "user_id": "UKEYWORD",
            "trigger_id": "request", "text": "brief",
        }
        self.slash[COMMAND](ack=Mock(), command={**command, **changes}, respond=Mock())

    def drain(self, worker=None):
        for _ in range(15):
            if not (worker or self.briefs).process_next(self.client):
                return
        self.fail("worker did not drain")

    def brief(self):
        with closing(get_conn(self.db)) as conn:
            rows = conn.execute("SELECT * FROM team_briefs").fetchall()
        self.assertEqual(len(rows), 1)
        return dict(rows[0])

    def approve_keyword(self, user):
        action_id = "hermes_team_keyword_approve"
        card = next(call.kwargs for call in self.client.chat_update.call_args_list
                    if any(el.get("action_id") == action_id
                           for block in call.kwargs.get("blocks", []) for el in block.get("elements", [])))
        button = next(el for block in card["blocks"] for el in block.get("elements", [])
                      if el["action_id"] == action_id)
        self.actions[action_id](
            ack=Mock(), body={"team": {"id": "TTEAM"}, "channel": {"id": "CCHANNEL"},
                              "user": {"id": user}, "message": {"ts": card["ts"]}},
            action={"action_id": action_id, "value": button["value"]}, respond=Mock(), client=self.client,
        )

    def test_request_roles_and_candidate_approval_through_bolt_handlers(self):
        self.submit(channel_id="COTHER")
        self.submit(user_id="UUNKNOWN")
        self.assertFalse(self.briefs.process_next(self.client))
        self.submit()
        self.submit()
        self.drain()
        brief = self.brief()
        self.assertIn("키워드 승인 대기", self.client.chat_update.call_args_list[-2].kwargs["text"])
        self.assertIsNone(get_tracking_status(self.db))
        model = self.models["content_editor"]
        model.assert_not_called()
        self.approve_keyword("UCONTENT")
        self.approve_keyword("UKEYWORD")
        self.approve_keyword("UKEYWORD")
        self.drain()
        model.assert_not_called()

        self.approve_keyword("ULEGAL")
        # Simulate the worker dying after claiming the approved handoff.
        self.assertEqual(workflow.claim_handoffs("draft_content", db_path=self.db), [])
        claims = workflow.claim_handoffs("draft_content", db_path=self.db, team_brief_id=brief["brief_id"])
        self.assertEqual(len(claims), 1)
        self.drain(self.new_worker())
        model.assert_called_once()
        self.assertIn("상담 준비", model.call_args.kwargs["user_prompt"])
        jobs, items, handoffs = self.briefs.team.snapshot(brief["brief_id"])
        self.assertTrue(all(row["status"] == "completed" for row in jobs.values()))
        self.assertEqual({row["agent_id"] for row in items},
                         {"keyword_analyst", "data_assistant", "performance_analyst", "content_editor"})
        self.assertEqual(handoffs[0]["status"], "completed")
        roots = [call for call in self.client.chat_postMessage.call_args_list if "thread_ts" not in call.kwargs]
        self.assertEqual(len(roots), 1)
        replies = [call.kwargs for call in self.client.chat_postMessage.call_args_list if "thread_ts" in call.kwargs]
        self.assertEqual({item["thread_ts"] for item in replies}, {brief["message_ts"]})
        parent_updates = [call.kwargs["text"] for call in self.client.chat_update.call_args_list
                          if call.kwargs["ts"] == brief["message_ts"]]
        self.assertIn("초안 1건", parent_updates[-1])
        self.assertIn("개선안 1건", parent_updates[-1])

    def test_critical_branch_blocks_ads_and_leaves_global_tracking_untouched(self):
        work = workflow.create_work_item(
            agent_id="data_assistant", output={"status_summary": "Normal"},
            routed_to="data_assistant.tracking_status", model_version="test", input_summary="existing",
            db_path=self.db,
        )
        upsert_tracking_status("Normal", {"status_summary": "Normal"}, work["work_item_id"], 1, self.db)
        self.models["data_assistant"].return_value = DataAssistantOutput(
            status_summary="Critical", reason="전환 이벤트 급락", tracking_anomalies=["전환 0회"],
        )
        self.submit(text="brief critical")
        self.drain()
        brief = self.brief()
        self.models["performance_analyst"].assert_not_called()
        self.assertEqual(get_tracking_status(self.db)["work_item_id"], work["work_item_id"])
        reports = [call.kwargs["text"] for call in self.client.chat_update.call_args_list
                   if call.kwargs["ts"] == brief["message_ts"]]
        self.assertIn("퍼포먼스 마케팅팀 · 보류", reports[-1])
        self.assertIn("키워드 승인 대기", reports[-1])

    def test_slack_retry_does_not_repeat_analysis(self):
        self.client.chat_update.side_effect = RuntimeError("simulated delivery error")
        self.submit()
        self.drain()
        brief = self.brief()
        self.assertGreater(brief["sync_version"], brief["delivered_version"])
        for name in ("keyword_analyst", "data_assistant", "performance_analyst"):
            self.models[name].assert_called_once()

        with closing(get_conn(self.db)) as conn, conn:
            conn.execute("UPDATE team_briefs SET retry_at=0")
        self.client.chat_update.side_effect = None
        self.drain(self.new_worker())
        synced = self.brief()
        self.assertEqual(synced["sync_version"], synced["delivered_version"])
        self.assertEqual(brief["message_ts"], synced["message_ts"])
        for name in ("keyword_analyst", "data_assistant", "performance_analyst"):
            self.models[name].assert_called_once()
