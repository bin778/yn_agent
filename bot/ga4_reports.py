"""Read date-level GA4 reports without changing the property."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import time
from dataclasses import dataclass, replace
from decimal import InvalidOperation
from zoneinfo import ZoneInfo

from .brief_inputs import (
    EVENT_NAMES, InputError, _amount, _check_csv_scope, _rows, ga4_ready_at, ga4_wait_message, parse_date, report_period,
)
from .db import utc_iso, utc_now

TIMEZONE = "Asia/Seoul"
_EVENT_LOOKBACK_DAYS = 7
_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
_RETRYABLE = {429, 500, 502, 503, 504}
_PERMISSION_ERROR = "GA4 조회 권한 없음. GA4 속성의 뷰어 권한과 EC2 인증을 확인해 주세요."
_UNAVAILABLE = "GA4 조회에 실패했습니다. 잠시 후 다시 시도하거나 기존 CSV 입력을 사용해 주세요."


@dataclass
class Report:
    sessions: dict[str, int]
    events: dict[tuple[str, str], int]
    fetched_at: str
    pending: bool
    verified_zeros: int = 0
    inferred_zeros: int = 0
    recent_events: frozenset[tuple[str, str]] = frozenset()

    def with_inferred_zeros(self) -> Report:
        if self.pending:
            return self
        recorded = self.recent_events | {key for key, count in self.events.items() if count > 0}
        eligible = {
            (parse_date(day).toordinal() + offset, event)
            for day, event in recorded
            for offset in range(1, _EVENT_LOOKBACK_DAYS + 1)
        }
        zeros = {
            (day, event): 0
            for day in self.sessions
            for event in EVENT_NAMES
            if (day, event) not in self.events and (parse_date(day).toordinal(), event) in eligible
        }
        return replace(self, events=self.events | zeros, inferred_zeros=self.inferred_zeros + len(zeros))

    def with_verified_zeros(self, content: str) -> Report:
        if self.pending:
            return self
        first, last = map(parse_date, (min(self.sessions), max(self.sessions)))
        label = "GA4 이벤트 원본 CSV"
        _check_csv_scope(content, label, first, last, TIMEZONE)
        reference = {}
        try:
            for row in _rows("events", content):
                event = row["이벤트 이름"]
                if event not in EVENT_NAMES:
                    continue
                day = parse_date(row["날짜"]).strftime("%Y%m%d")
                if day not in self.sessions:
                    continue
                key = day, event
                if key in reference:
                    raise InputError(f"{label}에 같은 날짜·이벤트의 행이 중복되었습니다.")
                reference[key] = int(_amount(row["이벤트 수"], integer=True))
        except InputError:
            raise
        except (TypeError, AttributeError, ValueError, InvalidOperation, csv.Error):
            raise InputError(f"{label}의 날짜와 이벤트 수 형식을 확인해 주세요.") from None
        if not reference:
            raise InputError(f"{label}에서 조회 기간의 필수 이벤트 행을 찾을 수 없습니다.")

        for (day, event), count in reference.items():
            actual = self.events.get((day, event))
            if actual != count and (actual is not None or count != 0):
                date = parse_date(day).isoformat()
                raise InputError(f"GA4 API와 원본 CSV의 이벤트 수가 일치하지 않습니다. ({date} · {event})")
        zeros = {key: 0 for key, value in reference.items() if value == 0 and key not in self.events}
        return replace(self, events=self.events | zeros, verified_zeros=len(zeros))

    def tables(self) -> dict[str, str]:
        session_rows = sorted(self.sessions.items())
        event_rows = [(*key, value) for key, value in sorted(self.events.items())]
        tables = {}
        for kind, header, rows in (
            ("sessions", ("날짜", "세션수"), session_rows),
            ("events", ("날짜", "이벤트 이름", "이벤트 수"), event_rows),
        ):
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
            tables[kind] = stream.getvalue()
        return tables


def fetch_report(property_id: str, start: str, end: str) -> Report:
    from google.auth import default
    from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
    from google.auth.transport.requests import AuthorizedSession
    from requests.exceptions import RequestException

    _, last = report_period(start, end)
    now = utc_now()
    seoul = ZoneInfo(TIMEZONE)
    if last > now.astimezone(seoul).date():
        raise InputError("GA4 보고 종료일은 한국 시간 기준 오늘까지 선택할 수 있습니다.")
    if not re.fullmatch(r"[0-9]+", property_id):
        raise InputError("GA4 속성 ID 설정을 확인해 주세요.")
    try:
        credentials, _ = default(scopes=[_SCOPE])
    except (DefaultCredentialsError, RefreshError):
        raise InputError(_PERMISSION_ERROR) from None
    except TransportError:
        raise InputError(_UNAVAILABLE) from None
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:batchRunReports"
    try:
        with AuthorizedSession(
            credentials, refresh_timeout=4, max_refresh_attempts=0,
        ) as session:
            return read_report(start, end, lambda payload: _run(session, url, payload))
    except (DefaultCredentialsError, RefreshError):
        raise InputError(_PERMISSION_ERROR) from None
    except (RequestException, TransportError):
        raise InputError(_UNAVAILABLE) from None


def _run(session, url: str, payload: dict) -> dict:
    from google.auth.exceptions import TransportError
    from requests.exceptions import ConnectionError, Timeout

    for attempt in range(2):
        try:
            response = session.post(
                url, json=payload, timeout=(3, 5), max_allowed_time=9, allow_redirects=False,
            )
        except (ConnectionError, Timeout, TransportError):
            if attempt:
                raise
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    raise InputError(_UNAVAILABLE) from None
            if response.status_code in {401, 403}:
                raise InputError(_PERMISSION_ERROR)
            if response.status_code not in _RETRYABLE:
                break
        if not attempt:
            time.sleep(0.5)
    raise InputError(_UNAVAILABLE)


def read_report(start: str, end: str, run) -> Report:
    first, last = report_period(start, end)
    if (last - first).days == 6 and first >= dt.date.min + dt.timedelta(days=7):
        first -= dt.timedelta(days=7)
    now = utc_now()
    pending = now < ga4_ready_at(last, TIMEZONE)
    recent_first = first if pending else dt.date.fromordinal(max(1, first.toordinal() - _EVENT_LOOKBACK_DAYS))
    dates = [{"startDate": first.isoformat(), "endDate": last.isoformat()}]
    event_dates = [{"startDate": recent_first.isoformat(), "endDate": last.isoformat()}]
    day = {"name": "date"}
    payload = {"requests": [
        {"dateRanges": dates, "dimensions": [day], "metrics": [{"name": "sessions"}], "keepEmptyRows": True},
        {"dateRanges": event_dates, "dimensions": [day, {"name": "eventName"}], "metrics": [{"name": "eventCount"}],
         "keepEmptyRows": True, "dimensionFilter": {"filter": {
             "fieldName": "eventName", "inListFilter": {"values": list(EVENT_NAMES), "caseSensitive": True},
         }}},
    ]}
    response = run(payload)
    try:
        sessions_report, events_report = response["reports"]
        sessions = _report_rows(sessions_report, ("date",), "sessions", first, last)
        events = _report_rows(events_report, ("date", "eventName"), "eventCount", recent_first, last)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("GA4 응답의 날짜 또는 지표 형식을 확인할 수 없습니다.") from None
    expected = {(first + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range((last - first).days + 1)}
    missing = sorted(expected - {key[0] for key in sessions})
    if missing:
        if pending:
            raise InputError(ga4_wait_message(last, TIMEZONE))
        raise InputError("GA4 세션 날짜 누락 · " + ", ".join(missing[:5]))
    recent = frozenset(key for key, count in events.items() if key[0] not in expected and count > 0)
    report_events = {key: count for key, count in events.items() if key[0] in expected}
    return Report(
        {key[0]: value for key, value in sessions.items()}, report_events, utc_iso(utc_now()), pending,
        recent_events=recent,
    )


def _report_rows(report, dimensions, metric, first, last):
    metadata = report["metadata"]
    if metadata.get("timeZone") != TIMEZONE:
        raise InputError("GA4 보고서 시간대를 확인할 수 없거나 Asia/Seoul과 일치하지 않습니다.")
    if (any(metadata.get(field) for field in (
        "subjectToThresholding", "dataLossFromOtherRow", "samplingMetadatas", "dataTruncationReasons",
    )) or metadata.get("schemaRestrictionResponse", {}).get("activeMetricRestrictions")):
        raise InputError("GA4 데이터 제한 · 임계값, 샘플링 또는 집계 제한이 적용되었습니다.")
    if (tuple(h["name"] for h in report["dimensionHeaders"]) != dimensions
            or [h["name"] for h in report["metricHeaders"]] != [metric]):
        raise ValueError("unexpected headers")
    rows = report.get("rows", [])
    if report.get("rowCount", 0) != len(rows):
        raise InputError("GA4 데이터 제한 · 조회 응답의 일부 행이 누락되었습니다.")
    values = {}
    for row in rows:
        key = tuple(d["value"] for d in row["dimensionValues"])
        value, = row["metricValues"]
        raw = value["value"]
        if (len(key) != len(dimensions) or not re.fullmatch(r"[0-9]{8}", key[0])
                or not first.strftime("%Y%m%d") <= key[0] <= last.strftime("%Y%m%d")
                or (len(key) == 2 and key[1] not in EVENT_NAMES)
                or key in values or not re.fullmatch(r"[0-9]{1,15}", raw)):
            raise ValueError("invalid row")
        values[key] = int(raw)
    return values
