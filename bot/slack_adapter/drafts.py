"""Accept Slack draft requests and deliver their approval cards from SQLite."""

from __future__ import annotations

import fcntl
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from threading import Event

from .. import workflow
from ..agents.content_editor import draft_from_topic
from ..db import get_conn, get_work_item
from ..slack_approval_demo import CONTENT_COMMAND, SlackApprovalCards
from .config import SlackConfigurationError, SlackMapping

COMMAND = "/hermes-draft"
LOGGER = logging.getLogger(__name__)
REQUEST_ROLES = {"keyword_approver", "content_approver", "workflow_admin"}


class SlackDrafts(SlackApprovalCards):
    actions = {
        "hermes_draft_approve": ("콘텐츠 승인", "approved"),
        "hermes_draft_reject": ("반려", "rejected"),
        "hermes_draft_revision": ("수정 요청", "revision_requested"),
    }
    reason_modal = "hermes_draft_reason"

    def __init__(self, mapping: SlackMapping, db_path: Path, channel_id: str):
        if not re.fullmatch(r"[CG][A-Z0-9]+", channel_id):
            raise SlackConfigurationError("SLACK_DRAFT_CHANNEL_ID must be a Slack channel ID")
        super().__init__(mapping, db_path)
        self.workspace_id = mapping.workspace_id
        self.channel_id = channel_id

    def _actor(self, team_id: str, channel_id: str, user_id: str) -> workflow.ActorContext:
        if channel_id != self.channel_id:
            raise SlackConfigurationError("draft request is outside the configured channel")
        actor = self.identity.actor_for(team_id, user_id)
        return workflow.ActorContext.verified_actor(actor.actor_id, actor.roles, platform="slack")

    def _state(self, request_id: str) -> dict:
        state = workflow.get_approval_state(request_id, self.db_path)
        with closing(get_conn(self.db_path)) as conn:
            job = conn.execute(
                """SELECT 1 FROM slack_draft_jobs
                   WHERE job_id=? AND team_id=? AND channel_id=? AND failure IS NULL""",
                (state["work_item_id"], self.workspace_id, self.channel_id),
            ).fetchone()
        work = get_work_item(state["work_item_id"], state["version"], self.db_path)
        if job is None or work is None or work["agent_id"] != "content_editor":
            raise workflow.NotFoundError("not a Slack draft approval request")
        remaining = [
            entry["role"] for entry in state["requirements"]
            if entry["approved"] < entry["required"]
        ]
        roles = remaining if state["status"] == "pending" else [
            entry["role"] for entry in state["requirements"]
        ]
        return {
            **state,
            "command": CONTENT_COMMAND,
            "role": ", ".join(roles),
            "card_title": "Hermes · 콘텐츠 초안",
            "actions": self.actions,
            "output": work["output"],
        }

    def _acting_role(self, actor: workflow.ActorContext, state: dict) -> str:
        # Keep the original role for repeat clicks, including a retry after a Slack update failed.
        for decision in state["decisions"]:
            if decision["actor_id"] == actor.actor_id:
                self._require_role(actor, decision["actor_role"])
                return decision["actor_role"]
        for entry in state["requirements"]:
            if entry["role"] in actor.roles and entry["approved"] < entry["required"]:
                return entry["role"]
        raise SlackConfigurationError("no remaining approval role for this user")

    def handle_command(self, *, ack, command: dict, respond) -> None:
        ack()
        topic = " ".join(str(command.get("text", "")).split())
        try:
            actor = self._actor(command["team_id"], command["channel_id"], command["user_id"])
            if not actor.roles & REQUEST_ROLES:
                raise SlackConfigurationError("user cannot request drafts")
            trigger_id = command["trigger_id"]
            if not trigger_id:
                raise KeyError("trigger_id")
        except (KeyError, SlackConfigurationError):
            respond(response_type="ephemeral", text="지정된 채널과 초안 요청 권한을 확인해 주세요.")
            return
        if not 1 <= len(topic) <= 300:
            respond(response_type="ephemeral", text=f"사용법: `{COMMAND} <주제>` (1~300자)")
            return
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes:draft:{self.workspace_id}:{trigger_id}"))
        try:
            with closing(get_conn(self.db_path)) as conn, conn:
                conn.execute(
                    """INSERT INTO slack_draft_jobs (job_id, team_id, channel_id, user_id, topic)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT(job_id) DO NOTHING""",
                    (job_id, self.workspace_id, self.channel_id, command["user_id"], topic),
                )
        except sqlite3.Error as exc:
            LOGGER.warning("Slack draft enqueue failed error_type=%s", type(exc).__name__)
            respond(response_type="ephemeral", text="요청을 접수하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            return
        respond(response_type="ephemeral", text="초안 요청을 접수했습니다. 준비되면 이 채널에 승인 카드가 올라옵니다.")

    def process_next(self, client) -> bool:
        """Run one due job. A process lock releases automatically when the service exits."""
        lock_path = self.db_path.with_name(f"{self.db_path.name}.slack-drafts.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            with closing(get_conn(self.db_path)) as conn:
                row = conn.execute(
                    """SELECT * FROM slack_draft_jobs
                       WHERE team_id=? AND channel_id=? AND finished=0 AND retry_at<=?
                       ORDER BY rowid LIMIT 1""",
                    (self.workspace_id, self.channel_id, time.time()),
                ).fetchone()
            if row is None:
                return False
            job = dict(row)
            try:
                self._deliver(client, job)
            except sqlite3.Error:
                raise
            except Exception as exc:
                LOGGER.warning("Slack draft delivery failed job_id=%s error_type=%s",
                               job["job_id"], type(exc).__name__)
                with closing(get_conn(self.db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE slack_draft_jobs SET retry_at=? WHERE job_id=?",
                        (time.time() + 60, job["job_id"]),
                    )
            else:
                with closing(get_conn(self.db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE slack_draft_jobs SET finished=1, topic='' WHERE job_id=?",
                        (job["job_id"],),
                    )
            return True

    def _deliver(self, client, job: dict) -> None:
        job_id = job["job_id"]
        if job["failure"] is None:
            # The deterministic work ID lets a restart after generation resume with the saved draft.
            with closing(get_conn(self.db_path)) as conn:
                request = conn.execute(
                    """SELECT request_id FROM approval_requests
                       WHERE work_item_id=? AND work_item_version=1""", (job_id,),
                ).fetchone()
            if request is None:
                try:
                    request = draft_from_topic(job["topic"], work_item_id=job_id, db_path=self.db_path)
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    job["failure"] = type(exc).__name__
                    LOGGER.warning("Slack draft generation failed job_id=%s error_type=%s",
                                   job_id, job["failure"])
                    with closing(get_conn(self.db_path)) as conn, conn:
                        conn.execute(
                            "UPDATE slack_draft_jobs SET failure=?, topic='' WHERE job_id=?",
                            (job["failure"], job_id),
                        )
            if job["failure"] is None:
                with self.publish_lock:
                    self._publish(client, self.workspace_id, self.channel_id,
                                  self._state(request["request_id"]))
                return
        client.chat_postEphemeral(
            channel=self.channel_id, user=job["user_id"],
            text="콘텐츠 초안을 만들지 못했습니다. 잠시 후 다시 요청해 주세요. 문제가 계속되면 관리자에게 알려 주세요.",
        )

    def run(self, client, stop: Event) -> None:
        while not stop.is_set():
            try:
                if self.process_next(client):
                    continue
            except (OSError, sqlite3.Error) as exc:
                LOGGER.warning("Slack draft worker storage failed error_type=%s", type(exc).__name__)
            stop.wait(5)
