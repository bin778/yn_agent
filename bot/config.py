"""
환경변수 및 경로 설정.
.env는 ~/.hermes/.env 를 우선 읽고, 없으면 yn_agent/bot/.env 를 읽는다.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# yn_agent/bot/config.py 기준 → yn_agent/ 가 BASE_DIR
BOT_DIR = Path(__file__).resolve().parent
BASE_DIR = BOT_DIR.parent  # yn_agent/

_hermes_env = Path.home() / ".hermes" / ".env"
_local_env = BOT_DIR / ".env"

if _hermes_env.exists():
    load_dotenv(_hermes_env)
elif _local_env.exists():
    load_dotenv(_local_env)
else:
    print(f"[WARN] .env 파일을 찾을 수 없습니다. ({_hermes_env} 또는 {_local_env})")

# ── 필수 키 ─────────────────────────────────────────
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── Discord 채널 ID (queues.md 기준, 미설정이면 콘솔 경고 후 스킵) ──
DISCORD_CHANNEL_KEYWORD_QUEUE = os.getenv("DISCORD_CHANNEL_KEYWORD_QUEUE")       # #서연우-키워드큐
DISCORD_CHANNEL_COMPLIANCE_REVIEW = os.getenv("DISCORD_CHANNEL_COMPLIANCE_REVIEW")  # #컴플라이언스-검토
DISCORD_CHANNEL_HUMAN_REVIEW = os.getenv("DISCORD_CHANNEL_HUMAN_REVIEW")        # #콘텐츠-검수대기
DISCORD_CHANNEL_PUBLISH = os.getenv("DISCORD_CHANNEL_PUBLISH")                  # #발행대기
DISCORD_CHANNEL_TRACKING_STATUS = os.getenv("DISCORD_CHANNEL_TRACKING_STATUS")  # #오지민-트래킹상태
DISCORD_CHANNEL_BROADCAST = os.getenv("DISCORD_CHANNEL_BROADCAST")              # #전체-긴급알림
DISCORD_CHANNEL_DEV_TEAM = os.getenv("DISCORD_CHANNEL_DEV_TEAM")                # #개발팀-에러알림

# ── DB ──────────────────────────────────────────────
DB_PATH = BOT_DIR / "hermes.db"

# ── 데모/개발 모드 스위치 ──────────────────────────────
# True면 GA4/Naver/Ads 대신 fake_data.py의 가짜 데이터를 사용한다.
USE_FAKE_DATA = os.getenv("USE_FAKE_DATA", "true").lower() == "true"


def check_required_env():
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not GOOGLE_API_KEY:
        missing.append("GOOGLE_API_KEY")
    if missing:
        raise RuntimeError(f"필수 환경변수가 없습니다: {', '.join(missing)}")
    if not DISCORD_CHANNEL_KEYWORD_QUEUE:
        print("[WARN] DISCORD_CHANNEL_KEYWORD_QUEUE 미설정 — 해당 채널로는 게시되지 않습니다.")
    if not DISCORD_CHANNEL_COMPLIANCE_REVIEW:
        print("[WARN] DISCORD_CHANNEL_COMPLIANCE_REVIEW 미설정 — 해당 채널로는 게시되지 않습니다.")
    if not DISCORD_CHANNEL_HUMAN_REVIEW:
        print("[WARN] DISCORD_CHANNEL_HUMAN_REVIEW 미설정 — 해당 채널로는 게시되지 않습니다.")
    if not DISCORD_CHANNEL_PUBLISH:
        print("[WARN] DISCORD_CHANNEL_PUBLISH 미설정 — 해당 채널로는 게시되지 않습니다.")
    if not DISCORD_CHANNEL_TRACKING_STATUS:
        print("[WARN] DISCORD_CHANNEL_TRACKING_STATUS 미설정 — 해당 채널로는 게시되지 않습니다.")
    if not DISCORD_CHANNEL_BROADCAST:
        print("[WARN] DISCORD_CHANNEL_BROADCAST 미설정 — 해당 채널로는 게시되지 않습니다.")
    if not DISCORD_CHANNEL_DEV_TEAM:
        print("[WARN] DISCORD_CHANNEL_DEV_TEAM 미설정 — 해당 채널로는 게시되지 않습니다.")
