"""
한도윤 (content_editor) 작업 실행 로직.
routine.yaml의 task: draft_content / steps 를 코드로 옮긴 것.

steps 매핑:
  fetch_keyword → content_editor.inbox(승인된 키워드) / record_id 조회
  draft         → call_gemini_structured()
  self_flag     → LLM이 flagged_claims / sensitive_flags 채움
  classify      → flagged 또는 sensitive → human_review.queue
                  else                 → publish_queue
  record        → db.record_run()
"""
import json

from ..loaders import build_system_prompt
from ..llm_client import call_gemini_structured
from ..schemas import ContentEditorOutput, KeywordItem
from ..db import record_run, recent_rejections, get_record, list_approved_keywords_awaiting_draft
from ..config import GEMINI_MODEL

AGENT_ID = "content_editor"


def _format_rejections() -> str:
    rows = recent_rejections(AGENT_ID, limit=5)
    if not rows:
        return "(최근 반려 이력 없음)"
    lines = []
    for created_at, output_json, routed_to, human_decision in rows:
        try:
            output = json.loads(output_json)
            summary = f"{output.get('title', '?')} | flagged={output.get('flagged_claims', [])}"
        except (json.JSONDecodeError, AttributeError):
            summary = output_json[:120]
        lines.append(f"- {created_at[:19]} | {summary} | routed_to={routed_to}")
    return "\n".join(lines)


def _keyword_payload_from_item(item: KeywordItem | dict) -> dict:
    if isinstance(item, KeywordItem):
        return item.model_dump()
    return item


def draft_content(keyword_item: KeywordItem | dict) -> dict:
    """
    키워드 1건으로 초안을 작성하고 라우팅한다.
    반환: {"item": ContentEditorOutput, "target": str, "record_id": int}
    """
    payload = _keyword_payload_from_item(keyword_item)
    system_prompt = build_system_prompt(AGENT_ID)
    rejections_text = _format_rejections()

    user_prompt = f"""아래는 서연우가 승인한 키워드 후보입니다.
SOUL.md의 판단 원칙과 금지 표현 규칙에 따라 법률 콘텐츠 초안을 작성하고,
자기점검 결과(flagged_claims, sensitive_flags)를 빠짐없이 JSON으로 응답하세요.

# 최근 반려된 콘텐츠 (같은 실수를 반복하지 말 것)
{rejections_text}

# 승인된 키워드 입력
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

    result: ContentEditorOutput = call_gemini_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=ContentEditorOutput,
    )

    # config.md / routine.yaml classify: flagged 또는 sensitive → human_review, else publish
    if result.flagged_claims or result.sensitive_flags:
        target = "human_review.queue"
    else:
        target = "publish_queue"

    record_id = record_run(
        agent_id=AGENT_ID,
        output=result.model_dump(),
        routed_to=target,
        model_version=GEMINI_MODEL,
        input_summary=result.target_keyword,
    )
    return {"item": result, "target": target, "record_id": record_id}


def draft_content_from_record(keyword_record_id: int) -> dict:
    """서연우 agent_memory record_id로부터 초안 작성."""
    record = get_record(keyword_record_id)
    if not record:
        raise ValueError(f"record #{keyword_record_id}를 찾을 수 없습니다.")
    if record["agent_id"] != "keyword_analyst":
        raise ValueError(f"record #{keyword_record_id}는 keyword_analyst 결과가 아닙니다.")
    if record["routed_to"] != "content_editor.inbox":
        raise ValueError(
            f"record #{keyword_record_id}는 content_editor.inbox 대상이 아닙니다 "
            f"(routed_to={record['routed_to']})."
        )
    return draft_content(record["output"])


def process_backlog(limit: int = 3) -> list[dict]:
    """routine.yaml backlog_sweep: 승인됐지만 초안 없는 키워드를 처리."""
    pending = list_approved_keywords_awaiting_draft(limit=limit)
    results = []
    for row in pending:
        results.append(draft_content(row["output"]))
    return results
