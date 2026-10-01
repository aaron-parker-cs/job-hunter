"""Notifier implementations and factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from job_hunter.config import Config
from job_hunter.notify.base import Notifier
from job_hunter.secrets import Secrets, SecretsError
from job_hunter.store import Store


def build_notifiers(cfg: Config, secrets: Secrets, store: Store) -> list[Notifier]:
    """Create the notifiers selected by `notifier:` (telegram | discord | both)."""
    notifiers: list[Notifier] = []
    if cfg.notifier in ("telegram", "both"):
        from telegram import Bot

        from job_hunter.notify.telegram_bot import TelegramNotifier

        if not secrets.telegram_bot_token or secrets.telegram_chat_id is None:
            raise SecretsError("Telegram credentials missing")
        bot = Bot(secrets.telegram_bot_token.get_secret_value())
        notifiers.append(TelegramNotifier(bot, secrets.telegram_chat_id, store))
    if cfg.notifier in ("discord", "both"):
        from job_hunter.notify.discord_bot import (
            DiscordBot,
            DiscordNotifier,
            DiscordWebhookNotifier,
        )

        if (
            secrets.discord_bot_token
            and secrets.discord_channel_id is not None
            and secrets.discord_allowed_user_id is not None
        ):
            discord_bot = DiscordBot(
                store, secrets.discord_channel_id, secrets.discord_allowed_user_id
            )
            notifiers.append(
                DiscordNotifier(
                    discord_bot,
                    secrets.discord_bot_token.get_secret_value(),
                    secrets.discord_channel_id,
                    store,
                )
            )
        elif secrets.discord_webhook_url:
            notifiers.append(DiscordWebhookNotifier(secrets.discord_webhook_url.get_secret_value()))
    return notifiers


@dataclass
class Listeners:
    """Inbound side of the notifiers, used only by `run` mode."""

    telegram_app: Any = None  # telegram.ext.Application
    discord_bot: Any = None  # discord_bot.DiscordBot
    discord_token: str | None = None


def build_runtime(cfg: Config, secrets: Secrets, store: Store) -> tuple[list[Notifier], Listeners]:
    """Like build_notifiers, but wired for listening (handlers, guards, slash commands)."""
    notifiers: list[Notifier] = []
    listeners = Listeners()
    if cfg.notifier in ("telegram", "both"):
        from telegram.ext import Application

        from job_hunter.notify.telegram_bot import TelegramNotifier

        if not secrets.telegram_bot_token or secrets.telegram_chat_id is None:
            raise SecretsError("Telegram credentials missing")
        app = Application.builder().token(secrets.telegram_bot_token.get_secret_value()).build()
        telegram = TelegramNotifier(app.bot, secrets.telegram_chat_id, store, owns_bot=False)
        telegram.register(app)
        notifiers.append(telegram)
        listeners.telegram_app = app
    if cfg.notifier in ("discord", "both"):
        from job_hunter.notify.discord_bot import DiscordNotifier

        discord_cfg = cfg.model_copy(update={"notifier": "discord"})
        for notifier in build_notifiers(discord_cfg, secrets, store):
            notifiers.append(notifier)
            if isinstance(notifier, DiscordNotifier):
                notifier.owns_login = False  # the bot connects via bot.start() instead
                listeners.discord_bot = notifier.bot
                if secrets.discord_bot_token:
                    listeners.discord_token = secrets.discord_bot_token.get_secret_value()
    return notifiers, listeners
