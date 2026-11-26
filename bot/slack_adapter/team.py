"""Require a human keyword approval and deliver all team results in one Slack thread."""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing
from functools import lru_cache
from html import escape

from slack_sdk.errors import SlackClientError

from .. import brief_inputs, workflow
from ..db import CODEX_TIMEOUT_ERROR, get_conn, get_work_item
from ..slack_approval_demo import COMMAND as KEYWORD_CARD, CONTENT_COMMAND, SlackApprovalCards, render_card
from ..schemas import DRAFT_TOPICS
from ..team_brief import AGENTS, TeamBriefs
from . import brief_input
from .blocks import seoul_time_label
from .config import SlackConfigurationError
from .drafts import REQUEST_ROLES

COMMAND = "/hermes-team"
DRAFT_RETRY_ACTION = "hermes_team_draft_retry"
PERFORMANCE_RETRY_ACTION = "hermes_team_performance_retry"
BRIEF_SCENARIOS = ("normal", "critical", "stale", "missing", "zero")
LOGGER = logging.getLogger(__name__)
TITLE = "Hermes · 주간 콘텐츠·광고 브리핑 · 합성 데이터"
REQUESTOR_ROLES = REQUEST_ROLES | {"data_owner", "performance_approver"}
ROLE_LABELS = {
    "keyword_approver": "키워드 승인자",
    "legal_compliance": "법무·컴플라이언스 승인자",
}

STAGE_LABELS = {"queued": "대기", "failed": "실패", "completed": "완료"}


def _input_summary(data: dict) -> str:
    comparison = brief_inputs.comparison_text(data)
    weekly = data["mode"] == "weekly"
    ga4 = data["mode"] in {"ga4", "weekly"}
    source = "GA4 Data API · " if ga4 else "GA4, "
    ads = "광고 " if ga4 else ""
    return "\n".join((
        f"출처 · {source}{data.get('ads_source', '광고 성과 보고서')}"
        + (" 및 검색어 보고서" if data["keywords"] else ""),
        f"보고 기간 · {data['start']} ~ {data['end']} ({data['timezone']})",
        *(["집계 · 브리핑 기준 충족 · 사용 중 캠페인 (실험 제외)",
           f"Google Ads API 조회 시각 · {seoul_time_label(data['ads_fetched_at'])}",
           "광고 전환수는 조회 이후에도 갱신될 수 있습니다."] if weekly else [
           f"{ads}집계 완료 · {'확인' if data['complete'] else '미확인'}",
           f"{ads}원본 집계 시각 · {seoul_time_label(data['collected_through']) if data['collected_through'] else '미확인'}",
        ]),
        *([f"GA4 API 조회 시각 · {seoul_time_label(data['ga4_fetched_at'])}",
           "GA4 원본 집계 완료 시각 · 미확인"] if ga4 else []),
        *([f"GA4 이벤트 원본 CSV · 0건 확인 행 {data['ga4_verified_zeros']}개 보완"]
          if ga4 and data.get("ga4_verified_zeros") else []),
        *([f"GA4 미기록일은 0건으로 집계 · 날짜·이벤트 조합 {data['ga4_inferred_zeros']}개"]
          if ga4 and data.get("ga4_inferred_zeros") else []),
        f"제출 처리 시각 · {seoul_time_label(data['imported_at'])}",
        brief_inputs.summary_text(data),
        *([("추적 확인 · " if ga4 else "누락 · ") + " / ".join(data["warnings"])]
          if data["warnings"] and not data.get("ga4_pending") else []),
        *([comparison] if comparison else []),
    ))


def _report(jobs, items, handoffs, permalink=None, *, imported=None) -> tuple[str, list[dict]]:
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
    approved_keywords = {
        (item["work_item_id"], item["version"]): item["output"]["keyword"]
        for item in keywords if item["status"] == "approved"
    }
    timed_out = [
        (approved_keywords[key], handoff)
        for handoff in handoffs
        if handoff["status"] == "failed" and handoff["error"] == CODEX_TIMEOUT_ERROR
        and handoff["claim_token"]
        and (key := (handoff["work_item_id"], handoff["work_item_version"])) in approved_keywords
    ]
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
        if imported is not None and not imported["keywords"]:
            content.append("키워드 분석 대기 · 검색어 보고서 필요")
        elif keywords:
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
    historical = [item for item in performance if item["output"].get("historical_summary")]
    proposals = [item for item in performance if item["status"] == "pending_approval"]
    performance_job = jobs["performance"]
    performance_timeout = (
        performance_job["status"] == "failed" and performance_job["error"] == CODEX_TIMEOUT_ERROR
    )
    ads = STAGE_LABELS[performance_job["status"]]
    if halted:
        outcome = halted[0]["output"]
        ads = "보류 · " + outcome["reason"][:350]
        if outcome.get("next_action"):
            ads += " " + outcome["next_action"]
    elif historical:
        ads = "성과 분석 완료"
    elif performance:
        ads = f"개선안 {len(performance)}건"
    elif performance_job["status"] == "completed":
        ads = "개선안 없음"
    elif performance_timeout:
        ads = "광고 분석 시간 초과"
    elif performance_job["status"] == "queued" and performance_job["retry_count"]:
        ads = "광고 분석 재시도 접수"

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
            ("광고 성과 확인", historical),
        )
        if candidates
    ]
    title = TITLE if imported is None else f"Hermes · 주간 콘텐츠·광고 브리핑 · {brief_input.MODES[imported['mode']]}"
    if imported is not None:
        summary = _input_summary(imported) + "\n\n" + summary
    fallback = f"{title}\n{summary}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        _section(summary),
    ]
    if actions or timed_out or performance_timeout:
        labels = [f"{label} {len(candidates)}건" for label, candidates in actions]
        if timed_out:
            labels.append(f"초안 생성 시간 초과 {len(timed_out)}건")
        if performance_timeout:
            labels.append("광고 분석 재시도")
        fallback += "\n다음 행동 · " + " · ".join(labels)
        blocks.append(_section("다음 행동"))
        if performance_timeout:
            blocks.append({
                **_section("광고 분석 시간 초과 · 이 브리핑의 광고 분석을 다시 실행할 수 있습니다."),
                "accessory": {
                    "type": "button", "action_id": PERFORMANCE_RETRY_ACTION,
                    "text": {"type": "plain_text", "text": "광고 분석 재시도"},
                    "value": f"{performance_job['brief_id']}:{performance_job['retry_count']}",
                },
            })
        missing_link = False
        for label, candidates in actions:
            blocks.append(_section(f"{label} {len(candidates)}건"))
            for item in candidates[:3]:
                output = item["output"]
                item_title = output.get("keyword") or output.get("title") or output.get("status_summary") or "보류 상세"
                item_title = " ".join(item_title.split())[:180]
                url = permalink(item["work_item_id"]) if permalink else None
                if url:
                    blocks.append({"type": "section", "text": {
                        "type": "mrkdwn", "verbatim": True,
                        "text": f"{escape(item_title, quote=False)} · <{url}|카드 열기>",
                    }})
                else:
                    blocks.append(_section(item_title))
                    missing_link = True
            if len(candidates) > 3:
                blocks.append(_section(f"나머지 {len(candidates) - 3}건은 스레드에서 확인할 수 있습니다."))
        if permalink and missing_link:
            blocks.append(_section("링크가 없는 항목은 이 스레드에서 확인할 수 있습니다."))
        available = 50 - len(blocks)
        overflow = len(timed_out) > available
        if overflow:
            available -= 1
        for keyword, handoff in timed_out[:available]:
            blocks.append({
                **_section(f"{' '.join(keyword.split())[:180]} · 초안 생성 시간 초과"),
                "accessory": {
                    "type": "button", "action_id": DRAFT_RETRY_ACTION,
                    "text": {"type": "plain_text", "text": "초안 재시도"},
                    "value": f"{handoff['handoff_id']}:{handoff['claim_token']}",
                },
            })
        if overflow:
            blocks.append(_section("표시된 재시도를 등록하면 나머지 시간 초과 작업을 확인할 수 있습니다."))
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
        return "\n".join(filter(None, [
            "김도현 · 광고 제안 보류", output["reason"], output.get("next_action"),
        ]))
    if output.get("historical_summary"):
        return f"김도현 · {output.get('title', '광고 성과 요약')}\n{output['reason']}"
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
        keyword_approval_mode=workflow.KeywordApprovalMode.LEGAL_BEFORE_DRAFT, ga4_fetch=None, ads_fetch=None,
    ):
        super().__init__(mapping, db_path)
        self.ga4_fetch = ga4_fetch
        self.ads_fetch = ads_fetch
        self.team = TeamBriefs(
            db_path, mapping.workspace_id, channel_id, approval_mode,
            keyword_approval_mode=keyword_approval_mode,
        )

    @property
    def weekly_enabled(self):
        return self.ga4_fetch is not None and self.ads_fetch is not None

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
        brief = self.team.get_brief(brief_id)
        if brief is None:
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
        if "reported_impressions" in output:
            detail = f"보고 기간 광고 노출 {output['reported_impressions']:,}회\n" + detail
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
            "card_title": (
                "윤서진 · 키워드 후보 · 실제 검색어"
                if brief["tracking_scenario"] == "imported" else "윤서진 · 키워드 후보 · 합성 데이터"
            ),
            "detail": detail,
        }

    def handle_command(self, *, ack, command, respond, client) -> None:
        ack()
        try:
            actor = self._actor(command["team_id"], command["channel_id"], command["user_id"])
            if not actor.roles & REQUESTOR_ROLES or not command["trigger_id"]:
                raise SlackConfigurationError("team brief request role is missing")
        except (KeyError, SlackConfigurationError):
            respond(response_type="ephemeral", text="지정된 채널과 팀 브리핑 요청 권한을 확인해 주세요.")
            return

        words = command.get("text", "").strip().lower().split()
        if words == ["brief", "ga4"] and self.ga4_fetch is None:
            respond(response_type="ephemeral", text="GA4 API 입력이 비활성화되어 있습니다. /hermes-team brief csv를 사용해 주세요.")
            return
        if words == ["brief", "weekly"] and not self.weekly_enabled:
            fallback = brief_input.WEEKLY_FALLBACK if self.ga4_fetch else "/hermes-team brief csv를 사용해 주세요."
            respond(response_type="ephemeral", text=f"주간 API 브리핑 연결을 사용할 수 없습니다. {fallback}")
            return
        if len(words) == 2 and words[0] == "brief" and words[1] in brief_input.MODES:
            try:
                client.views_open(trigger_id=command["trigger_id"], view=brief_input.make_view(words[1], command["channel_id"]))
            except SlackClientError as exc:
                LOGGER.warning("Brief input modal failed error_type=%s", type(exc).__name__)
                hint = " CSV 첨부에는 Hermes 앱의 files:read 권한이 필요합니다." if words[1] in {"csv", "ga4"} else ""
                respond(response_type="ephemeral", text="입력창을 열지 못했습니다." + hint)
            return
        if words != ["brief"] and (len(words) != 2 or words[0] != "brief" or words[1] not in BRIEF_SCENARIOS):
            scenarios = "|".join(BRIEF_SCENARIOS)
            ga4 = "|ga4" if self.ga4_fetch is not None else ""
            weekly = "|weekly" if self.weekly_enabled else ""
            respond(response_type="ephemeral", text=f"/hermes-team brief [csv|text{ga4}{weekly}|{scenarios}]")
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

    def handle_input_submission(self, *, ack, body, client) -> None:
        ack()
        try:
            view = body["view"]
            metadata = json.loads(view["private_metadata"])
            mode, channel = metadata["mode"], metadata["channel"]
            user = body["user"]["id"]
            actor = self._actor(body["team"]["id"], channel, user)
            input_id = str(uuid.UUID(metadata["id"]))
            if not actor.roles & REQUESTOR_ROLES or mode not in brief_input.MODES:
                return
        except (SlackConfigurationError, KeyError, ValueError, TypeError):
            return

        blocks = None
        try:
            data = brief_input.read_view(client, view, user, mode, ga4_fetch=self.ga4_fetch, ads_fetch=self.ads_fetch)
            data = self.team.save_input(input_id, actor.actor_id, data)
            issue = brief_inputs.hold_reason(data)
            text = f"{brief_input.MODES[mode]} 검증\n{_input_summary(data)}"
            if issue:
                text += f"\n광고 분석 보류 · {issue}"
            elif mode == "weekly":
                text += "\n광고 성과 비교 · 게시 가능"
            if not data["keywords"]:
                text += "\n키워드 분석 대기 · 검색어 보고서 필요"
            text += "\n아래에서 30분 내 확인하면 PM 브리핑을 시작합니다."
            blocks = [_section(text), {"type": "actions", "elements": [{
                "type": "button", "action_id": brief_input.CONFIRM, "value": input_id,
                "text": {"type": "plain_text", "text": "확인하고 게시" if mode == "weekly" else "확인하고 브리핑 시작"},
                "style": "primary",
            }]}]
        except brief_inputs.InputError as exc:
            if mode == "weekly":
                fallback = brief_input.WEEKLY_FALLBACK if self.ga4_fetch else "/hermes-team brief csv를 사용해 주세요."
                text = f"{exc}\n{fallback}"
            else:
                text = f"{exc}\n/hermes-team brief {mode}에서 수정해 다시 제출해 주세요."
        except (sqlite3.Error, workflow.NotFoundError, KeyError, ValueError, TypeError) as exc:
            LOGGER.warning("Brief input failed error_type=%s", type(exc).__name__)
            text = "검증을 완료하지 못했습니다. 잠시 후 다시 제출해 주세요."
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=escape(text, quote=False),
                                      **({"blocks": blocks} if blocks else {}))
        except SlackClientError as exc:
            LOGGER.warning("Brief preview delivery failed error_type=%s", type(exc).__name__)

    def handle_input_confirmation(self, *, ack, body, action, respond) -> None:
        ack()
        try:
            actor = self._actor(body["team"]["id"], body["channel"]["id"], body["user"]["id"])
            if not actor.roles & REQUESTOR_ROLES:
                raise workflow.AuthorizationError("team brief request role is missing")
            brief_id = self.team.confirm_input(
                str(uuid.UUID(action["value"])), actor.actor_id, allow_ga4=self.ga4_fetch is not None,
                allow_weekly=self.weekly_enabled,
            )
            text = f"실제 데이터 브리핑을 접수했습니다. 결과는 이 채널의 PM 스레드에 게시합니다. · {brief_id}"
        except brief_inputs.InputError as exc:
            text = str(exc)
        except (workflow.NotFoundError, workflow.AuthorizationError, SlackConfigurationError, KeyError, TypeError, ValueError):
            text = "본인이 제출한 유효한 미리보기에서 다시 확인해 주세요."
        except sqlite3.Error as exc:
            LOGGER.warning("Brief input confirmation failed error_type=%s", type(exc).__name__)
            text = "브리핑을 접수하지 못했습니다. 잠시 후 버튼을 다시 눌러 주세요."
        respond(response_type="ephemeral", replace_original=False, text=text)

    def handle_draft_retry(self, *, ack, body: dict, action: dict, respond) -> None:
        ack()
        try:
            team_id = body["team"]["id"]
            channel_id = body["channel"]["id"]
            actor = self._actor(team_id, channel_id, body["user"]["id"])
            handoff_id, claim_token = (str(uuid.UUID(part)) for part in action["value"].split(":"))
            queued = workflow.retry_team_draft_timeout(
                actor=actor, team_id=team_id, channel_id=channel_id,
                message_ts=body["message"]["ts"], handoff_id=handoff_id,
                claim_token=claim_token, db_path=self.db_path,
            )
            feedback = (
                "기존 승인으로 초안 재시도를 등록했습니다. 결과는 같은 스레드에서 확인할 수 있습니다."
                if queued else "현재 카드에서 이 초안을 다시 시도할 수 없습니다. 최신 PM 카드 상태를 확인해 주세요."
            )
        except (SlackConfigurationError, workflow.AuthorizationError):
            feedback = "이 채널에서 초안을 재시도하려면 키워드 승인 권한이 필요합니다."
        except (KeyError, ValueError, TypeError, AttributeError):
            feedback = "최신 PM 카드에서 초안 재시도를 눌러 주세요."
        except sqlite3.Error as exc:
            LOGGER.warning("Brief draft retry enqueue failed error_type=%s", type(exc).__name__)
            feedback = "재시도를 등록하지 못했습니다. 잠시 후 다시 시도해 주세요."
        respond(response_type="ephemeral", replace_original=False, text=feedback)

    def handle_performance_retry(self, *, ack, body: dict, action: dict, respond) -> None:
        ack()
        try:
            actor = self._actor(body["team"]["id"], body["channel"]["id"], body["user"]["id"])
            if not actor.roles & REQUESTOR_ROLES:
                raise workflow.AuthorizationError("team brief request role is missing")
            raw_id, raw_count = action["value"].split(":")
            queued = self.team.retry_performance_timeout(
                brief_id=str(uuid.UUID(raw_id)), message_ts=body["message"]["ts"],
                retry_count=int(raw_count), actor=actor,
            )
            feedback = (
                "광고 분석 재시도를 등록했습니다. 같은 스레드에서 결과를 확인할 수 있습니다."
                if queued else "재시도할 수 있는 최신 카드인지 확인해 주세요. 브리핑 요청자나 광고 승인 권한자가 재시도할 수 있습니다."
            )
        except (SlackConfigurationError, workflow.AuthorizationError):
            feedback = "이 채널에서 광고 분석을 재시도할 권한이 필요합니다."
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            feedback = "최신 PM 카드에서 광고 분석 재시도를 눌러 주세요."
        except sqlite3.Error as exc:
            LOGGER.warning("Brief performance retry enqueue failed error_type=%s", type(exc).__name__)
            feedback = "재시도를 등록하지 못했습니다. 잠시 후 다시 시도해 주세요."
        respond(response_type="ephemeral", replace_original=False, text=feedback)

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
        imported = self.team.imported_input(brief_id) if brief["tracking_scenario"] == "imported" else None
        if imported is None and brief["tracking_scenario"] == "imported":
            raise workflow.NotFoundError("real-data input is missing")
        jobs, items, handoffs = self.team.snapshot(brief_id)
        thread_ts = brief["message_ts"]
        new_report = thread_ts is None
        if new_report:
            text, blocks = _report(jobs, items, handoffs, imported=imported)
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
                    "card_title": (
                        "한도윤 · 검토용 콘텐츠 초안 · 실제 검색어"
                        if imported else "한도윤 · 검토용 콘텐츠 초안 · 합성 데이터"
                    ),
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
                imported=imported,
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
