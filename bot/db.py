"""SQLite persistence for the platform-independent Hermes workflow."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from .config import DB_PATH


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).astimezone(dt.timezone.utc).isoformat()


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS work_items (
            work_item_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            agent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            output_json TEXT NOT NULL,
            routed_to TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'recorded', 'pending_approval', 'approved', 'rejected',
                'revision_requested', 'expired', 'cancelled',
                'acknowledgement_pending', 'acknowledged', 'halted'
            )),
            model_version TEXT,
            input_summary TEXT,
            correlation_id TEXT NOT NULL,
            parent_version INTEGER,
            PRIMARY KEY (work_item_id, version)
        );
        CREATE TABLE IF NOT EXISTS approval_requests (
            request_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            policy_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'pending', 'approved', 'rejected', 'revision_requested', 'expired', 'cancelled'
            )),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            resolved_at TEXT,
            requested_by TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            distinct_approvers INTEGER NOT NULL DEFAULT 1 CHECK (distinct_approvers IN (0, 1)),
            UNIQUE (work_item_id, work_item_version),
            FOREIGN KEY (work_item_id, work_item_version) REFERENCES work_items(work_item_id, version)
        );
        CREATE TABLE IF NOT EXISTS approval_requirements (
            requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            role_key TEXT NOT NULL,
            approvals_required INTEGER NOT NULL DEFAULT 1 CHECK (approvals_required > 0),
            UNIQUE (request_id, role_key),
            FOREIGN KEY (request_id) REFERENCES approval_requests(request_id)
        );
        CREATE TABLE IF NOT EXISTS approval_decisions (
            decision_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'revision_requested')),
            reason TEXT,
            decided_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (request_id, actor_id),
            FOREIGN KEY (request_id) REFERENCES approval_requests(request_id)
        );
        CREATE TABLE IF NOT EXISTS acknowledgement_requirements (
            requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            role_key TEXT NOT NULL,
            acknowledgements_required INTEGER NOT NULL DEFAULT 1 CHECK (acknowledgements_required > 0),
            UNIQUE (work_item_id, work_item_version, role_key),
            FOREIGN KEY (work_item_id, work_item_version) REFERENCES work_items(work_item_id, version)
        );
        CREATE TABLE IF NOT EXISTS acknowledgements (
            acknowledgement_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            actor_id TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            acknowledged_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (work_item_id, work_item_version, actor_id),
            FOREIGN KEY (work_item_id, work_item_version) REFERENCES work_items(work_item_id, version)
        );
        CREATE TABLE IF NOT EXISTS handoff_jobs (
            handoff_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            handoff_type TEXT NOT NULL,
            target TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'failed')),
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            claim_token TEXT,
            error TEXT,
            correlation_id TEXT NOT NULL,
            UNIQUE (work_item_id, work_item_version, handoff_type),
            FOREIGN KEY (work_item_id, work_item_version) REFERENCES work_items(work_item_id, version)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            model_provider TEXT,
            model_id TEXT,
            prompt_version TEXT,
            occurred_at TEXT NOT NULL,
            previous_state TEXT,
            new_state TEXT,
            reason TEXT,
            correlation_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_requests (
            execution_request_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            approval_request_id TEXT NOT NULL,
            execution_type TEXT NOT NULL,
            target TEXT NOT NULL,
            payload_summary_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            compliance_policy_version TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('disabled', 'read_only', 'dry_run')),
            status TEXT NOT NULL CHECK (status IN (
                'requested', 'blocked', 'read_only_succeeded',
                'dry_run_succeeded', 'failed', 'unknown'
            )),
            requested_by TEXT NOT NULL,
            adapter_name TEXT,
            blocked_reason TEXT,
            response_summary_json TEXT,
            correlation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (work_item_id, work_item_version)
                REFERENCES work_items(work_item_id, version)
        );
        CREATE TABLE IF NOT EXISTS execution_attempts (
            attempt_id TEXT PRIMARY KEY,
            execution_request_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'blocked', 'read_only_succeeded', 'dry_run_succeeded',
                'failed', 'unknown'
            )),
            reason TEXT,
            response_summary_json TEXT,
            correlation_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            FOREIGN KEY (execution_request_id)
                REFERENCES execution_requests(execution_request_id)
        );
        CREATE TABLE IF NOT EXISTS tracking_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            work_item_version INTEGER NOT NULL,
            output_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slack_draft_jobs (
            job_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            retry_at REAL NOT NULL DEFAULT 0,
            finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
            failure TEXT
        );
        CREATE TABLE IF NOT EXISTS slack_draft_edits (
            job_id TEXT PRIMARY KEY REFERENCES approval_requests(request_id),
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            user_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            next_request_id TEXT REFERENCES approval_requests(request_id),
            retry_at REAL NOT NULL DEFAULT 0,
            finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
            failure TEXT
        );
        CREATE TABLE IF NOT EXISTS slack_approval_demo_cards (
            request_id TEXT NOT NULL REFERENCES approval_requests(request_id),
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            PRIMARY KEY (request_id, team_id, channel_id),
            UNIQUE (team_id, channel_id, message_ts)
        );
        CREATE TABLE IF NOT EXISTS slack_test_sessions (
            session_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE,
            fixture_id TEXT NOT NULL,
            fixture_type TEXT NOT NULL CHECK (fixture_type IN (
                'keyword', 'content', 'performance', 'data'
            )),
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_ts TEXT,
            created_by TEXT NOT NULL,
            required_roles_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'pending', 'approved', 'rejected', 'revision_requested',
                'acknowledged', 'expired'
            )),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slack_interaction_events (
            interaction_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            action_type TEXT NOT NULL CHECK (action_type IN (
                'approved', 'rejected', 'revision_requested',
                'acknowledged', 'expired'
            )),
            team_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            acting_role TEXT,
            reason TEXT,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES slack_test_sessions(session_id)
        );
        CREATE TRIGGER IF NOT EXISTS approval_decisions_no_update
        BEFORE UPDATE ON approval_decisions BEGIN
            SELECT RAISE(ABORT, 'approval decisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS approval_decisions_no_delete
        BEFORE DELETE ON approval_decisions BEGIN
            SELECT RAISE(ABORT, 'approval decisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS execution_attempts_no_update
        BEFORE UPDATE ON execution_attempts BEGIN
            SELECT RAISE(ABORT, 'execution attempts are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS execution_attempts_no_delete
        BEFORE DELETE ON execution_attempts BEGIN
            SELECT RAISE(ABORT, 'execution attempts are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS slack_interaction_events_no_update
        BEFORE UPDATE ON slack_interaction_events BEGIN
            SELECT RAISE(ABORT, 'Slack interaction events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS slack_interaction_events_no_delete
        BEFORE DELETE ON slack_interaction_events BEGIN
            SELECT RAISE(ABORT, 'Slack interaction events are append-only');
        END;
        """
    )
    conn.commit()
    return conn


def recent_history(agent_id: str, limit: int = 5, db_path: str | Path | None = None) -> list[tuple]:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT created_at, output_json, routed_to, status FROM work_items WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
        (agent_id, limit),
    ).fetchall()
    conn.close()
    return [tuple(row) for row in rows]


def recent_rejections(agent_id: str, limit: int = 5, db_path: str | Path | None = None) -> list[tuple]:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT created_at, output_json, routed_to, status FROM work_items WHERE agent_id=? AND status='rejected' ORDER BY created_at DESC LIMIT ?",
        (agent_id, limit),
    ).fetchall()
    conn.close()
    return [tuple(row) for row in rows]


def get_work_item(work_item_id: str, version: int | None = None, db_path: str | Path | None = None) -> dict | None:
    conn = get_conn(db_path)
    if version is None:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_item_id=? ORDER BY version DESC LIMIT 1", (work_item_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_item_id=? AND version=?", (work_item_id, version)
        ).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result["output"] = json.loads(result.pop("output_json"))
    return result


def upsert_tracking_status(status: str, output: dict, work_item_id: str, work_item_version: int, db_path: str | Path | None = None) -> None:
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO tracking_status (id, status, updated_at, work_item_id, work_item_version, output_json)
           VALUES (1, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,
             work_item_id=excluded.work_item_id, work_item_version=excluded.work_item_version,
             output_json=excluded.output_json""",
        (status, utc_iso(), work_item_id, work_item_version, json.dumps(output, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_tracking_status(db_path: str | Path | None = None) -> dict | None:
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT status, updated_at, work_item_id, work_item_version, output_json FROM tracking_status WHERE id=1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"status": row["status"], "updated_at": row["updated_at"], "work_item_id": row["work_item_id"],
            "version": row["work_item_version"], "output": json.loads(row["output_json"])}
