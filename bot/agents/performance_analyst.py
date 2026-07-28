"""
정하준 (performance_analyst) 작업 실행 로직.
routine.yaml의 task: run_bid_optimization / steps 를 코드로 옮긴 것.

steps 매핑:
  check_tracking_status → get_tracking_status()
      Critical → halt (제안 생성 안 함)
      Warning  → caution_flag=True 로 진행
  fetch_data → get_fake_ads_data()
  analyze    → call_gemini_structured()
  classify   → |adj|>20 or PAUSE → human_approval.queue
               else → auto_apply_queue
               sensitive → compliance_review.queue 병행
  record     → db.record_run()
"""
import json

from ..loaders import build_system_prompt
from ..fake_data import get_fake_ads_data
from ..llm_client import call_gemini_structured
from ..schemas import PerformanceAnalystOutput, BidRecommendation
from ..db import record_run, recent_history, get_tracking_status
from ..config import GEMINI_MODEL, USE_FAKE_DATA

AGENT_ID = "performance_analyst"


def _format_history() -> str:
    rows = recent_history(AGENT_ID, limit=5)
    if not rows:
        return "(최근 제안 이력 없음)"
    lines = []
    for created_at, output_json, routed_to, human_decision in rows:
        try:
            output = json.loads(output_json)
            kw = output.get("keyword", "?")
            adj = output.get("adjustment_percentage", "?")
            action = output.get("action", "?")
            summary = f"{kw} | {action} {adj}% | 사람판단={human_decision or '대기중'}"
        except (json.JSONDecodeError, AttributeError):
            summary = output_json[:120]
        lines.append(f"- {created_at[:19]} | {summary} | routed_to={routed_to}")
    return "\n".join(lines)


def _classify_targets(item: BidRecommendation) -> list[str]:
    needs_human = abs(item.adjustment_percentage) > 20 or item.action == "PAUSE"
    primary = "human_approval.queue" if needs_human else "auto_apply_queue"
    targets = [primary]
    if item.sensitive_keyword or item.category in ("성범죄", "미성년자"):
        if "compliance_review.queue" not in targets:
            targets.append("compliance_review.queue")
    return targets


def run_bid_optimization(tracking_override: str | None = None) -> dict:
    """
    매일 08:30 / 슬래시커맨드가 실행하는 작업.

    tracking_override: 데모용. None이면 DB tracking_status 사용.
      "normal" | "warning" | "critical"

    반환:
      halted=True 이면 recommendations 없음, halt_reason / notify_targets 포함
      아니면 {"results": [{"item", "targets", "record_id"}, ...], "caution": bool, "tracking_status": str}
    """
    tracking = get_tracking_status()
    if tracking_override:
        key = tracking_override.lower().strip()
        status = {"critical": "Critical", "warning": "Warning"}.get(key, "Normal")
    elif tracking:
        status = tracking["status"]
    else:
        status = "Normal"  # 아직 오지민 미실행 시 시범용 기본값

    if status == "Critical":
        # routine: halt_task, notify data_assistant/dev_team
        halt_payload = {
            "halted": True,
            "tracking_status": status,
            "halt_reason": (
                "오지민 tracking_status=Critical — 오염된 데이터로 입찰 조정을 제안하지 않습니다. "
                "트래킹 복구 후 재실행하세요."
            ),
            "notify_targets": ["dev_team.queue"],
            "results": [],
        }
        record_id = record_run(
            agent_id=AGENT_ID,
            output={"halted": True, "reason": halt_payload["halt_reason"], "tracking_status": status},
            routed_to="dev_team.queue",
            model_version=GEMINI_MODEL,
            input_summary="halt:tracking_critical",
        )
        halt_payload["record_id"] = record_id
        return halt_payload

    caution = status == "Warning"
    ads_data = get_fake_ads_data() if USE_FAKE_DATA else get_fake_ads_data()
    system_prompt = build_system_prompt(AGENT_ID)
    history_text = _format_history()

    user_prompt = f"""아래는 전일 Google Ads / Naver 검색광고 성과 데이터입니다.
SOUL.md 원칙에 따라 입찰/예산 조정 제안을 JSON으로 응답하세요.

중요:
- rationale에는 관찰된 지표만 쓰세요. 이용자 심리(불안, 패닉 등) 언급 금지.
- ±20% 초과 또는 PAUSE는 사람이 승인해야 하므로 action/adjustment를 그에 맞게 설정하세요.
- 성범죄·미성년자 카테고리는 sensitive_keyword=true 로 표시하세요.
- 트래킹 상태가 Warning이면 caution_flag=true 로 표시하세요. (현재: {status})

# 트래킹 상태 (오지민)
status={status}, caution={caution}

# 최근 제안 이력
{history_text}

# 성과 데이터
{json.dumps(ads_data, ensure_ascii=False, indent=2)}
"""

    result: PerformanceAnalystOutput = call_gemini_structured(
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
        record_id = record_run(
            agent_id=AGENT_ID,
            output=item.model_dump(),
            routed_to=",".join(targets),
            model_version=GEMINI_MODEL,
            input_summary=item.keyword,
        )
        routed_results.append({"item": item, "targets": targets, "record_id": record_id})

    return {
        "halted": False,
        "tracking_status": status,
        "caution": caution,
        "results": routed_results,
    }
