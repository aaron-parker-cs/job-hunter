import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.ext import ApplicationHandlerStop

from job_hunter.config import Config
from job_hunter.feedback import ACTIONS, apply_feedback, revert_feedback
from job_hunter.models import Job
from job_hunter.notify.base import format_salary, where_text
from job_hunter.notify.discord_bot import (
    CHANNEL as DISCORD,
)
from job_hunter.notify.discord_bot import (
    DiscordWebhookNotifier,
    FeedbackButton,
    build_embed,
    handle_reaction,
)
from job_hunter.notify.telegram_bot import (
    CHOICE_MARKER,
    TelegramNotifier,
    format_job_html,
    handle_button,
    is_authorized,
)
from job_hunter.pipeline import ScoredJob, deliver, digest_text
from job_hunter.score import ScoreOutcome, ScoreResult, Usage
from job_hunter.store import RunSummary, Store

CHAT = 4242
USER = 777


def make_job(**over: Any) -> Job:
    base: dict[str, Any] = {
        "id": "a" * 64,
        "url": "https://x.example/1?a=1&b=<2>",
        "title": "DevOps <Engineer> & Co",
        "company": "Acme",
        "location": "Austin, TX",
        "is_remote": False,
        "salary_min": 110000.0,
        "salary_max": 140000.0,
        "salary_interval": "yearly",
        "date_posted": None,
        "description": "d",
        "site": "indeed",
        "distance_miles": 12.0,
    }
    return Job(**(base | over))


def make_result(score: int = 85) -> ScoreResult:
    return ScoreResult(
        score=score,
        verdict="strong",
        reasons=["Great fit", "Good pay", "third"],
        concerns=["Long commute", "second"],
        seniority_match=True,
    )


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.add_job(make_job())
    return s


# --- formatting ---------------------------------------------------------------------


def test_salary_and_where() -> None:
    assert format_salary(make_job()) == "$110k–$140k/yr"
    assert format_salary(make_job(salary_min=40, salary_max=55, salary_interval="hourly")) == (
        "$40–$55/hr"
    )
    assert format_salary(make_job(salary_min=None, salary_max=None)) is None
    assert where_text(make_job()) == "12 mi"
    assert where_text(make_job(is_remote=True)) == "Remote"
    assert "distance unknown" in where_text(make_job(distance_miles=None))


def test_telegram_message_is_escaped_and_complete() -> None:
    text = format_job_html(make_job(), make_result())
    assert "DevOps &lt;Engineer&gt; &amp; Co" in text
    assert "Acme" in text and "12 mi" in text and "85/100" in text
    assert "Great fit" in text and "Good pay" in text and "third" not in text  # 2 reasons
    assert "Long commute" in text and "second" not in text  # 1 concern
    assert 'href="https://x.example/1?a=1&amp;b=&lt;2&gt;"' in text


def test_discord_embed_fields() -> None:
    embed = build_embed(make_job(), make_result())
    assert embed.title and embed.url
    assert {f.name for f in embed.fields} >= {"Company", "Where", "Score", "Salary"}


# --- telegram guard + buttons --------------------------------------------------------


def tg_update(chat_id: int, data: str | None = None, message_id: int = 9) -> Any:
    message = SimpleNamespace(message_id=message_id, text_html="<b>Job</b>", reply_markup="kb")
    query = SimpleNamespace(
        data=data, message=message, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), callback_query=query)


def test_chat_guard() -> None:
    assert is_authorized(tg_update(CHAT), CHAT)
    assert not is_authorized(tg_update(CHAT + 1), CHAT)
    assert not is_authorized(SimpleNamespace(effective_chat=None), CHAT)


async def test_guard_handler_blocks_other_chats(store: Store) -> None:
    notifier = TelegramNotifier(AsyncMock(), CHAT, store)
    with pytest.raises(ApplicationHandlerStop):
        await notifier._guard(tg_update(CHAT + 1), None)  # type: ignore[arg-type]
    await notifier._guard(tg_update(CHAT), None)  # type: ignore[arg-type]  # no exception


async def test_send_job_records_notification_with_buttons(store: Store) -> None:
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    notifier = TelegramNotifier(bot, CHAT, store)
    await notifier.send_job(make_job(), make_result())
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == CHAT and kwargs["parse_mode"] == "HTML"
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [f"fb:{a}" for a in ACTIONS]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert store.job_for_message("telegram", "55") == "a" * 64


async def test_button_press_records_feedback_and_edits_message(store: Store) -> None:
    store.add_notification("a" * 64, "telegram", "9")
    update = tg_update(CHAT, "fb:interested")
    await handle_button(store, CHAT, update)
    assert store.recent_feedback() == [("interested", "DevOps <Engineer> & Co", "Acme")]
    edited = update.callback_query.edit_message_text.call_args
    assert edited.args[0] == "<b>Job</b>" + CHOICE_MARKER + "Interested"
    assert edited.kwargs["reply_markup"] == "kb"


async def test_button_from_wrong_chat_is_ignored(store: Store) -> None:
    store.add_notification("a" * 64, "telegram", "9")
    update = tg_update(CHAT + 1, "fb:hide_company")
    await handle_button(store, CHAT, update)
    assert store.recent_feedback() == [] and store.hidden_companies() == set()
    update.callback_query.answer.assert_not_called()


async def test_button_on_unknown_message(store: Store) -> None:
    update = tg_update(CHAT, "fb:interested", message_id=123)
    await handle_button(store, CHAT, update)
    assert store.recent_feedback() == []


async def test_hide_company_button_hides_company(store: Store) -> None:
    store.add_notification("a" * 64, "telegram", "9")
    await handle_button(store, CHAT, tg_update(CHAT, "fb:hide_company"))
    assert store.hidden_companies() == {"acme"}


# --- discord -----------------------------------------------------------------------------


def payload(emoji: str, user: int = USER, channel: int = 5, message: int = 100) -> Any:
    return SimpleNamespace(emoji=emoji, user_id=user, channel_id=channel, message_id=message)


def react(store: Store, p: Any, removed: bool = False, bot_id: int | None = 1) -> bool:
    return handle_reaction(
        store, p, allowed_user_id=USER, bot_user_id=bot_id, channel_id=5, removed=removed
    )


def test_reaction_add_and_remove(store: Store) -> None:
    store.add_notification("a" * 64, DISCORD, "100")
    assert react(store, payload(ACTIONS["not_fit"][1]))
    assert store.recent_feedback()[0][0] == "not_fit"
    assert react(store, payload(ACTIONS["not_fit"][1]), removed=True)
    assert store.recent_feedback() == []


def test_reaction_hide_company_round_trip(store: Store) -> None:
    store.add_notification("a" * 64, DISCORD, "100")
    react(store, payload(ACTIONS["hide_company"][1]))
    assert store.hidden_companies() == {"acme"}
    react(store, payload(ACTIONS["hide_company"][1]), removed=True)
    assert store.hidden_companies() == set()


@pytest.mark.parametrize(
    "p",
    [
        payload(ACTIONS["applied"][1], user=USER + 1),  # someone else
        payload(ACTIONS["applied"][1], user=1),  # the bot itself
        payload(ACTIONS["applied"][1], channel=6),  # other channel
        payload("\N{PARTY POPPER}"),  # unmapped emoji
        payload(ACTIONS["applied"][1], message=999),  # not one of our messages
    ],
)
def test_reactions_ignored(store: Store, p: Any) -> None:
    store.add_notification("a" * 64, DISCORD, "100")
    assert not react(store, p)
    assert store.recent_feedback() == []


def test_reaction_bot_id_equal_to_allowed_user_ignored(store: Store) -> None:
    store.add_notification("a" * 64, DISCORD, "100")
    assert not react(store, payload(ACTIONS["applied"][1]), bot_id=USER)


def test_dynamic_button_template_matches_custom_id() -> None:
    m = FeedbackButton.__discord_ui_compiled_template__.fullmatch(f"fb:hide_company:{'a' * 64}")
    assert m and m["action"] == "hide_company" and m["job"] == "a" * 64
    assert not FeedbackButton.__discord_ui_compiled_template__.fullmatch("fb:applied:short")


async def test_button_check_only_allows_configured_user() -> None:
    btn = FeedbackButton("applied", "a" * 64)
    client = SimpleNamespace(allowed_user_id=USER)
    ok = SimpleNamespace(client=client, user=SimpleNamespace(id=USER))
    bad = SimpleNamespace(client=client, user=SimpleNamespace(id=USER + 1))
    assert await btn.interaction_check(ok)  # type: ignore[arg-type]
    assert not await btn.interaction_check(bad)  # type: ignore[arg-type]


async def test_button_callback_records_feedback(store: Store) -> None:
    btn = FeedbackButton("applied", "a" * 64)
    embed = build_embed(make_job(), make_result())
    interaction = SimpleNamespace(
        client=SimpleNamespace(store=store, allowed_user_id=USER),
        message=SimpleNamespace(embeds=[embed]),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )
    await btn.callback(interaction)  # type: ignore[arg-type]
    assert store.recent_feedback()[0][0] == "applied"
    interaction.response.edit_message.assert_awaited_once()
    assert embed.footer.text == "Your choice: Applied"


async def test_webhook_fallback_posts_embed_without_feedback() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = DiscordWebhookNotifier("https://discord.com/api/webhooks/1/x", client)
    await n.send_job(make_job(), make_result())
    await n.send_text("digest")
    assert seen[0]["embeds"][0]["title"].startswith("DevOps")
    assert "components" not in seen[0]
    assert seen[1] == {"content": "digest"}
    await n.aclose()


# --- feedback helpers ---------------------------------------------------------------------


def test_apply_feedback_validates(store: Store) -> None:
    assert not apply_feedback(store, "b" * 64, "interested", "x")  # unknown job
    assert not apply_feedback(store, "a" * 64, "bogus", "x")  # unknown action
    assert apply_feedback(store, "a" * 64, "interested", "x")
    assert apply_feedback(store, "a" * 64, "interested", "x")  # idempotent
    assert len(store.recent_feedback()) == 1
    assert revert_feedback(store, "a" * 64, "interested", "x")
    assert store.recent_feedback() == []


def test_test_jobs_excluded_from_feedback_summary(store: Store) -> None:
    store.add_job(make_job(id="c" * 64, url="https://t/1", site="test"))
    apply_feedback(store, "c" * 64, "interested", "x")
    assert store.recent_feedback() == []


# --- delivery ------------------------------------------------------------------------------


class RecordingNotifier:
    def __init__(self, name: str = "rec", fail: bool = False) -> None:
        self.name, self.fail = name, fail
        self.jobs: list[str] = []
        self.texts: list[str] = []

    async def send_job(self, job: Job, result: ScoreResult) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.jobs.append(job.title)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def aclose(self) -> None:
        pass


def scored_jobs(scores: list[int]) -> list[ScoredJob]:
    return [
        ScoredJob(
            make_job(id=f"{i:064x}", title=f"job{i}"),
            ScoreOutcome(result=make_result(s), model="m", usage=Usage(), cost_usd=0),
        )
        for i, s in enumerate(scores)
    ]


def cfg() -> Config:
    return Config.model_validate({"home_location": "Austin, TX", "searches": [{"term": "x"}]})


async def test_deliver_filters_orders_and_caps(store: Store) -> None:
    n = RecordingNotifier()
    summary = RunSummary()
    scores = [50, 99, 70, 69] + [90] * 20
    sent = await deliver([n], scored_jobs(scores), summary, cfg(), store)
    assert sent == 15 and len(n.jobs) == 15 and n.texts == []
    assert n.jobs[0] == "job1"  # highest score first
    assert "job0" not in n.jobs and "job3" not in n.jobs  # below threshold 70
    assert summary.sent == 15


async def test_deliver_digest_when_nothing_matches(store: Store) -> None:
    n = RecordingNotifier()
    summary = RunSummary(fetched=10, new_jobs=4, in_radius=3, scored=3, budget_exhausted=True)
    assert await deliver([n], scored_jobs([10, 20]), summary, cfg(), store) == 0
    assert len(n.texts) == 1 and "none scored 70+" in n.texts[0]
    assert "budget" in digest_text(summary, 70)


async def test_one_failing_notifier_does_not_block_others(store: Store) -> None:
    bad, good = RecordingNotifier("bad", fail=True), RecordingNotifier("good")
    summary = RunSummary()
    sent = await deliver([bad, good], scored_jobs([90, 80]), summary, cfg(), store)
    assert sent == 2 and good.jobs == ["job0", "job1"]


# --- factory / bot construction -------------------------------------------------------------


def secrets_for(**over: Any) -> Any:
    from pydantic import SecretStr

    from job_hunter.secrets import Secrets

    base: dict[str, Any] = {"anthropic_api_key": SecretStr("k" * 10)}
    return Secrets(**(base | over))


def test_build_notifiers_selection(store: Store) -> None:
    from pydantic import SecretStr

    from job_hunter.notify import build_notifiers

    tg = {"telegram_bot_token": SecretStr("1:" + "t" * 35), "telegram_chat_id": CHAT}
    dc = {
        "discord_bot_token": SecretStr("d" * 30),
        "discord_channel_id": 5,
        "discord_allowed_user_id": USER,
    }
    hook = {"discord_webhook_url": SecretStr("https://discord.com/api/webhooks/1/x")}

    def names(notifier: str, **secrets: Any) -> list[str]:
        c = Config.model_validate(
            {"home_location": "A, TX", "searches": [{"term": "x"}], "notifier": notifier}
        )
        return [n.name for n in build_notifiers(c, secrets_for(**secrets), store)]

    assert names("telegram", **tg) == ["telegram"]
    assert names("both", **tg, **dc) == ["telegram", "discord"]
    assert names("discord", **hook) == ["discord-webhook"]
    assert names("discord", **dc, **hook) == ["discord"]  # bot wins over webhook


def test_discord_bot_intents_need_no_message_content(store: Store) -> None:
    from job_hunter.notify.discord_bot import DiscordBot

    intents = DiscordBot(store, 5, USER).intents
    assert intents.guilds and intents.guild_messages and intents.guild_reactions
    assert not intents.message_content and not intents.members
