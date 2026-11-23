"""Require a human keyword approval and deliver all team results in one Slack thread."""

from __future__ import annotations

import fcntl
import logging
import sqlite3
import time
from contextlib import closing
from html import escape

from .. import workflow
from ..db import get_conn, get_work_item
from ..slack_approval_demo import COMMAND as KEYWORD_CARD, CONTENT_COMMAND, SlackApprovalCards, render_card
from ..team_brief import AGENTS, TeamBriefs
from .blocks import seoul_time_label
from .config import SlackConfigurationError
from .drafts import REQUEST_ROLES

COMMAND = "/hermes-team"
LOGGER = logging.getLogger(__name__)
TITLE = "Hermes · 주간 콘텐츠·광고 브리핑 · 합성 데이터"
REQUESTOR_ROLES = REQUEST_ROLES | {"data_owner", "performance_approver"}
ROLE_LABELS = {
    "keyword_approver": "키워드 승인자",
    "legal_compliance": "법무·컴플라이언스 승인자",
}

STAGE_LABELS = {"queued": "대기", "failed": "실패", "completed": "완료"}


def _report(jobs, items, handoffs) -> str:
    pending_keywords = sum(
        item["agent_id"] == AGENTS["keyword"] and item["status"] == "pending_approval"
        for item in items
    )
    drafts = sum(item["agent_id"] == "content_editor" for item in items)
    draft_waiting = sum(item["status"] in {"pending", "claimed"} for item in handoffs)
    draft_failed = sum(item["status"] == "failed" for item in handoffs)
    content = []
    if drafts:
        content.append(f"초안 {drafts}건")
    if pending_keywords:
        content.append(f"키워드 승인 대기 {pending_keywords}건")
    if draft_waiting:
        content.append(f"초안 작업 대기 {draft_waiting}건")
    if draft_failed:
        content.append(f"초안 실패 {draft_failed}건")
    if not content:
        if any(item["agent_id"] == AGENTS["keyword"] for item in items):
            content.append("선택된 키워드 없음")
        elif jobs["keyword"]["status"] == "completed":
            content.append("키워드 후보 없음")
        else:
            content.append(f"키워드 분석 {STAGE_LABELS[jobs['keyword']['status']]}")
    data = next((item for item in items if item["agent_id"] == AGENTS["data"]), None)
    data_status = STAGE_LABELS[jobs["data"]["status"]]
    if data:
        data_status = f"{data['output']['status_summary']} · {seoul_time_label(data['created_at'])}"
    performance = [item for item in items if item["agent_id"] == AGENTS["performance"]]
    ads = STAGE_LABELS[jobs["performance"]["status"]]
    if performance and performance[0]["output"].get("halted"):
        ads = "보류 · " + performance[0]["output"]["reason"][:350]
    elif performance:
        ads = f"개선안 {len(performance)}건"
    elif jobs["performance"]["status"] == "completed":
        ads = "개선안 없음"
    return "\n".join([
        TITLE,
        f"콘텐츠 제작팀 · {' / '.join(content)}",
        f"데이터 분석팀 · {data_status}",
        f"퍼포먼스 마케팅팀 · {ads}",
        "확인할 일 · 스레드에서 제작할 키워드를 승인해 주세요." if pending_keywords else
        "직원별 산출물과 근거는 이 메시지의 스레드에서 확인할 수 있습니다.",
    ])


def _detail(item: dict) -> str:
    output = item["output"]
    if item["agent_id"] == AGENTS["data"]:
        anomalies = "\n".join(f"• {entry}" for entry in output.get("tracking_anomalies", [])[:5])
        actions = "\n".join(f"• {entry}" for entry in output.get("action_items", [])[:5])
        return "\n".join(filter(None, [
            f"임지혁 · 데이터 분석 · {output['status_summary']}",
            f"점검 시각 {seoul_time_label(item['created_at'])}",
            output["reason"], anomalies, actions,
        ]))
    if output.get("halted"):
        return f"김도현 · 광고 제안 보류\n{output['reason']}"
    return (
        f"김도현 · 광고 개선안 · {output['keyword']}\n"
        f"{output.get('campaign', '')} · {output['action']} {output['adjustment_percentage']:+d}%\n"
        f"{output['rationale']}"
    )


class SlackTeamBriefs(SlackApprovalCards):
    actions = {
        "hermes_team_keyword_approve": ("키워드 승인", "approved"),
        "hermes_team_keyword_reject": ("반려", "rejected"),
    }
    reason_modal = "hermes_team_keyword_reason"

    def __init__(self, mapping, db_path, channel_id, *, approval_mode):
        super().__init__(mapping, db_path)
        self.team = TeamBriefs(db_path, mapping.workspace_id, channel_id, approval_mode)

    def _actor(self, team_id, channel_id, user_id):
        if channel_id != self.team.channel_id:
            raise SlackConfigurationError("team brief is outside the configured channel")
        actor = self.identity.actor_for(team_id, user_id)
        return workflow.ActorContext.verified_actor(actor.actor_id, actor.roles, platform="slack")

    def _acting_role(self, actor, state):
        for decision in state["decisions"]:
            if decision["actor_id"] == actor.actor_id:
                self._require_role(actor, decision["actor_role"])
                return decision["actor_role"]
        for entry in state["requirements"]:
            if entry["role"] in actor.roles and entry["approved"] < entry["required"]:
                return entry["role"]
        raise SlackConfigurationError("no remaining keyword approval role for this user")

    def _state(self, request_id: str) -> dict:
        state = workflow.get_approval_state(request_id, self.db_path)
        work = get_work_item(state["work_item_id"], state["version"], self.db_path)
        if work is None or work["agent_id"] != AGENTS["keyword"]:
            raise workflow.NotFoundError("not a team keyword")
        brief_id = work["correlation_id"]
        if self.team.get_brief(brief_id) is None:
            raise workflow.NotFoundError("keyword is outside this team channel")
        output = work["output"]
        roles = [ROLE_LABELS[entry["role"]] for entry in state["requirements"]]
        return {
            **state, "brief_id": brief_id, "command": KEYWORD_CARD, "output": output,
            "role": ", ".join(roles), "actions": self.actions,
            "expiry_label": seoul_time_label(state["expires_at"]),
            "card_title": "윤서진 · 키워드 후보 · 합성 데이터",
            "detail": f"검색 의도 {output['intent_type']} · 위험도 {output['compliance_risk']}\n"
                      f"{output['reason']}\n{output.get('compliance_note', '')}",
        }

    def handle_command(self, *, ack, command, respond) -> None:
        ack()
        try:
            actor = self._actor(command["team_id"], command["channel_id"], command["user_id"])
            if not actor.roles & REQUESTOR_ROLES or not command["trigger_id"]:
                raise SlackConfigurationError("team brief request role is missing")
        except (KeyError, SlackConfigurationError):
            respond(response_type="ephemeral", text="지정된 채널과 팀 브리핑 요청 권한을 확인해 주세요.")
            return

        words = command.get("text", "").strip().lower().split()
        if words not in (["brief"], ["brief", "normal"], ["brief", "critical"]):
            respond(response_type="ephemeral", text="사용법: /hermes-team brief [normal|critical]")
            return
        scenario = words[-1] if len(words) == 2 else "normal"
        try:
            brief_id = self.team.enqueue(
                trigger_id=command["trigger_id"], user_id=actor.actor_id, scenario=scenario,
            )
        except sqlite3.Error as exc:
            LOGGER.warning("Brief enqueue failed error_type=%s", type(exc).__name__)
            respond(response_type="ephemeral", text="브리핑을 접수하지 못했습니다. 잠시 후 다시 요청해 주세요.")
            return
        respond(response_type="ephemeral", text=f"팀 브리핑을 접수했습니다. · {brief_id}")

    def process_next(self, client) -> bool:
        """Serialize brief work across restarts and separate model and delivery retries."""
        lock_path = self.db_path.with_name(f"{self.db_path.name}.slack-team.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            delivered = self._sync_next(client)
            worked = self.team.next_job()
            return delivered or worked

    def _sync_next(self, client) -> bool:
        with closing(get_conn(self.db_path)) as conn:
            brief = conn.execute(
                """SELECT * FROM team_briefs WHERE team_id=? AND channel_id=?
                     AND sync_version>delivered_version AND retry_at<=?
                   ORDER BY created_at LIMIT 1""",
                (self.team.team_id, self.team.channel_id, time.time()),
            ).fetchone()
        if brief is None:
            return False
        with self.publish_lock:
            try:
                self._sync(client, dict(brief))
            except Exception as exc:
                LOGGER.warning("Brief delivery failed brief_id=%s error_type=%s", brief["brief_id"], type(exc).__name__)
                with closing(get_conn(self.db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE team_briefs SET retry_at=? WHERE brief_id=?",
                        (time.time() + 30, brief["brief_id"]),
                    )
        return True

    def _sync(self, client, brief: dict) -> None:
        brief_id, channel_id = brief["brief_id"], brief["channel_id"]
        jobs, items, handoffs = self.team.snapshot(brief_id)
        report = _report(jobs, items, handoffs)
        thread_ts = brief["message_ts"]
        if thread_ts is None:
            message = client.chat_postMessage(channel=channel_id, text=escape(report, quote=False))
            thread_ts = message["ts"]
            with closing(get_conn(self.db_path)) as conn, conn:
                conn.execute("UPDATE team_briefs SET message_ts=? WHERE brief_id=?", (thread_ts, brief_id))
        else:
            client.chat_update(channel=channel_id, ts=thread_ts, text=escape(report, quote=False))
        for item in items:
            if item["agent_id"] == AGENTS["keyword"]:
                state = self._state(item["request_id"])
                self._publish(client, brief["team_id"], channel_id, state, thread_ts=thread_ts)
            elif item["agent_id"] == "content_editor":
                state = workflow.get_approval_state(item["request_id"], self.db_path)
                text, blocks = render_card({
                    **state, "command": CONTENT_COMMAND, "output": item["output"], "actions": {},
                    "card_title": "한도윤 · 검토용 콘텐츠 초안 · 합성 데이터",
                    "role": "팀 브리핑에서 초안 확인", "status_label": "브리핑용 초안 작성 완료",
                    "expiry_label": seoul_time_label(state["expires_at"]),
                })
                self._reply(client, brief, thread_ts, item["work_item_id"], text, blocks=blocks)
            else:
                text = escape(_detail(item)[:2800], quote=False)
                self._reply(client, brief, thread_ts, item["work_item_id"], text)
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE team_briefs SET delivered_version=?, retry_at=0 WHERE brief_id=?",
                (brief["sync_version"], brief_id),
            )

    def _reply(self, client, brief, thread_ts, key, text, **kwargs):
        brief_id = brief["brief_id"]
        with closing(get_conn(self.db_path)) as conn:
            if conn.execute(
                "SELECT 1 FROM team_brief_replies WHERE brief_id=? AND reply_key=?", (brief_id, key),
            ).fetchone():
                return
            message = client.chat_postMessage(
                channel=brief["channel_id"], thread_ts=thread_ts, text=text, **kwargs,
            )
            with conn:
                conn.execute(
                    "INSERT INTO team_brief_replies (brief_id, reply_key, message_ts) VALUES (?, ?, ?)",
                    (brief_id, key, message["ts"]),
                )
