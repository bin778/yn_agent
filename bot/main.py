"""
Hermes AI Bot — 서연우(keyword_analyst) + 한도윤(content_editor).

실행: (yn_agent/ 에서) python -m bot.main
필요: ~/.hermes/.env 에 DISCORD_BOT_TOKEN, GOOGLE_API_KEY,
      DISCORD_CHANNEL_KEYWORD_QUEUE, DISCORD_CHANNEL_COMPLIANCE_REVIEW,
      DISCORD_CHANNEL_HUMAN_REVIEW, DISCORD_CHANNEL_PUBLISH 설정

- 서연우: daily_keyword_scan(매일 09:00 KST) + /run_keyword_scan
- 한도윤: 키워드 큐 승인 시 자동 초안 + /run_content_draft + backlog_sweep(10:00/15:00)
"""
import asyncio

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from .config import DISCORD_BOT_TOKEN, check_required_env
from .agents.keyword_analyst import run_daily_keyword_scan
from .agents.content_editor import process_backlog
from .discord_ui import post_keyword_result, post_content_result

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Seoul"))


async def execute_keyword_scan():
    print("[서연우] 키워드 스캔 시작")
    try:
        results = await asyncio.to_thread(run_daily_keyword_scan)
    except Exception as e:
        print(f"[서연우][ERROR] 스캔 실패: {e}")
        return
    for r in results:
        await post_keyword_result(bot, r)
    high_risk_count = sum(1 for r in results if r["target"] == "compliance_review.queue")
    print(f"[서연우] {len(results)}건 처리 완료 (high-risk {high_risk_count}건 → 법무 검토로 분리)")


async def execute_content_backlog():
    print("[한도윤] 백로그 스윕 시작")
    try:
        results = await asyncio.to_thread(process_backlog, 3)
    except Exception as e:
        print(f"[한도윤][ERROR] 백로그 실패: {e}")
        return
    for r in results:
        await post_content_result(bot, r)
    print(f"[한도윤] 백로그 {len(results)}건 초안 완료")


@bot.event
async def on_ready():
    print(f"[Hermes] {bot.user} 로그인 완료. 서버: {[g.name for g in bot.guilds]}")

    if not scheduler.get_job("daily_keyword_scan"):
        scheduler.add_job(execute_keyword_scan, "cron", hour=9, minute=0, id="daily_keyword_scan")
    if not scheduler.get_job("content_backlog_sweep"):
        # routine.yaml backlog_sweep: 10:00, 15:00 KST
        scheduler.add_job(execute_content_backlog, "cron", hour="10,15", minute=0, id="content_backlog_sweep")
    if not scheduler.running:
        scheduler.start()
    print("[Hermes] 스케줄러 시작됨 (서연우 09:00 / 한도윤 백로그 10:00·15:00 KST)")


@bot.tree.command(name="run_keyword_scan", description="[데모용] 서연우 키워드 분석을 즉시 실행합니다")
async def run_keyword_scan_command(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 서연우가 키워드 분석을 시작합니다...", ephemeral=True)
    await execute_keyword_scan()


@bot.tree.command(name="run_content_draft", description="[데모용] 승인된 키워드 백로그로 한도윤 초안을 즉시 실행합니다")
async def run_content_draft_command(interaction: discord.Interaction):
    await interaction.response.send_message("✍️ 한도윤이 승인 대기 키워드로 초안을 작성합니다...", ephemeral=True)
    await execute_content_backlog()


@bot.event
async def setup_hook():
    await bot.tree.sync()
    print("[Hermes] 슬래시커맨드 동기화 완료")


def main():
    check_required_env()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
