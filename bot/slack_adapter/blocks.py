"""Pure Block Kit renderers for Phase 3 test messages and modals."""

from __future__ import annotations

import datetime as dt
import json
from typing import Iterable
from zoneinfo import ZoneInfo

from .fixtures import SlackTestFixture


STATE_LABELS = {
    "pending": "대기 중",
    "approved": "모의 승인 완료",
    "rejected": "모의 반려",
    "revision_requested": "모의 수정 요청",
    "acknowledged": "모의 확인 완료",
    "expired": "만료",
}


def seoul_time_label(timestamp: str) -> str:
    local = dt.datetime.fromisoformat(timestamp).astimezone(ZoneInfo("Asia/Seoul"))
    weekday = "월화수목금토일"[local.weekday()]
    period = "오전" if local.hour < 12 else "오후"
    return (f"{local.year}년 {local.month}월 {local.day}일({weekday}) "
            f"{period} {local.hour % 12 or 12}:{local.minute:02d} (한국 시간)")


def test_card_text(session: dict, fixture: SlackTestFixture, *, live_gate: bool = False) -> str:
    prefix = "PHASE 3 LIVE GATE: PASS — " if live_gate else ""
    return (
        f"{prefix}PHASE 3 TEST — 실제 승인 아님 — "
        f"{fixture.employee_name} — {STATE_LABELS[session['state']]}"
    )


def render_test_card(
    session: dict,
    fixture: SlackTestFixture,
    *,
    live_gate: bool = False,
) -> list[dict]:
    completed = session.get("completed_roles", [])
    required = session["required_roles"]
    remaining = session.get("remaining_roles", required)
    progress = f"{len(completed)}/{len(required)}"
    title = "PHASE 3 LIVE GATE: PASS" if live_gate else "PHASE 3 TEST"
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title, "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":warning: *실제 승인 아님* · Phase 1 승인과 외부 실행은 변경되지 않습니다.",
                }
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{fixture.employee_name}*\n{fixture.summary}",
            },
            "fields": [
                {"type": "mrkdwn", "text": f"*Fixture*\n`{fixture.fixture_id}`"},
                {"type": "mrkdwn", "text": f"*위험도*\n`{fixture.risk_level}`"},
                {"type": "mrkdwn", "text": f"*상태*\n{STATE_LABELS[session['state']]}"},
                {"type": "mrkdwn", "text": f"*모의 진행률*\n{progress}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*필요 역할*\n"
                    + ", ".join(f"`{role}`" for role in required)
                    + "\n*남은 역할*\n"
                    + (", ".join(f"`{role}`" for role in remaining) or "없음")
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Correlation `{session['session_id']}` · 실행 `{session['created_at']}`"
                        f" · 만료 `{session['expires_at']}`"
                    ),
                }
            ],
        },
    ]
    if session["state"] == "pending":
        if fixture.interaction_kind == "acknowledgement":
            elements = [
                {
                    "type": "button",
                    "action_id": "phase3_acknowledge",
                    "text": {"type": "plain_text", "text": "확인"},
                    "style": "primary",
                    "value": session["session_id"],
                }
            ]
        else:
            elements = [
                {
                    "type": "button",
                    "action_id": "phase3_approve",
                    "text": {"type": "plain_text", "text": "승인"},
                    "style": "primary",
                    "value": session["session_id"],
                },
                {
                    "type": "button",
                    "action_id": "phase3_reject",
                    "text": {"type": "plain_text", "text": "반려"},
                    "style": "danger",
                    "value": session["session_id"],
                },
                {
                    "type": "button",
                    "action_id": "phase3_revision",
                    "text": {"type": "plain_text", "text": "수정 요청"},
                    "value": session["session_id"],
                },
            ]
        blocks.append({"type": "actions", "block_id": "phase3_actions", "elements": elements})
    if len(blocks) > 50:
        raise ValueError("Slack messages may not exceed 50 blocks")
    return blocks


def render_action_modal(
    *,
    session_id: str,
    action_type: str,
    eligible_roles: Iterable[str],
) -> dict:
    roles = sorted(set(eligible_roles))
    if not roles:
        raise ValueError("at least one eligible role is required")
    labels = {
        "approved": ("모의 승인", "승인"),
        "rejected": ("모의 반려", "반려"),
        "revision_requested": ("모의 수정 요청", "요청"),
        "acknowledged": ("모의 확인", "확인"),
    }
    if action_type not in labels:
        raise ValueError("unsupported modal action")
    title, submit = labels[action_type]
    blocks: list[dict] = [
        {
            "type": "input",
            "block_id": "acting_role_block",
            "label": {"type": "plain_text", "text": "담당 역할"},
            "element": {
                "type": "static_select",
                "action_id": "acting_role",
                "options": [
                    {
                        "text": {"type": "plain_text", "text": role},
                        "value": role,
                    }
                    for role in roles
                ],
            },
        }
    ]
    if action_type in {"rejected", "revision_requested"}:
        blocks.append(
            {
                "type": "input",
                "block_id": "reason_block",
                "label": {"type": "plain_text", "text": "사유"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason",
                    "multiline": True,
                },
            }
        )
    return {
        "type": "modal",
        "callback_id": f"phase3_{action_type}_modal",
        "private_metadata": json.dumps({"session_id": session_id}),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": submit},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks,
    }
