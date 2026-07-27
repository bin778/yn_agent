"""
서연우 (keyword_analyst) 작업 실행 로직.
routine.yaml의 task: run_daily_keyword_scan / steps 를 그대로 코드로 옮긴 것.

steps 매핑:
  fetch_data → get_fake_search_data() (또는 실제 API, config.USE_FAKE_DATA로 전환)
  analyze    → call_gemini_structured()
  classify   → compliance_risk == "high" → compliance_review.queue
               else                       → content_editor.inbox
  record     → db.record_run()
"""
import json

from ..loaders import build_system_prompt
from ..fake_data import get_fake_search_data
from ..llm_client import call_gemini_structured
from ..schemas import KeywordAnalystOutput
from ..db import record_run, recent_history
from ..config import GEMINI_MODEL

AGENT_ID = "keyword_analyst"


def _format_history(agent_id: str) -> str:
    rows = recent_history(agent_id, limit=5)
    if not rows:
        return "(최근 이력 없음 — 첫 실행이거나 아직 반려 사례가 없음)"
    lines = []
    for created_at, output_json, routed_to, human_decision in rows:
        lines.append(f"- {created_at[:19]} | routed_to={routed_to} | 사람판단={human_decision or '대기중'}")
    return "\n".join(lines)


def run_daily_keyword_scan() -> list[dict]:
    """
    routine.yaml의 daily_keyword_scan 트리거가 실행하는 작업.
    반환값: [{"item": KeywordItem, "target": str, "record_id": int}, ...]
    """
    system_prompt = build_system_prompt(AGENT_ID)
    data = get_fake_search_data()
    history_text = _format_history(AGENT_ID)

    user_prompt = f"""아래는 최근 검색 데이터입니다. SOUL.md의 판단 원칙과 컴플라이언스 규정에 따라
각 항목의 urgency_level, compliance_risk를 판정하고 JSON으로 응답하세요.

# 최근 작업 이력 (패턴 참고용)
{history_text}

# 오늘의 검색 데이터
{json.dumps(data, ensure_ascii=False, indent=2)}
"""

    result: KeywordAnalystOutput = call_gemini_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=KeywordAnalystOutput,
    )

    routed_results = []
    for item in result.items:
        # config.md classify 규칙: compliance_risk == high → compliance_review.queue
        target = "compliance_review.queue" if item.compliance_risk == "high" else "content_editor.inbox"

        record_id = record_run(
            agent_id=AGENT_ID,
            output=item.model_dump(),
            routed_to=target,
            model_version=GEMINI_MODEL,
            input_summary=item.keyword,
        )
        routed_results.append({"item": item, "target": target, "record_id": record_id})

    return routed_results
