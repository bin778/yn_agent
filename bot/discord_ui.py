"""
queues.md 기준 큐 → Discord 채널 매핑, embed 생성, 승인/반려 버튼.
"""
import discord

from .config import DISCORD_CHANNEL_KEYWORD_QUEUE, DISCORD_CHANNEL_COMPLIANCE_REVIEW
from .db import record_decision

# queues.md 큐 이름 ↔ 채널ID 매핑 (하드코딩 최소화, config.py의 .env 값을 그대로 참조)
CHANNEL_MAP = {
    "content_editor.inbox": DISCORD_CHANNEL_KEYWORD_QUEUE,
    "compliance_review.queue": DISCORD_CHANNEL_COMPLIANCE_REVIEW,
}


class ApprovalView(discord.ui.View):
    """
    승인/반려 버튼. timeout=None 이라 봇 프로세스가 켜져있는 동안은 계속 작동한다.
    (재시작 후에도 버튼을 살리려면 custom_id에 record_id를 인코딩하고
     on_ready에서 영속 View로 재등록해야 하는데, 데모 단계에서는 불필요해 생략함)
    """

    def __init__(self, record_id: int):
        super().__init__(timeout=None)
        self.record_id = record_id

    @discord.ui.button(label="✅ 승인", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        record_decision(self.record_id, "approved", interaction.user.display_name)
        button.disabled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ **{interaction.user.display_name}** 님이 승인했습니다. (record #{self.record_id})"
        )

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


async def post_keyword_result(client: discord.Client, result: dict):
    channel_id = CHANNEL_MAP.get(result["target"])
    if not channel_id:
        print(f"[WARN] '{result['target']}'에 매핑된 채널ID가 없습니다. .env를 확인하세요.")
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        print(f"[WARN] 채널(id={channel_id})을 찾을 수 없습니다. 봇이 해당 채널에 접근 권한이 있는지 확인하세요.")
        return

    embed = build_keyword_embed(result["item"], result["target"], result["record_id"])
    view = ApprovalView(result["record_id"])
    await channel.send(embed=embed, view=view)
