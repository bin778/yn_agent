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
        self.client.chat_getPermalink.side_effect = lambda channel, message_ts: {
            "permalink": f"https://test.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"
        }
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

    def new_worker(
        self, mode=workflow.KeywordApprovalMode.LEGAL_BEFORE_DRAFT,
        content_mode=workflow.ContentApprovalMode.RISK_BASED,
    ):
        return SlackTeamBriefs(
            self.mapping, self.db, "CCHANNEL", approval_mode=content_mode,
            keyword_approval_mode=mode,
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

    def pm_card(self):
        thread_ts = self.brief()["message_ts"]
        return next(call.kwargs for call in reversed(self.client.chat_update.call_args_list)
                    if call.kwargs["ts"] == thread_ts)

    @staticmethod
    def pm_action_text(report):
        return "\n".join(block["text"]["text"] for block in report["blocks"][2:])

    def keyword_action(self, user, action_id="hermes_team_keyword_approve"):
        card = next(call.kwargs for call in self.client.chat_update.call_args_list
                    if any(el.get("action_id") == action_id
                           for block in call.kwargs.get("blocks", []) for el in block.get("elements", [])))
        button = next(el for block in card["blocks"] for el in block.get("elements", [])
                      if el["action_id"] == action_id)
        respond = Mock()
        self.actions[action_id](
            ack=Mock(), body={"team": {"id": "TTEAM"}, "channel": {"id": "CCHANNEL"},
                              "user": {"id": user}, "message": {"ts": card["ts"]}, "trigger_id": "test-trigger"},
            action={"action_id": action_id, "value": button["value"]}, respond=respond, client=self.client,
        )

        return respond.call_args.kwargs["text"] if respond.called else None

    def test_request_roles_and_candidate_approval_through_bolt_handlers(self):
        self.submit(channel_id="COTHER")
        self.submit(user_id="UUNKNOWN")
        self.assertFalse(self.briefs.process_next(self.client))
        self.submit()
        self.submit()
        self.drain()
        brief = self.brief()
        report = self.pm_card()
        self.assertIn("키워드 승인 대기", report["text"])
        keyword_card = next(
            call.kwargs for call in self.client.chat_update.call_args_list
            if "윤서진 · 키워드 후보" in call.kwargs["text"]
        )
        keyword_url = f"https://test.slack.com/archives/CCHANNEL/p{keyword_card['ts'].replace('.', '')}"
        self.assertIn(f"<{keyword_url}|카드 열기>", self.pm_action_text(report))
        self.assertIsNone(get_tracking_status(self.db))
        model = self.models["content_editor"]
        model.assert_not_called()
        self.keyword_action("UCONTENT")
        version_before_approval = self.brief()["sync_version"]
        response = self.keyword_action("UKEYWORD")
        self.assertIn("승인을 기록했습니다 (1/2)", response)
        self.assertIn("법무·컴플라이언스 승인자", response)
        card = self.client.chat_update.call_args.kwargs["text"]
        self.assertIn("상태: 일부 승인 완료", card)
        self.assertIn("남은 승인 역할: 법무·컴플라이언스 승인자", card)
        self.assertGreater(self.brief()["sync_version"], version_before_approval)
        self.assertIn("이미 승인하셨습니다", self.keyword_action("UKEYWORD"))
        conflict = self.keyword_action("UKEYWORD", "hermes_team_keyword_reject")
        self.assertIn("이미 결정을 남기셨습니다", conflict)
        self.client.views_open.assert_not_called()
        self.drain()
        model.assert_not_called()
        report = next(call.kwargs["text"] for call in reversed(self.client.chat_update.call_args_list)
                      if call.kwargs["ts"] == brief["message_ts"])
        self.assertIn("법무 승인 대기 1건", report)
        pm_actions = self.pm_action_text(self.pm_card())
        self.assertIn("별도 법무 승인 1건", pm_actions)
        self.assertIn(f"<{keyword_url}|카드 열기>", pm_actions)
        self.assertNotIn("키워드 승인 1건", pm_actions)

        self.assertIn("초안 작업을 대기열", self.keyword_action("ULEGAL"))
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
        self.assertIn("검토용 초안 확인 1건", self.pm_action_text(self.pm_card()))

    def test_advisory_approval_creates_generalized_draft_with_separate_legal_approval(self):
        content_mode = workflow.ContentApprovalMode.SINGLE
        advisory = self.new_worker(workflow.KeywordApprovalMode.DRAFT_ADVISORY, content_mode)
        result = self.models["keyword_analyst"].return_value
        result.items[0].keyword = "합성 사건 고유 키워드"
        result.items[0].draft_topic_category = "sexual_offense"
        brief_id = advisory.team.enqueue(trigger_id="advisory", user_id="UKEYWORD", scenario="normal")

        # A restart and mode change must not change the accepted brief's policy.
        self.drain(self.new_worker(content_mode=content_mode))
        _, items, handoffs = advisory.team.snapshot(brief_id)
        self.assertEqual(handoffs, [])
        keyword = next(item for item in items if item["agent_id"] == "keyword_analyst")
        state = advisory._state(keyword["request_id"])
        self.assertEqual([row["role"] for row in state["requirements"]], ["keyword_approver"])
        self.assertIn("법무·컴플라이언스 검토 권고", state["detail"])
        self.assertIn("성범죄 사건의 일반적인 수사 절차", state["detail"])
        self.assertIn("법무 검토 권고 1건", "\n".join(
            call.kwargs["text"] for call in self.client.chat_update.call_args_list
        ))

        self.assertIn("일반화한 주제", self.keyword_action("UKEYWORD"))
        self.drain(self.new_worker(content_mode=content_mode))
        draft_call = self.models["content_editor"].call_args.kwargs["user_prompt"]
        self.assertIn("성범죄 사건의 일반적인 수사 절차", draft_call)
        self.assertNotIn("합성 사건 고유 키워드", draft_call)
        _, items, handoffs = advisory.team.snapshot(brief_id)
        self.assertEqual([row["status"] for row in handoffs], ["completed"])
        draft = next(item for item in items if item["agent_id"] == "content_editor")
        self.assertEqual(draft["routed_to"], "human_review.queue")
        self.assertTrue(draft["output"]["requires_legal_before_publish"])

        editor = workflow.ActorContext.verified_actor("editor", {"content_approver"})
        edited = {key: value for key, value in draft["output"].items()
                  if key != "requires_legal_before_publish"}
        revised = workflow.revise_pending_content(
            request_id=draft["request_id"], expected_version=1, actor=editor,
            revised_output=edited, db_path=self.db,
        )
        self.assertEqual(revised["required_roles"], ["content_approver", "legal_compliance"])
        state = workflow.decide(
            request_id=revised["request_id"], expected_version=2, actor=editor,
            acting_role="content_approver", decision="approved", db_path=self.db,
        )
        self.assertEqual(state["status"], "pending")
        self.assertEqual(workflow.claim_handoffs("queue_for_publish", db_path=self.db), [])

    def test_existing_brief_keeps_two_roles_after_switch_to_advisory(self):
        self.submit()
        self.drain(self.new_worker(workflow.KeywordApprovalMode.DRAFT_ADVISORY))
        brief = self.brief()
        self.assertEqual(brief["keyword_approval_mode"], "legal_before_draft")
        _, items, _ = self.briefs.team.snapshot(brief["brief_id"])
        keyword = next(item for item in items if item["agent_id"] == "keyword_analyst")
        self.assertEqual(
            [row["role"] for row in self.briefs._state(keyword["request_id"])["requirements"]],
            ["keyword_approver", "legal_compliance"],
        )

    def test_existing_database_adds_mode_without_changing_approval_requirements(self):
        self.submit()
        self.drain()
        brief_id = self.brief()["brief_id"]
        with closing(get_conn(self.db)) as conn, conn:
            conn.execute("ALTER TABLE team_briefs DROP COLUMN keyword_approval_mode")

        self.assertEqual(self.briefs.team.get_brief(brief_id)["keyword_approval_mode"], "legal_before_draft")
        _, items, _ = self.briefs.team.snapshot(brief_id)
        request = next(item["request_id"] for item in items if item["agent_id"] == "keyword_analyst")
        self.assertEqual(len(self.briefs._state(request)["requirements"]), 2)

    def test_reject_feedback_and_no_draft_handoff(self):
        self.submit()
        self.drain()
        self.keyword_action("UKEYWORD", "hermes_team_keyword_reject")
        view = self.client.views_open.call_args.kwargs["view"]
        view["state"] = {"values": {"reason": {"reason": {"value": "테스트 반려 사유"}}}}
        self.briefs.handle_view(
            ack=Mock(), client=self.client,
            body={"team": {"id": "TTEAM"}, "user": {"id": "UKEYWORD"}, "view": view},
        )
        self.assertEqual(self.client.chat_postEphemeral.call_args.kwargs["text"], "키워드를 반려했습니다.")
        _, items, handoffs = self.briefs.team.snapshot(self.brief()["brief_id"])
        keyword = next(item for item in items if item["agent_id"] == "keyword_analyst")
        self.assertEqual(keyword["status"], "rejected")
        self.assertEqual(handoffs, [])
        self.drain()
        self.assertIn("반려 1건", self.pm_card()["text"])
        self.assertNotIn("키워드 승인 1건", self.pm_action_text(self.pm_card()))

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
        actions = self.pm_action_text(self.pm_card())
        self.assertIn("전환 추적 점검 확인 1건", actions)
        self.assertIn("광고 보류 사유 확인 1건", actions)
        self.assertNotIn("광고 개선안 검토", actions)

    def test_pm_status_is_delivered_when_slack_cannot_return_a_message_link(self):
        self.client.chat_getPermalink.side_effect = OSError("simulated link lookup failure")
        self.submit()
        self.drain()
        brief = self.brief()
        self.assertEqual(brief["sync_version"], brief["delivered_version"])
        actions = self.pm_action_text(self.pm_card())
        self.assertIn("키워드 승인 1건", actions)
        self.assertIn("링크가 없는 항목은 이 스레드에서 확인", actions)
        self.assertNotIn("|카드 열기>", actions)

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
