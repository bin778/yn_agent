import datetime as dt
import unittest
from unittest.mock import Mock, patch

from bot import brief_inputs, ga4_reports
from scripts.compare_ga4_report import reconcile

DATES = [f"202611{day:02}" for day in range(7, 21)]


def response(sessions=None, events=None, **metadata):
    if sessions is None:
        sessions = {(day,): 100 for day in DATES}
    if events is None:
        events = {(day, name): 0 for day in DATES for name in brief_inputs.EVENT_NAMES}
    return {"reports": [
        {
            "metadata": {"timeZone": "Asia/Seoul", **metadata},
            "dimensionHeaders": [{"name": name} for name in dims],
            "metricHeaders": [{"name": metric}],
            "rowCount": len(rows),
            "rows": [{"dimensionValues": [{"value": field} for field in key],
                      "metricValues": [{"value": str(value)}]} for key, value in rows.items()],
        }
        for dims, metric, rows in (
            (("date",), "sessions", sessions), (("date", "eventName"), "eventCount", events),
        )
    ]}


class GA4ReportsTest(unittest.TestCase):
    def read(self, data, *, now=dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)):
        client = Mock(return_value=data)
        with patch.object(ga4_reports, "utc_now", return_value=now):
            report = ga4_reports.read_report("2026-11-14", "2026-11-20", client)
        params, = client.call_args.args
        sessions_request, events_request = params["requests"]
        self.assertEqual(sessions_request["dateRanges"], [{"startDate": "2026-11-07", "endDate": "2026-11-20"}])
        event_start = "2026-11-07" if report.pending else "2026-10-31"
        self.assertEqual(events_request["dateRanges"], [{"startDate": event_start, "endDate": "2026-11-20"}])
        event_filter = params["requests"][1]["dimensionFilter"]["filter"]
        self.assertEqual(event_filter["inListFilter"]["values"], list(brief_inputs.EVENT_NAMES))
        return report

    def test_current_values_and_previous_missing_events_still_allow_ads_comparison(self):
        events = {(day, name): 0 for day in DATES[7:] for name in brief_inputs.EVENT_NAMES}
        report = self.read(response(events=events))
        self.assertIn("20261114,form_submit,0", report.tables()["events"])
        tables = report.tables()
        tables["ads"] = "날짜,전환수,노출수,클릭수,비용,캠페인\n" + "".join(
            f"{day},1,2,1,20,검색\n" for day in DATES
        )
        data = brief_inputs.normalize(
            tables, start="2026-11-14", end="2026-11-20",
            ga_timezone="Asia/Seoul", ads_timezone="Asia/Seoul", complete=True, mode="ga4", ga4_report=report,
        )
        self.assertIsNone(brief_inputs.hold_reason(data))
        self.assertIsNone(data["comparison"]["previous"]["form_submit"])
        self.assertEqual(data["comparison"]["ad_changes"]["campaign"]["name"], "검색")

    def test_requires_explicit_csv_zeros_and_rejects_conflicting_reference(self):
        report = self.read(response(events={("20261114", "form_submit"): 3}))
        header = "날짜,이벤트 이름,이벤트 수\n"
        csv = header + "20261114,form_submit,3\n20261114,home_kakao_click,0\n"
        supplemented = report.with_verified_zeros(csv)
        self.assertEqual(supplemented.verified_zeros, 1)
        self.assertEqual(supplemented.events[("20261114", "home_kakao_click")], 0)
        self.assertNotIn(("20261115", "home_kakao_click"), supplemented.events)
        self.assertNotIn(("20261114", "home_kakao_click"), report.events)
        for prefix, row, reason in (
            ("", "20261114,form_submit,0", "일치하지 않습니다"),
            ("", "20261115,form_submit,1", "일치하지 않습니다"),
            ("", "20261115,form_submit,", "형식"),
            ("# 20261114-20261120\n", "20261114,home_kakao_click,0", "원본 기간"),
            ("# 시간대: UTC\n", "20261114,home_kakao_click,0", "시간대"),
        ):
            with self.subTest(row=row), self.assertRaisesRegex(brief_inputs.InputError, reason):
                report.with_verified_zeros(prefix + header + row)

    def test_infers_zeros_per_event_for_only_seven_days_after_a_recorded_occurrence(self):
        report = self.read(response(events={
            ("20261106", "form_submit"): 1,
            ("20261114", "home_kakao_click"): 0,
            ("20261118", "home_kakao_click"): 2,
        }))
        self.assertNotIn(("20261106", "form_submit"), report.events)
        filled = report.with_inferred_zeros()
        self.assertEqual(filled.inferred_zeros, 9)
        self.assertEqual(filled.events[("20261113", "form_submit")], 0)
        self.assertNotIn(("20261114", "form_submit"), filled.events)
        self.assertNotIn(("20261115", "home_kakao_click"), filled.events)
        self.assertEqual(filled.events[("20261118", "home_kakao_click")], 2)
        self.assertEqual(filled.events[("20261120", "home_kakao_click")], 0)

    def test_within_48_hours_at_seoul_day_boundary(self):
        before = dt.datetime(2026, 11, 22, 14, 59, tzinfo=dt.timezone.utc)
        at = before + dt.timedelta(minutes=1)
        pending = self.read(response(events={("20261119", "form_submit"): 1}), now=before)
        self.assertTrue(pending.pending)
        self.assertNotIn(("20261120", "form_submit"), pending.with_inferred_zeros().events)
        self.assertFalse(self.read(response(), now=at).pending)

    def test_rejects_timezone_restrictions_and_missing_sessions(self):
        for data, reason in (
            (response(timeZone="UTC"), "시간대"),
            (response(subjectToThresholding=True), "데이터 제한"),
            (response(samplingMetadatas=[{"samplesReadCount": "5"}]), "데이터 제한"),
            (response(sessions={(day,): 100 for day in DATES[:-1]}), "세션 날짜 누락.*20261120"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(brief_inputs.InputError, reason):
                self.read(data)

    @patch.object(ga4_reports.time, "sleep")
    def test_transport_retries_transient_errors_only(self, sleep):
        session = Mock()
        session.post.side_effect = [Mock(status_code=429), Mock(status_code=200, json=Mock(return_value={"ok": True}))]
        self.assertEqual(ga4_reports._run(session, "test", {}), {"ok": True})
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once()
        session.reset_mock()
        session.post.side_effect = None
        session.post.return_value.status_code = 403
        with self.assertRaisesRegex(brief_inputs.InputError, "GA4 조회 권한 없음"):
            ga4_reports._run(session, "test", {})
        session.post.assert_called_once()

    @patch("builtins.print")
    def test_csv_comparison_lists_missing_zeroes_but_fails_missing_positive_counts(self, output):
        key = ("20261114", "form_submit")
        self.assertTrue(reconcile({}, {key: 0}, kind="events"))
        self.assertIn("CSV 0건 / API 행 없음", output.call_args.args[0])
        self.assertFalse(reconcile({}, {key: 1}, kind="events"))
