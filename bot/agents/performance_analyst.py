"""
김도현 (performance_analyst) 작업 실행 로직.
routine.yaml의 task: run_bid_optimization / steps 를 코드로 옮긴 것.

steps 매핑:
  check_tracking_status → get_tracking_status()
      Critical → halt (제안 생성 안 함)
      Warning  → caution_flag=True 로 진행
  fetch_data → get_fake_ads_data()
  analyze    → call_structured()
  classify   → 모든 제안은 퍼포먼스 승인 대기
               |adj|>20 or PAUSE → operations_owner 추가
               sensitive → legal_compliance 추가
  record     → workflow.create_work_item()
"""
import json
from decimal import Decimal

from ..brief_inputs import comparison_text
from ..loaders import build_system_prompt
from ..fake_data import get_fake_ads_data
from ..llm_client import call_structured, get_model_identity
from ..schemas import PerformanceAnalystOutput, BidRecommendation
from ..db import recent_history, get_tracking_status
from ..workflow import create_work_item
from ..config import USE_FAKE_DATA

AGENT_ID = "performance_analyst"


def _signed_change(value: Decimal, prefix: str = "", suffix: str = "", *, places=None) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    magnitude = f"{abs(value):,.{places}f}" if places is not None else f"{abs(value):,}"
    return f"{sign}{prefix}{magnitude}{suffix}"


def _weekly_ad_detail(data: dict) -> str:
    comparison = data.get("comparison", {})
    previous = comparison.get("previous")
    if previous is None:
        return comparison_text(data)
    previous_conversions = Decimal(previous["conversions"])
    current_conversions = Decimal(data["conversions"])
    previous_cpa = Decimal(previous["cost"]) / previous_conversions if previous_conversions else None
    current_cpa = Decimal(data["cost"]) / current_conversions if current_conversions else None
    before = f"₩{previous_cpa:,.0f}" if previous_cpa is not None else "계산 불가 (전환 0건)"
    after = f"₩{current_cpa:,.0f}" if current_cpa is not None else "계산 불가 (전환 0건)"
    cpa = f"광고 CPA · {before} → {after}"
    if previous_cpa is not None and current_cpa is not None:
        cpa += f" ({_signed_change(current_cpa - previous_cpa, '₩', places=0)})"

    lines = [comparison_text(data), cpa]
    changes = comparison.get("ad_changes")
    if changes is None:
        return "\n".join(lines)
    actions = []
    for key, label, prefix, suffix, task in (
        ("cost", "광고비", "₩", "", "게재량과 비용 변화가 있었던 광고 항목을 원본 보고서에서 확인하세요."),
        ("conversions", "광고 전환수", "", "건", "전환 액션별 기록과 전환 지연 여부를 확인하세요."),
    ):
        largest = max(changes["daily"], key=lambda row: abs(Decimal(row[key])))
        delta = Decimal(largest[key])
        if delta:
            actions.append(
                f"{largest['previous_date']} → {largest['current_date']} {label} "
                f"{_signed_change(delta, prefix, suffix)} (같은 요일 중 최대 변동). {task}"
            )
    campaign = changes["campaign"]
    if campaign is not None:
        campaign_cost = Decimal(campaign["cost"])
        campaign_conversions = Decimal(campaign["conversions"])
        if campaign_cost or campaign_conversions:
            name = " ".join(campaign["name"].split())
            actions.append(
                f"캠페인 {name} · 광고비 {_signed_change(campaign_cost, '₩')}, "
                f"광고 전환수 {_signed_change(campaign_conversions, suffix='건')}. "
                "캠페인 변경 이력과 성과를 대조하세요."
            )
    if not actions:
        actions.append("같은 요일끼리 비교한 일별 광고비와 광고 전환수의 차이가 0입니다. 다음 7일도 같은 기준으로 추적하세요.")
    lines.extend(("다음 행동", *(f"{i}. {action}" for i, action in enumerate(actions, 1))))
    return "\n".join(lines)


def record_imported_analysis(data: dict, issue: str | None, *, db_path, record_work_item) -> None:
    if issue:
        output = {"halted": True, "reason": issue}
    else:
        cost, conversions = Decimal(data["cost"]), Decimal(data["conversions"])
        clicks, impressions = data["clicks"], data["impressions"]
        ctr = f"{Decimal(clicks) * 100 / impressions:.2f}%" if impressions else "계산 불가"
        cpc = f"₩{cost / clicks:,.0f}" if clicks else "계산 불가"
        cpa = f"₩{cost / conversions:,.0f}" if conversions else "계산 불가"
        reason = f"노출 {impressions:,}회 · 클릭 {clicks:,}회\nCTR {ctr} · CPC {cpc} · CPA {cpa}"
        detail = _weekly_ad_detail(data)
        output = {
            "historical_summary": True,
            "title": "광고 성과 비교·점검 제안" if data.get("comparison", {}).get("ad_changes") else "광고 성과 요약",
            "reason": f"{reason}\n\n{detail}" if detail else reason,
        }
    record_work_item(
        db_path=db_path, agent_id=AGENT_ID, output=output,
        routed_to="dev_team.queue" if issue else "performance_review.queue",
        model_version="policy-engine/no-llm", input_summary="verified_input:advertising",
    )


def _format_history(db_path=None) -> str:
    rows = recent_history(AGENT_ID, limit=5, db_path=db_path)
    if not rows:
        return "(최근 제안 이력 없음)"
    lines = []
    for created_at, output_json, routed_to, workflow_status in rows:
        try:
            output = json.loads(output_json)
            kw = output.get("keyword", "?")
            adj = output.get("adjustment_percentage", "?")
            action = output.get("action", "?")
            summary = f"{kw} | {action} {adj}% | workflow_status={workflow_status}"
        except (json.JSONDecodeError, AttributeError):
            summary = output_json[:120]
        lines.append(f"- {created_at[:19]} | {summary} | routed_to={routed_to}")
    return "\n".join(lines)


def _classify_targets(item: BidRecommendation) -> list[str]:
    targets = ["performance_review.queue"]
    if item.sensitive_keyword or item.category in ("성범죄", "미성년자"):
        if "compliance_review.queue" not in targets:
            targets.append("compliance_review.queue")
    return targets


def run_bid_optimization(
    tracking_override: str | None = None, *, db_path=None, record_work_item=None,
) -> dict:
    """
    매일 08:30 / 슬래시커맨드가 실행하는 작업.

    tracking_override: 데모용. None이면 DB tracking_status 사용.
      "normal" | "warning" | "critical"

    반환:
      halted=True 이면 recommendations 없음, halt_reason / notify_targets 포함
      아니면 {"results": [{"item", "targets", "work_item_id", "version"}, ...], "caution": bool, "tracking_status": str}
    """
    record = record_work_item or create_work_item
    tracking = None if tracking_override else get_tracking_status(db_path)
    if tracking_override:
        key = tracking_override.lower().strip()
        status = {"critical": "Critical", "warning": "Warning"}.get(key, "Normal")
    elif tracking:
        status = tracking["status"]
    else:
        status = "Normal"  # 아직 임지혁 미실행 시 시범용 기본값

    if status == "Critical":
        # routine: halt_task, notify data_assistant/dev_team
        halt_payload = {
            "halted": True,
            "tracking_status": status,
            "halt_reason": (
                "임지혁 tracking_status=Critical — 오염된 데이터로 입찰 조정을 제안하지 않습니다. "
                "트래킹 복구 후 재실행하세요."
            ),
            "notify_targets": ["dev_team.queue"],
            "results": [],
        }
        workflow_ref = record(
            agent_id=AGENT_ID,
            output={"halted": True, "reason": halt_payload["halt_reason"], "tracking_status": status},
            routed_to="dev_team.queue",
            model_version="policy-engine/no-llm",
            input_summary="halt:tracking_critical",
            db_path=db_path,
        )
        halt_payload.update(workflow_ref)
        return halt_payload

    caution = status == "Warning"
    ads_data = get_fake_ads_data() if USE_FAKE_DATA else get_fake_ads_data()
    system_prompt = build_system_prompt(AGENT_ID)
    history_text = _format_history(db_path)

    user_prompt = f"""아래는 전일 Google Ads / Naver 검색광고 성과 데이터입니다.
SOUL.md 원칙에 따라 입찰/예산 조정 제안을 JSON으로 응답하세요.

중요:
- rationale에는 관찰된 지표만 쓰세요. 이용자 심리(불안, 패닉 등) 언급 금지.
- ±20% 초과 또는 PAUSE는 사람이 승인해야 하므로 action/adjustment를 그에 맞게 설정하세요.
- 성범죄·미성년자 카테고리는 sensitive_keyword=true 로 표시하세요.
- 트래킹 상태가 Warning이면 caution_flag=true 로 표시하세요. (현재: {status})

# 트래킹 상태 (임지혁)
status={status}, caution={caution}

# 최근 제안 이력
{history_text}

# 성과 데이터
{json.dumps(ads_data, ensure_ascii=False, indent=2)}
"""

    result: PerformanceAnalystOutput = call_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=PerformanceAnalystOutput,
    )

    routed_results = []
    for item in result.recommendations:
        if caution and not item.caution_flag:
            item = item.model_copy(update={"caution_flag": True})
        # 카테고리 기반 sensitive 보정
        if item.category in ("성범죄", "미성년자") and not item.sensitive_keyword:
            item = item.model_copy(update={"sensitive_keyword": True})

        targets = _classify_targets(item)
        workflow_ref = record(
            agent_id=AGENT_ID,
            output=item.model_dump(),
            routed_to=",".join(targets),
            model_version=get_model_identity(),
            input_summary=item.keyword,
            db_path=db_path,
        )
        routed_results.append({"item": item, "targets": targets, **workflow_ref})

    return {
        "halted": False,
        "tracking_status": status,
        "caution": caution,
        "results": routed_results,
    }
