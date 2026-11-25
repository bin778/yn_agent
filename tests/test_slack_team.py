import datetime as dt
import io
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from bot import brief_inputs, workflow
from bot.ga4_reports import Report
from bot.agents.content_editor import SlackContentEditorOutput
from bot.db import get_conn, get_tracking_status, upsert_tracking_status, utc_now
from bot.fake_data import get_fake_tracking_data
from bot.llm_client import CodexTimeoutError
from bot.schemas import DataAssistantOutput, KeywordAnalystOutput, PerformanceAnalystOutput
from bot.slack_adapter.app import build_dependencies, register_handlers
from bot.slack_adapter.brief_input import CONFIRM, MODAL
from bot.slack_adapter.config import SlackMapping, SlackSettings
from bot.slack_adapter.team import COMMAND, DRAFT_RETRY_ACTION, PERFORMANCE_RETRY_ACTION, SlackTeamBriefs


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
        self.slash, self.actions, self.views = {}, {}, {}
        app = Mock()
        app.command.side_effect = lambda name: lambda fn: self.slash.setdefault(name, fn)
        app.action.side_effect = lambda name: lambda fn: self.actions.setdefault(name, fn)
        app.view.side_effect = lambda name: lambda fn: self.views.setdefault(name, fn)
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
        self.slash[COMMAND](ack=Mock(), command={**command, **changes}, respond=Mock(), client=self.client)

    def submit_real(
        self, mode, *, complete=False, ads_timezone="Asia/Seoul", with_keywords=False,
        event_period="", file_owner="UKEYWORD", tables=None, start="2026-10-25", end="2026-10-25",
    ):
        self.submit(text=f"brief {mode}")
        view = self.client.views_open.call_args.kwargs["view"]
        tables = tables or {
            "sessions": "날짜,세션수\n2026-10-25,12",
            "events": event_period + "날짜,이벤트 이름,이벤트 수\n2026-10-25,form_submit,0\n2026-10-25,home_kakao_click,3",
            "ads": "날짜,전환수,노출수,클릭수,비용\n2026-10-25,2,100,10,2070",
        }
        if with_keywords:
            tables["keywords"] = "날짜,검색어,노출수\n2026-10-25,상담 준비,70"
        inputs = {key: {"value": text} if mode == "text" else {"files": [{"id": key}]}
                  for key, text in tables.items()}
        inputs.update(
            start={"selected_date": start}, end={"selected_date": end},
            ga_timezone={"value": "Asia/Seoul"}, ads_timezone={"value": ads_timezone},
            complete={"selected_option": {"value": "complete" if complete else "unknown"}},
        )
        view["state"] = {"values": {key: {key: value} for key, value in inputs.items()}}
        body = {"team": {"id": "TTEAM"}, "user": {"id": "UKEYWORD"}, "view": view}
        self.client.token = "test-bot-token"
        self.client.files_info.side_effect = lambda *, file: {"file": {
            "user": file_owner, "name": f"{file}.csv",
            "url_private_download": f"https://files.slack.com/files-pri/{file}",
        }}

        def fetch(request, *, timeout):
            self.assertEqual(request.get_header("Authorization"), "Bearer test-bot-token")
            return io.BytesIO(tables[request.full_url.rsplit("/", 1)[-1]].encode("utf-8-sig"))

        with patch("bot.slack_adapter.brief_input.build_opener") as opener:
            opener.return_value.open.side_effect = fetch
            self.views[MODAL](ack=Mock(), body=body, client=self.client)
        expected_downloads = len(tables) if mode in {"csv", "ga4"} and file_owner == "UKEYWORD" else 0
        self.assertEqual(opener.return_value.open.call_count, expected_downloads)
        preview = self.client.chat_postEphemeral.call_args.kwargs
        self.assertEqual(preview["user"], "UKEYWORD")
        self.client.chat_postMessage.assert_not_called()
        self.assertFalse(self.briefs.process_next(self.client))
        return preview

    def confirm_real(self, preview, user="UKEYWORD"):
        button = preview["blocks"][-1]["elements"][0]
        response = Mock()
        self.actions[CONFIRM](
            ack=Mock(), body={"team": {"id": "TTEAM"}, "channel": {"id": "CCHANNEL"}, "user": {"id": user}},
            action=button, respond=response,
        )
        return response.call_args.kwargs["text"]

    def test_explicit_text_zero_and_unconfirmed_data_hold(self):
        preview = self.submit_real("text")
        self.assertIn("폼 제출 0회", preview["text"])
        self.assertIn("광고 분석 보류", preview["text"])
        self.assertIn("유효한 미리보기", self.confirm_real(preview, user="UCONTENT"))
        self.assertEqual(self.confirm_real(preview), self.confirm_real(preview))
        self.drain()
        report = self.pm_card()
        self.assertIn("직접 입력", report["text"])
        self.assertIn("집계 완료가 미확인", report["text"])
        self.assertIn("키워드 분석 대기 · 검색어 보고서 필요", report["text"])
        replies = self.client.chat_postMessage.call_args_list[1:]
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(call.kwargs["thread_ts"] == report["ts"] for call in replies))
        for model in self.models.values():
            model.assert_not_called()

    def test_csv_confirmation_routes_real_keywords_and_records_ad_metrics(self):
        preview = self.submit_real("csv", complete=True, with_keywords=True)
        self.assertIn("광고비 ₩2,070", preview["text"])
        self.confirm_real(preview)
        self.drain()
        report = self.pm_card()
        self.assertIn("실제 집계 CSV", report["text"])
        self.assertIn("성과 분석 완료", report["text"])
        self.assertIn("키워드 승인 대기", report["text"])
        self.models["performance_analyst"].assert_not_called()
        replies = self.client.chat_postMessage.call_args_list
        self.assertTrue(any("CPA ₩1,035" in call.kwargs["text"] and call.kwargs["thread_ts"] == report["ts"]
                            for call in replies[1:]))
        self.keyword_action("UKEYWORD")
        self.keyword_action("ULEGAL")
        self.drain()
        self.assertIn("검토용 초안 확인", self.pm_card()["text"])

    def test_ga4_mode_runs_existing_campaign_proposal_from_hybrid_input(self):
        dates = [f"202611{day:02}" for day in range(7, 21)]
        missing = ("20261114", "form_submit")
        self.briefs.ga4_fetch = Mock(return_value=Report(
            {day: 100 for day in dates},
            {(day, event): 0 for day in dates for event in brief_inputs.EVENT_NAMES if (day, event) != missing},
            "2026-11-25T00:00:00+00:00", False,
        ))
        ads = "날짜,전환수,노출수,클릭수,비용,캠페인\n" + "".join(
            f"{day},1,10,2,{100 if day < '20261114' else 200},검색 캠페인\n" for day in dates
        )
        preview = self.submit_real(
            "ga4", complete=True,
            tables={"ads": ads, "events": "날짜,이벤트 이름,이벤트 수\n20261114,form_submit,0\n"},
            start="2026-11-14", end="2026-11-20",
        )
        self.briefs.ga4_fetch.assert_called_once_with("2026-11-14", "2026-11-20")
        self.assertIn("GA4 Data API · Google Ads CSV", preview["text"])
        self.assertIn("GA4 API 조회 시각", preview["text"])
        self.assertIn("0건 확인 행 1개 보완", preview["text"])
        self.assertIn("광고 집계 완료 · 확인", preview["text"])
        self.confirm_real(preview)
        self.drain()
        self.assertIn("검색 캠페인 · 광고비 +₩700", self.performance_reply()["text"])

    def test_ga4_permission_error_never_produces_confirmation(self):
        self.briefs.ga4_fetch = Mock(side_effect=brief_inputs.InputError("GA4 조회 권한 없음"))
        preview = self.submit_real("ga4", tables={"ads": "required CSV"})
        self.assertIn("GA4 조회 권한 없음", preview["text"])
        self.assertNotIn("blocks", preview)

    def test_ga4_pending_holds_and_can_be_disabled_before_confirmation(self):
        day = "20261025"
        self.briefs.ga4_fetch = Mock(return_value=Report(
            {day: 100}, {(day, event): 0 for event in brief_inputs.EVENT_NAMES},
            "2026-10-26T00:00:00+00:00", True,
        ))
        preview = self.submit_real("ga4", complete=True, tables={
            "ads": "날짜,전환수,노출수,클릭수,비용\n2026-10-25,2,100,10,2070",
        })
        self.assertIn("광고 분석 보류 · GA4 집계 대기", preview["text"])
        fetch = self.briefs.ga4_fetch
        self.briefs.ga4_fetch = None
        self.assertIn("비활성화", self.confirm_real(preview))
        self.assertFalse(self.briefs.process_next(self.client))
        self.briefs.ga4_fetch = fetch
        self.confirm_real(preview)
        self.drain()
        self.assertIn("GA4 집계 대기", self.performance_reply()["text"])

    def test_ga4_mode_holds_for_missing_event(self):
        self.assert_ga4_hold({"form_submit": 0}, complete=True, reason="GA4 필수 이벤트의 일별 행")

    def test_ga4_mode_holds_for_unconfirmed_ads(self):
        self.assert_ga4_hold(dict.fromkeys(brief_inputs.EVENT_NAMES, 0), complete=False, reason="집계 완료가 미확인")

    def assert_ga4_hold(self, events, *, complete, reason):
        day = "20261025"
        self.briefs.ga4_fetch = Mock(return_value=Report(
            {day: 100}, {(day, event): value for event, value in events.items()},
            "2026-10-30T00:00:00+00:00", False,
        ))
        preview = self.submit_real("ga4", complete=complete, tables={
            "ads": "날짜,전환수,노출수,클릭수,비용\n2026-10-25,2,100,10,2070",
        })
        self.assertIn(reason, preview["text"])
        self.confirm_real(preview)
        self.drain()
        self.assertIn(reason, self.performance_reply()["text"])

    def test_time_zone_mismatch_returns_private_correction(self):
        preview = self.submit_real("text", ads_timezone="Etc/UTC")
        self.assertIn("시간대가 다릅니다", preview["text"])
        self.assertNotIn("blocks", preview)

    def submit_week(
        self, mode, *, complete=True, include_previous=True, missing_previous_form=False,
        campaign=False, zero_previous_conversions=False,
    ):
        tables = {
            "sessions": "날짜,세션수\n",
            "events": "날짜,이벤트 이름,이벤트 수\n",
            "ads": "날짜,전환수,노출수,클릭수,비용" + (",캠페인" if campaign else "") + "\n",
        }
        first = 6 if include_previous else 14
        for day in range(first, 21):
            date = f"2026-11-{day:02d}"
            previous = day < 14
            tables["sessions"] += f"{date},{10 if previous else 12}\n"
            if not (missing_previous_form and day == 7):
                tables["events"] += f"{date},form_submit,{0 if previous else 2}\n"
            tables["events"] += f"{date},home_kakao_click,{3 if previous else 1}\n"
            conversions = ("0" if zero_previous_conversions else "0.25") if previous else "1.5"
            cost = 4070 if day == 20 and campaign else 2070
            fields = f"{date},{conversions},100,10,{cost}"
            tables["ads"] += fields + (",검색 캠페인" if campaign else "") + "\n"
        return self.submit_real(mode, tables=tables, start="2026-11-14", end="2026-11-20", complete=complete)

    def test_csv_uses_two_equal_weeks_and_keeps_conversion_metrics_separate(self):
        preview = self.submit_week("csv")
        self.confirm_real(preview)
        self.drain()
        for text in (preview["text"], self.pm_card()["text"]):
            self.assertIn("2026-11-07 ~ 2026-11-13 → 2026-11-14 ~ 2026-11-20", text)
            self.assertIn("GA4 세션 · 70회 → 84회 (+14회)", text)
            self.assertIn("GA4 폼 제출 · 0건 → 14건 (+14건)", text)
            self.assertIn("GA4 카카오톡 클릭 · 21회 → 7회 (-14회)", text)
            self.assertIn("광고 플랫폼 전환수 · 1.75건 → 10.5건 (+8.75건)", text)
        reply = self.performance_reply()
        self.assertIn("광고 CPA · ₩8,280 → ₩1,380 (-₩6,900)", reply["text"])
        self.assertIn("2026-11-07 → 2026-11-14 광고 전환수 +1.25건", reply["text"])
        self.assertIn("전환 액션별 기록과 전환 지연 여부를 확인하세요.", reply["text"])
        self.assertNotIn("캠페인 변경 이력", reply["text"])

    def performance_reply(self):
        return next(call.kwargs for call in self.client.chat_postMessage.call_args_list
                    if call.kwargs.get("thread_ts") == self.brief()["message_ts"]
                    and call.kwargs["text"].startswith("김도현"))

    def test_text_weekly_actions_include_a_comparable_campaign_and_handle_zero_conversion_cpa(self):
        preview = self.submit_week("text", campaign=True, zero_previous_conversions=True)
        self.confirm_real(preview)
        self.drain()
        reply = self.performance_reply()["text"]
        self.assertIn("광고 CPA · 계산 불가 (전환 0건) → ₩1,570", reply)
        self.assertIn("2026-11-13 → 2026-11-20 광고비 +₩2,000", reply)
        self.assertIn("캠페인 검색 캠페인 · 광고비 +₩2,000, 광고 전환수 +10.5건", reply)
        self.assertEqual(len([line for line in reply.splitlines() if line[:3] in ("1. ", "2. ", "3. ")]), 3)

    def test_text_comparison_excludes_missing_previous_events_without_treating_them_as_zero(self):
        preview = self.submit_week("text", missing_previous_form=True)
        self.assertIn("GA4 폼 제출 · 비교 보류 (직전 기간 일별 행 누락)", preview["text"])
        self.assertIn("GA4 카카오톡 클릭 · 21회 → 7회", preview["text"])
        self.assertIn("광고 플랫폼 전환수 · 1.75건 → 10.5건", preview["text"])

    def test_week_comparison_waits_for_collection_confirmation(self):
        preview = self.submit_week("csv", complete=False)
        self.assertIn("비교 보류 · 집계 완료가 미확인입니다.", preview["text"])
        self.assertNotIn("광고 플랫폼 전환수 ·", preview["text"])
        self.confirm_real(preview)
        self.drain()
        reply = self.performance_reply()["text"]
        self.assertIn("광고 제안 보류", reply)
        self.assertNotIn("다음 행동", reply)

    def test_one_week_export_still_delivers_brief_with_comparison_on_hold(self):
        preview = self.submit_week("csv", include_previous=False)
        self.assertIn("비교 보류 · GA4 세션 자료에 보고 기간의 날짜가 빠져 있습니다", preview["text"])
        self.confirm_real(preview)
        self.drain()
        self.assertIn("성과 분석 완료", self.pm_card()["text"])

    def test_source_export_period_mismatch_identifies_event_file(self):
        preview = self.submit_real("text", event_period="# 20261026-20261123\n")
        self.assertIn("GA4 이벤트 자료의 원본 기간", preview["text"])
        self.assertNotIn("blocks", preview)

    def test_file_owned_by_another_user_is_never_downloaded(self):
        preview = self.submit_real("csv", file_owner="ULEGAL")
        self.assertIn("본인이 이 입력창에 첨부한 CSV", preview["text"])
        self.assertNotIn("blocks", preview)

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

    def test_retry_only_requeues_the_clicked_timeout_attempt_on_the_bound_pm_card(self):
        self.submit()
        self.drain()
        brief = self.brief()
        self.keyword_action("UKEYWORD")
        self.keyword_action("ULEGAL")
        model = self.models["content_editor"]
        model.side_effect = [CodexTimeoutError(), CodexTimeoutError(), model.return_value]
        self.drain()
        _, items, handoffs = self.briefs.team.snapshot(brief["brief_id"])
        request_id = next(item["request_id"] for item in items if item["agent_id"] == "keyword_analyst")
        approval = workflow.get_approval_state(request_id, self.db)
        handoff_id = handoffs[0]["handoff_id"]

        def retry_value():
            report = self.pm_card()
            self.assertIn("상담 준비 · 초안 생성 시간 초과", self.pm_action_text(report))
            return next(
                block["accessory"]["value"] for block in report["blocks"]
                if block.get("accessory", {}).get("action_id") == DRAFT_RETRY_ACTION
            )

        def click(value, user="UKEYWORD", message_ts=brief["message_ts"]):
            ack, respond = Mock(), Mock()
            self.actions[DRAFT_RETRY_ACTION](
                ack=ack, respond=respond, action={"value": value},
                body={"team": {"id": "TTEAM"}, "channel": {"id": "CCHANNEL"},
                      "user": {"id": user}, "message": {"ts": message_ts}},
            )
            ack.assert_called_once()
            return respond.call_args.kwargs["text"]

        first_value = retry_value()
        self.assertIn("승인 권한이 필요", click(first_value, user="UCONTENT"))
        self.assertIn("다시 시도할 수 없습니다", click(first_value, message_ts="1.99"))
        self.assertIn("기존 승인으로 초안 재시도를 등록", click(first_value))
        self.assertIn("다시 시도할 수 없습니다", click(first_value))
        self.drain()
        second_value = retry_value()
        self.assertNotEqual(first_value, second_value)
        self.assertIn("다시 시도할 수 없습니다", click(first_value))
        self.assertIn("기존 승인으로 초안 재시도를 등록", click(second_value))
        self.drain()

        self.assertEqual(model.call_count, 3)
        self.assertEqual(workflow.get_approval_state(request_id, self.db), approval)
        _, _, handoffs = self.briefs.team.snapshot(brief["brief_id"])
        self.assertEqual([(row["handoff_id"], row["status"]) for row in handoffs],
                         [(handoff_id, "completed")])
        self.assertNotIn("재시도", self.pm_action_text(self.pm_card()))
        self.assertIn("검토용 초안 확인 1건", self.pm_action_text(self.pm_card()))
        replies = [call.kwargs for call in self.client.chat_postMessage.call_args_list
                   if "한도윤 · 검토용 콘텐츠 초안" in call.kwargs["text"]]
        self.assertEqual([reply["thread_ts"] for reply in replies], [brief["message_ts"]])
        for name in ("keyword_analyst", "data_assistant", "performance_analyst"):
            self.models[name].assert_called_once()

    def test_refreshes_existing_timeout_card_once_and_skips_other_errors(self):
        self.submit()
        self.drain()
        self.keyword_action("UKEYWORD")
        self.keyword_action("ULEGAL")
        self.models["content_editor"].side_effect = RuntimeError()
        self.drain()
        self.assertNotIn("시간 초과", self.pm_action_text(self.pm_card()))
        prior = self.brief()
        with closing(get_conn(self.db)) as conn, conn:
            conn.execute("UPDATE handoff_jobs SET error='CodexTimeoutError'")
            conn.execute("PRAGMA user_version=0")

        self.drain(self.new_worker())
        updated = self.brief()
        self.assertEqual(updated["sync_version"], prior["sync_version"] + 1)
        self.assertEqual(updated["delivered_version"], updated["sync_version"])
        self.assertEqual(updated["message_ts"], prior["message_ts"])
        self.assertIn("상담 준비 · 초안 생성 시간 초과", self.pm_action_text(self.pm_card()))
        self.assertFalse(self.new_worker().process_next(self.client))
        self.models["content_editor"].assert_called_once()

    def performance_retry_value(self):
        return next(
            (block["accessory"]["value"] for block in self.pm_card()["blocks"]
             if block.get("accessory", {}).get("action_id") == PERFORMANCE_RETRY_ACTION),
            None,
        )

    def click_performance_retry(self, value, *, user="UKEYWORD", message_ts=None):
        ack, respond = Mock(), Mock()
        self.actions[PERFORMANCE_RETRY_ACTION](
            ack=ack, respond=respond, action={"value": value},
            body={"team": {"id": "TTEAM"}, "channel": {"id": "CCHANNEL"}, "user": {"id": user},
                  "message": {"ts": message_ts or self.brief()["message_ts"]}},
        )
        ack.assert_called_once()
        return respond.call_args.kwargs["text"]

    def test_performance_retry_refreshes_legacy_card_and_only_retries_the_selected_attempt(self):
        model = self.models["performance_analyst"]
        model.side_effect = [CodexTimeoutError(), CodexTimeoutError(), model.return_value]
        self.submit()
        self.drain()
        before = self.brief()
        with closing(get_conn(self.db)) as conn, conn:
            conn.execute("ALTER TABLE team_brief_jobs DROP COLUMN retry_count")
            conn.execute("PRAGMA user_version=1")
        self.drain(self.new_worker())
        self.assertEqual(self.brief()["sync_version"], before["sync_version"] + 1)
        self.assertIn("광고 분석 시간 초과", self.pm_card()["text"])
        first = self.performance_retry_value()
        self.assertIsNotNone(first)
        self.assertIn("최신 카드", self.click_performance_retry(first, user="UCONTENT"))
        self.assertIn("최신 카드", self.click_performance_retry(first, message_ts="1.99"))
        self.assertIn("재시도를 등록했습니다", self.click_performance_retry(first))
        self.assertIn("최신 카드", self.click_performance_retry(first))
        self.drain()
        second = self.performance_retry_value()
        self.assertNotEqual(first, second)
        self.assertIn("최신 카드", self.click_performance_retry(first))
        self.assertIn("재시도를 등록했습니다", self.click_performance_retry(second))
        self.drain()

        self.assertEqual(model.call_count, 3)
        self.assertIsNone(self.performance_retry_value())
        self.assertIn("개선안 1건", self.pm_card()["text"])
        replies = [call.kwargs for call in self.client.chat_postMessage.call_args_list
                   if "김도현 · 광고 개선안" in call.kwargs["text"]]
        self.assertEqual([reply["thread_ts"] for reply in replies], [before["message_ts"]])
        for name in ("keyword_analyst", "data_assistant"):
            self.models[name].assert_called_once()
        self.models["content_editor"].assert_not_called()

    def test_performance_retry_reapplies_tracking_gate(self):
        model = self.models["performance_analyst"]
        model.side_effect = CodexTimeoutError()
        self.submit()
        self.drain()
        value = self.performance_retry_value()
        self.assertIsNotNone(value)
        with closing(get_conn(self.db)) as conn, conn:
            conn.execute("UPDATE work_items SET output_json=json_set(output_json, '$.status_summary', 'Critical')"
                         " WHERE agent_id='data_assistant'")
        self.assertIn("재시도를 등록했습니다", self.click_performance_retry(value))
        self.drain()
        model.assert_called_once()
        self.assertIn("퍼포먼스 마케팅팀 · 보류", self.pm_card()["text"])
        self.assertIsNone(self.performance_retry_value())

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

    def test_stale_tracking_blocks_ads_but_allows_approved_content_draft(self):
        self.submit(text="brief stale")
        self.drain()
        self.models["data_assistant"].assert_not_called()
        self.models["performance_analyst"].assert_not_called()
        self.assertIn("퍼포먼스 마케팅팀 · 보류 · 트래킹 수집 지연", self.pm_card()["text"])
        self.assertIn("집계가 갱신되면 새 브리핑", self.pm_card()["text"])
        self.keyword_action("UKEYWORD")
        self.keyword_action("ULEGAL")
        self.drain()
        self.models["content_editor"].assert_called_once()
        self.assertIn("초안 1건", self.pm_card()["text"])

    def test_missing_required_count_blocks_ads_and_identifies_metric(self):
        self.submit(text="brief missing")
        self.drain()
        self.models["data_assistant"].assert_not_called()
        self.models["performance_analyst"].assert_not_called()
        self.models["keyword_analyst"].assert_called_once()
        self.assertIn("트래킹 필수 지표 누락 또는 오류 · 폼 제출", self.pm_card()["text"])

    def test_complete_zero_count_is_preserved_and_allows_ad_analysis(self):
        self.submit(text="brief zero")
        self.drain()
        self.models["data_assistant"].assert_called_once()
        self.models["performance_analyst"].assert_called_once()
        _, items, _ = self.briefs.team.snapshot(self.brief()["brief_id"])
        data = next(item["output"] for item in items if item["agent_id"] == "data_assistant")
        self.assertEqual(data["tracking_input"]["counts"]["submit_form"], 0)
        self.assertIn("퍼포먼스 마케팅팀 · 개선안 1건", self.pm_card()["text"])

    def test_incomplete_zero_counts_still_block_ads(self):
        raw = get_fake_tracking_data("zero")
        raw["collection_complete"] = False
        with patch("bot.agents.data_assistant.get_fake_tracking_data", return_value=raw):
            self.submit(text="brief zero")
            self.drain()
        self.models["performance_analyst"].assert_not_called()
        self.assertIn("트래킹 집계가 완료되지 않았습니다", self.pm_card()["text"])

    def test_retry_checks_source_age_instead_of_the_time_of_the_previous_decision(self):
        model = self.models["performance_analyst"]
        model.side_effect = CodexTimeoutError()
        self.submit()
        self.drain()
        value = self.performance_retry_value()
        self.assertIn("재시도를 등록했습니다", self.click_performance_retry(value))
        with patch("bot.agents.data_assistant.utc_now", return_value=utc_now() + dt.timedelta(hours=4)):
            self.drain()
        model.assert_called_once()
        self.assertIn("퍼포먼스 마케팅팀 · 보류 · 트래킹 수집 지연", self.pm_card()["text"])
