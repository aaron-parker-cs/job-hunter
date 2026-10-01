"""Discord notifier: embeds with persistent buttons and reactions, plus a webhook fallback."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord
import httpx
from discord import app_commands

from job_hunter import commands
from job_hunter.feedback import ACTIONS, EMOJI_TO_ACTION, apply_feedback, revert_feedback
from job_hunter.models import Job
from job_hunter.notify.base import format_salary, where_text
from job_hunter.score import ScoreResult
from job_hunter.store import Store

if TYPE_CHECKING:
    from job_hunter.service import Service

log = logging.getLogger(__name__)

CHANNEL = "discord"
BUTTON_SOURCE = "discord"
REACTION_SOURCE = "discord_reaction"
EMBED_COLORS = {"strong": 0x2ECC71, "maybe": 0xF1C40F, "weak": 0x95A5A6}
_STYLES = {
    "interested": discord.ButtonStyle.success,
    "not_fit": discord.ButtonStyle.secondary,
    "hide_company": discord.ButtonStyle.danger,
    "applied": discord.ButtonStyle.primary,
}


def build_embed(job: Job, result: ScoreResult) -> discord.Embed:
    embed = discord.Embed(
        title=job.title[:256],
        url=job.url,
        description="\n".join(
            [f"✅ {r}" for r in result.reasons[:2]] + [f"⚠️ {c}" for c in result.concerns[:1]]
        )[:4000],
        color=EMBED_COLORS[result.verdict],
    )
    embed.add_field(name="Company", value=job.company[:1024], inline=True)
    embed.add_field(name="Where", value=where_text(job)[:1024], inline=True)
    embed.add_field(name="Score", value=f"{result.score}/100 ({result.verdict})", inline=True)
    salary = format_salary(job)
    if salary:
        embed.add_field(name="Salary", value=salary, inline=True)
    return embed


class FeedbackButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"fb:(?P<action>[a-z_]+):(?P<job>[0-9a-f]{64})",
):
    """Per-job button whose custom_id encodes the action and job, so it survives restarts."""

    def __init__(self, action: str, job_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=ACTIONS[action][0],
                style=_STYLES[action],
                custom_id=f"fb:{action}:{job_id}",
            )
        )
        self.action = action
        self.job_id = job_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str]
    ) -> FeedbackButton:
        return cls(match["action"], match["job"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client: Any = interaction.client
        return bool(interaction.user.id == client.allowed_user_id)

    async def callback(self, interaction: discord.Interaction) -> None:
        client: Any = interaction.client
        if not apply_feedback(client.store, self.job_id, self.action, BUTTON_SOURCE):
            await interaction.response.send_message("Unknown job.", ephemeral=True)
            return
        message = interaction.message
        if message is None or not message.embeds:
            await interaction.response.defer()
            return
        embed = message.embeds[0]
        embed.set_footer(text=f"Your choice: {ACTIONS[self.action][0]}")
        await interaction.response.edit_message(embed=embed)


def handle_reaction(
    store: Store,
    payload: discord.RawReactionActionEvent,
    *,
    allowed_user_id: int,
    bot_user_id: int | None,
    channel_id: int,
    removed: bool,
) -> bool:
    """Apply (or undo) feedback for a reaction. Returns True if it counted."""
    if payload.user_id == bot_user_id or payload.user_id != allowed_user_id:
        return False
    if payload.channel_id != channel_id:
        return False
    action = EMOJI_TO_ACTION.get(str(payload.emoji))
    if action is None:
        return False
    job_id = store.job_for_message(CHANNEL, str(payload.message_id))
    if job_id is None:
        return False
    if removed:
        return revert_feedback(store, job_id, action, REACTION_SOURCE)
    return apply_feedback(store, job_id, action, REACTION_SOURCE)


class GuardedTree(app_commands.CommandTree):
    """Slash commands only work for the allowed user; everyone else is ignored."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client: Any = self.client
        return bool(interaction.user.id == client.allowed_user_id)

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.error("slash command failed: %s", type(error).__name__)


class DiscordBot(discord.Client):
    def __init__(self, store: Store, channel_id: int, allowed_user_id: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        super().__init__(intents=intents)
        self.store = store
        self.channel_id = channel_id
        self.allowed_user_id = allowed_user_id
        self.tree = GuardedTree(self)
        self.commands_enabled = False  # set by register_commands in `run` mode

    async def setup_hook(self) -> None:
        self.add_dynamic_items(FeedbackButton)
        if self.commands_enabled:
            await self._sync_commands()

    async def _sync_commands(self) -> None:
        """Publish slash commands to the channel's server (guild sync is instant)."""
        try:
            channel: Any = await self.fetch_channel(self.channel_id)
            self.tree.copy_global_to(guild=channel.guild)
            await self.tree.sync(guild=channel.guild)
        except (discord.HTTPException, AttributeError):
            log.exception(
                "could not register slash commands; re-invite the bot with the "
                "applications.commands scope"
            )

    def _handle(self, payload: discord.RawReactionActionEvent, removed: bool) -> None:
        handle_reaction(
            self.store,
            payload,
            allowed_user_id=self.allowed_user_id,
            bot_user_id=self.user.id if self.user else None,
            channel_id=self.channel_id,
            removed=removed,
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        self._handle(payload, removed=False)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        self._handle(payload, removed=True)


class DiscordNotifier:
    name = CHANNEL

    def __init__(
        self, bot: DiscordBot, token: str, channel_id: int, store: Store, *, owns_login: bool = True
    ) -> None:
        self.bot = bot
        self._token = token
        self._channel_id = channel_id
        self._store = store
        self.owns_login = owns_login  # False when `run` mode already connected the client
        self._logged_in = False

    async def _channel(self) -> Any:
        if self.owns_login and not self._logged_in:
            await self.bot.login(self._token)  # REST only; no gateway needed to post
            self._logged_in = True
        elif not self.owns_login:
            await self.bot.wait_until_ready()
        return await self.bot.fetch_channel(self._channel_id)

    async def send_job(self, job: Job, result: ScoreResult) -> None:
        channel = await self._channel()
        view = discord.ui.View(timeout=None)
        for action in ACTIONS:
            view.add_item(FeedbackButton(action, job.id))
        message = await channel.send(embed=build_embed(job, result), view=view)
        self._store.add_notification(job.id, CHANNEL, str(message.id))
        for _, emoji in ACTIONS.values():
            await message.add_reaction(emoji)

    async def send_text(self, text: str) -> None:
        channel = await self._channel()
        await channel.send(text)

    async def aclose(self) -> None:
        if self._logged_in:
            await self.bot.close()
            self._logged_in = False


class DiscordWebhookNotifier:
    """One-way fallback: plain embeds, no buttons and no feedback."""

    name = "discord-webhook"

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def _post(self, payload: dict[str, Any]) -> None:
        resp = await self._client.post(self._url, json=payload)
        resp.raise_for_status()

    async def send_job(self, job: Job, result: ScoreResult) -> None:
        await self._post({"embeds": [build_embed(job, result).to_dict()]})

    async def send_text(self, text: str) -> None:
        await self._post({"content": text})

    async def aclose(self) -> None:
        await self._client.aclose()


def register_commands(bot: DiscordBot, service: Service) -> None:
    """Add /status /run /pause /resume /top slash commands (published in setup_hook)."""
    bot.commands_enabled = True
    tree = bot.tree

    @tree.command(name="status", description="Last run, next run and spend")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(commands.status_text(service), ephemeral=True)

    @tree.command(name="run", description="Run a search now")
    async def run(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(commands.run_text(service), ephemeral=True)

    @tree.command(name="pause", description="Pause scheduled runs")
    async def pause(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(commands.pause_text(service), ephemeral=True)

    @tree.command(name="resume", description="Resume scheduled runs")
    async def resume(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(commands.resume_text(service), ephemeral=True)

    @tree.command(name="top", description="Best unapplied matches this week")
    async def top(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            commands.top_text(service, wrap_links=True), ephemeral=True
        )
