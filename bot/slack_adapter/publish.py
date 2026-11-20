"""Slack modals for the approved content queue and manual publication history."""

from __future__ import annotations

import logging
from pathlib import Path

from ..publication import PUBLISHER_ROLE, URL_LIMIT, PublicationError, PublicationQueue
from ..workflow import ActorContext
from .blocks import seoul_time_label
from .config import SlackConfigurationError, SlackIdentityProvider, SlackMapping

COMMAND = "/hermes-publish"
OPEN_ENTRY = "hermes_publish_open_entry"
CHANGE_PAGE = "hermes_publish_page"
RECORD_VIEW = "hermes_publish_record"
URL_FIELD = "publication_url"
PAGE_SIZE = 8
STATUS_LABELS = {"pending": "발행 대기", "completed": "발행 기록"}

LOGGER = logging.getLogger(__name__)
ACCESS_ERROR = "초안 요청 채널에서 발행 담당자 권한이 있는 계정으로 열어 주세요."


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _section(text: str) -> dict:
    return {"type": "section", "text": _plain(text)}


def _button(text: str, action_id: str, value: str) -> dict:
    return {"type": "button", "text": _plain(text), "action_id": action_id, "value": value}


def _modal(title: str, scope: str, blocks: list[dict]) -> dict:
    return {"type": "modal", "title": _plain(title), "private_metadata": scope,
            "close": _plain("닫기"), "blocks": blocks}


def _entry_summary(entry: dict, title_limit: int = 200) -> str:
    title = entry["output"]["title"]
    if len(title) > title_limit:
        title = title[:title_limit - 1] + "…"
    status = "발행 대기" if entry["status"] == "pending" else "발행 완료"
    return (f"{title} · v{entry['version']}\n"
            f"{status} · 승인 {seoul_time_label(entry['approved_at'])}")


def _list_view(queue: PublicationQueue, scope: str, status: str, page: int) -> dict:
    entries = queue.list(status, limit=PAGE_SIZE + 1, offset=PAGE_SIZE * page)
    other = "completed" if status == "pending" else "pending"
    blocks = [
        _section(f"{STATUS_LABELS[status]} · {page + 1}페이지"),
        {"type": "actions", "elements": [
            _button(STATUS_LABELS[other], CHANGE_PAGE, f"{other}:0"),
        ]},
    ]
    for entry in entries[:PAGE_SIZE]:
        blocks.append({
            **_section(_entry_summary(entry)),
            "accessory": _button("승인본 보기", OPEN_ENTRY, entry["handoff_id"]),
        })
    if not entries:
        blocks.append(_section("표시할 항목이 없습니다."))

    # Separate action blocks let previous and next share the same action handler.
    if page > 0:
        blocks.append({"type": "actions", "elements": [
            _button("이전", CHANGE_PAGE, f"{status}:{page - 1}"),
        ]})
    if len(entries) > PAGE_SIZE:
        blocks.append({"type": "actions", "elements": [
            _button("다음", CHANGE_PAGE, f"{status}:{page + 1}"),
        ]})
    return _modal("Hermes 발행 대기열", scope, blocks)


def _record_summary(entry: dict) -> str:
    slack_user = entry["recorded_by"].rsplit(":", 1)[-1]
    return (f"기록 담당자 {slack_user}\n"
            f"기록 시간 {seoul_time_label(entry['recorded_at'])}\n"
            f"발행 URL {entry['publication_url']}")


def _entry_view(entry: dict, scope: str) -> dict:
    pending = entry["status"] == "pending"
    output = entry["output"]
    markdown = f"# {output['title']}\n\n{output['hook']}\n\n{output['body']}"
    blocks = [_section(_entry_summary(entry))]
    if not pending:
        blocks.append(_section(_record_summary(entry)))
    blocks.extend([
        {"type": "divider"},
        _section("아래 승인본을 복사해 주세요."),
    ])
    for start in range(0, len(markdown), 2800):
        blocks.append(_section(markdown[start:start + 2800]))
    view = _modal("승인된 콘텐츠", scope, blocks)
    if pending:
        view["callback_id"] = RECORD_VIEW
        view["private_metadata"] = f"{scope}:{entry['handoff_id']}"
        view["submit"] = _plain("발행 결과 저장")
        blocks.append({
            "type": "input", "block_id": URL_FIELD,
            "label": _plain("실제 발행 URL"),
            "hint": _plain("CMS에서 직접 발행한 뒤 공개 페이지 주소를 입력해 주세요."),
            "element": {"type": "plain_text_input", "action_id": "value", "max_length": URL_LIMIT},
        })
    return view


def register_publish_handlers(app, mapping: SlackMapping, db_path: Path, channel_id: str) -> None:
    identity = SlackIdentityProvider(mapping)
    queue = PublicationQueue(db_path, mapping.workspace_id, channel_id)
    scope = f"{mapping.workspace_id}:{channel_id}"

    def publisher(team_id: str, user_id: str) -> ActorContext:
        actor = identity.actor_for(team_id, user_id)
        if PUBLISHER_ROLE not in actor.roles:
            raise SlackConfigurationError("Slack user is not a content publisher")
        return ActorContext.verified_actor(actor.actor_id, actor.roles, platform="slack")

    def require_modal_access(body: dict) -> ActorContext:
        return publisher((body.get("team") or {}).get("id", ""), body["user"]["id"])

    def show_action_error(client, body: dict, message: str) -> None:
        try:
            client.views_push(trigger_id=body["trigger_id"],
                              view=_modal("발행 대기열 안내", scope, [_section(message)]))
        except Exception as exc:
            LOGGER.warning("Slack publication error modal failed error_type=%s", type(exc).__name__)

    @app.command(COMMAND)
    def open_queue(ack, command, respond, client):
        ack()
        try:
            publisher(command["team_id"], command["user_id"])
            if command["channel_id"] != channel_id:
                raise SlackConfigurationError("not the draft channel")
            client.views_open(trigger_id=command["trigger_id"],
                              view=_list_view(queue, scope, "pending", 0))
        except (KeyError, SlackConfigurationError):
            respond(response_type="ephemeral", text=ACCESS_ERROR)
        except Exception as exc:
            LOGGER.warning("Slack publication queue failed error_type=%s", type(exc).__name__)
            respond(response_type="ephemeral", text="발행 대기열을 열지 못했습니다. 다시 시도해 주세요.")

    @app.action(CHANGE_PAGE)
    @app.action(OPEN_ENTRY)
    def navigate(ack, body, action, client):
        ack()
        try:
            require_modal_access(body)
            view = body["view"]
            if view["private_metadata"] != scope:
                raise SlackConfigurationError("not the draft channel")
            if action["action_id"] == OPEN_ENTRY:
                client.views_push(trigger_id=body["trigger_id"],
                                  view=_entry_view(queue.get(action["value"]), scope))
                return
            status, raw_page = action["value"].split(":")
            page = int(raw_page)
            if status not in STATUS_LABELS or not 0 <= page <= 1_000_000:
                raise ValueError("invalid queue page")
            revision = {"hash": view["hash"]} if view.get("hash") else {}
            client.views_update(view_id=view["id"], view=_list_view(queue, scope, status, page),
                                **revision)
        except SlackConfigurationError:
            show_action_error(client, body, ACCESS_ERROR)
        except PublicationError as exc:
            show_action_error(client, body, str(exc))
        except Exception as exc:
            LOGGER.warning("Slack publication navigation failed error_type=%s", type(exc).__name__)
            show_action_error(client, body, "목록을 다시 열어 주세요.")

    @app.view(RECORD_VIEW)
    def record_result(ack, body):
        try:
            actor = require_modal_access(body)
            view = body["view"]
            team_id, view_channel, handoff_id = view["private_metadata"].split(":")
            if (team_id, view_channel) != queue.scope:
                raise SlackConfigurationError("not the draft channel")
            url = view["state"]["values"][URL_FIELD]["value"]["value"] or ""
            entry = queue.record(handoff_id, url, actor)
        except SlackConfigurationError:
            ack(response_action="errors", errors={URL_FIELD: ACCESS_ERROR})
        except PublicationError as exc:
            ack(response_action="errors", errors={URL_FIELD: str(exc)})
        except Exception as exc:
            LOGGER.warning("Slack publication record failed error_type=%s", type(exc).__name__)
            ack(response_action="errors", errors={URL_FIELD: "저장하지 못했습니다. 다시 시도해 주세요."})
        else:
            ack(response_action="update", view=_modal("발행 결과 저장 완료", scope, [
                _section(_entry_summary(entry)),
                _section(_record_summary(entry)),
            ]))
