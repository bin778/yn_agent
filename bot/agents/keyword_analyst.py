"""
윤서진 (keyword_analyst) 작업 실행 로직.
routine.yaml의 task: run_daily_keyword_scan / steps 를 그대로 코드로 옮긴 것.

steps 매핑:
  fetch_data → get_fake_search_data() (또는 실제 API, config.USE_FAKE_DATA로 전환)
  analyze    → call_structured()
  classify   → compliance_risk == "high" → compliance_review.queue
               else                       → content_editor.inbox
  record     → workflow.create_work_item() + 역할별 승인 요청
"""
import json

from ..loaders import build_system_prompt
from ..fake_data import get_fake_search_data
from ..llm_client import call_structured, get_model_identity
from ..schemas import KeywordAnalystOutput
from ..db import recent_history
from ..workflow import create_work_item

AGENT_ID = "keyword_analyst"


def _format_history(agent_id: str, db_path=None) -> str:
    rows = recent_history(agent_id, limit=5, db_path=db_path)
    if not rows:
        return "(최근 이력 없음 — 첫 실행이거나 아직 반려 사례가 없음)"
    lines = []
    for created_at, output_json, routed_to, workflow_status in rows:
        lines.append(f"- {created_at[:19]} | routed_to={routed_to} | workflow_status={workflow_status}")
    return "\n".join(lines)


def run_daily_keyword_scan(*, db_path=None, record_work_item=None) -> list[dict]:
    """
    routine.yaml의 daily_keyword_scan 트리거가 실행하는 작업.
    반환값: [{"item", "target", "work_item_id", "version", "request_id"}, ...]
    """
    record = record_work_item or create_work_item
    system_prompt = build_system_prompt(AGENT_ID)
    data = get_fake_search_data()
    history_text = _format_history(AGENT_ID, db_path)

    user_prompt = f"""아래는 최근 검색 데이터입니다. SOUL.md의 판단 원칙과 컴플라이언스 규정에 따라
각 항목의 urgency_level, compliance_risk를 판정하고 JSON으로 응답하세요.
고위험 후보에는 장소·사건·개인을 제외한 일반 검토용 주제에 맞게 draft_topic_category를 선택하세요.
선택지는 investigation(수사), sexual_offense(성범죄), traffic(교통), appeal(불복), settlement(합의), general(일반)입니다.

# 최근 작업 이력 (패턴 참고용)
{history_text}

# 오늘의 검색 데이터
{json.dumps(data, ensure_ascii=False, indent=2)}
"""

    result: KeywordAnalystOutput = call_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=KeywordAnalystOutput,
    )

    routed_results = []
    for item in result.items:
        # config.md classify 규칙: compliance_risk == high → compliance_review.queue
        target = "compliance_review.queue" if item.compliance_risk == "high" else "content_editor.inbox"

        workflow_ref = record(
            agent_id=AGENT_ID,
            output=item.model_dump(),
            routed_to=target,
            model_version=get_model_identity(),
            input_summary=item.keyword,
            db_path=db_path,
        )
        routed_results.append({"item": item, "target": target, **workflow_ref})

    return routed_results
