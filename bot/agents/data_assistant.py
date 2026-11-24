"""
임지혁 (data_assistant) 작업 실행 로직.
routine.yaml의 task: run_anomaly_check / steps 를 코드로 옮긴 것.

steps 매핑:
  fetch_data  → get_fake_tracking_data() (또는 실제 GA4/GTM)
  mask_pii    → _mask_pii() — 원문 제거, privacy_flags 후보 기록
  analyze     → call_structured()
  classify    → Critical → broadcast + dev_team
                privacy_flags → dev_team
                항상 tracking_status 갱신
  record      → workflow.create_work_item() + upsert_tracking_status()
"""
import copy
import datetime as dt
import json

from ..loaders import build_system_prompt
from ..fake_data import get_fake_tracking_data
from ..llm_client import call_structured, get_model_identity
from ..schemas import DataAssistantOutput
from ..db import recent_history, upsert_tracking_status, utc_now
from ..workflow import create_work_item
from ..config import USE_FAKE_DATA

AGENT_ID = "data_assistant"
MAX_TRACKING_AGE_HOURS = 3
REQUIRED_TRACKING_COUNTS = {
    "sessions_today": "세션",
    "click_kakaotalk": "카카오톡 클릭",
    "submit_form": "폼 제출",
}


def _source_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = dt.datetime.fromisoformat(value)
        return timestamp.astimezone(dt.timezone.utc) if timestamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _tracking_snapshot(raw: dict) -> dict:
    ga4 = raw.get("ga4")
    ga4 = ga4 if isinstance(ga4, dict) else {}
    events = ga4.get("events")
    events = events if isinstance(events, dict) else {}
    counts = {"sessions_today": ga4.get("sessions_today")}
    for name in ("click_kakaotalk", "submit_form"):
        event = events.get(name)
        counts[name] = event.get("today") if isinstance(event, dict) else None
    timestamp = _source_time(raw.get("collected_through"))
    return {
        "collected_through": timestamp.isoformat() if timestamp else None,
        "collection_complete": raw.get("collection_complete") is True,
        "counts": {name: value if type(value) is int and value >= 0 else None for name, value in counts.items()},
    }


def tracking_input_issue(snapshot: object) -> tuple[str, str] | None:
    """Check reported source coverage again at the time of the advertising decision."""
    if not isinstance(snapshot, dict) or not snapshot:
        return ("트래킹 수집 정보가 없습니다.", "새 브리핑으로 트래킹을 다시 점검해 주세요.")
    timestamp = _source_time(snapshot.get("collected_through"))
    now = utc_now()
    if timestamp is None or timestamp > now:
        return ("트래킹 수집 기준 시각을 확인할 수 없습니다.", "소스의 수집 시각을 확인해 주세요.")
    if now - timestamp > dt.timedelta(hours=MAX_TRACKING_AGE_HOURS):
        return (
            f"트래킹 수집 지연 · 마지막 수집 기준 시각이 {MAX_TRACKING_AGE_HOURS}시간을 초과했습니다.",
            "집계가 갱신되면 새 브리핑을 실행해 주세요.",
        )
    if snapshot.get("collection_complete") is not True:
        return ("트래킹 집계가 완료되지 않았습니다.", "집계 완료 후 새 브리핑을 실행해 주세요.")
    counts = snapshot.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    missing = [
        label for name, label in REQUIRED_TRACKING_COUNTS.items()
        if type(counts.get(name)) is not int or counts[name] < 0
    ]
    if missing:
        return (f"트래킹 필수 지표 누락 또는 오류 · {', '.join(missing)}.", "누락된 지표의 수집 상태를 확인해 주세요.")
    return None


def _format_anomaly_history(db_path=None) -> str:
    rows = recent_history(AGENT_ID, limit=5, db_path=db_path)
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


def run_anomaly_check(scenario: str = "critical", *, db_path=None, record_work_item=None) -> dict:
    """
    routine.yaml continuous_tracking_check / 슬래시커맨드가 실행하는 작업.
    반환: {
      "item": DataAssistantOutput,
      "targets": [str, ...],
      "work_item_id": str, "version": int,
      "scenario": str,
    }
    """
    if not USE_FAKE_DATA:
        # 실제 API 연동 전까지는 fake와 동일 경로 (자리표시)
        raw = get_fake_tracking_data(scenario)
    else:
        raw = get_fake_tracking_data(scenario)

    record = record_work_item or create_work_item
    masked, pre_flags = _mask_pii(raw)
    tracking_snapshot = _tracking_snapshot(masked)
    issue = tracking_input_issue(tracking_snapshot)
    if issue:
        reason, next_action = issue
        result = DataAssistantOutput(status_summary="Critical", reason=reason, action_items=[next_action])
        model_version = "policy-engine/no-llm"
    else:
        result = _analyze_tracking(masked, pre_flags, db_path)
        model_version = get_model_identity()

    # 전처리에서 잡은 privacy_flags는 모델이 빠뜨려도 병합
    merged_flags = list(dict.fromkeys([*result.privacy_flags, *pre_flags]))
    if merged_flags != result.privacy_flags:
        result = result.model_copy(update={"privacy_flags": merged_flags})

    targets = _classify_targets(result)
    output = {**result.model_dump(), "tracking_input": tracking_snapshot}
    workflow_ref = record(
        agent_id=AGENT_ID,
        output=output,
        routed_to=",".join(targets),
        model_version=model_version,
        input_summary=f"scenario={scenario}|status={result.status_summary}",
        db_path=db_path,
    )
    if record_work_item is None:
        upsert_tracking_status(
            result.status_summary, output, workflow_ref["work_item_id"], workflow_ref["version"], db_path=db_path,
        )

    return {"item": result, "targets": targets, **workflow_ref, "scenario": scenario}


def _analyze_tracking(masked: dict, pre_flags: list[str], db_path) -> DataAssistantOutput:
    system_prompt = build_system_prompt(AGENT_ID)
    history_text = _format_anomaly_history(db_path)

    user_prompt = f"""아래는 GA4/GTM 트래킹 스냅샷입니다 (이미 PII 마스킹 처리됨).
SOUL.md 원칙에 따라 status_summary를 판정하고 JSON으로 응답하세요.
개인 식별 정보 원문(IP, 기기ID, User-Agent)은 절대 출력에 넣지 마세요.
항상 집계/캠페인/페이지 단위로만 서술하고, action_items에 실행 가능한 다음 조치를 넣으세요.
집계가 완료된 숫자 0은 유효한 관측치입니다. 오류나 비정상 급락의 근거 없이 0을 누락이나 Critical로 분류하지 마세요.

코드 전처리에서 감지한 privacy 신호(참고, 해당 시 privacy_flags에 반영):
{json.dumps(pre_flags, ensure_ascii=False)}

# 최근 이상탐지 이력
{history_text}

# 오늘의 트래킹 스냅샷
{json.dumps(masked, ensure_ascii=False, indent=2)}
"""

    return call_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=DataAssistantOutput,
    )
