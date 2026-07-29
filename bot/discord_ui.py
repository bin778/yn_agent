"""
queues.md 기준 큐 → Discord 채널 매핑, embed 생성, 승인/반려 버튼.
키워드 큐 승인 시 한도윤(content_editor) 초안 작성은 main.py의 자동 handoff로 넘어감
(더 이상 이 파일의 버튼 콜백에서 트리거하지 않음).

버튼 권한 체크:
- roles.md 기준으로 큐별 승인 가능 role을 제한한다.
- 해당 큐에 필요한 role의 discord_role_id가 .env에 아직 설정되지 않았으면
  경고만 찍고 통과시킨다 (role 세팅 전까지 데모가 막히지 않도록).
"""
import asyncio

import discord

from . import config
from .config import (
    DISCORD_CHANNEL_KEYWORD_QUEUE,
    DISCORD_CHANNEL_COMPLIANCE_REVIEW,
    DISCORD_CHANNEL_HUMAN_REVIEW,
    DISCORD_CHANNEL_PUBLISH,
    DISCORD_CHANNEL_TRACKING_STATUS,
    DISCORD_CHANNEL_BROADCAST,
    DISCORD_CHANNEL_DEV_TEAM,
    DISCORD_CHANNEL_AUTO_APPLY,
    DISCORD_CHANNEL_HUMAN_APPROVAL,
)
from .db import record_decision

CHANNEL_MAP = {
    "content_editor.inbox": DISCORD_CHANNEL_KEYWORD_QUEUE,
    "compliance_review.queue": DISCORD_CHANNEL_COMPLIANCE_REVIEW,
    "human_review.queue": DISCORD_CHANNEL_HUMAN_REVIEW,
    "publish_queue": DISCORD_CHANNEL_PUBLISH,
    "data_assistant.tracking_status": DISCORD_CHANNEL_TRACKING_STATUS,
    "broadcast.all_agents": DISCORD_CHANNEL_BROADCAST,
    "dev_team.queue": DISCORD_CHANNEL_DEV_TEAM,
    "auto_apply_queue": DISCORD_CHANNEL_AUTO_APPLY,
    "human_approval.queue": DISCORD_CHANNEL_HUMAN_APPROVAL,
}

# 큐별 승인 권한 (roles.md 기준). 여기 없는 큐는 제한 없음 (마케팅팀 전체 확인용).
TARGET_ALLOWED_ROLE_ENVS = {
    "compliance_review.queue": ["DISCORD_ROLE_LEGAL_COMPLIANCE", "DISCORD_ROLE_MARKETING_LEAD"],
    "human_review.queue": ["DISCORD_ROLE_ATTORNEY_REVIEWER", "DISCORD_ROLE_MARKETING_LEAD"],
    "human_approval.queue": ["DISCORD_ROLE_MARKETING_LEAD"],
    "publish_queue": ["DISCORD_ROLE_PUBLISH_OPERATOR", "DISCORD_ROLE_MARKETING_LEAD"],
}

BODY_PREVIEW_LIMIT = 500


def _has_permission(user: discord.abc.User, target: str) -> bool:
    """
    target 큐에 필요한 role을 사용자가 가지고 있는지 확인.
    - 이 큐에 권한 제한이 없으면 → 통과
    - 필요한 role ID가 .env에 하나도 설정 안 됐으면 → 경고만 찍고 통과
      (roles.md/DISCORD_SETUP_CHECKLIST.md 진행 전까지 데모가 막히지 않도록)
    - role ID는 설정되어 있는데 사용자에게 그 role이 없으면 → 거부
    """
    required_env_keys = TARGET_ALLOWED_ROLE_ENVS.get(target)
    if not required_env_keys:
        return True

    allowed_role_ids = [getattr(config, key, None) for key in required_env_keys]
    allowed_role_ids = [rid for rid in allowed_role_ids if rid]  # 값이 채워진 것만

    if not allowed_role_ids:
        print(f"[WARN] '{target}'의 승인 role이 .env에 아직 설정되지 않아 권한 체크를 건너뜁니다.")
        return True

    user_role_ids = {str(r.id) for r in getattr(user, "roles", [])}
    return any(rid in user_role_ids for rid in allowed_role_ids)


class ApprovalView(discord.ui.View):
    """
    승인/반려 버튼.
    - target: 이 항목이 속한 큐 이름 (권한 체크에 사용). 빈 문자열이면 권한 제한 없음.
    - handoff: 더 이상 사용하지 않음 (자동 handoff는 main.py에서 처리). 하위 호환을 위해 남겨둠.
    """

    def __init__(self, record_id: int, target: str = "", handoff: str | None = None):
        super().__init__(timeout=None)
        self.record_id = record_id
        self.target = target
        self.handoff = handoff  # 더 이상 사용 안 함 (main.py의 자동 handoff로 대체됨)

    @discord.ui.button(label="✅ 승인", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_permission(interaction.user, self.target):
            await interaction.response.send_message(
                "⛔ 이 항목은 담당 권한이 있는 분만 승인할 수 있습니다.", ephemeral=True
            )
            return

        record_decision(self.record_id, "approved", interaction.user.display_name)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ **{interaction.user.display_name}** 님이 확인했습니다. (record #{self.record_id})"
        )

    @discord.ui.button(label="❌ 반려", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_permission(interaction.user, self.target):
            await interaction.response.send_message(
                "⛔ 이 항목은 담당 권한이 있는 분만 반려할 수 있습니다.", ephemeral=True
            )
            return

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


def build_tracking_embed(item, target: str, record_id: int, scenario: str = "") -> discord.Embed:
    colors = {
        "Critical": discord.Color.red(),
        "Warning": discord.Color.orange(),
        "Normal": discord.Color.green(),
    }
    embed = discord.Embed(
        title=f"📡 트래킹 상태: {item.status_summary}",
        description=item.reason,
        color=colors.get(item.status_summary, discord.Color.greyple()),
    )
    if scenario:
        embed.add_field(name="시나리오", value=scenario, inline=True)
    embed.add_field(name="의심 레이어", value=item.suspected_layer or "-", inline=True)
    embed.add_field(name="게시 큐", value=target, inline=True)

    if item.tracking_anomalies:
        anomalies = "\n".join(f"• {a}" for a in item.tracking_anomalies)
        embed.add_field(name="이상 징후", value=anomalies, inline=False)
    if item.action_items:
        actions = "\n".join(f"• {a}" for a in item.action_items)
        embed.add_field(name="다음 조치", value=actions, inline=False)
    if item.privacy_flags:
        privacy = "\n".join(f"• {p}" for p in item.privacy_flags)
        embed.add_field(name="🔒 privacy_flags", value=privacy, inline=False)

    embed.set_footer(text=f"오지민 · record #{record_id} · routed_to: {target}")
    return embed


def build_bid_embed(item, target: str, record_id: int) -> discord.Embed:
    is_human = target == "human_approval.queue"
    is_compliance = target == "compliance_review.queue"
    if is_compliance:
        color = discord.Color.red()
    elif is_human:
        color = discord.Color.orange()
    else:
        color = discord.Color.green()

    adj = item.adjustment_percentage
    adj_text = f"{adj:+d}%" if item.action != "PAUSE" else "PAUSE"
    embed = discord.Embed(
        title=f"📊 {item.keyword}",
        description=item.rationale,
        color=color,
    )
    embed.add_field(name="action", value=item.action, inline=True)
    embed.add_field(name="adjustment", value=adj_text, inline=True)
    embed.add_field(name="campaign", value=item.campaign or "-", inline=True)
    embed.add_field(name="device", value=item.target_device or "all", inline=True)
    embed.add_field(name="시간대", value=item.target_hour_range or "전체", inline=True)
    embed.add_field(name="category", value=item.category or "-", inline=True)
    if item.sensitive_keyword:
        embed.add_field(name="⚠️ sensitive", value="true (법무 병행)", inline=True)
    if item.caution_flag:
        embed.add_field(name="caution_flag", value="true (트래킹 Warning)", inline=True)
    embed.set_footer(text=f"정하준 · record #{record_id} · routed_to: {target}")
    return embed


def build_halt_embed(halt_reason: str, record_id: int, tracking_status: str) -> discord.Embed:
    embed = discord.Embed(
        title="⛔ 정하준 입찰 제안 보류",
        description=halt_reason,
        color=discord.Color.dark_red(),
    )
    embed.add_field(name="tracking_status", value=tracking_status, inline=True)
    embed.set_footer(text=f"정하준 · record #{record_id} · halted")
    return embed


async def _post_to_channel(
    client: discord.Client,
    target: str,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
    content: str | None = None,
):
    channel_id = CHANNEL_MAP.get(target)
    if not channel_id:
        print(f"[WARN] '{target}'에 매핑된 채널ID가 없습니다. .env를 확인하세요.")
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        print(f"[WARN] 채널(id={channel_id})을 찾을 수 없습니다. 봇이 해당 채널에 접근 권한이 있는지 확인하세요.")
        return
    kwargs = {"embed": embed}
    if view is not None:
        kwargs["view"] = view
    if content:
        kwargs["content"] = content
    await channel.send(**kwargs)


async def post_keyword_result(client: discord.Client, result: dict):
    target = result["target"]
    embed = build_keyword_embed(result["item"], target, result["record_id"])
    view = ApprovalView(result["record_id"], target=target)
    await _post_to_channel(client, target, embed, view)


async def post_content_result(client: discord.Client, result: dict):
    target = result["target"]
    embed = build_content_embed(result["item"], target, result["record_id"])
    view = ApprovalView(result["record_id"], target=target)
    await _post_to_channel(client, target, embed, view)


async def post_data_assistant_result(client: discord.Client, result: dict):
    """
    오지민은 승인 버튼 없음 (알림 전용).
    Normal → Discord 게시 안 함 (DB tracking_status만 갱신됨).
    Warning → tracking_status 채널
    Critical → broadcast (+ dev_team은 targets에 있으면 별도 게시)
    privacy → dev_team
    """
    item = result["item"]
    record_id = result["record_id"]
    scenario = result.get("scenario", "")
    targets = result.get("targets") or []

    discord_targets = []
    for t in targets:
        if t == "data_assistant.tracking_status":
            # Critical은 broadcast로 충분히 보이므로 tracking 채널은 Warning만
            if item.status_summary == "Warning":
                discord_targets.append(t)
            elif item.status_summary == "Critical":
                pass  # broadcast로 대체
            # Normal: 조용히
        elif t in ("broadcast.all_agents", "dev_team.queue"):
            discord_targets.append(t)

    if not discord_targets:
        print(f"[오지민] status={item.status_summary} — Discord 게시 생략 (DB만 갱신, record #{record_id})")
        return

    for target in discord_targets:
        embed = build_tracking_embed(item, target, record_id, scenario=scenario)
        content = "@here" if target == "broadcast.all_agents" else None
        await _post_to_channel(client, target, embed, view=None, content=content)


async def post_performance_result(client: discord.Client, batch: dict):
    """
    정하준 배치 결과 게시.
    halted면 dev_team에 보류 알림.
    그 외 각 recommendation을 targets 채널에 승인 버튼과 함께 게시.
    """
    if batch.get("halted"):
        record_id = batch["record_id"]
        embed = build_halt_embed(batch["halt_reason"], record_id, batch["tracking_status"])
        for target in batch.get("notify_targets") or ["dev_team.queue"]:
            await _post_to_channel(client, target, embed, view=None)
        return

    for result in batch.get("results") or []:
        item = result["item"]
        record_id = result["record_id"]
        for target in result["targets"]:
            embed = build_bid_embed(item, target, record_id)
            view = ApprovalView(record_id, target=target)
            await _post_to_channel(client, target, embed, view)