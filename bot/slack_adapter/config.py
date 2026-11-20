"""Fail-closed Slack configuration and platform identity mapping."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


ROLE_KEYS = frozenset(
    {
        "keyword_approver",
        "content_approver",
        "content_publisher",
        "performance_approver",
        "data_owner",
        "legal_compliance",
        "operations_owner",
        "privacy_officer",
        "workflow_admin",
    }
)

PHASE3_QUEUE_KEYS = frozenset(
    {
        "compliance_review.queue",
        "human_review.queue",
        "performance_review.queue",
        "data_assistant.tracking_status",
        "dev_team.queue",
    }
)

_TEAM_ID = re.compile(r"^T[A-Z0-9]+$")
_USER_ID = re.compile(r"^[UW][A-Z0-9]+$")
_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]+$")


class SlackConfigurationError(RuntimeError):
    """Slack configuration is absent, malformed, or outside the allowlist."""


@dataclass(frozen=True)
class SlackSettings:
    bot_token: str
    app_token: str
    mapping_path: Path
    session_ttl_minutes: int = 15

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SlackSettings":
        source = os.environ if env is None else env
        raw_ttl = source.get("SLACK_TEST_SESSION_TTL_MINUTES", "15").strip()
        try:
            ttl = int(raw_ttl)
        except ValueError as exc:
            raise SlackConfigurationError(
                "SLACK_TEST_SESSION_TTL_MINUTES must be a positive integer"
            ) from exc
        if ttl <= 0:
            raise SlackConfigurationError(
                "SLACK_TEST_SESSION_TTL_MINUTES must be a positive integer"
            )
        raw_path = source.get(
            "SLACK_MAPPING_PATH", str(Path.home() / ".hermes" / "slack_mapping.yaml")
        ).strip()
        if not raw_path:
            raise SlackConfigurationError("SLACK_MAPPING_PATH must not be empty")
        return cls(
            bot_token=source.get("SLACK_BOT_TOKEN", "").strip(),
            app_token=source.get("SLACK_APP_TOKEN", "").strip(),
            mapping_path=Path(raw_path).expanduser(),
            session_ttl_minutes=ttl,
        )

    def validate_runtime(self) -> None:
        if not self.bot_token.startswith("xoxb-"):
            raise SlackConfigurationError("SLACK_BOT_TOKEN must be a Bot token (xoxb-)")
        if not self.app_token.startswith("xapp-"):
            raise SlackConfigurationError("SLACK_APP_TOKEN must be an App token (xapp-)")


@dataclass(frozen=True)
class SlackMapping:
    workspace_id: str
    allowed_users: dict[str, frozenset[str]]
    queue_channels: dict[str, str]

    @property
    def development_channel_id(self) -> str:
        return self.queue_channels["dev_team.queue"]


@dataclass(frozen=True)
class SlackActor:
    actor_id: str
    roles: frozenset[str]
    is_admin: bool
    platform: str = "slack"


class SlackIdentityProvider:
    def __init__(self, mapping: SlackMapping):
        self.mapping = mapping

    def actor_for(self, team_id: str, user_id: str) -> SlackActor:
        if team_id != self.mapping.workspace_id:
            raise SlackConfigurationError("Slack workspace is not allowed")
        roles = self.mapping.allowed_users.get(user_id)
        if not roles:
            raise SlackConfigurationError("Slack user is not mapped to a Hermes role")
        return SlackActor(
            actor_id=f"slack:{team_id}:{user_id}",
            roles=roles,
            is_admin="workflow_admin" in roles,
        )

    def require_development_channel(self, channel_id: str) -> None:
        if channel_id != self.mapping.development_channel_id:
            raise SlackConfigurationError("command is only allowed in the mapped development channel")


def _require_id(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SlackConfigurationError(f"invalid {label}")
    return value


def load_slack_mapping(path: str | Path) -> SlackMapping:
    mapping_path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SlackConfigurationError("Slack mapping file could not be read") from exc
    except yaml.YAMLError as exc:
        raise SlackConfigurationError("Slack mapping file is invalid YAML") from exc
    if not isinstance(raw, dict):
        raise SlackConfigurationError("Slack mapping must be a YAML object")

    workspace_id = _require_id(raw.get("workspace_id"), _TEAM_ID, "workspace_id")
    raw_users = raw.get("allowed_users")
    if not isinstance(raw_users, dict) or not raw_users:
        raise SlackConfigurationError("allowed_users must contain at least one user")
    users: dict[str, frozenset[str]] = {}
    for raw_user_id, config in raw_users.items():
        user_id = _require_id(raw_user_id, _USER_ID, "user_id")
        if not isinstance(config, dict) or not isinstance(config.get("roles"), list):
            raise SlackConfigurationError(f"roles must be a list for user {user_id}")
        roles = frozenset(config["roles"])
        if not roles:
            raise SlackConfigurationError(f"at least one role is required for user {user_id}")
        unknown = roles - ROLE_KEYS
        if unknown:
            raise SlackConfigurationError(
                f"unknown role for user {user_id}: {', '.join(sorted(unknown))}"
            )
        users[user_id] = roles

    raw_queues = raw.get("queue_channels")
    if not isinstance(raw_queues, dict):
        raise SlackConfigurationError("queue_channels must be a YAML object")
    unknown_queues = set(raw_queues) - PHASE3_QUEUE_KEYS
    missing_queues = PHASE3_QUEUE_KEYS - set(raw_queues)
    if unknown_queues:
        raise SlackConfigurationError(
            f"unknown Phase 3 queue: {', '.join(sorted(unknown_queues))}"
        )
    if missing_queues:
        raise SlackConfigurationError(
            f"missing Phase 3 queue: {', '.join(sorted(missing_queues))}"
        )
    queues = {
        key: _require_id(value, _CHANNEL_ID, f"channel_id for {key}")
        for key, value in raw_queues.items()
    }
    return SlackMapping(workspace_id, users, queues)
