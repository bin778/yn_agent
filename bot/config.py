"""Environment variables and paths shared by the headless Hermes services."""

import os
from pathlib import Path

from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
BASE_DIR = BOT_DIR.parent

_hermes_env = Path.home() / ".hermes" / ".env"
_local_env = BOT_DIR / ".env"
if _hermes_env.exists():
    load_dotenv(_hermes_env)
elif _local_env.exists():
    load_dotenv(_local_env)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "codex_cli").strip()
CODEX_COMMAND = os.getenv("CODEX_COMMAND", "codex").strip()
DB_PATH = Path(os.getenv("HERMES_DB_PATH") or str(BOT_DIR / "hermes.db"))
USE_FAKE_DATA = os.getenv("USE_FAKE_DATA", "true").lower() == "true"


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


CODEX_TIMEOUT_SECONDS = _positive_int("CODEX_TIMEOUT_SECONDS", 180)
CODEX_MAX_CONCURRENCY = _positive_int("CODEX_MAX_CONCURRENCY", 1)


def check_required_env() -> None:
    """Validate non-secret LLM configuration without touching login credentials."""
    if LLM_PROVIDER != "codex_cli":
        raise RuntimeError("LLM_PROVIDER must be codex_cli")
    if not CODEX_COMMAND:
        raise RuntimeError("CODEX_COMMAND must not be empty")
