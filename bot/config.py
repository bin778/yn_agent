"""Environment variables and paths shared by the headless Phase 1 services."""

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

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
DB_PATH = Path(os.getenv("HERMES_DB_PATH") or str(BOT_DIR / "hermes.db"))
USE_FAKE_DATA = os.getenv("USE_FAKE_DATA", "true").lower() == "true"


def check_required_env() -> None:
    if not GOOGLE_API_KEY:
        raise RuntimeError("필수 환경변수가 없습니다: GOOGLE_API_KEY")
