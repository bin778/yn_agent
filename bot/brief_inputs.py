"""Normalize operator-supplied CSV and pasted tables for a historical team brief."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import utc_iso, utc_now

FIELDS = {
    "sessions": ("GA4 세션", ("날짜", "세션수")),
    "events": ("GA4 이벤트", ("날짜", "이벤트 이름", "이벤트 수")),
    "ads": ("광고 성과", ("날짜", "전환수", "노출수", "클릭수", "비용")),
    "keywords": ("검색어", ("날짜", "검색어", "노출수")),
}
EVENT_NAMES = ("form_submit", "home_kakao_click")
WEEK = dt.timedelta(days=7)
COMPARISON_METRICS = (
    ("sessions", "GA4 세션", "", "회"),
    ("form_submit", "GA4 폼 제출", "", "건"),
    ("home_kakao_click", "GA4 카카오톡 클릭", "", "회"),
    ("impressions", "광고 노출", "", "회"),
    ("clicks", "광고 클릭", "", "회"),
    ("cost", "광고비", "₩", ""),
    ("conversions", "광고 플랫폼 전환수", "", "건"),
)
_KOREAN_DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(?:\([^)]*\))?")


class InputError(ValueError):
    """A safe explanation that can be shown to the submitter."""


def parse_date(value: str) -> dt.date:
    value = value.strip()
    match = _KOREAN_DATE.fullmatch(value)
    if match:
        return dt.date(*map(int, match.groups()))
    if re.fullmatch(r"\d{8}", value):
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:]))
    return dt.date.fromisoformat(value)


def _amount(value: str, *, integer: bool = False) -> Decimal:
    raw = value.strip().removeprefix("₩").replace(",", "")
    if not re.fullmatch(r"\d{1,15}(?:\.\d{1,9})?", raw):
        raise ValueError("invalid amount")
    result = Decimal(raw)
    if integer and result != result.to_integral_value():
        raise ValueError("non-integer count")
    return result


def _rows(kind: str, content: str) -> list[dict]:
    label, columns = FIELDS[kind]
    csv_text = content.lstrip("\ufeff")
    lines = [line for line in csv_text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise InputError(f"{label} 자료가 비어 있습니다.")
    header = lines[0]
    delimiter = "\t" if "\t" in header else ","
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter, skipinitialspace=True, strict=True)
    names = [name.strip() for name in (reader.fieldnames or [])]
    if len(set(names)) != len(names) or not set(columns).issubset(names):
        raise InputError(f"{label} 자료의 열을 확인해 주세요. 필요한 열은 {', '.join(columns)}입니다.")
    reader.fieldnames = names
    result = []
    for row in reader:
        if not (row.get("날짜") or "").strip():
            if "총 합계" in (row.get(None) or []) or "총 합계" in row.values():
                continue
            raise InputError(f"{label} 자료에 날짜가 비어 있는 행이 있습니다.")
        if None in row:
            raise InputError(f"{label} 자료에 열 개수가 맞지 않는 행이 있습니다.")
        result.append(row)
    return result


def report_period(start: str, end: str) -> tuple[dt.date, dt.date]:
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise InputError("보고 기간의 시작일과 종료일을 선택해 주세요.") from None
    if not 0 <= (last - first).days < 92:
        raise InputError("보고 기간은 시작일부터 최대 92일입니다.")
    return first, last


def normalize(
    tables: dict[str, str], *, start: str, end: str, ga_timezone: str, ads_timezone: str,
    complete: bool, collected_through: str = "", mode: str, ads_source: str = "", ga4_report=None,
) -> dict:
    first, last = report_period(start, end)
    ga_timezone, ads_timezone = ga_timezone.strip(), ads_timezone.strip()
    for label, name in (("GA4", ga_timezone), ("광고 성과", ads_timezone)):
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise InputError(f"{label} 시간대를 확인해 주세요. 예: Asia/Seoul") from None
    if ga_timezone != ads_timezone:
        raise InputError("GA4와 광고 성과의 시간대가 다릅니다. 같은 시간대로 내보낸 보고서를 제출해 주세요.")

    timestamp = None
    if collected_through.strip():
        try:
            cutoff = dt.datetime.fromisoformat(collected_through.strip())
            if cutoff.tzinfo is None or cutoff > utc_now():
                raise ValueError
            if cutoff.astimezone(ZoneInfo(ga_timezone)).date() < last:
                raise ValueError
            timestamp = utc_iso(cutoff)
        except (ValueError, OverflowError):
            raise InputError("원본 집계 시각은 보고 종료일 이후의 확인된 시각을 UTC 오프셋과 함께 입력해 주세요.") from None

    current, _, current_ads = _read_period(tables, first, last, ga_timezone, from_ga4=ga4_report is not None)
    data = {
        **current,
        "mode": mode, "start": first.isoformat(), "end": last.isoformat(), "timezone": ga_timezone,
        "ads_source": " ".join(ads_source.split())[:80] or "광고 성과 보고서",
        "complete": complete, "collected_through": timestamp, "imported_at": utc_iso(),
    }
    if ga4_report is not None:
        data.update(
            ga4_fetched_at=ga4_report.fetched_at, ga4_pending=ga4_report.pending,
            ga4_verified_zeros=ga4_report.verified_zeros,
        )
    if last - first == WEEK - dt.timedelta(days=1) and first >= dt.date.min + WEEK:
        previous_start, previous_end = first - WEEK, last - WEEK
        comparison = {"start": previous_start.isoformat(), "end": previous_end.isoformat()}
        issue = hold_reason(data)
        if issue:
            comparison["reason"] = issue
        else:
            required = {kind: tables[kind] for kind in ("sessions", "events", "ads")}
            try:
                previous, missing_events, previous_ads = _read_period(
                    required, previous_start, previous_end, ga_timezone, from_ga4=ga4_report is not None,
                )
                if any(name for _, name in current_ads) != any(name for _, name in previous_ads):
                    raise InputError("광고 성과 자료의 계정 합계와 캠페인별 집계 단위를 맞춰 주세요.")
            except InputError as exc:
                comparison["reason"] = str(exc)
            else:
                comparison["previous"] = {
                    key: None if key in missing_events else previous[key]
                    for key, _, _, _ in COMPARISON_METRICS
                }
                comparison["ad_changes"] = _advertising_changes(previous_ads, current_ads)
        data["comparison"] = comparison
    return data


def _check_csv_scope(content: str, label: str, first: dt.date, last: dt.date, timezone: str) -> None:
    timezone_header = re.search(r"^#\s*(?:시간대|Time\s*zone)\s*:\s*(\S+)\s*$", content, re.M | re.I)
    if timezone_header and timezone_header[1] != timezone:
        raise InputError(f"{label} 자료의 시간대가 입력한 시간대와 다릅니다.")
    period_header = re.search(r"^#[ \t]*(\d{8})[ \t]*-[ \t]*(\d{8})[ \t]*\r?$", content, re.M)
    if period_header:
        try:
            report_start, report_end = map(parse_date, period_header.groups())
            if not report_start <= first <= last <= report_end:
                raise ValueError
        except ValueError:
            raise InputError(
                f"{label} 자료의 원본 기간이 선택한 보고 기간을 포함하지 않습니다. 기간을 맞춰 다시 제출해 주세요."
            ) from None


def _read_period(
    tables: dict[str, str], first: dt.date, last: dt.date, timezone: str, *, from_ga4: bool = False,
) -> tuple[dict, dict[str, int], dict]:
    days = {first + dt.timedelta(days=i) for i in range((last - first).days + 1)}
    facts: dict[str, dict] = {}
    for kind, content in tables.items():
        label = FIELDS[kind][0]
        parsed: dict = {}
        source_days: set[dt.date] = set()
        _check_csv_scope(content, label, first, last, timezone)
        try:
            for row in _rows(kind, content):
                day = parse_date(row["날짜"])
                source_days.add(day)
                if day not in days:
                    continue
                if kind == "sessions":
                    key, value = day, int(_amount(row["세션수"], integer=True))
                elif kind == "events":
                    if row["이벤트 이름"] not in EVENT_NAMES:
                        continue
                    key = day, row["이벤트 이름"]
                    value = int(_amount(row["이벤트 수"], integer=True))
                elif kind == "ads":
                    campaign = (row.get("캠페인") or "").strip()
                    if len(campaign) > 120:
                        raise ValueError("campaign length")
                    key = day, campaign
                    value = tuple(
                        _amount(row[column], integer=column in {"노출수", "클릭수"})
                        for column in ("전환수", "노출수", "클릭수", "비용")
                    )
                else:
                    term = row["검색어"].strip()
                    if not term or len(term) > 120 or re.search(r"@|https?://|[0-9]{3}[- ]?[0-9]{3,4}[- ]?[0-9]{4}", term, re.I):
                        raise ValueError("search term")
                    key, value = (day, term), int(_amount(row["노출수"], integer=True))
                if key in parsed:
                    raise InputError(f"{label} 자료에 같은 날짜·항목의 행이 중복되었습니다.")
                parsed[key] = value
        except (TypeError, AttributeError, ValueError, InvalidOperation, csv.Error) as exc:
            if isinstance(exc, InputError):
                raise
            raise InputError(f"{label} 자료의 날짜, 빈칸, 숫자 또는 검색어 형식을 확인해 주세요.") from None
        if kind in {"sessions", "ads"}:
            missing = sorted(days - source_days)
            if missing:
                shown = ", ".join(day.isoformat() for day in missing[:5])
                raise InputError(f"{label} 자료에 보고 기간의 날짜가 빠져 있습니다 ({shown}).")
        elif not (from_ga4 and kind == "events") and source_days and (min(source_days) > last or max(source_days) < first):
            raise InputError(f"{label} 자료에 선택한 보고 기간의 행이 없습니다.")
        facts[kind] = parsed

    events = facts["events"]
    event_counts = {
        name: [events[day, name] for day in days if (day, name) in events] for name in EVENT_NAMES
    }
    missing_events = {name: len(days) - len(counts) for name, counts in event_counts.items() if len(counts) < len(days)}
    warnings = [f"{name} 행 없음 {count}일" for name, count in missing_events.items()]
    ads = facts["ads"]
    campaign_levels = {bool(campaign) for _, campaign in ads}
    if len(campaign_levels) > 1:
        raise InputError("광고 성과 자료에 계정 합계와 캠페인별 행이 섞여 있습니다. 한 가지 단위로 제출해 주세요.")
    summary = [sum(row[i] for row in ads.values()) for i in range(4)]
    keywords: dict[str, int] = {}
    for (_, term), impressions in facts.get("keywords", {}).items():
        keywords[term] = keywords.get(term, 0) + impressions

    return {
        "sessions": sum(facts["sessions"].values()),
        "form_submit": sum(event_counts["form_submit"]) if event_counts["form_submit"] else None,
        "home_kakao_click": sum(event_counts["home_kakao_click"]) if event_counts["home_kakao_click"] else None,
        "conversions": str(summary[0]), "impressions": int(summary[1]), "clicks": int(summary[2]),
        "cost": str(summary[3]), "warnings": warnings,
        "keywords": [{"term": term, "impressions": count} for term, count in
                     sorted(keywords.items(), key=lambda entry: (-entry[1], entry[0]))[:30]],
    }, missing_events, ads


def _advertising_changes(previous_ads: dict, current_ads: dict) -> dict:
    def totals(rows):
        by_day = defaultdict(lambda: [Decimal(0), Decimal(0)])
        by_campaign = defaultdict(dict)
        for (day, campaign), (conversions, _, _, cost) in rows.items():
            by_day[day][0] += cost
            by_day[day][1] += conversions
            if campaign:
                by_campaign[campaign][day] = (cost, conversions)
        return by_day, by_campaign

    def change(before, after):
        before_cost, before_conversions = before
        after_cost, after_conversions = after
        return {"cost": str(after_cost - before_cost), "conversions": str(after_conversions - before_conversions)}

    previous_days, previous_campaigns = totals(previous_ads)
    current_days, current_campaigns = totals(current_ads)
    daily = [
        {"previous_date": day.isoformat(), "current_date": (day + WEEK).isoformat(),
         **change(previous_days[day], current_days[day + WEEK])}
        for day in sorted(previous_days)
    ]
    campaigns = []
    for name in sorted(previous_campaigns.keys() & current_campaigns.keys()):
        before, after = previous_campaigns[name], current_campaigns[name]
        if len(before) != WEEK.days or len(after) != WEEK.days:
            continue
        before_totals = tuple(map(sum, zip(*before.values())))
        after_totals = tuple(map(sum, zip(*after.values())))
        campaigns.append({"name": name, **change(before_totals, after_totals)})
    largest_campaign = max(
        campaigns, default=None,
        key=lambda row: (abs(Decimal(row["cost"])), abs(Decimal(row["conversions"]))),
    )
    return {"daily": daily, "campaign": largest_campaign}


def comparison_text(data: dict) -> str:
    comparison = data.get("comparison")
    if comparison is None:
        return ""
    header = f"직전 7일 비교 · {comparison['start']} ~ {comparison['end']} → {data['start']} ~ {data['end']}"
    if "reason" in comparison:
        return f"{header}\n비교 보류 · {comparison['reason']}"
    lines = [header]
    for key, label, prefix, suffix in COMPARISON_METRICS:
        previous = comparison["previous"][key]
        if previous is None:
            lines.append(f"{label} · 비교 보류 (직전 기간 일별 행 누락)")
            continue
        before, current = Decimal(previous), Decimal(data[key])
        change = current - before
        sign = "+" if change > 0 else "-" if change < 0 else ""
        lines.append(
            f"{label} · {prefix}{before:,}{suffix} → {prefix}{current:,}{suffix}"
            f" ({sign}{prefix}{abs(change):,}{suffix})"
        )
    return "\n".join(lines)


def hold_reason(data: dict) -> str | None:
    if data.get("ga4_pending"):
        return "GA4 집계 대기 · 보고 종료일이 끝난 뒤 48시간 이내입니다."
    if not data["complete"]:
        return "집계 완료가 미확인입니다."
    if data["warnings"]:
        if data["mode"] == "ga4":
            return "GA4 필수 이벤트의 일별 행이 빠져 있습니다. 원본 GA4 이벤트 CSV에서 0건을 확인할 수 있으면 첨부해 주세요."
        return "GA4 필수 이벤트의 일별 행이 빠져 있습니다. 유효한 0건은 숫자 0으로 입력해 주세요."
    return None


def summary_text(data: dict) -> str:
    def observed(value):
        return f"{value:,}회" if value is not None else "기록 행 없음"

    return (
        f"세션 {data['sessions']:,}회 · 폼 제출 {observed(data['form_submit'])} · "
        f"카카오톡 클릭 {observed(data['home_kakao_click'])}\n"
        f"광고비 ₩{Decimal(data['cost']):,} · 광고 전환 {data['conversions']}건"
    )
