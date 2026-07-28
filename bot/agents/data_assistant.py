"""
오지민 (data_assistant) 작업 실행 로직.
routine.yaml의 task: run_anomaly_check / steps 를 코드로 옮긴 것.

steps 매핑:
  fetch_data  → get_fake_tracking_data() (또는 실제 GA4/GTM)
  mask_pii    → _mask_pii() — 원문 제거, privacy_flags 후보 기록
  analyze     → call_gemini_structured()
  classify    → Critical → broadcast + dev_team
                privacy_flags → dev_team
                항상 tracking_status 갱신
  record      → db.record_run() + upsert_tracking_status()
"""
import copy
import json

from ..loaders import build_system_prompt
from ..fake_data import get_fake_tracking_data
from ..llm_client import call_gemini_structured
from ..schemas import DataAssistantOutput
from ..db import record_run, recent_history, upsert_tracking_status
from ..config import GEMINI_MODEL, USE_FAKE_DATA

AGENT_ID = "data_assistant"


def _format_anomaly_history() -> str:
    rows = recent_history(AGENT_ID, limit=5)
    if not rows:
        return "(최근 이상탐지 이력 없음)"
    lines = []
    for created_at, output_json, routed_to, _human in rows:
        try:
            output = json.loads(output_json)
            summary = (
                f"status={output.get('status_summary')} | "
                f"anomalies={output.get('tracking_anomalies', [])[:2]}"
            )
        except (json.JSONDecodeError, AttributeError):
            summary = output_json[:120]
        lines.append(f"- {created_at[:19]} | {summary} | routed_to={routed_to}")
    return "\n".join(lines)


def _mask_pii(raw: dict) -> tuple[dict, list[str]]:
    """
    식별 가능 필드를 제거하고 privacy_flags 후보를 만든다.
    LLM 입력/출력에 원문이 남지 않게 한다.
    """
    data = copy.deepcopy(raw)
    flags: list[str] = []

    raw_fields = data.pop("raw_fields_present", []) or []
    unmasked = data.pop("sample_rows_UNMASKED_DO_NOT_OUTPUT", None)
    if unmasked or any(f in ("client_ip", "device_id", "user_agent", "ip") for f in raw_fields):
        if "client_ip" in raw_fields or (unmasked and any("client_ip" in r for r in unmasked)):
            flags.append("원본 IP 미마스킹")
        if "device_id" in raw_fields or (unmasked and any("device_id" in r for r in unmasked)):
            flags.append("원본 기기ID 미마스킹")
        if "user_agent" in raw_fields:
            flags.append("원본 User-Agent 미마스킹")
        data["pii_note"] = (
            "입력에 식별 가능 필드가 포함되어 있었으나 집계 단위로만 전달합니다. "
            "원문은 폐기되었습니다."
        )

    return data, flags


def _classify_targets(result: DataAssistantOutput) -> list[str]:
    targets = ["data_assistant.tracking_status"]
    if result.status_summary == "Critical":
        targets.extend(["broadcast.all_agents", "dev_team.queue"])
    if result.privacy_flags and "dev_team.queue" not in targets:
        targets.append("dev_team.queue")
    # 순서 유지하며 중복 제거
    seen = set()
    ordered = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def run_anomaly_check(scenario: str = "critical") -> dict:
    """
    routine.yaml continuous_tracking_check / 슬래시커맨드가 실행하는 작업.
    반환: {
      "item": DataAssistantOutput,
      "targets": [str, ...],
      "record_id": int,
      "scenario": str,
    }
    """
    if not USE_FAKE_DATA:
        # 실제 API 연동 전까지는 fake와 동일 경로 (자리표시)
        raw = get_fake_tracking_data(scenario)
    else:
        raw = get_fake_tracking_data(scenario)

    masked, pre_flags = _mask_pii(raw)
    system_prompt = build_system_prompt(AGENT_ID)
    history_text = _format_anomaly_history()

    user_prompt = f"""아래는 GA4/GTM 트래킹 스냅샷입니다 (이미 PII 마스킹 처리됨).
SOUL.md 원칙에 따라 status_summary를 판정하고 JSON으로 응답하세요.
개인 식별 정보 원문(IP, 기기ID, User-Agent)은 절대 출력에 넣지 마세요.
항상 집계/캠페인/페이지 단위로만 서술하고, action_items에 실행 가능한 다음 조치를 넣으세요.

코드 전처리에서 감지한 privacy 신호(참고, 해당 시 privacy_flags에 반영):
{json.dumps(pre_flags, ensure_ascii=False)}

# 최근 이상탐지 이력
{history_text}

# 오늘의 트래킹 스냅샷
{json.dumps(masked, ensure_ascii=False, indent=2)}
"""

    result: DataAssistantOutput = call_gemini_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=DataAssistantOutput,
    )

    # 전처리에서 잡은 privacy_flags는 모델이 빠뜨려도 병합
    merged_flags = list(dict.fromkeys([*result.privacy_flags, *pre_flags]))
    if merged_flags != result.privacy_flags:
        result = result.model_copy(update={"privacy_flags": merged_flags})

    targets = _classify_targets(result)
    routed_to = ",".join(targets)

    record_id = record_run(
        agent_id=AGENT_ID,
        output=result.model_dump(),
        routed_to=routed_to,
        model_version=GEMINI_MODEL,
        input_summary=f"scenario={scenario}|status={result.status_summary}",
    )
    upsert_tracking_status(result.status_summary, result.model_dump(), record_id)

    return {
        "item": result,
        "targets": targets,
        "record_id": record_id,
        "scenario": scenario,
    }
