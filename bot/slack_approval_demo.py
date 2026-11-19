"""Development-only Slack entry point into the actual approval workflow."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import closing
from pathlib import Path

from . import workflow
from .db import get_conn, get_work_item
from .schemas import KeywordItem
from .slack_adapter.config import SlackConfigurationError, SlackMapping, SlackIdentityProvider

COMMAND = "approval-demo"
ACTION_ID = "phase4_demo_approve"
ACTIONS: dict[str, tuple[str, workflow.Decision]] = {
    ACTION_ID: ("키워드 승인", "approved"),
    "phase4_demo_reject": ("반려", "rejected"),
    "phase4_demo_revision": ("수정 요청", "revision_requested"),
}
REASON_MODAL = "phase4_demo_reason"
ROLE = "keyword_approver"
CREATOR = "system:slack-approval-demo"
LOGGER = logging.getLogger("hermes.slack.approval_demo")
KEYWORD = KeywordItem(
    keyword="상담 절차 안내 예시",
    estimated_search_volume=0,
    intent_type="Information",
    urgency_level=1,
    compliance_risk="low",
    reason="승인 연동을 검증하기 위한 합성 키워드",
)


def render_card(state: dict) -> tuple[str, list[dict]]:
    status = {
        "pending": "승인 대기",
        "approved": "승인 완료 · 콘텐츠 초안 작업 대기열 등록",
        "rejected": "반려",
        "revision_requested": "수정 요청",
        "expired": "만료",
        "cancelled": "취소",
    }[state["status"]]
    text = (
        f"PHASE 4 개발 검증 · 합성 키워드\n"
        f"{KEYWORD.keyword}\n"
        f"상태: {status}\n"
        f"승인 요청: {state['request_id']} · 버전 {state['version']}"
    )
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": text}}]
    if state["status"] == "pending":
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "action_id": action_id,
                "text": {"type": "plain_text", "text": label},
                "value": f"{state['request_id']}:{state['version']}",
            } for action_id, (label, _) in ACTIONS.items()],
        })
    return text, blocks


class ApprovalDemo:
    def __init__(self, mapping: SlackMapping, db_path: Path):
        self.identity = SlackIdentityProvider(mapping)
        self.db_path = db_path
        self.publish_lock = threading.Lock()

    def _actor(self, team_id: str, channel_id: str, user_id: str) -> workflow.ActorContext:
        actor = self.identity.actor_for(team_id, user_id)
        self.identity.require_development_channel(channel_id)
        if ROLE not in actor.roles:
            raise SlackConfigurationError("keyword approval role is required")
        return workflow.ActorContext.verified_actor(actor.actor_id, actor.roles, platform="slack")

    def _state(self, request_id: str) -> dict:
        state = workflow.get_approval_state(request_id, self.db_path)
        work = get_work_item(state["work_item_id"], state["version"], self.db_path)
        if (
            work is None
            or work["created_by"] != CREATOR
            or work["agent_id"] != "keyword_analyst"
            or {entry["role"] for entry in state["requirements"]} != {ROLE}
        ):
            raise workflow.NotFoundError("not an approval demo request")
        return state

    def _request(self, team_id: str, trigger_id: str) -> str:
        if not trigger_id:
            raise ValueError("Slack trigger id is required")
        work_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes:approval-demo:{team_id}:{trigger_id}"))
        with closing(get_conn(self.db_path)) as conn:
            existing = conn.execute(
                "SELECT request_id FROM approval_requests WHERE work_item_id=? AND work_item_version=1",
                (work_id,),
            ).fetchone()
        if existing:
            return existing["request_id"]
        return workflow.create_work_item(
            agent_id="keyword_analyst",
            output=KEYWORD,
            routed_to="content_editor.inbox",
            model_version=None,
            input_summary=KEYWORD.keyword,
            created_by=CREATOR,
            work_item_id=work_id,
            db_path=self.db_path,
        )["request_id"]

    def _publish(self, client, team_id: str, channel_id: str, state: dict) -> None:
        request_id = state["request_id"]
        with closing(get_conn(self.db_path)) as conn:
            row = conn.execute(
                """SELECT message_ts FROM slack_approval_demo_cards
                   WHERE request_id=? AND team_id=? AND channel_id=?""",
                (request_id, team_id, channel_id),
            ).fetchone()
            if row is None:
                # A post that succeeds before a binding failure has no actionable button.
                message = client.chat_postMessage(
                    channel=channel_id,
                    text=f"PHASE 4 개발 검증 · 카드 준비 중 · 승인 요청 {request_id}",
                )
                message_ts = message["ts"]
                with conn:
                    conn.execute(
                        """INSERT INTO slack_approval_demo_cards
                           (request_id, team_id, channel_id, message_ts) VALUES (?, ?, ?, ?)""",
                        (request_id, team_id, channel_id, message_ts),
                    )
            else:
                message_ts = row["message_ts"]
        self._update(client, channel_id, message_ts, state)

    @staticmethod
    def _update(client, channel_id: str, message_ts: str, state: dict) -> None:
        text, blocks = render_card(state)
        client.chat_update(channel=channel_id, ts=message_ts, text=text, blocks=blocks)

    def handle_command(self, *, ack, command: dict, respond, client) -> None:
        ack()
        request_id = None
        try:
            team_id = command["team_id"]
            channel_id = command["channel_id"]
            self._actor(team_id, channel_id, command["user_id"])
            arguments = command["text"].split()[1:]
            if len(arguments) > 1:
                raise ValueError("too many arguments")
            with self.publish_lock:
                request_id = (
                    str(uuid.UUID(arguments[0])) if arguments
                    else self._request(team_id, command.get("trigger_id", ""))
                )
                self._publish(client, team_id, channel_id, self._state(request_id))
        except (KeyError, ValueError, SlackConfigurationError, workflow.NotFoundError):
            respond(response_type="ephemeral", text="개발 채널, 키워드 승인 역할과 요청 ID를 확인해 주세요.")
        except Exception as exc:
            LOGGER.warning("approval demo card failed error_type=%s", type(exc).__name__)
            retry = f" `/hermes-test {COMMAND} {request_id}`로 다시 표시할 수 있습니다." if request_id else ""
            respond(response_type="ephemeral", text=f"승인 카드를 표시하지 못했습니다.{retry}")

    def _bound_state(self, team_id, channel_id, message_ts, request_id, version) -> dict:
        state = self._state(request_id)
        with closing(get_conn(self.db_path)) as conn:
            binding = conn.execute(
                """SELECT 1 FROM slack_approval_demo_cards
                   WHERE request_id=? AND team_id=? AND channel_id=? AND message_ts=?""",
                (request_id, team_id, channel_id, message_ts),
            ).fetchone()
        latest = get_work_item(state["work_item_id"], db_path=self.db_path)
        if (
            binding is None or version != state["version"]
            or latest is None or latest["version"] != version
        ):
            raise workflow.StaleActionError("message or version does not match")
        return state

    def _apply(
        self, *, client, actor, team_id, channel_id, message_ts, request_id,
        version, decision: workflow.Decision, reason: str | None = None,
    ) -> None:
        with self.publish_lock:
            self._bound_state(team_id, channel_id, message_ts, request_id, version)
            try:
                workflow.decide(
                    request_id=request_id,
                    expected_version=version,
                    actor=actor,
                    acting_role=ROLE,
                    decision=decision,
                    reason=reason,
                    db_path=self.db_path,
                )
            except workflow.StaleActionError:
                self._update(client, channel_id, message_ts, self._state(request_id))
                raise
            self._update(client, channel_id, message_ts, self._state(request_id))

    def handle_action(self, *, ack, body: dict, action: dict, respond, client) -> None:
        ack()
        request_id = None
        try:
            team_id = body.get("team", {}).get("id", "")
            channel_id = body.get("channel", {}).get("id", "")
            message_ts = body.get("message", {}).get("ts", "")
            actor = self._actor(team_id, channel_id, body.get("user", {}).get("id", ""))
            raw_id, raw_version = action["value"].split(":")
            request_id = str(uuid.UUID(raw_id))
            version = int(raw_version)
            label, decision = ACTIONS[action["action_id"]]
            if decision == "approved":
                self._apply(
                    client=client, actor=actor, team_id=team_id, channel_id=channel_id,
                    message_ts=message_ts, request_id=request_id, version=version, decision=decision,
                )
                return
            state = self._bound_state(team_id, channel_id, message_ts, request_id, version)
            if state["status"] != "pending":
                raise workflow.StaleActionError("request is already closed")
            client.views_open(trigger_id=body["trigger_id"], view={
                "type": "modal",
                "callback_id": REASON_MODAL,
                "private_metadata": json.dumps({
                    "team_id": team_id, "channel_id": channel_id, "message_ts": message_ts,
                    "request_id": request_id, "version": version, "action_id": action["action_id"],
                }),
                "title": {"type": "plain_text", "text": label},
                "submit": {"type": "plain_text", "text": "제출"},
                "blocks": [{
                    "type": "input",
                    "block_id": "reason",
                    "label": {"type": "plain_text", "text": "사유"},
                    "element": {"type": "plain_text_input", "action_id": "reason", "multiline": True},
                }],
            })
        except (KeyError, ValueError, SlackConfigurationError, workflow.WorkflowError):
            respond(response_type="ephemeral", text="이 승인 요청은 처리할 수 없습니다. 권한과 카드의 최신 상태를 확인해 주세요.")
        except Exception as exc:
            LOGGER.warning("approval demo action failed error_type=%s", type(exc).__name__)
            retry = f" `/hermes-test {COMMAND} {request_id}`로 현재 상태를 확인해 주세요." if request_id else ""
            respond(response_type="ephemeral", text=f"승인 상태를 표시하지 못했습니다.{retry}")

    def handle_view(self, *, ack, body: dict, client) -> None:
        view = body["view"]
        values = view.get("state", {}).get("values", {})
        reason = (values.get("reason", {}).get("reason", {}).get("value") or "").strip()
        if not reason:
            ack(response_action="errors", errors={"reason": "사유를 입력해 주세요."})
            return

        ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = None
        try:
            metadata = json.loads(view["private_metadata"])
            team_id = body.get("team", {}).get("id", "")
            if team_id != metadata["team_id"]:
                raise SlackConfigurationError("modal workspace does not match")
            actor = self._actor(team_id, metadata["channel_id"], user_id)
            channel_id = metadata["channel_id"]
            _, decision = ACTIONS[metadata["action_id"]]
            if decision == "approved":
                raise ValueError("unsupported modal decision")
            self._apply(
                client=client, actor=actor, team_id=team_id, channel_id=channel_id,
                message_ts=metadata["message_ts"], request_id=metadata["request_id"],
                version=metadata["version"], decision=decision, reason=reason,
            )
        except Exception as exc:
            LOGGER.warning("approval demo modal failed error_type=%s", type(exc).__name__)
            if channel_id is not None:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="요청을 처리하거나 표시하지 못했습니다. 승인 카드의 최신 상태를 확인해 주세요.",
                    )
                except Exception as notify_error:
                    LOGGER.warning("approval demo notification failed error_type=%s", type(notify_error).__name__)
