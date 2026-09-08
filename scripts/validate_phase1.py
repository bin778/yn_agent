#!/usr/bin/env python3
"""Validate the Phase 1 approval workflow and its platform boundary."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.db import get_conn
from bot.workflow import policy_for

REQUIRED_FILES = (
    "bot/workflow.py",
    "bot/db.py",
    "docs/phase1/approval_workflow.md",
    "tests/test_phase1_workflow.py",
)
REMOVED_FILES = ("bot/main.py", "bot/discord_ui.py", "bot/SETUP.md")
REQUIRED_TABLES = {
    "work_items", "approval_requests", "approval_requirements", "approval_decisions",
    "acknowledgement_requirements", "acknowledgements", "handoff_jobs", "audit_events",
    "tracking_status",
}


def automated_errors() -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            errors.append(f"required file missing: {relative}")
    for relative in REMOVED_FILES:
        if (ROOT / relative).exists():
            errors.append(f"retired platform file still exists: {relative}")

    requirements = (ROOT / "bot/requirements.txt").read_text(encoding="utf-8").lower()
    if "discord" in requirements or "apscheduler" in requirements:
        errors.append("bot/requirements.txt: retired runtime dependency remains")
    for path in (ROOT / "bot").rglob("*.py"):
        if "discord" in path.read_text(encoding="utf-8").lower():
            errors.append(f"active platform dependency remains: {path.relative_to(ROOT)}")

    for agent in ("keyword_analyst", "content_editor", "performance_analyst", "data_assistant"):
        path = ROOT / agent / "routine.yaml"
        try:
            routine = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
            continue
        if routine.get("interface", {}).get("type") != "platform_adapter_pending":
            errors.append(f"{path.relative_to(ROOT)}: interface must be platform_adapter_pending")

    with tempfile.TemporaryDirectory() as temp_dir:
        conn = get_conn(Path(temp_dir) / "gate.db")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    missing_tables = REQUIRED_TABLES - tables
    if missing_tables:
        errors.append(f"workflow schema missing tables: {', '.join(sorted(missing_tables))}")

    matrix = {
        "keyword": policy_for("keyword_analyst", {"compliance_risk": "high"}).roles,
        "content": policy_for("content_editor", {"flagged_claims": ["x"]}).roles,
        "performance": policy_for(
            "performance_analyst",
            {"action": "PAUSE", "adjustment_percentage": 0, "sensitive_keyword": True},
        ).roles,
    }
    expected = {
        "keyword": ("keyword_approver", "legal_compliance"),
        "content": ("content_approver", "legal_compliance"),
        "performance": ("performance_approver", "operations_owner", "legal_compliance"),
    }
    if matrix != expected:
        errors.append("approval policy matrix does not match Phase 1 contract")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="enforce the Phase 2 entry gate")
    args = parser.parse_args()
    errors = automated_errors()
    if errors:
        print(f"PHASE 1 AUTOMATED CHECK: FAIL ({len(errors)} errors)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PHASE 1 AUTOMATED CHECK: PASS")
    if args.gate:
        print("PHASE 1 ENGINEERING GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
