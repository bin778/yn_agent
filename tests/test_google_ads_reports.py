import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from bot import google_ads_reports as ads
from bot.brief_inputs import InputError

CUSTOMER = "1234567890"
ACCOUNT = [{"customer": {"id": CUSTOMER, "timeZone": "Asia/Seoul", "currencyCode": "KRW"}}]
CAMPAIGNS = [{"campaign": {"id": "1", "name": "검색"}}, {"campaign": {"id": "2", "name": "검색"}}]
ACTIVE = {"campaign": {"id": "1"}, "metrics": {
    "impressions": "10", "clicks": "2", "costMicros": "123500000", "conversions": Decimal("0.245"),
}}
DAY = {**ACTIVE, "segments": {"date": "2026-11-14"}}


class GoogleAdsReportsTest(unittest.TestCase):
    def read(self, *, account=ACCOUNT, totals=(ACTIVE,), daily=(DAY,)):
        search = Mock(side_effect=[account, CAMPAIGNS, totals, daily])
        report = ads.read_report(CUSTOMER, "2026-11-14", "2026-11-20", search)
        for call in search.call_args_list[-2:]:
            self.assertIn("BETWEEN '2026-11-07' AND '2026-11-20'", call.args[0])
            self.assertIn("campaign.status = 'ENABLED'", call.args[0])
            self.assertIn("campaign.experiment_type = 'BASE'", call.args[0])
        return report

    def test_zeros_and_reporting_precision_after_reconciling_raw_totals(self):
        report = self.read()
        self.assertEqual(len(report.rows), 28)
        self.assertIn("2026-11-14,검색 (1),10,2,124,0.25", report.table())
        self.assertIn("2026-11-20,검색 (2),0,0,0,0.00", report.table())

    def test_blocks_partial_duplicate_and_unmatched_rows(self):
        for daily, reason in (([], "합계가 일치"), ([DAY, DAY], "일부 데이터")):
            with self.subTest(reason=reason), self.assertRaisesRegex(InputError, reason):
                self.read(daily=daily)
        with self.assertRaisesRegex(InputError, "시간대와 통화"):
            self.read(account=[{"customer": {"id": CUSTOMER, "timeZone": "UTC", "currencyCode": "KRW"}}])
        with self.assertRaisesRegex(InputError, "7일"):
            ads.read_report(CUSTOMER, "2026-11-14", "2026-11-19", Mock())

    @patch.object(ads.time, "sleep")
    def test_retries_temporary_failure_and_rejects_permission_error(self, sleep):
        session = Mock()
        session.post.side_effect = [Mock(status_code=429), Mock(status_code=200, json=Mock(return_value=[
            {"results": [DAY]}, {"fieldMask": "campaign.id"},
        ]))]
        self.assertEqual(ads._search(session, "test", "query"), [DAY])
        sleep.assert_called_once()
        session.reset_mock()
        session.post.side_effect = None
        session.post.return_value.status_code = 403
        with self.assertRaisesRegex(InputError, "조회 권한"):
            ads._search(session, "test", "query")
        session.post.assert_called_once()

    def test_rejects_partial_response_envelope(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = [{"results": [DAY]}, {"error": {"code": 500}}]
        with self.assertRaisesRegex(InputError, "일부 데이터"):
            ads._search(session, "test", "query")
