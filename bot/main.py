"""
Hermes AI Bot — 서연우 + 한도윤 + 오지민.

실행: (yn_agent/ 에서) python -m bot.main
필요: ~/.hermes/.env 채널 ID (키워드/컴플라이언스/검수/발행/트래킹/긴급/개발팀)

- 서연우: 매일 09:00 + /run_keyword_scan
- 한도윤: 키워드 승인 시 초안 + /run_content_draft + 10:00·15:00 백로그
- 오지민: 30분마다 + /run_anomaly_check (scenario: critical/warning/normal/privacy)
"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from .config import DISCORD_BOT_TOKEN, check_required_env
from .agents.keyword_analyst import run_daily_keyword_scan
from .agents.content_editor import process_backlog
from .agents.data_assistant import run_anomaly_check
from .discord_ui import post_keyword_result, post_content_result, post_data_assistant_result

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


async def execute_anomaly_check(scenario: str = "critical"):
    print(f"[오지민] 이상탐지 시작 (scenario={scenario})")
    try:
        result = await asyncio.to_thread(run_anomaly_check, scenario)
    except Exception as e:
        print(f"[오지민][ERROR] 이상탐지 실패: {e}")
        return None
    await post_data_assistant_result(bot, result)
    print(
        f"[오지민] 완료 status={result['item'].status_summary} "
        f"targets={result['targets']} record=#{result['record_id']}"
    )
    return result


@bot.event
async def on_ready():
    print(f"[Hermes] {bot.user} 로그인 완료. 서버: {[g.name for g in bot.guilds]}")

    if not scheduler.get_job("daily_keyword_scan"):
        scheduler.add_job(execute_keyword_scan, "cron", hour=9, minute=0, id="daily_keyword_scan")
    if not scheduler.get_job("content_backlog_sweep"):
        scheduler.add_job(execute_content_backlog, "cron", hour="10,15", minute=0, id="content_backlog_sweep")
    if not scheduler.get_job("tracking_check"):
        # routine.yaml: */30 * * * * — 스케줄은 critical 시연 데이터 사용
        scheduler.add_job(
            execute_anomaly_check,
            "cron",
            minute="*/30",
            id="tracking_check",
            kwargs={"scenario": "critical"},
        )
    if not scheduler.running:
        scheduler.start()
    print("[Hermes] 스케줄러 시작됨 (서연우 09:00 / 한도윤 10·15시 / 오지민 */30분)")


@bot.tree.command(name="run_keyword_scan", description="[데모용] 서연우 키워드 분석을 즉시 실행합니다")
async def run_keyword_scan_command(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 서연우가 키워드 분석을 시작합니다...", ephemeral=True)
    await execute_keyword_scan()


@bot.tree.command(name="run_content_draft", description="[데모용] 승인된 키워드 백로그로 한도윤 초안을 즉시 실행합니다")
async def run_content_draft_command(interaction: discord.Interaction):
    await interaction.response.send_message("✍️ 한도윤이 승인 대기 키워드로 초안을 작성합니다...", ephemeral=True)
    await execute_content_backlog()


@bot.tree.command(name="run_anomaly_check", description="[데모용] 오지민 트래킹 이상탐지를 즉시 실행합니다")
@app_commands.describe(scenario="critical / warning / normal / privacy")
@app_commands.choices(
    scenario=[
        app_commands.Choice(name="critical (전환 급락 → 긴급알림)", value="critical"),
        app_commands.Choice(name="warning (국소 하락)", value="warning"),
        app_commands.Choice(name="normal (정상·Discord 조용)", value="normal"),
        app_commands.Choice(name="privacy (PII → 개발팀)", value="privacy"),
    ]
)
async def run_anomaly_check_command(
    interaction: discord.Interaction,
    scenario: app_commands.Choice[str] = None,
):
    scen = scenario.value if scenario else "critical"
    await interaction.response.send_message(
        f"📡 오지민이 이상탐지를 시작합니다... (scenario=`{scen}`)",
        ephemeral=True,
    )
    result = await execute_anomaly_check(scen)
    if result is None:
        await interaction.followup.send("⚠️ 이상탐지 실패 — 콘솔 로그를 확인하세요.", ephemeral=True)
        return
    status = result["item"].status_summary
    await interaction.followup.send(
        f"완료: **{status}** → `{', '.join(result['targets'])}` (record #{result['record_id']})",
        ephemeral=True,
    )


@bot.event
async def setup_hook():
    await bot.tree.sync()
    print("[Hermes] 슬래시커맨드 동기화 완료")


def main():
    check_required_env()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
