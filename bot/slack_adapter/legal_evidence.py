"""Review official legal sources for a specific Slack draft version."""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING

from .. import workflow
from .blocks import seoul_time_label
from .config import SlackConfigurationError

if TYPE_CHECKING:
    from .drafts import SlackDrafts

OPEN = "hermes_legal_evidence"
NAVIGATE = "hermes_legal_evidence_navigate"
SAVE = "hermes_legal_evidence_save"
PAGE_SIZE = 8
DECISION_LABELS = {"confirmed": "확인 완료", "needs_revision": "수정 필요"}
STATUS = {**DECISION_LABELS, "noted": "근거 기록"}
LOGGER = logging.getLogger(__name__)
ACCESS_ERROR = "검토 권한과 초안의 최신 버전을 확인해 주세요."


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _section(text: str) -> dict:
    return {"type": "section", "text": _plain(text)}


def _button(label: str, value: str) -> dict:
    return {"type": "button", "text": _plain(label), "action_id": NAVIGATE, "value": value}


def _modal(metadata: dict, blocks: list[dict]) -> dict:
    return {
        "type": "modal", "title": _plain("법률 근거 확인"),
        "close": _plain("닫기"), "private_metadata": json.dumps(metadata), "blocks": blocks,
    }


def _list_view(metadata: dict, state: dict, page: int) -> dict:
    claims, reviews = state["legal_claims"], state["legal_reviews"]
    blocks = [
        _section(f"초안 v{state['version']} · {state['legal_review_label']}"),
        _section("법률 근거 확인은 권고 사항입니다. 검토 결과나 근거만 기록할 수 있습니다."),
    ]
    start = page * PAGE_SIZE
    for index, claim in enumerate(claims[start:start + PAGE_SIZE], start):
        review = reviews.get(index)
        label = STATUS[review["status"]] if review else "기록 없음"
        blocks.append({
            **_section(f"{index + 1}. {label}\n{claim[:2750]}"),
            "accessory": _button("검토 보기", f"claim:{index}"),
        })
    if page:
        blocks.append({"type": "actions", "elements": [_button("이전", f"page:{page - 1}")]})
    if start + PAGE_SIZE < len(claims):
        blocks.append({"type": "actions", "elements": [_button("다음", f"page:{page + 1}")]})
    return _modal(metadata, blocks)


def _input(key: str, label: str, value: str, limit: int, *, optional: bool = False) -> dict:
    element = {"type": "plain_text_input", "action_id": "value", "max_length": limit}
    if value:
        element["initial_value"] = value
    return {
        "type": "input", "block_id": key, "label": _plain(label),
        "optional": optional, "element": element,
    }


def _detail_view(metadata: dict, state: dict, index: int, *, editable: bool) -> dict:
    claim = state["legal_claims"][index]
    review = state["legal_reviews"].get(index, {})
    blocks = [_section(f"초안 v{state['version']} · 문장 {index + 1}")]
    blocks.extend(_section(claim[pos:pos + 2800]) for pos in range(0, len(claim), 2800))
    if review:
        reviewer = review["reviewed_by"].rsplit(":", 1)[-1]
        blocks.append(_section(
            f"{STATUS[review['status']]} · {reviewer} · {seoul_time_label(review['reviewed_at'])}"
        ))
    if not editable and review:
        blocks.extend(_section(review[key]) for key in ("source_reference", "source_url", "note") if review[key])
    if not editable and not review:
        blocks.append(_section("아직 검토 결과가 없습니다."))
    blocks.append({"type": "actions", "elements": [_button("목록으로", f"page:{index // PAGE_SIZE}")]})
    view = _modal({**metadata, "claim_index": index}, blocks)
    if editable:
        options = [{"text": _plain(label), "value": key} for key, label in DECISION_LABELS.items()]
        result = {"type": "static_select", "action_id": "value", "options": options}
        if review.get("status") in DECISION_LABELS:
            result["initial_option"] = next(option for option in options if option["value"] == review["status"])
        blocks.extend([
            {"type": "input", "block_id": "result", "label": _plain("검토 결과"),
             "optional": True, "element": result},
            _input("source_url", "공식 원문 URL", review.get("source_url", ""), 1800, optional=True),
            _input("source_reference", "법령명·조문이나 판례 정보",
                   review.get("source_reference", ""), 300, optional=True),
            _input("note", "검토 메모", review.get("note", ""), 1200, optional=True),
        ])
        blocks[-1]["element"]["multiline"] = True
        view.update(callback_id=SAVE, submit=_plain("검토 저장"))
    return view


def register_legal_evidence_handlers(app, drafts: SlackDrafts) -> None:
    def load_state(body: dict, metadata: dict):
        team_id = (body.get("team") or {}).get("id", "")
        actor = drafts._actor(team_id, metadata["channel_id"], body["user"]["id"])
        state = drafts._bound_state(team_id, metadata["channel_id"], metadata["message_ts"],
                                    metadata["request_id"], metadata["version"])
        if not state["legal_claims"] or not actor.roles.intersection({"content_approver", "legal_compliance"}):
            raise workflow.AuthorizationError("legal evidence review is unavailable")
        requires_legal = any(item["role"] == "legal_compliance" for item in state["requirements"])
        role = "legal_compliance" if requires_legal else "content_approver"
        editable = (role in actor.roles and state["status"] == "pending" and not state["decisions"]
                    and workflow.utc_now() < dt.datetime.fromisoformat(state["expires_at"]))
        return actor, state, editable

    @app.action(OPEN)
    def open_review(ack, body, action, respond, client):
        ack()
        try:
            request_id, version = action["value"].split(":")
            metadata = {"channel_id": body["channel"]["id"], "message_ts": body["message"]["ts"],
                        "request_id": request_id, "version": int(version)}
            _, state, _ = load_state(body, metadata)
            client.views_open(trigger_id=body["trigger_id"], view=_list_view(metadata, state, 0))
        except (KeyError, ValueError, SlackConfigurationError, workflow.WorkflowError):
            respond(response_type="ephemeral", replace_original=False, text=ACCESS_ERROR)
        except Exception as exc:
            LOGGER.warning("Slack legal evidence open failed error_type=%s", type(exc).__name__)
            respond(response_type="ephemeral", replace_original=False, text="근거 검토 창을 열지 못했습니다. 다시 시도해 주세요.")

    @app.action(NAVIGATE)
    def navigate(ack, body, action, client):
        ack()
        metadata = {}
        try:
            view = body["view"]
            metadata = json.loads(view["private_metadata"])
            _, state, editable = load_state(body, metadata)
            kind, raw_index = action["value"].split(":")
            index = int(raw_index)
            if kind == "page" and 0 <= index * PAGE_SIZE < len(state["legal_claims"]):
                next_view = _list_view(metadata, state, index)
            elif kind == "claim" and 0 <= index < len(state["legal_claims"]):
                next_view = _detail_view(metadata, state, index, editable=editable)
            else:
                raise ValueError("invalid evidence selection")
        except (KeyError, ValueError, SlackConfigurationError, workflow.WorkflowError):
            next_view = _modal(metadata, [_section(ACCESS_ERROR)])
        view = body.get("view") or {}
        if view.get("id"):
            revision = {"hash": view["hash"]} if view.get("hash") else {}
            client.views_update(view_id=view["id"], view=next_view, **revision)

    @app.view(SAVE)
    def save(ack, body, client):
        try:
            view = body["view"]
            metadata = json.loads(view["private_metadata"])
            actor, _, editable = load_state(body, metadata)
            if not editable:
                raise workflow.AuthorizationError("evidence cannot be updated")
            fields = view["state"]["values"]
            selection = fields.get("result", {}).get("value", {}).get("selected_option") or {}
            status = selection.get("value")
            index = metadata["claim_index"]
            workflow.record_legal_review(
                request_id=metadata["request_id"], expected_version=metadata["version"],
                claim_index=index, actor=actor, status=status, db_path=drafts.db_path,
                source_url=fields.get("source_url", {}).get("value", {}).get("value") or "",
                source_reference=fields.get("source_reference", {}).get("value", {}).get("value") or "",
                note=fields.get("note", {}).get("value", {}).get("value") or "",
            )
        except workflow.LegalEvidenceError as exc:
            ack(response_action="errors", errors={"result": str(exc)})
            return
        except (KeyError, TypeError, ValueError, SlackConfigurationError, workflow.WorkflowError):
            ack(response_action="errors", errors={"result": ACCESS_ERROR})
            return
        except Exception as exc:
            LOGGER.warning("Slack legal evidence save failed error_type=%s", type(exc).__name__)
            ack(response_action="errors", errors={"result": "검토 결과를 저장하지 못했습니다. 다시 시도해 주세요."})
            return

        try:
            state = drafts._state(metadata["request_id"])
            result = _list_view(metadata, state, index // PAGE_SIZE)
        except Exception as exc:
            LOGGER.warning("Slack legal evidence list refresh failed error_type=%s", type(exc).__name__)
            result = _modal(metadata, [_section("검토를 저장했습니다. 승인 카드에서 다시 확인해 주세요.")])
        ack(response_action="update", view=result)
        try:
            with drafts.publish_lock:
                state = drafts._bound_state(drafts.workspace_id, metadata["channel_id"], metadata["message_ts"],
                                            metadata["request_id"], metadata["version"])
                drafts._update(client, metadata["channel_id"], metadata["message_ts"], state)
        except Exception as exc:
            LOGGER.warning("Slack legal evidence card refresh failed error_type=%s", type(exc).__name__)
