"""Read and reconcile campaign-level Google Ads reports."""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .brief_inputs import InputError, WEEK, report_period
from .db import utc_iso
from .ga4_reports import TIMEZONE

LOGGER = logging.getLogger(__name__)
_SCOPE = "https://www.googleapis.com/auth/adwords"
_METRICS = "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions"
_EMPTY = (0, 0, 0, Decimal(0))
_PERMISSION = "Google Ads 조회 권한을 확인해 주세요. 관리자에게 계정의 읽기 전용 권한과 API 접근 등급 확인을 요청해 주세요."
_UNAVAILABLE = "Google Ads를 조회하지 못했습니다. 잠시 후 다시 시도해 주세요."
_INVALID = "Google Ads 보고서의 일부 데이터를 검증할 수 없습니다. 다시 조회해 주세요."


def weekly_period(start: str, end: str) -> tuple[dt.date, dt.date]:
    first, last = report_period(start, end)
    if last - first != dt.timedelta(days=6) or first.toordinal() <= WEEK.days:
        raise InputError("시작일부터 종료일까지 7일을 선택해 주세요.")
    return first, last


def csv_metrics(values) -> tuple[int, int, Decimal, Decimal]:
    impressions, clicks, micros, conversions = values
    cost = (Decimal(micros) / 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return impressions, clicks, cost, conversions.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Report:
    names: dict[str, str]
    rows: dict[tuple[dt.date, str], tuple[int, int, int, Decimal]]
    fetched_at: str

    def table(self) -> str:
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(("날짜", "캠페인", "노출수", "클릭수", "비용", "전환수"))
        counts = Counter(self.names.values())
        labels = {
            campaign: name if counts[name] == 1 else f"{name} ({campaign})"
            for campaign, name in self.names.items()
        }
        if len(set(labels.values())) != len(labels):
            raise InputError("이름이 겹치는 Google Ads 캠페인을 구분할 수 없습니다.")
        for (day, campaign), values in sorted(self.rows.items()):
            writer.writerow((day.isoformat(), labels[campaign], *csv_metrics(values)))
        return stream.getvalue()


def _natural(value) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", str(value)):
        raise ValueError("expected a nonnegative integer")
    return int(value)


def _values(row) -> tuple[int, int, int, Decimal]:
    metrics = row.get("metrics", {})
    conversions = Decimal(str(metrics.get("conversions", 0)))
    if not conversions.is_finite() or not 0 <= conversions < 10**15:
        raise ValueError("invalid conversions")
    return (
        _natural(metrics.get("impressions", 0)),
        _natural(metrics.get("clicks", 0)),
        _natural(metrics.get("costMicros", 0)),
        conversions,
    )


def _read_report(customer_id, first, last, search) -> Report:
    account, = search("SELECT customer.id, customer.time_zone, customer.currency_code FROM customer")
    settings = account["customer"]
    if str(settings["id"]) != customer_id:
        raise InputError("Google Ads에서 설정한 계정을 확인할 수 없습니다.")
    if settings.get("timeZone") != TIMEZONE or settings.get("currencyCode") != "KRW":
        raise InputError("Google Ads 계정의 시간대와 통화를 확인해 주세요. Asia/Seoul과 KRW가 필요합니다.")

    scope = "FROM campaign WHERE campaign.status = 'ENABLED' AND campaign.experiment_type = 'BASE'"
    names = {}
    for row in search(f"SELECT campaign.id, campaign.name {scope}"):
        campaign = row["campaign"]
        key = str(_natural(campaign["id"]))
        name = campaign["name"].strip()
        if key in names or not name:
            raise ValueError("duplicate or missing campaign")
        names[key] = name
    if not names:
        raise InputError("Google Ads 계정에 사용 중인 캠페인이 없습니다.")

    scope += f" AND segments.date BETWEEN '{first.isoformat()}' AND '{last.isoformat()}'"
    totals = {}
    for row in search(f"SELECT campaign.id, {_METRICS} {scope}"):
        key = str(_natural(row["campaign"]["id"]))
        if key not in names or key in totals:
            raise ValueError("unexpected campaign total")
        totals[key] = _values(row)
    rows = {}
    sums = defaultdict(lambda: _EMPTY)
    for row in search(f"SELECT campaign.id, segments.date, {_METRICS} {scope}"):
        key = str(_natural(row["campaign"]["id"]))
        raw_date = row["segments"]["date"]
        day = dt.date.fromisoformat(raw_date)
        if key not in names or not first <= day <= last or (day, key) in rows:
            raise ValueError("unexpected daily row")
        values = rows[day, key] = _values(row)
        sums[key] = tuple(a + b for a, b in zip(sums[key], values))

    for key in names:
        observed, expected = sums[key], totals.get(key, _EMPTY)
        if observed[:3] != expected[:3] or abs(observed[3] - expected[3]) > Decimal("0.00000001"):
            raise InputError("Google Ads 일별 지표와 기간 합계가 일치하지 않습니다. 잠시 후 다시 조회해 주세요.")
    for offset in range((last - first).days + 1):
        day = first + dt.timedelta(days=offset)
        for key in names:
            rows.setdefault((day, key), _EMPTY)
    return Report(names, rows, utc_iso())


def read_report(customer_id: str, start: str, end: str, search) -> Report:
    first, last = weekly_period(start, end)
    try:
        return _read_report(customer_id, first - WEEK, last, search)
    except InputError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
        raise InputError(_INVALID) from None


def _search(session, url, query):
    from google.auth.exceptions import TransportError
    from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

    for attempt in range(2):
        try:
            response = session.post(
                url, json={"query": query}, timeout=(3, 6), max_allowed_time=10, allow_redirects=False,
            )
        except (ChunkedEncodingError, ConnectionError, Timeout, TransportError):
            if attempt:
                raise
        else:
            if response.status_code == 200:
                chunks = response.json(parse_float=Decimal)
                if not isinstance(chunks, list) or any(
                    not isinstance(chunk, dict) or "error" in chunk
                    or not isinstance(chunk.get("results", []), list)
                    or not ({"results", "fieldMask", "queryResourceConsumption"} & chunk.keys())
                    for chunk in chunks
                ):
                    raise InputError(_INVALID)
                return [row for chunk in chunks for row in chunk.get("results", [])]
            LOGGER.warning("Google Ads reporting request failed status=%d", response.status_code)
            if response.status_code in {401, 403}:
                raise InputError(_PERMISSION)
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        if not attempt:
            time.sleep(0.5)
    raise InputError(_UNAVAILABLE)


def fetch_report(customer_id: str, credentials_path: str, start: str, end: str) -> Report:
    from google.auth import load_credentials_from_file
    from google.auth.aws import Credentials
    from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
    from google.auth.transport.requests import AuthorizedSession
    from requests.exceptions import RequestException

    weekly_period(start, end)
    if not re.fullmatch(r"[0-9]{10}", customer_id) or not credentials_path:
        raise InputError("관리자에게 Google Ads 계정과 서버 연결 설정 확인을 요청해 주세요.")
    url = f"https://googleads.googleapis.com/v26/customers/{customer_id}/googleAds:searchStream"
    try:
        credentials, _ = load_credentials_from_file(credentials_path, scopes=[_SCOPE])
        email = getattr(credentials, "service_account_email", None) or ""
        if not isinstance(credentials, Credentials) or not email.startswith("hermes-ads-reader@"):
            raise InputError("관리자에게 전용 Google Ads 단기 인증 설정 확인을 요청해 주세요.")
        with AuthorizedSession(credentials, refresh_timeout=4, max_refresh_attempts=0) as session:
            return read_report(customer_id, start, end, lambda query: _search(session, url, query))
    except (RequestException, TransportError):
        raise InputError(_UNAVAILABLE) from None
    except (DefaultCredentialsError, RefreshError, OSError):
        raise InputError(_PERMISSION) from None
