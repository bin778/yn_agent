"""Verify the CMS dry-run boundary against a queued content approval on SQLite copies."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from bot.execution import (
    DryRunAdapter,
    ExecutionRequestSpec,
    ExecutionSettings,
    ServiceContext,
    execute_request,
)
from bot.workflow import create_work_item

TARGET = "hermes-cms-sandbox"
POLICY_VERSION = "phase0-implementation-baseline"
SERVICE = ServiceContext.verified_service("verify-cms-dry-run", ["execution:request"])


class VerificationError(RuntimeError):
    """The selected queue item or a verification result does not match expectations."""


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _copy_database(source: Path, destination: Path) -> None:
    with closing(_open_read_only(source)) as origin, closing(sqlite3.connect(destination)) as copy:
        origin.backup(copy)


def _queued_request(db_path: Path, approval_request_id: str) -> ExecutionRequestSpec:
    with closing(_open_read_only(db_path)) as conn:
        row = conn.execute(
            """SELECT w.work_item_id, w.version, w.correlation_id, w.output_json
               FROM approval_requests a
               JOIN work_items w ON w.work_item_id=a.work_item_id AND w.version=a.work_item_version
               JOIN handoff_jobs h ON h.work_item_id=w.work_item_id AND h.work_item_version=w.version
               WHERE a.request_id=? AND a.status='approved' AND w.status='approved'
                 AND w.agent_id='content_editor' AND h.handoff_type='queue_for_publish'
                 AND h.target='publish_queue' AND h.status='pending'""",
            (approval_request_id,),
        ).fetchone()
    if row is None:
        raise VerificationError("The approval ID must identify approved content with a pending publish handoff.")
    work_item_id, version, correlation_id, output_json = row
    return ExecutionRequestSpec(
        work_item_id=work_item_id,
        expected_version=version,
        approval_request_id=approval_request_id,
        execution_type="cms_publish",
        target=TARGET,
        payload=json.loads(output_json),
        compliance_policy_version=POLICY_VERSION,
        correlation_id=correlation_id,
    )


def verify_publish_dry_run(source: Path, approval_request_id: str) -> dict:
    settings = ExecutionSettings(
        app_env="test",
        enabled=True,
        mode="dry_run",
        kill_switch=False,
        allowed_targets=frozenset({TARGET}),
        compliance_policy_version=POLICY_VERSION,
    )

    with tempfile.TemporaryDirectory(prefix="hermes-publish-dry-run-") as directory:
        root = Path(directory)
        baseline = root / "baseline.db"
        _copy_database(source, baseline)
        spec = _queued_request(baseline, approval_request_id)

        positive_db = root / "positive.db"
        _copy_database(baseline, positive_db)
        adapter = DryRunAdapter()
        positive = execute_request(spec, SERVICE, settings, adapter=adapter, db_path=positive_db)
        if positive.status != "dry_run_succeeded" or positive.duplicate:
            raise VerificationError(f"Expected a fresh dry-run success, got {positive.status} ({positive.reason}).")
        duplicate = execute_request(spec, SERVICE, settings, adapter=adapter, db_path=positive_db)
        with closing(_open_read_only(positive_db)) as conn:
            attempts = conn.execute(
                "SELECT status FROM execution_attempts WHERE execution_request_id=?",
                (positive.execution_request_id,),
            ).fetchall()
        if (
            not duplicate.duplicate or duplicate.execution_request_id != positive.execution_request_id
            or duplicate.status != positive.status or attempts != [("dry_run_succeeded",)]
            or len(adapter.calls) != 1 or adapter.calls[0].get("transmitted") is not False
        ):
            raise VerificationError("Expected one recorded dry-run attempt and no additional call on replay.")

        # Each block case uses the pre-execution snapshot, so deduplication
        # cannot return the earlier successful result instead of checking guards.
        stale_db = root / "stale.db"
        _copy_database(baseline, stale_db)
        create_work_item(
            agent_id="content_editor",
            output=dict(spec.payload),
            routed_to="publish_queue",
            model_version=None,
            input_summary="Newer version for dry-run verification",
            work_item_id=spec.work_item_id,
            version=spec.expected_version + 1,
            parent_version=spec.expected_version,
            db_path=stale_db,
        )

        tampered_db = root / "tampered.db"
        _copy_database(baseline, tampered_db)
        changed_output = {**spec.payload, "body": "Dry-run verification value changed after approval."}
        kill_switch_db = root / "kill-switch.db"
        _copy_database(baseline, kill_switch_db)
        blocked_reasons = []
        for db_path, request, config, expected_reason in (
            (stale_db, spec, settings, "stale_work_item_version"),
            (tampered_db, replace(spec, payload=changed_output), settings, "payload_not_approved"),
            (kill_switch_db, spec, replace(settings, kill_switch=True), "kill_switch_active"),
        ):
            blocker_adapter = DryRunAdapter()
            blocked = execute_request(request, SERVICE, config, adapter=blocker_adapter, db_path=db_path)
            if (
                blocked.status != "blocked" or blocked.reason != expected_reason
                or blocked.duplicate or blocker_adapter.calls
            ):
                raise VerificationError(f"Expected {expected_reason} without an adapter call.")
            blocked_reasons.append(blocked.reason)

        return {
            "approval_request_id": approval_request_id,
            "work_item_version": spec.expected_version,
            "target": TARGET,
            "status": positive.status,
            "transmitted": False,
            "execution_attempts": len(attempts),
            "duplicate_reused_result": duplicate.duplicate,
            "blocked_without_adapter": blocked_reasons,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="source SQLite DB, opened read-only")
    parser.add_argument("--approval-request-id", required=True, help="approved content request ID")
    args = parser.parse_args()
    try:
        report = verify_publish_dry_run(args.db.expanduser(), args.approval_request_id)
    except (VerificationError, sqlite3.Error, OSError, ValueError) as exc:
        parser.exit(1, f"Publish dry-run verification failed: {exc}\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
