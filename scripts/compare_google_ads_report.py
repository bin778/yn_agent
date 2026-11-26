"""Compare live Google Ads campaign days with an original campaign export."""

import argparse
import csv
import os
from pathlib import Path

from bot.brief_inputs import InputError, WEEK, _amount, parse_date
from bot.google_ads_reports import csv_metrics, fetch_report, weekly_period

_COLUMNS = ("노출수", "클릭수", "비용", "전환수")


def reference_rows(path, first, last):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        lines = csv.reader(stream, strict=True)
        required = {"일", "캠페인", "캠페인 상태", "통화 코드", *_COLUMNS}
        header = next((row for row in lines if required <= set(row)), None)
        if header is None:
            raise InputError("Google Ads 원본 캠페인 보고서의 열을 확인해 주세요.")
        reader = csv.DictReader(stream, fieldnames=header, strict=True)
        rows = {}
        for row in reader:
            if row["캠페인 상태"] != "사용중":
                continue
            day = parse_date(row["일"])
            if not first <= day <= last:
                continue
            key = day, row["캠페인"].strip()
            if not key[1] or key in rows or row["통화 코드"] != "KRW":
                raise InputError("기준 CSV의 캠페인 중복, 이름 또는 통화 코드를 확인해 주세요.")
            rows[key] = tuple(_amount(row[col], integer=col in _COLUMNS[:2]) for col in _COLUMNS)
    if not rows:
        raise InputError("기준 CSV에 해당 기간의 사용 중 캠페인 행이 없습니다.")
    return rows


def reconcile(report, reference):
    if len(report.names) != len(set(report.names.values())):
        raise InputError("같은 이름의 캠페인이 있어 이름으로 내보낸 CSV와 대조할 수 없습니다.")
    actual = {(day, report.names[key]): csv_metrics(values) for (day, key), values in report.rows.items()}
    differences = 0
    for key in sorted(actual.keys() | reference.keys()):
        if actual.get(key) != reference.get(key):
            differences += 1
            print(f"불일치 {key[0]} · {key[1]} · CSV {reference.get(key)} · API {actual.get(key)}")
    print(f"캠페인 {len(report.names)}개 · CSV {len(reference)}행 · API {len(actual)}행 · 차이 {differences}행")
    return differences == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="현재 주 시작일")
    parser.add_argument("--end", required=True, help="현재 주 종료일")
    parser.add_argument("--csv", required=True, type=Path)
    args = parser.parse_args()
    try:
        first, last = weekly_period(args.start, args.end)
        reference = reference_rows(args.csv, first - WEEK, last)
        report = fetch_report(
            os.environ.get("HERMES_GOOGLE_ADS_CUSTOMER_ID", "").strip().replace("-", ""),
            os.environ.get("HERMES_GOOGLE_ADS_CREDENTIALS", "").strip(), args.start, args.end,
        )
        print(f"대조 기간 {first - WEEK} ~ {last} · 지표 순서 {' / '.join(_COLUMNS)} · API 조회 {report.fetched_at}")
        return 0 if reconcile(report, reference) else 1
    except (ValueError, OSError, csv.Error) as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
