"""SQLite-backed Phase 3 Slack test sessions.

The service never creates Phase 1 approvals, handoffs, Codex calls, or external
execution requests. Its tables are an intentionally disposable UI sandbox.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from ..db import get_conn, utc_iso, utc_now
from .fixtures import SlackTestFixture


class SlackTestError(RuntimeError):
    pass


class SlackTestAuthorizationError(SlackTestError):
    pass


class SlackTestStaleActionError(SlackTestError):
    pass


class SlackTestValidationError(SlackTestError):
    pass


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


class SlackTestService:
    def __init__(self, db_path: str | Path, *, session_ttl_minutes: int = 15):
        if session_ttl_minutes <= 0:
            raise ValueError("session_ttl_minutes must be positive")
        self.db_path = Path(db_path)
        self.session_ttl = dt.timedelta(minutes=session_ttl_minutes)

    def _session_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        session = dict(row)
        session["required_roles"] = json.loads(session.pop("required_roles_json"))
        events = [
            dict(item)
            for item in conn.execute(
                """SELECT interaction_id, action_type, user_id, acting_role, reason, occurred_at
                   FROM slack_interaction_events
                   WHERE session_id=? ORDER BY occurred_at, interaction_id""",
                (session["session_id"],),
            ).fetchall()
        ]
        success_action = (
            "acknowledged" if session["fixture_type"] == "data" else "approved"
        )
        completed_roles = [
            event["acting_role"]
            for event in events
            if event["action_type"] == success_action and event["acting_role"]
        ]
        session["events"] = events
        session["completed_roles"] = completed_roles
        session["remaining_roles"] = [
            role for role in session["required_roles"] if role not in completed_roles
        ]
        return session

    def get_session(self, session_id: str) -> dict:
        conn = get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise SlackTestValidationError("Slack test session was not found")
            return self._session_dict(conn, row)
        finally:
            conn.close()

    def create_session(
        self,
        *,
        command_id: str,
        fixture: SlackTestFixture,
        team_id: str,
        channel_id: str,
        created_by: str,
        now: dt.datetime | None = None,
    ) -> dict:
        if not command_id:
            raise SlackTestValidationError("command id is required")
        current = now or utc_now()
        conn = get_conn(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE command_id=?", (command_id,)
            ).fetchone()
            if existing is not None:
                result = self._session_dict(conn, existing)
                result["duplicate"] = True
                conn.rollback()
                return result
            session_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO slack_test_sessions (
                       session_id, command_id, fixture_id, fixture_type,
                       team_id, channel_id, created_by, required_roles_json,
                       state, created_at, expires_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    session_id,
                    command_id,
                    fixture.fixture_id,
                    fixture.fixture_type,
                    team_id,
                    channel_id,
                    created_by,
                    json.dumps(fixture.required_roles),
                    utc_iso(current),
                    utc_iso(current + self.session_ttl),
                    utc_iso(current),
                ),
            )
            row = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            result = self._session_dict(conn, row)
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def bind_message(self, session_id: str, *, message_ts: str) -> dict:
        if not message_ts:
            raise SlackTestValidationError("Slack message timestamp is required")
        conn = get_conn(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise SlackTestValidationError("Slack test session was not found")
            if row["message_ts"] not in {None, message_ts}:
                raise SlackTestStaleActionError("Slack test session is bound to another message")
            conn.execute(
                "UPDATE slack_test_sessions SET message_ts=?, updated_at=? WHERE session_id=?",
                (message_ts, utc_iso(), session_id),
            )
            updated = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            result = self._session_dict(conn, updated)
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def validate_action_target(
        self,
        *,
        session_id: str,
        team_id: str,
        channel_id: str,
        message_ts: str,
        now: dt.datetime | None = None,
    ) -> dict:
        """Reject stale Slack UI targets before opening a modal."""
        current = now or utc_now()
        conn = get_conn(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise SlackTestValidationError("Slack test session was not found")
            if row["team_id"] != team_id:
                raise SlackTestStaleActionError("Slack workspace does not match the session")
            if row["channel_id"] != channel_id:
                raise SlackTestStaleActionError("Slack channel does not match the session")
            if not row["message_ts"] or row["message_ts"] != message_ts:
                raise SlackTestStaleActionError("Slack message does not match the session")
            if row["state"] != "pending":
                raise SlackTestStaleActionError(
                    f"Slack test session is already {row['state']}"
                )
            if current >= _parse_time(row["expires_at"]):
                conn.execute(
                    "UPDATE slack_test_sessions SET state='expired', updated_at=? WHERE session_id=?",
                    (utc_iso(current), session_id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO slack_interaction_events (
                           interaction_id, session_id, action_type, team_id,
                           user_id, acting_role, reason, occurred_at
                       ) VALUES (?, ?, 'expired', ?, 'system', NULL, ?, ?)""",
                    (
                        f"expiry:{session_id}",
                        session_id,
                        team_id,
                        "Phase 3 test session TTL elapsed",
                        utc_iso(current),
                    ),
                )
                conn.commit()
                raise SlackTestStaleActionError("Slack test session has expired")
            result = self._session_dict(conn, row)
            conn.rollback()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def apply_action(
        self,
        *,
        interaction_id: str,
        session_id: str,
        action_type: str,
        team_id: str,
        channel_id: str,
        message_ts: str,
        user_id: str,
        actor_roles: Iterable[str],
        acting_role: str,
        reason: str | None = None,
        now: dt.datetime | None = None,
    ) -> dict:
        if action_type not in {
            "approved",
            "rejected",
            "revision_requested",
            "acknowledged",
        }:
            raise SlackTestValidationError("unsupported Slack test action")
        if action_type in {"rejected", "revision_requested"} and not (reason or "").strip():
            raise SlackTestValidationError("reason is required")
        current = now or utc_now()
        conn = get_conn(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT session_id FROM slack_interaction_events WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["session_id"] != session_id:
                    raise SlackTestStaleActionError("interaction id belongs to another session")
                row = conn.execute(
                    "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                result = self._session_dict(conn, row)
                result["duplicate"] = True
                conn.rollback()
                return result

            row = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise SlackTestValidationError("Slack test session was not found")
            if row["team_id"] != team_id:
                raise SlackTestStaleActionError("Slack workspace does not match the session")
            if row["channel_id"] != channel_id:
                raise SlackTestStaleActionError("Slack channel does not match the session")
            if not row["message_ts"] or row["message_ts"] != message_ts:
                raise SlackTestStaleActionError("Slack message does not match the session")
            if row["state"] != "pending":
                raise SlackTestStaleActionError(
                    f"Slack test session is already {row['state']}"
                )
            if current >= _parse_time(row["expires_at"]):
                conn.execute(
                    "UPDATE slack_test_sessions SET state='expired', updated_at=? WHERE session_id=?",
                    (utc_iso(current), session_id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO slack_interaction_events (
                           interaction_id, session_id, action_type, team_id,
                           user_id, acting_role, reason, occurred_at
                       ) VALUES (?, ?, 'expired', ?, 'system', NULL, ?, ?)""",
                    (
                        f"expiry:{session_id}",
                        session_id,
                        team_id,
                        "Phase 3 test session TTL elapsed",
                        utc_iso(current),
                    ),
                )
                conn.commit()
                raise SlackTestStaleActionError("Slack test session has expired")

            required_roles = tuple(json.loads(row["required_roles_json"]))
            verified_roles = frozenset(actor_roles)
            if acting_role not in verified_roles:
                raise SlackTestAuthorizationError("acting role is not mapped to this Slack user")
            if acting_role not in required_roles:
                raise SlackTestAuthorizationError("acting role is not required by this test session")
            data_session = row["fixture_type"] == "data"
            if data_session and action_type != "acknowledged":
                raise SlackTestValidationError("data test sessions only accept acknowledgement")
            if not data_session and action_type == "acknowledged":
                raise SlackTestValidationError("approval test sessions do not accept acknowledgement")

            previous = conn.execute(
                """SELECT 1 FROM slack_interaction_events
                   WHERE session_id=? AND user_id=? AND action_type IN ('approved', 'acknowledged')""",
                (session_id, user_id),
            ).fetchone()
            if previous is not None:
                raise SlackTestAuthorizationError(
                    "one Slack user cannot satisfy more than one required role"
                )
            if action_type in {"approved", "acknowledged"}:
                already_covered = conn.execute(
                    """SELECT 1 FROM slack_interaction_events
                       WHERE session_id=? AND acting_role=? AND action_type=?""",
                    (session_id, acting_role, action_type),
                ).fetchone()
                if already_covered is not None:
                    raise SlackTestStaleActionError(
                        "required role has already been completed by another user"
                    )

            conn.execute(
                """INSERT INTO slack_interaction_events (
                       interaction_id, session_id, action_type, team_id,
                       user_id, acting_role, reason, occurred_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction_id,
                    session_id,
                    action_type,
                    team_id,
                    user_id,
                    acting_role,
                    (reason or "").strip() or None,
                    utc_iso(current),
                ),
            )

            new_state = "pending"
            if action_type in {"rejected", "revision_requested"}:
                new_state = action_type
            else:
                success_action = "acknowledged" if data_session else "approved"
                covered = {
                    event[0]
                    for event in conn.execute(
                        """SELECT acting_role FROM slack_interaction_events
                           WHERE session_id=? AND action_type=?""",
                        (session_id, success_action),
                    ).fetchall()
                }
                if set(required_roles).issubset(covered):
                    new_state = success_action
            conn.execute(
                "UPDATE slack_test_sessions SET state=?, updated_at=? WHERE session_id=?",
                (new_state, utc_iso(current), session_id),
            )
            updated = conn.execute(
                "SELECT * FROM slack_test_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            result = self._session_dict(conn, updated)
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
