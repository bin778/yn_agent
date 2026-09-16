"""Load the fixed, synthetic Phase 3 Slack UI fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import BASE_DIR


FIXTURE_IDS = {
    "keyword": "ROUTE-KW-001",
    "content": "ROUTE-CE-002",
    "performance": "ROUTE-PA-003",
    "data": "ROUTE-DA-004",
}

EMPLOYEE_NAMES = {
    "keyword": "윤서진 · 키워드 전략가",
    "content": "한도윤 · 콘텐츠 에디터",
    "performance": "김도현 · 퍼포먼스 마케터",
    "data": "임지혁 · 데이터 분석가",
}


@dataclass(frozen=True)
class SlackTestFixture:
    fixture_type: str
    fixture_id: str
    agent_id: str
    employee_name: str
    summary: str
    risk_level: str
    required_roles: tuple[str, ...]
    interaction_kind: str


def _data_roles(case: dict) -> tuple[str, ...]:
    input_data = case.get("input", {})
    roles: list[str] = []
    if input_data.get("status_summary") in {"Warning", "Critical"}:
        roles.append("data_owner")
    if input_data.get("privacy_flags"):
        roles.append("privacy_officer")
    return tuple(roles)


def load_test_fixture(
    fixture_type: str,
    *,
    fixture_path: str | Path | None = None,
) -> SlackTestFixture:
    if fixture_type not in FIXTURE_IDS:
        raise ValueError(f"unsupported Slack test fixture: {fixture_type}")
    path = Path(fixture_path or BASE_DIR / "evals" / "fixtures" / "routing_cases.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw.get("dataset") != "routing_fixtures":
        raise ValueError("unexpected routing fixture dataset")
    wanted = FIXTURE_IDS[fixture_type]
    case = next((item for item in raw.get("cases", []) if item.get("id") == wanted), None)
    if case is None:
        raise ValueError(f"required Slack fixture is missing: {wanted}")
    if case.get("provenance") != "synthetic" or case.get("contains_personal_data") is not False:
        raise ValueError(f"Slack fixture must be synthetic and non-personal: {wanted}")

    if fixture_type == "data":
        roles = _data_roles(case)
        kind = "acknowledgement"
    else:
        roles = tuple(case.get("expected", {}).get("requires_approval", ()))
        kind = "approval"
    if not roles:
        raise ValueError(f"Slack fixture has no test roles: {wanted}")
    return SlackTestFixture(
        fixture_type=fixture_type,
        fixture_id=wanted,
        agent_id=case["agent_id"],
        employee_name=EMPLOYEE_NAMES[fixture_type],
        summary=case["purpose"],
        risk_level="high" if len(roles) > 1 else "standard",
        required_roles=roles,
        interaction_kind=kind,
    )
