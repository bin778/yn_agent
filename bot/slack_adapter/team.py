"""Require a human keyword approval and deliver all team results in one Slack thread."""

from __future__ import annotations

import fcntl
import logging
import sqlite3
import time
from collections import Counter
from contextlib import closing
from functools import lru_cache
from html import escape

from slack_sdk.errors import SlackClientError

from .. import workflow
from ..db import get_conn, get_work_item
from ..slack_approval_demo import COMMAND as KEYWORD_CARD, CONTENT_COMMAND, SlackApprovalCards, render_card
from ..schemas import DRAFT_TOPICS
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


def _report(jobs, items, handoffs, permalink=None) -> tuple[str, list[dict]]:
    keywords = [item for item in items if item["agent_id"] == AGENTS["keyword"]]
    pending = [item for item in keywords if item["status"] == "pending_approval"]
    legal_pending = [
        item for item in pending if item["policy_key"] == "keyword_high" and item["keyword_approved"]
    ]
    legal_ids = {item["work_item_id"] for item in legal_pending}
    keyword_pending = [item for item in pending if item["work_item_id"] not in legal_ids]
    advisory = [
        item for item in keywords if item["policy_key"] == "keyword_high_advisory"
        and item["status"] in {"pending_approval", "approved"}
    ]
    drafts = [item for item in items if item["agent_id"] == "content_editor"]
    draft_waiting = sum(item["status"] in {"pending", "claimed"} for item in handoffs)
    draft_failed = sum(item["status"] == "failed" for item in handoffs)
    counts = Counter(item["status"] for item in keywords)
    content = [
        f"{label} {count}건"
        for label, count in (
            ("키워드 승인 대기", len(keyword_pending)),
            ("법무 승인 대기", len(legal_pending)),
            ("법무 검토 권고", len(advisory)),
            ("반려", counts["rejected"]),
            ("만료", counts["expired"]),
            ("취소", counts["cancelled"]),
            ("초안", len(drafts)),
            ("초안 작업 대기", draft_waiting),
            ("초안 실패", draft_failed),
        )
        if count
    ]
    if not content:
        if keywords:
            content.append("선택된 키워드 없음")
        elif jobs["keyword"]["status"] == "completed":
            content.append("키워드 후보 없음")
        else:
            content.append(f"키워드 분석 {STAGE_LABELS[jobs['keyword']['status']]}")

    data = next((item for item in items if item["agent_id"] == AGENTS["data"]), None)
    data_status = STAGE_LABELS[jobs["data"]["status"]]
    tracking_review = []
    if data:
        status = data["output"]["status_summary"]
        data_status = f"{status} · {seoul_time_label(data['created_at'])}"
        if data["status"] == "acknowledgement_pending" or status == "Critical":
            tracking_review.append(data)

    performance = [item for item in items if item["agent_id"] == AGENTS["performance"]]
    halted = [item for item in performance if item["output"].get("halted")]
    proposals = [item for item in performance if item["status"] == "pending_approval"]
    ads = STAGE_LABELS[jobs["performance"]["status"]]
    if halted:
        ads = "보류 · " + halted[0]["output"]["reason"][:350]
    elif performance:
        ads = f"개선안 {len(performance)}건"
    elif jobs["performance"]["status"] == "completed":
        ads = "개선안 없음"

    summary = "\n".join([
        f"콘텐츠 제작팀 · {' / '.join(content)}",
        f"데이터 분석팀 · {data_status}",
        f"퍼포먼스 마케팅팀 · {ads}",
    ])
    actions = [
        (label, candidates)
        for label, candidates in (
            ("키워드 승인", keyword_pending),
            ("별도 법무 승인", legal_pending),
            ("법무 검토 권고 확인", [item for item in advisory if item["status"] == "approved"]),
            ("검토용 초안 확인", [item for item in drafts if item["status"] == "pending_approval"]),
            ("전환 추적 점검 확인", tracking_review),
            ("광고 보류 사유 확인", halted),
            ("광고 개선안 검토", proposals),
        )
        if candidates
    ]
    fallback = f"{TITLE}\n{summary}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": TITLE}},
        _section(summary),
    ]
    if actions:
        labels = " · ".join(f"{label} {len(candidates)}건" for label, candidates in actions)
        fallback += f"\n다음 행동 · {labels}"
        blocks.append(_section("다음 행동"))
        missing_link = False
        for label, candidates in actions:
            blocks.append(_section(f"{label} {len(candidates)}건"))
            for item in candidates[:3]:
                output = item["output"]
                title = output.get("keyword") or output.get("title") or output.get("status_summary") or "보류 상세"
                title = " ".join(title.split())[:180]
                url = permalink(item["work_item_id"]) if permalink else None
                if url:
                    blocks.append({"type": "section", "text": {
                        "type": "mrkdwn", "verbatim": True,
                        "text": f"{escape(title, quote=False)} · <{url}|카드 열기>",
                    }})
                else:
                    blocks.append(_section(title))
                    missing_link = True
            if len(candidates) > 3:
                blocks.append(_section(f"나머지 {len(candidates) - 3}건은 스레드에서 확인할 수 있습니다."))
        if permalink and missing_link:
            blocks.append(_section("링크가 없는 항목은 이 스레드에서 확인할 수 있습니다."))
    elif draft_waiting:
        fallback += "\n다음 행동 · 초안이 작성되면 스레드에서 확인해 주세요."
        blocks.append(_section("초안이 작성되면 스레드에서 확인해 주세요."))
    elif draft_failed or any(job["status"] == "failed" for job in jobs.values()):
        fallback += "\n다음 행동 · 실패한 작업 상태를 확인해 주세요."
        blocks.append(_section("실패한 작업 상태를 확인해 주세요."))
    return escape(fallback, quote=False), blocks


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "plain_text", "text": text}}


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

    def __init__(
        self, mapping, db_path, channel_id, *, approval_mode,
        keyword_approval_mode=workflow.KeywordApprovalMode.LEGAL_BEFORE_DRAFT,
    ):
        super().__init__(mapping, db_path)
        self.team = TeamBriefs(
            db_path, mapping.workspace_id, channel_id, approval_mode,
            keyword_approval_mode=keyword_approval_mode,
        )

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
        requirements = state["requirements"]
        missing_roles = [
            ROLE_LABELS[entry["role"]]
            for entry in requirements if entry["approved"] < entry["required"]
        ]
        approved = sum(entry["approved"] for entry in requirements)
        required = sum(entry["required"] for entry in requirements)
        pending = state["status"] == "pending"
        roles = missing_roles if pending else [ROLE_LABELS[entry["role"]] for entry in requirements]
        approval_feedback = "승인 완료 · 콘텐츠 초안 작업을 대기열에 등록했습니다."
        if pending:
            approval_feedback = (
                f"승인을 기록했습니다 ({approved}/{required}). "
                f"남은 승인: {', '.join(missing_roles)}."
            )
        detail = (
            f"검색 의도 {output['intent_type']} · 위험도 {output['compliance_risk']}\n"
            f"{output['reason']}\n{output.get('compliance_note', '')}"
        )
        if state["policy_key"] == "keyword_high_advisory":
            general_topic = DRAFT_TOPICS.get(output.get("draft_topic_category"), DRAFT_TOPICS["general"])
            detail = (
                "법무·컴플라이언스 검토 권고 · 검토용 초안 작성 가능\n"
                f"검토용 초안 주제 · {general_topic}\n"
                "외부 발행 전에 별도 법무 승인이 필요합니다.\n\n" + detail
            )
            if state["status"] == "approved":
                approval_feedback = "승인 완료 · 일반화한 주제로 검토용 초안을 작성합니다. 외부 발행 전 법무 승인이 필요합니다."
        return {
            **state, "brief_id": brief_id, "command": KEYWORD_CARD, "output": output,
            "role": ", ".join(roles), "role_label": "남은 승인 역할" if pending else "필요 역할",
            "actions": self.actions, "approval_progress": f"{approved}/{required}",
            "approval_feedback": approval_feedback,
            "duplicate_approval_feedback": (
                f"이미 승인하셨습니다. 남은 승인: {', '.join(missing_roles)}."
                if pending else "이미 승인 처리된 키워드입니다."
            ),
            "rejection_feedback": "키워드를 반려했습니다.",
            **({"status_label": "일부 승인 완료"} if pending and approved else {}),
            "expiry_label": seoul_time_label(state["expires_at"]),
            "card_title": "윤서진 · 키워드 후보 · 합성 데이터",
            "detail": detail,
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
        thread_ts = brief["message_ts"]
        new_report = thread_ts is None
        if new_report:
            text, blocks = _report(jobs, items, handoffs)
            message = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
            thread_ts = message["ts"]
            with closing(get_conn(self.db_path)) as conn, conn:
                conn.execute("UPDATE team_briefs SET message_ts=? WHERE brief_id=?", (thread_ts, brief_id))
        message_ids = {}
        for item in items:
            work_id = item["work_item_id"]
            if item["agent_id"] == AGENTS["keyword"]:
                state = self._state(item["request_id"])
                message_ts = self._publish(client, brief["team_id"], channel_id, state, thread_ts=thread_ts)
            elif item["agent_id"] == "content_editor":
                state = workflow.get_approval_state(item["request_id"], self.db_path)
                text, blocks = render_card({
                    **state, "command": CONTENT_COMMAND, "output": item["output"], "actions": {},
                    "card_title": "한도윤 · 검토용 콘텐츠 초안 · 합성 데이터",
                    "role": (
                        "외부 발행 전 콘텐츠·법무 승인 필요"
                        if item["output"].get("requires_legal_before_publish") else "팀 브리핑에서 초안 확인"
                    ),
                    "status_label": "브리핑용 초안 작성 완료",
                    "expiry_label": seoul_time_label(state["expires_at"]),
                })
                message_ts = self._reply(client, brief, thread_ts, work_id, text, blocks=blocks)
            else:
                text = escape(_detail(item)[:2800], quote=False)
                message_ts = self._reply(client, brief, thread_ts, work_id, text)
            message_ids[work_id] = message_ts
        if not new_report or message_ids:
            text, blocks = _report(
                jobs, items, handoffs,
                permalink=lambda work_id: self._message_link(client, channel_id, message_ids[work_id]),
            )
            client.chat_update(channel=channel_id, ts=thread_ts, text=text, blocks=blocks)
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE team_briefs SET delivered_version=?, retry_at=0 WHERE brief_id=?",
                (brief["sync_version"], brief_id),
            )

    @staticmethod
    @lru_cache(maxsize=256)
    def _permalink(client, channel_id: str, message_ts: str) -> str:
        return client.chat_getPermalink(channel=channel_id, message_ts=message_ts)["permalink"]

    def _message_link(self, client, channel_id: str, message_ts: str) -> str | None:
        try:
            url = self._permalink(client, channel_id, message_ts)
        except (SlackClientError, KeyError, OSError) as exc:
            LOGGER.warning("Brief message link unavailable error_type=%s", type(exc).__name__)
            return None
        if isinstance(url, str) and url.startswith("https://") and not any(c in url for c in "<>|"):
            return escape(url, quote=False)
        return None

    def _reply(self, client, brief, thread_ts, key, text, **kwargs) -> str:
        brief_id = brief["brief_id"]
        with closing(get_conn(self.db_path)) as conn:
            existing = conn.execute(
                "SELECT message_ts FROM team_brief_replies WHERE brief_id=? AND reply_key=?", (brief_id, key),
            ).fetchone()
            if existing:
                return existing["message_ts"]
            message = client.chat_postMessage(
                channel=brief["channel_id"], thread_ts=thread_ts, text=text, **kwargs,
            )
            with conn:
                conn.execute(
                    "INSERT INTO team_brief_replies (brief_id, reply_key, message_ts) VALUES (?, ?, ?)",
                    (brief_id, key, message["ts"]),
                )
            return message["ts"]
