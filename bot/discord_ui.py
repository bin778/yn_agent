"""
queues.md 기준 큐 → Discord 채널 매핑, embed 생성, 승인/반려 버튼.
키워드 큐에서 승인하면 한도윤(content_editor) 초안 작성을 트리거한다.
"""
import asyncio

import discord

from .config import (
    DISCORD_CHANNEL_KEYWORD_QUEUE,
    DISCORD_CHANNEL_COMPLIANCE_REVIEW,
    DISCORD_CHANNEL_HUMAN_REVIEW,
    DISCORD_CHANNEL_PUBLISH,
)
from .db import record_decision

CHANNEL_MAP = {
    "content_editor.inbox": DISCORD_CHANNEL_KEYWORD_QUEUE,
    "compliance_review.queue": DISCORD_CHANNEL_COMPLIANCE_REVIEW,
    "human_review.queue": DISCORD_CHANNEL_HUMAN_REVIEW,
    "publish_queue": DISCORD_CHANNEL_PUBLISH,
}

BODY_PREVIEW_LIMIT = 500


class ApprovalView(discord.ui.View):
    """
    승인/반려 버튼.
    handoff="content_editor" 이면 키워드 승인 시 한도윤 초안 작성을 이어서 실행한다.
    """

    def __init__(self, record_id: int, handoff: str | None = None):
        super().__init__(timeout=None)
        self.record_id = record_id
        self.handoff = handoff

    @discord.ui.button(label="✅ 승인", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        record_decision(self.record_id, "approved", interaction.user.display_name)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ **{interaction.user.display_name}** 님이 승인했습니다. (record #{self.record_id})"
        )

        if self.handoff == "content_editor":
            await interaction.followup.send(
                f"✍️ 한도윤이 초안 작성을 시작합니다... (키워드 record #{self.record_id})"
            )
            try:
                from .agents.content_editor import draft_content_from_record

                result = await asyncio.to_thread(draft_content_from_record, self.record_id)
                await post_content_result(interaction.client, result)
                await interaction.followup.send(
                    f"📝 한도윤 초안 완료 → `{result['target']}` (record #{result['record_id']})"
                )
            except Exception as e:
                print(f"[한도윤][ERROR] 승인 후 초안 실패: {e}")
                await interaction.followup.send(f"⚠️ 한도윤 초안 작성 실패: {e}")

    @discord.ui.button(label="❌ 반려", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        record_decision(self.record_id, "rejected", interaction.user.display_name)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"❌ **{interaction.user.display_name}** 님이 반려했습니다. (record #{self.record_id})"
        )


def build_keyword_embed(item, target: str, record_id: int) -> discord.Embed:
    is_high_risk = target == "compliance_review.queue"
    embed = discord.Embed(
        title=f"🔑 {item.keyword}",
        color=discord.Color.red() if is_high_risk else discord.Color.blue(),
    )
    embed.add_field(name="urgency", value=str(item.urgency_level), inline=True)
    embed.add_field(name="compliance_risk", value=item.compliance_risk, inline=True)
    embed.add_field(name="예상 검색량", value=str(item.estimated_search_volume), inline=True)
    embed.add_field(name="의도", value=item.intent_type, inline=True)
    embed.add_field(name="판단 사유", value=item.reason, inline=False)
    if item.compliance_note:
        embed.add_field(name="⚠️ 컴플라이언스 노트", value=item.compliance_note, inline=False)
    embed.set_footer(text=f"서연우 · record #{record_id} · routed_to: {target}")
    return embed


def build_content_embed(item, target: str, record_id: int) -> discord.Embed:
    needs_review = target == "human_review.queue"
    embed = discord.Embed(
        title=f"📝 {item.title}",
        description=item.hook,
        color=discord.Color.orange() if needs_review else discord.Color.green(),
    )
    embed.add_field(name="타겟 키워드", value=item.target_keyword, inline=True)
    embed.add_field(name="라우팅", value=target, inline=True)

    preview = item.body if len(item.body) <= BODY_PREVIEW_LIMIT else item.body[:BODY_PREVIEW_LIMIT] + "…"
    embed.add_field(name="본문 미리보기", value=preview, inline=False)

    if item.flagged_claims:
        flags = "\n".join(f"• {c}" for c in item.flagged_claims)
        embed.add_field(name="🚩 flagged_claims", value=flags, inline=False)
    if item.sensitive_flags:
        sens = "\n".join(f"• {c}" for c in item.sensitive_flags)
        embed.add_field(name="⚠️ sensitive_flags", value=sens, inline=False)

    embed.add_field(name="판단 사유", value=item.reason, inline=False)
    embed.set_footer(text=f"한도윤 · record #{record_id} · routed_to: {target}")
    return embed


async def _post_to_channel(client: discord.Client, target: str, embed: discord.Embed, view: discord.ui.View):
    channel_id = CHANNEL_MAP.get(target)
    if not channel_id:
        print(f"[WARN] '{target}'에 매핑된 채널ID가 없습니다. .env를 확인하세요.")
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        print(f"[WARN] 채널(id={channel_id})을 찾을 수 없습니다. 봇이 해당 채널에 접근 권한이 있는지 확인하세요.")
        return
    await channel.send(embed=embed, view=view)


async def post_keyword_result(client: discord.Client, result: dict):
    target = result["target"]
    # inbox로 간 키워드만 승인 시 한도윤 핸드오프
    handoff = "content_editor" if target == "content_editor.inbox" else None
    embed = build_keyword_embed(result["item"], target, result["record_id"])
    view = ApprovalView(result["record_id"], handoff=handoff)
    await _post_to_channel(client, target, embed, view)


async def post_content_result(client: discord.Client, result: dict):
    embed = build_content_embed(result["item"], result["target"], result["record_id"])
    view = ApprovalView(result["record_id"], handoff=None)
    await _post_to_channel(client, result["target"], embed, view)
