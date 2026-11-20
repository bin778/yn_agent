"""Accept Slack draft requests and deliver their approval cards from SQLite."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

from .. import workflow
from ..agents.content_editor import assess_edited_content, draft_from_topic
from ..db import get_conn, get_work_item
from ..slack_approval_demo import CONTENT_COMMAND, SlackApprovalCards, render_card
from .config import SlackConfigurationError, SlackMapping

COMMAND = "/hermes-draft"
LOGGER = logging.getLogger(__name__)
REQUEST_ROLES = {"keyword_approver", "content_approver", "workflow_admin"}
EDIT_ACTION = "hermes_draft_edit"
EDIT_MODAL = "hermes_draft_edit_save"
EDIT_FIELDS = {"title": "제목", "hook": "도입부", "body": "본문"}
APPROVAL_LABELS = {"content_approver": "콘텐츠 승인자", "legal_compliance": "법무·컴플라이언스 승인자"}
SEOUL = ZoneInfo("Asia/Seoul")


def _expiry_label(timestamp: str) -> str:
    local = dt.datetime.fromisoformat(timestamp).astimezone(SEOUL)
    weekday = "월화수목금토일"[local.weekday()]
    period = "오전" if local.hour < 12 else "오후"
    return (f"{local.year}년 {local.month}월 {local.day}일({weekday}) "
            f"{period} {local.hour % 12 or 12}:{local.minute:02d} (한국 시간)")


def _edit_fields(output: dict):
    for field, label in EDIT_FIELDS.items():
        text = output[field]
        parts = [text[pos:pos + 1400] for pos in range(0, len(text), 1400)] or [""]
        for index, part in enumerate(parts, start=1):
            yield field, f"{field}_{index}", f"{label} {index}" if len(parts) > 1 else label, part


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
            "role": ", ".join(APPROVAL_LABELS.get(role, role) for role in roles),
            "role_label": "승인 권한",
            "expiry_label": _expiry_label(state["expires_at"]),
            "card_title": "Hermes · 콘텐츠 초안",
            "actions": {
                key: value for key, value in self.actions.items() if value[1] != "revision_requested"
            },
            "edit_action_id": EDIT_ACTION,
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

    def _editable_state(self, actor, metadata: dict) -> dict:
        self._require_role(actor, "content_approver")
        state = self._bound_state(
            self.workspace_id, self.channel_id, metadata["message_ts"],
            metadata["request_id"], metadata["version"],
        )
        if (state["status"] != "pending"
                or dt.datetime.fromisoformat(state["expires_at"]) <= dt.datetime.now(dt.timezone.utc)):
            raise workflow.StaleActionError("only pending content can be edited")
        return state

    def open_edit(self, *, ack, body, action, respond, client) -> None:
        ack()
        try:
            actor = self._actor(body["team"]["id"], body["channel"]["id"], body["user"]["id"])
            request_id, version = action["value"].split(":")
            metadata = {
                "request_id": str(uuid.UUID(request_id)),
                "version": int(version),
                "message_ts": body["message"]["ts"],
            }
            state = self._editable_state(actor, metadata)
            blocks = []
            for _, block_id, label, part in _edit_fields(state["output"]):
                element = {
                    "type": "plain_text_input", "action_id": "value",
                    "multiline": block_id != "title_1", "max_length": 2800,
                }
                if part:
                    element["initial_value"] = part
                blocks.append({
                    "type": "input", "block_id": block_id, "optional": True,
                    "label": {"type": "plain_text", "text": label}, "element": element,
                })
            client.views_open(trigger_id=body["trigger_id"], view={
                "type": "modal", "callback_id": EDIT_MODAL,
                "private_metadata": json.dumps(metadata),
                "title": {"type": "plain_text", "text": "초안 수정"},
                "submit": {"type": "plain_text", "text": "수정본 저장"},
                "blocks": blocks,
            })
        except (KeyError, ValueError, SlackConfigurationError, workflow.WorkflowError):
            respond(response_type="ephemeral", text="콘텐츠 승인 권한과 카드의 최신 상태를 확인해 주세요.")
        except Exception as exc:
            LOGGER.warning("Slack edit modal failed error_type=%s", type(exc).__name__)
            respond(response_type="ephemeral", text="초안 수정 창을 열지 못했습니다. 다시 시도해 주세요.")

    def submit_edit(self, *, ack, body) -> None:
        user_id = body.get("user", {}).get("id", "")
        try:
            view = body["view"]
            metadata = json.loads(view["private_metadata"])
            actor = self._actor(body["team"]["id"], self.channel_id, user_id)
            state = self._editable_state(actor, metadata)
            values = view["state"]["values"]
            edited = dict.fromkeys(EDIT_FIELDS, "")
            for field, block_id, _, _ in _edit_fields(state["output"]):
                edited[field] += values[block_id]["value"].get("value") or ""
            errors = {}
            if not edited["title"].strip():
                errors["title_1"] = "제목을 입력해 주세요."
            if not edited["body"].strip():
                errors["body_1"] = "본문을 입력해 주세요."
            if len(edited["title"]) > 300 or len(edited["hook"]) + len(edited["body"]) > 63000:
                errors["title_1" if len(edited["title"]) > 300 else "body_1"] = "문안을 줄여 주세요."
            if errors:
                ack(response_action="errors", errors=errors)
                return
            if all(edited[field] == state["output"][field] for field in EDIT_FIELDS):
                ack(response_action="errors", errors={"title_1": "바뀐 내용이 없습니다."})
                return
            with closing(get_conn(self.db_path)) as conn, conn:
                inserted = conn.execute(
                    """INSERT INTO slack_draft_edits
                           (job_id, team_id, channel_id, message_ts, user_id, payload)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(job_id) DO UPDATE SET
                           user_id=excluded.user_id, payload=excluded.payload, retry_at=0,
                           finished=0, failure=NULL
                       WHERE slack_draft_edits.finished=1 AND slack_draft_edits.failure IS NOT NULL""",
                    (state["request_id"], self.workspace_id, self.channel_id, metadata["message_ts"],
                     user_id, json.dumps(edited, ensure_ascii=False)),
                ).rowcount
            if inserted:
                ack(response_action="update", view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "수정본 접수"},
                    "close": {"type": "plain_text", "text": "닫기"},
                    "blocks": [{
                        "type": "section",
                        "text": {
                            "type": "plain_text",
                            "text": "수정본의 위험 항목을 확인한 뒤 같은 승인 카드를 새 버전으로 업데이트합니다.",
                        },
                    }],
                })
            else:
                ack(response_action="errors", errors={"title_1": "이미 접수된 수정본을 검토 중입니다."})
        except (KeyError, TypeError, ValueError, SlackConfigurationError, workflow.WorkflowError):
            ack(response_action="errors", errors={"title_1": "최신 카드에서 수정 창을 다시 열어 주세요."})
        except sqlite3.Error as exc:
            LOGGER.warning("Slack edit enqueue failed error_type=%s", type(exc).__name__)
            ack(response_action="errors", errors={"title_1": "수정본을 접수하지 못했습니다. 다시 시도해 주세요."})

    def _deliver_edit(self, client, job: dict) -> None:
        new_request = job["next_request_id"]
        if not new_request and not job["failure"]:
            try:
                new_request = self._save_edit(job)
            except sqlite3.Error:
                raise
            except Exception as exc:
                job["failure"] = type(exc).__name__
                LOGGER.warning("Slack edit failed job_id=%s error_type=%s", job["job_id"], job["failure"])
                with closing(get_conn(self.db_path)) as conn, conn:
                    conn.execute("UPDATE slack_draft_edits SET failure=?, payload='' WHERE job_id=?",
                                 (job["failure"], job["job_id"]))
        if job["failure"]:
            client.chat_postEphemeral(
                channel=self.channel_id, user=job["user_id"],
                text="수정본을 저장하지 못했습니다. 카드의 최신 상태를 확인하고 다시 시도해 주세요.",
            )
            return
        with self.publish_lock:
            self._update(client, self.channel_id, job["message_ts"], self._state(new_request))

    def _save_edit(self, job: dict) -> str:
        actor = self._actor(job["team_id"], job["channel_id"], job["user_id"])
        self._require_role(actor, "content_approver")
        source = self._state(job["job_id"])
        edited = json.loads(job["payload"])
        # Recover the version if the service stopped after saving it but before rebinding the card.
        version = source["version"] + 1
        with closing(get_conn(self.db_path)) as conn:
            saved = conn.execute(
                """SELECT a.request_id, w.output_json FROM approval_requests a JOIN work_items w
                     ON w.work_item_id=a.work_item_id AND w.version=a.work_item_version
                   WHERE w.work_item_id=? AND w.version=? AND w.parent_version=? AND w.created_by=?""",
                (source["work_item_id"], version, source["version"], actor.actor_id),
            ).fetchone()
        if saved is not None:
            previous_output = json.loads(saved["output_json"])
            if any(previous_output[field] != edited[field] for field in EDIT_FIELDS):
                raise workflow.StaleActionError("the saved revision does not match")
            request_id = saved["request_id"]
        else:
            self._editable_state(actor, {
                "request_id": job["job_id"], "version": source["version"], "message_ts": job["message_ts"],
            })
            risk = assess_edited_content(edited)
            output = {**source["output"], **edited}
            for field in ("flagged_claims", "sensitive_flags"):
                output[field] = list(dict.fromkeys([*output.get(field, []), *getattr(risk, field)]))
            render_card({**source, "output": output})
            with self.publish_lock:
                request_id = workflow.revise_pending_content(
                    request_id=job["job_id"], expected_version=source["version"],
                    actor=actor, revised_output=output, db_path=self.db_path,
                )["request_id"]
        with closing(get_conn(self.db_path)) as conn, conn:
            rebound = conn.execute(
                """UPDATE slack_approval_demo_cards SET request_id=?
                   WHERE request_id=? AND team_id=? AND channel_id=? AND message_ts=?""",
                (request_id, job["job_id"], self.workspace_id, self.channel_id, job["message_ts"]),
            ).rowcount
            if not rebound:
                raise workflow.StaleActionError("the source card binding has changed")
            conn.execute(
                "UPDATE slack_draft_edits SET next_request_id=?, payload='' WHERE job_id=?",
                (request_id, job["job_id"]),
            )
        return request_id

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
                for table, payload_column, deliver in (
                    ("slack_draft_edits", "payload", self._deliver_edit),
                    ("slack_draft_jobs", "topic", self._deliver),
                ):
                    row = conn.execute(
                        f"""SELECT * FROM {table}
                            WHERE team_id=? AND channel_id=? AND finished=0 AND retry_at<=?
                            ORDER BY rowid LIMIT 1""",
                        (self.workspace_id, self.channel_id, time.time()),
                    ).fetchone()
                    if row is not None:
                        break
            if row is None:
                return False
            job = dict(row)
            try:
                deliver(client, job)
            except sqlite3.Error:
                raise
            except Exception as exc:
                LOGGER.warning("Slack draft delivery failed job_id=%s error_type=%s",
                               job["job_id"], type(exc).__name__)
                with closing(get_conn(self.db_path)) as conn, conn:
                    conn.execute(
                        f"UPDATE {table} SET retry_at=? WHERE job_id=?",
                        (time.time() + 60, job["job_id"]),
                    )
            else:
                with closing(get_conn(self.db_path)) as conn, conn:
                    conn.execute(
                        f"UPDATE {table} SET finished=1, {payload_column}='' WHERE job_id=?",
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
                    latest = get_work_item(job_id, db_path=self.db_path)
                    if latest["version"] == 1:
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
