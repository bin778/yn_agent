"""Slack input views and private CSV retrieval for real-data team briefs."""

import json
import uuid
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from slack_sdk.errors import SlackClientError

from ..brief_inputs import FIELDS, InputError, normalize

MODAL = "hermes_team_real_input"
CONFIRM = "hermes_team_real_confirm"
MODES = {"csv": "실제 집계 CSV", "text": "직접 입력"}
MAX_CSV_BYTES = 1024 * 1024


def _plain(text):
    return {"type": "plain_text", "text": text}


def _input(key, label, element, *, optional=False, hint=None):
    return {
        "type": "input", "block_id": key, "label": _plain(label),
        "optional": optional, "element": {"action_id": key, **element},
        **({"hint": _plain(hint)} if hint else {}),
    }


def make_view(mode, channel):
    options = [{"text": _plain(label), "value": value} for value, label in (
        ("unknown", "미확인"), ("complete", "두 출처 모두 집계 완료 확인"),
    )]
    blocks = [
        _input("ads_source", "광고 플랫폼", {
            "type": "plain_text_input", "placeholder": _plain("Google Ads, 네이버 검색광고 등"), "max_length": 80,
        }),
        _input("start", "보고 시작일", {"type": "datepicker"}),
        _input("end", "보고 종료일", {"type": "datepicker"}),
        _input("ga_timezone", "GA4 보고서 시간대", {
            "type": "plain_text_input", "placeholder": _plain("Asia/Seoul"), "max_length": 64,
        }),
        _input("ads_timezone", "광고 보고서 시간대", {
            "type": "plain_text_input", "placeholder": _plain("Asia/Seoul"), "max_length": 64,
        }),
        _input("complete", "원본 집계 상태", {
            "type": "static_select", "options": options, "initial_option": options[0],
        }, hint="직전 7일과 비교할 때는 두 기간의 집계 상태를 확인해 주세요."),
        _input("collected_through", "원본 집계 시각", {
            "type": "plain_text_input", "placeholder": _plain("2026-11-21T09:00:00+09:00"), "max_length": 40,
        }, optional=True),
    ]
    for key, (label, columns) in FIELDS.items():
        if mode == "csv":
            element = {"type": "file_input", "filetypes": ["csv"], "max_files": 1}
        else:
            element = {
                "type": "plain_text_input", "multiline": True, "max_length": 3000,
                "placeholder": _plain(",".join(columns)),
            }
        hint = "필요한 열 · " + " / ".join(columns)
        if key == "events":
            hint += " · form_submit, home_kakao_click"
        if key != "keywords":
            hint += " · 7일 비교에는 직전 7일도 같은 보고서에 포함"
        blocks.append(_input(key, label, element, optional=key == "keywords", hint=hint))
    return {
        "type": "modal", "callback_id": MODAL, "title": _plain(MODES[mode]),
        "submit": _plain("검증하기"), "close": _plain("닫기"), "blocks": blocks,
        "private_metadata": json.dumps({"mode": mode, "channel": channel, "id": str(uuid.uuid4())}),
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_csv(client, file_id, user):
    file = client.files_info(file=file_id)["file"]
    if (file.get("user") != user or file.get("is_external")
            or not file.get("name", "").lower().endswith(".csv")):
        raise InputError("본인이 이 입력창에 첨부한 CSV를 사용해 주세요.")
    url = file.get("url_private_download") or file.get("url_private") or ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "files.slack.com":
        raise InputError("Slack에서 첨부 파일을 읽을 수 없습니다. 파일을 다시 첨부해 주세요.")
    if file.get("size", 0) > MAX_CSV_BYTES:
        raise InputError("CSV 한 개의 크기는 최대 1MB입니다.")
    request = Request(url, headers={"Authorization": f"Bearer {client.token}"})
    with build_opener(_NoRedirect()).open(request, timeout=15) as response:
        data = response.read(MAX_CSV_BYTES + 1)
    if len(data) > MAX_CSV_BYTES:
        raise InputError("CSV 한 개의 크기는 최대 1MB입니다.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp949")
        except UnicodeDecodeError:
            raise InputError("CSV를 UTF-8로 저장해 다시 첨부해 주세요.") from None


def read_view(client, view, user, mode):
    values = view["state"]["values"]

    def state(key):
        return values.get(key, {}).get(key, {})

    tables = {}
    for key, (label, _) in FIELDS.items():
        value = state(key)
        try:
            if mode == "csv":
                files = value.get("files") or []
                if len(files) > 1:
                    raise InputError(f"{label} CSV는 한 개만 첨부해 주세요.")
                file_id = (files[0]["id"] if isinstance(files[0], dict) else files[0]) if files else None
                content = _download_csv(client, file_id, user) if file_id else ""
            else:
                content = value.get("value") or ""
        except (SlackClientError, URLError, TimeoutError) as exc:
            if getattr(exc, "response", {}).get("error") == "missing_scope":
                raise InputError("Hermes 앱의 files:read 권한과 재설치 상태를 확인해 주세요.") from None
            raise InputError(f"{label} CSV를 읽을 수 없습니다. 파일을 다시 첨부해 주세요.") from None
        if content.strip():
            tables[key] = content
        elif key != "keywords":
            raise InputError(f"{label} 자료를 제출해 주세요.")
    return normalize(
        tables, start=state("start").get("selected_date") or "", end=state("end").get("selected_date") or "",
        ga_timezone=state("ga_timezone").get("value") or "", ads_timezone=state("ads_timezone").get("value") or "",
        complete=(state("complete").get("selected_option") or {}).get("value") == "complete",
        collected_through=state("collected_through").get("value") or "", mode=mode,
        ads_source=state("ads_source").get("value") or "",
    )
