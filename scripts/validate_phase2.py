#!/usr/bin/env python3
"""Validate the Phase 2 execution safety boundary."""

from __future__ import annotations

import argparse
import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.db import get_conn
from bot.execution import (
    DryRunAdapter,
    ExecutionRequestSpec,
    ExecutionSettings,
    ServiceContext,
    execute_request,
)
from bot.workflow import ActorContext, create_work_item, decide


REQUIRED_FILES = (
    "bot/execution.py",
    "bot/redaction.py",
    "docs/phase2/execution_safety.md",
    "tests/test_phase2_execution.py",
)
REQUIRED_TABLES = {"execution_requests", "execution_attempts", "audit_events"}
FORBIDDEN_NETWORK_IMPORTS = {
    "aiohttp", "boto3", "botocore", "googleapiclient", "httpx", "requests",
    "urllib3", "slack_sdk",
}
POLICY_VERSION = "phase0-implementation-baseline"


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def automated_errors() -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            errors.append(f"required file missing: {relative}")

    execution_path = ROOT / "bot/execution.py"
    if execution_path.is_file():
        forbidden = _import_roots(execution_path) & FORBIDDEN_NETWORK_IMPORTS
        if forbidden:
            errors.append(
                "Phase 2 execution boundary imports network clients: "
                + ", ".join(sorted(forbidden))
            )

    defaults = ExecutionSettings.from_env({})
    if defaults.enabled or defaults.mode != "disabled" or not defaults.kill_switch:
        errors.append("execution environment defaults are not fail-closed")
    if defaults.allowed_targets:
        errors.append("execution target allowlist must be empty by default")

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "phase2-gate.db"
        conn = get_conn(db_path)
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        conn.close()
        missing = REQUIRED_TABLES - tables
        if missing:
            errors.append(f"execution schema missing tables: {', '.join(sorted(missing))}")
        for trigger in ("execution_attempts_no_update", "execution_attempts_no_delete"):
            if trigger not in triggers:
                errors.append(f"append-only trigger missing: {trigger}")

        approved_output = {"draft": "gate preview", "flagged_claims": []}
        item = create_work_item(
            agent_id="content_editor",
            output=approved_output,
            routed_to="content_approver",
            model_version="gate-test",
            input_summary="Phase 2 gate",
            db_path=db_path,
        )
        decide(
            request_id=item["request_id"],
            expected_version=1,
            actor=ActorContext.verified_actor("gate-approver", ["content_approver"]),
            acting_role="content_approver",
            decision="approved",
            db_path=db_path,
        )
        settings = ExecutionSettings(
            app_env="test",
            enabled=True,
            mode="dry_run",
            kill_switch=False,
            allowed_targets=frozenset({"phase2-sandbox"}),
            compliance_policy_version=POLICY_VERSION,
        )
        adapter = DryRunAdapter()
        result = execute_request(
            ExecutionRequestSpec(
                work_item_id=item["work_item_id"],
                expected_version=1,
                approval_request_id=item["request_id"],
                execution_type="cms_publish",
                target="phase2-sandbox",
                payload=approved_output,
                compliance_policy_version=POLICY_VERSION,
                correlation_id=item["correlation_id"],
            ),
            ServiceContext.verified_service("phase2-gate", ["execution:request"]),
            settings,
            adapter=adapter,
            db_path=db_path,
        )
        if result.status != "dry_run_succeeded" or len(adapter.calls) != 1:
            errors.append("approved Phase 2 dry-run smoke test failed")
        elif adapter.calls[0].get("transmitted") is not False:
            errors.append("Phase 2 dry-run reported an external transmission")

    env_example = (ROOT / "bot/.env.example").read_text(encoding="utf-8")
    for contract in (
        "EXECUTION_ENABLED=false",
        "EXECUTION_MODE=disabled",
        "EXECUTION_KILL_SWITCH=true",
    ):
        if contract not in env_example:
            errors.append(f"bot/.env.example missing safe default: {contract}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="enforce the Phase 3 entry gate")
    args = parser.parse_args()
    errors = automated_errors()
    if errors:
        print(f"PHASE 2 AUTOMATED CHECK: FAIL ({len(errors)} errors)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PHASE 2 AUTOMATED CHECK: PASS")
    if args.gate:
        print("PHASE 2 ENGINEERING GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
