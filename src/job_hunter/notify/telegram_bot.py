"""Telegram notifier: job messages with inline feedback buttons, chat-id guarded."""

from __future__ import annotations

import html
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from job_hunter import commands
from job_hunter.feedback import ACTIONS, apply_feedback
from job_hunter.models import Job
from job_hunter.notify.base import format_salary, where_text
from job_hunter.score import ScoreResult
from job_hunter.store import Store

if TYPE_CHECKING:
    from job_hunter.service import Service

log = logging.getLogger(__name__)

CHANNEL = "telegram"
FEEDBACK_PATTERN = "^fb:(" + "|".join(ACTIONS) + ")$"
CHOICE_MARKER = "\n\n<b>Your choice:</b> "
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def is_authorized(update: Update, chat_id: int) -> bool:
    """Only updates from the configured chat are ever processed."""
    chat = update.effective_chat
    return chat is not None and chat.id == chat_id


def format_job_html(job: Job, result: ScoreResult) -> str:
    esc = html.escape
    meta = [f"{esc(job.company)} — {esc(where_text(job))}"]
    salary = format_salary(job)
    if salary:
        meta.append(esc(salary))
    lines = [
        f"<b>{esc(job.title)}</b>",
        " · ".join(meta),
        f"Score <b>{result.score}/100</b> ({result.verdict})",
    ]
    lines += [f"✅ {esc(r)}" for r in result.reasons[:2]]
    lines += [f"⚠️ {esc(c)}" for c in result.concerns[:1]]
    lines.append(f'<a href="{esc(job.url, quote=True)}">View posting</a>')
    return "\n".join(lines)


def feedback_keyboard() -> InlineKeyboardMarkup:
    # callback_data is capped at 64 bytes, so the job is found via the message id instead.
    buttons = [
        InlineKeyboardButton(label, callback_data=f"fb:{action}")
        for action, (label, _) in ACTIONS.items()
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


async def handle_button(store: Store, chat_id: int, update: Update) -> None:
    """Process a feedback button press and edit the message to show the choice."""
    query = update.callback_query
    if query is None or not is_authorized(update, chat_id) or not query.data:
        return
    action = query.data.removeprefix("fb:")
    message = query.message
    job_id = (
        store.job_for_message(CHANNEL, str(message.message_id)) if message is not None else None
    )
    if job_id is None or not apply_feedback(store, job_id, action, CHANNEL):
        await query.answer("Unknown job.")
        return
    await query.answer(ACTIONS[action][0])
    base = getattr(message, "text_html", "") or ""
    base = base.split(CHOICE_MARKER)[0]
    try:
        await query.edit_message_text(
            base + CHOICE_MARKER + html.escape(ACTIONS[action][0]),
            parse_mode=ParseMode.HTML,
            reply_markup=getattr(message, "reply_markup", None),
            link_preview_options=NO_PREVIEW,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


class TelegramNotifier:
    name = CHANNEL

    def __init__(self, bot: Bot, chat_id: int, store: Store, *, owns_bot: bool = True) -> None:
        self._bot = bot
        self._owns_bot = owns_bot  # False when an Application manages the bot
        self._chat_id = chat_id
        self._store = store
        self._ready = False

    async def _ensure(self) -> None:
        if not self._ready:
            await self._bot.initialize()
            self._ready = True

    async def send_job(self, job: Job, result: ScoreResult) -> None:
        await self._ensure()
        msg = await self._bot.send_message(
            chat_id=self._chat_id,
            text=format_job_html(job, result),
            parse_mode=ParseMode.HTML,
            reply_markup=feedback_keyboard(),
            link_preview_options=NO_PREVIEW,
        )
        self._store.add_notification(job.id, CHANNEL, str(msg.message_id))

    async def send_text(self, text: str) -> None:
        await self._ensure()
        await self._bot.send_message(chat_id=self._chat_id, text=text)

    async def aclose(self) -> None:
        if self._ready and self._owns_bot:
            await self._bot.shutdown()
            self._ready = False

    # --- inbound (used by `run` mode) ---------------------------------------

    def register(self, app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        """Attach the chat guard and feedback handler to a polling application."""
        app.add_handler(TypeHandler(Update, self._guard), group=-1)
        app.add_handler(CallbackQueryHandler(self._on_button, pattern=FEEDBACK_PATTERN))

    async def _guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, self._chat_id):
            chat = update.effective_chat
            log.warning("ignored update from unauthorized chat %s", chat.id if chat else "?")
            raise ApplicationHandlerStop

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await handle_button(self._store, self._chat_id, update)


TELEGRAM_MAX_CHARS = 4096

# Telegram command names allow only letters, digits and underscores, hence last_scores here
# where Discord has /last-scores.
COMMAND_MENU = [
    ("status", "Last run, next run, spend and settings"),
    ("run", "Run a search now"),
    ("pause", "Pause scheduled runs"),
    ("resume", "Resume scheduled runs"),
    ("top", "Best unapplied matches this week"),
    ("last_scores", "Top scores from the last run, with explanations: /last_scores [n]"),
    ("threshold", "Show or set the notify threshold: /threshold 65 | reset"),
    ("radius", "Show or set the search radius: /radius 30 | reset"),
    ("location", "Show or set the home location: /location Tacoma, WA | reset"),
]

CommandFn = Callable[["Service", str | None], "str | Awaitable[str]"]


def register_commands(app: Application[Any, Any, Any, Any, Any, Any], service: Service) -> None:
    """Add the bot commands. The chat guard (group -1) already filters senders."""

    def reply(fn: CommandFn) -> Any:
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_message is None:
                return
            arg = " ".join(context.args or []) or None
            text = fn(service, arg)
            if inspect.isawaitable(text):
                text = await text
            for chunk in commands.split_message(str(text), TELEGRAM_MAX_CHARS):
                await update.effective_message.reply_text(chunk, link_preview_options=NO_PREVIEW)

        return handler

    table: dict[str, CommandFn] = {
        "status": lambda s, a: commands.status_text(s),
        "run": lambda s, a: commands.run_text(s),
        "pause": lambda s, a: commands.pause_text(s),
        "resume": lambda s, a: commands.resume_text(s),
        "top": lambda s, a: commands.top_text(s),
        "last_scores": commands.last_scores_text,
        "threshold": commands.threshold_text,
        "radius": commands.radius_text,
        "location": commands.location_text,
    }
    for name, fn in table.items():
        app.add_handler(CommandHandler(name, reply(fn)))


async def set_command_menu(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    await app.bot.set_my_commands([BotCommand(c, d[:256]) for c, d in COMMAND_MENU])
