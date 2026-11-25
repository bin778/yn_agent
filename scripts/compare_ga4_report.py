"""Compare a mature GA4 Data API report with existing exports before enabling Slack."""

import argparse
import datetime as dt
import os
from pathlib import Path

from bot.brief_inputs import EVENT_NAMES, InputError, _amount, _rows, parse_date, report_period
from bot.ga4_reports import fetch_report


def reconcile(report, reference, *, kind):
    failures, missing_zeroes, unverified = [], [], []
    for key in sorted(reference.keys() | report.keys()):
        expected, actual = reference.get(key), report.get(key)
        label = " / ".join(key) if isinstance(key, tuple) else key
        if expected == actual:
            continue
        if kind == "events" and expected == 0 and actual is None:
            missing_zeroes.append(label)
        elif kind == "events" and expected is None and actual == 0:
            unverified.append(label)
        else:
            failures.append(f"{label}  CSV={expected}  API={actual}")
    for label in missing_zeroes:
        print(f"CSV 0건 / API 행 없음  {label}")
    for label in unverified:
        print(f"CSV 행 없음 / API 0건  {label}")
    for line in failures:
        print(f"불일치  {kind}  {line}")
    return not failures


def reference_rows(path, *, kind, first, last):
    data = {}
    for row in _rows(kind, path.read_text(encoding="utf-8-sig")):
        day = parse_date(row["날짜"])
        if not first <= day <= last:
            continue
        key = day.strftime("%Y%m%d")
        column = "세션수"
        if kind == "events":
            if row["이벤트 이름"] not in EVENT_NAMES:
                continue
            key = (key, row["이벤트 이름"])
            column = "이벤트 수"
        if key in data:
            raise InputError(f"기준 CSV의 날짜·항목이 중복되었습니다. {kind} {key}")
        data[key] = int(_amount(row[column], integer=True))
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    args = parser.parse_args()

    try:
        first, last = report_period(args.start, args.end)
        if (last - first).days == 6 and first >= dt.date.min + dt.timedelta(days=7):
            first -= dt.timedelta(days=7)
        sessions = reference_rows(args.sessions, kind="sessions", first=first, last=last)
        events = reference_rows(args.events, kind="events", first=first, last=last)
        report = fetch_report(os.environ.get("HERMES_GA4_PROPERTY_ID", "").strip(), args.start, args.end)
        if report.pending:
            raise InputError("GA4 집계 대기 · 집계 대기 기간이 끝난 뒤 대조해 주세요.")
    except (OSError, ValueError) as exc:
        print(exc)
        return 1
    print(f"대조 기간  {first} ~ {last}  Asia/Seoul  조회 {report.fetched_at}")
    print(f"API 세션 {sum(report.sessions.values())}회")
    for event in EVENT_NAMES:
        print(f"API {event} {sum(v for (_, name), v in report.events.items() if name == event)}회")
    sessions_match = reconcile(report.sessions, sessions, kind="sessions")
    events_match = reconcile(report.events, events, kind="events")
    result = sessions_match and events_match
    print("CSV 대조 통과" if result else "CSV 대조 실패")
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
