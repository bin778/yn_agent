"""
Hermes AI Bot — 서연우(keyword_analyst) 파일럿.

실행: (yn_agent/ 에서) python -m bot.main
필요: ~/.hermes/.env 에 DISCORD_BOT_TOKEN, GOOGLE_API_KEY,
      DISCORD_CHANNEL_KEYWORD_QUEUE, DISCORD_CHANNEL_COMPLIANCE_REVIEW 설정

routine.yaml의 daily_keyword_scan(매일 09:00 KST)을 그대로 스케줄로 등록하고,
시연 편의를 위해 /run_keyword_scan 슬래시커맨드로 즉시 실행도 가능하게 했다.
"""
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from .config import DISCORD_BOT_TOKEN, check_required_env
from .agents.keyword_analyst import run_daily_keyword_scan
from .discord_ui import post_keyword_result

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Seoul"))


async def execute_keyword_scan():
    print("[서연우] 키워드 스캔 시작")
    try:
        results = run_daily_keyword_scan()
    except Exception as e:
        print(f"[서연우][ERROR] 스캔 실패: {e}")
        return
    for r in results:
        await post_keyword_result(bot, r)
    high_risk_count = sum(1 for r in results if r["target"] == "compliance_review.queue")
    print(f"[서연우] {len(results)}건 처리 완료 (high-risk {high_risk_count}건 → 법무 검토로 분리)")


@bot.event
async def on_ready():
    print(f"[Hermes] {bot.user} 로그인 완료. 서버: {[g.name for g in bot.guilds]}")

    if not scheduler.get_job("daily_keyword_scan"):
        scheduler.add_job(execute_keyword_scan, "cron", hour=9, minute=0, id="daily_keyword_scan")
    if not scheduler.running:
        scheduler.start()
    print("[Hermes] 스케줄러 시작됨 (서연우: 매일 09:00 KST)")


@bot.tree.command(name="run_keyword_scan", description="[데모용] 서연우 키워드 분석을 즉시 실행합니다")
async def run_keyword_scan_command(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 서연우가 키워드 분석을 시작합니다...", ephemeral=True)
    await execute_keyword_scan()


@bot.event
async def setup_hook():
    await bot.tree.sync()
    print("[Hermes] 슬래시커맨드 동기화 완료")


def main():
    check_required_env()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
