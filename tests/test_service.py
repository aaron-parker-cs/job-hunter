import asyncio
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from job_hunter import commands
from job_hunter.config import Config
from job_hunter.feedback import apply_feedback
from job_hunter.models import Job
from job_hunter.notify import telegram_bot
from job_hunter.notify.discord_bot import DiscordBot, GuardedTree, register_commands
from job_hunter.pipeline import ScoredJob
from job_hunter.score import ScoreOutcome, ScoreResult, Usage
from job_hunter.service import Service
from job_hunter.store import RunSummary, Store

CHAT, USER = 42, 7


def make_job(n: int = 0, **over: Any) -> Job:
    base: dict[str, Any] = {
        "id": f"{n:064x}",
        "url": f"https://x.example/{n}",
        "title": f"Job {n}",
        "company": f"Co{n}",
        "location": "Austin, TX",
        "is_remote": False,
        "salary_min": None,
        "salary_max": None,
        "salary_interval": None,
        "date_posted": None,
        "description": "d",
        "site": "indeed",
        "distance_miles": 5.0,
    }
    return Job(**(base | over))


def result(score: int) -> ScoreResult:
    return ScoreResult(
        score=score, verdict="strong", reasons=["r"], concerns=["c"], seniority_match=True
    )


def save(store: Store, job: Job, score: int, created: datetime | None = None) -> None:
    store.add_job(job)
    store.add_score(job.id, result(score).model_dump_json(), "m", 1, 1, 0, 0)
    if created:
        store.conn.execute(
            "UPDATE scores SET created_at = ? WHERE job_id = ?",
            (created.isoformat(timespec="seconds"), job.id),
        )
        store.conn.commit()


def make_cfg(**over: Any) -> Config:
    base = {"home_location": "Austin, TX", "searches": [{"term": "x"}]}
    return Config.model_validate(base | over)


class Recorder:
    name = "rec"

    def __init__(self) -> None:
        self.jobs: list[str] = []
        self.texts: list[str] = []

    async def send_job(self, job: Job, r: ScoreResult) -> None:
        self.jobs.append(job.title)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def aclose(self) -> None:
        pass


def make_service(
    store: Store, run_sync: Any = None, budget: float | None = None
) -> tuple[Service, Recorder]:
    rec = Recorder()
    save(store, make_job(1), 90)  # the run's worker has already stored it

    def default() -> tuple[RunSummary, list[ScoredJob]]:
        return RunSummary(scored=1), [
            ScoredJob(
                make_job(1), ScoreOutcome(result=result(90), model="m", usage=Usage(), cost_usd=0)
            )
        ]

    return Service(make_cfg(), store, [rec], run_sync or default, weekly_budget=budget), rec


def discord_interaction() -> Any:
    """A fake interaction whose response tracks is_done() like discord.py's."""
    state = {"done": False}

    async def send_message(*a: Any, **k: Any) -> None:
        state["done"] = True

    async def defer(*a: Any, **k: Any) -> None:
        state["done"] = True

    response = SimpleNamespace(
        send_message=AsyncMock(side_effect=send_message),
        defer=AsyncMock(side_effect=defer),
        is_done=lambda: state["done"],
    )
    return SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


# --- store additions ---------------------------------------------------------------


def test_state_roundtrip_and_migration_version(store: Store) -> None:
    assert store.get_state("k", "dflt") == "dflt"
    store.set_state("k", "v")
    assert store.get_state("k") == "v"
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_top_matches_filters_and_orders(store: Store) -> None:
    now = datetime.now(UTC)
    save(store, make_job(1), 95)
    save(store, make_job(2), 80)
    save(store, make_job(3), 99, created=now - timedelta(days=9))  # too old
    save(store, make_job(4), 60)  # below min score
    save(store, make_job(5), 90)
    save(store, make_job(6), 98)
    apply_feedback(store, make_job(6).id, "applied", "t")  # applied -> excluded
    store.add_job(make_job(8, url="https://x/8", company="Hidden"))
    store.add_score(make_job(8).id, result(96).model_dump_json(), "m", 1, 1, 0, 0)
    store.hide_company("Hidden")
    top = store.top_matches(since=now - timedelta(days=7), min_score=70, limit=5)
    assert [(j.title, r.score) for j, r in top] == [("Job 1", 95), ("Job 5", 90), ("Job 2", 80)]


def test_last_run(store: Store) -> None:
    assert store.last_run() is None
    store.record_run("2026-10-01T12:00:00+00:00", RunSummary(fetched=3, scored=2))
    run = store.last_run()
    assert run is not None and run["fetched"] == 3


# --- commands --------------------------------------------------------------------------


def test_status_idle_paused_and_budget(store: Store) -> None:
    service, _ = make_service(store, budget=5.0)
    text = commands.status_text(service)
    assert "State: idle" in text and "Last run: none yet" in text and "of $5.00" in text
    store.record_spend("m", 1.25)
    store.record_run("2026-10-01T12:00:00+00:00", RunSummary(fetched=9, new_jobs=4, sent=2))
    commands.pause_text(service)
    text = commands.status_text(service)
    assert "paused" in text and "fetched 9" in text and "$1.25 of $5.00" in text
    commands.resume_text(service)
    assert "State: idle" in commands.status_text(service)
    assert not service.paused


def test_pause_persists_across_service_instances(tmp_path: Path) -> None:
    path = tmp_path / "j.db"
    first, _ = make_service(Store(path))
    first.set_paused(True)
    second, _ = make_service(Store(path))
    assert second.paused


def test_top_text(store: Store) -> None:
    service = Service(make_cfg(), store, [], lambda: (RunSummary(), []))
    assert "No unapplied matches" in commands.top_text(service)
    save(store, make_job(1, salary_min=100000, salary_max=120000, salary_interval="yearly"), 91)
    text = commands.top_text(service, wrap_links=True)
    assert "[91] Job 1 — Co1" in text and "<https://x.example/1>" in text and "$100k" in text


# --- service runs ------------------------------------------------------------------------


async def test_run_delivers_matches(store: Store) -> None:
    service, rec = make_service(store)
    await service.execute("manual")
    assert rec.jobs == ["Job 1"] and not service.is_running


async def test_start_run_rejects_overlap_and_reports(store: Store) -> None:
    gate = asyncio.Event()
    calls: list[int] = []

    def slow() -> tuple[RunSummary, list[ScoredJob]]:
        calls.append(1)
        import time

        time.sleep(0.2)
        return RunSummary(), []

    service, rec = make_service(store, run_sync=slow)
    assert "Starting a run" in commands.run_text(service)
    await asyncio.sleep(0.05)
    assert service.is_running
    assert "already in progress" in commands.run_text(service)
    assert "State: running now" in commands.status_text(service)
    await asyncio.gather(*service._tasks)
    assert len(calls) == 1
    assert rec.texts and "none scored" in rec.texts[0]  # digest from deliver()
    gate.set()


async def test_scheduled_run_skipped_while_paused_but_manual_works(store: Store) -> None:
    service, rec = make_service(store)
    service.set_paused(True)
    await service.scheduled_run()
    assert rec.jobs == []
    await service.execute("manual")
    assert rec.jobs == ["Job 1"]
    service.set_paused(False)
    await service.scheduled_run()
    # the scheduled run happened (it queued a match) but the 5-minute cooldown holds it back
    assert len(rec.jobs) == 1 and store.outbox_count() == 1


async def test_failed_run_notifies_without_leaking_details(store: Store) -> None:
    def boom() -> tuple[RunSummary, list[ScoredJob]]:
        raise ValueError("secret-detail sk-ant-123456789")

    service, rec = make_service(store, run_sync=boom)
    await service.execute("manual")
    assert rec.texts and "ValueError" in rec.texts[0] and "secret-detail" not in rec.texts[0]
    assert not service.is_running  # lock released, next run can proceed


async def test_scheduler_wiring(store: Store) -> None:
    service, _ = make_service(store, budget=None)
    scheduler = AsyncIOScheduler(timezone="America/Chicago")
    service.attach_scheduler(scheduler)
    scheduler.start()
    try:
        nxt = service.next_run_time()
        assert nxt is not None and nxt.hour in (7, 12, 18) and nxt.minute == 0
        assert {j.id for j in scheduler.get_jobs()} == {"pipeline", "heartbeat", "outbox"}
    finally:
        scheduler.shutdown(wait=False)


def test_heartbeat_file(store: Store, tmp_path: Path) -> None:
    hb = tmp_path / ".heartbeat"
    service = Service(make_cfg(), store, [], lambda: (RunSummary(), []), heartbeat_path=hb)
    service.heartbeat()
    assert datetime.fromisoformat(hb.read_text()).tzinfo is not None


def test_invalid_cron_rejected_at_config_load() -> None:
    with pytest.raises(ValueError, match="schedule_cron"):
        make_cfg(schedule_cron="99 * * * *")


# --- telegram commands ----------------------------------------------------------------------


async def test_telegram_commands_registered_and_reply(store: Store) -> None:
    service, _ = make_service(store)
    handlers: list[Any] = []
    app = SimpleNamespace(add_handler=lambda h, group=0: handlers.append(h))
    telegram_bot.register_commands(app, service)  # type: ignore[arg-type]
    names = {next(iter(h.commands)) for h in handlers}
    assert names == {
        "status", "run", "pause", "resume", "top", "last_scores", "threshold", "radius", "location"
    }  # fmt: skip
    assert all("-" not in name for name in names)  # Telegram forbids hyphens in commands
    pause = next(h for h in handlers if "pause" in h.commands)
    message = SimpleNamespace(reply_text=AsyncMock())
    await pause.callback(SimpleNamespace(effective_message=message), SimpleNamespace(args=[]))
    assert service.paused and "Paused" in message.reply_text.call_args.args[0]


async def test_telegram_commands_blocked_for_other_chats(store: Store) -> None:
    from telegram.ext import ApplicationHandlerStop

    service, _ = make_service(store)
    notifier = telegram_bot.TelegramNotifier(AsyncMock(), CHAT, store, owns_bot=False)
    outsider = SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT + 1))
    with pytest.raises(ApplicationHandlerStop):  # guard runs in group -1, before any command
        await notifier._guard(outsider, None)  # type: ignore[arg-type]
    assert not service.paused


# --- discord commands ---------------------------------------------------------------------------


def test_discord_slash_commands_and_guard(store: Store) -> None:
    service, _ = make_service(store)
    bot = DiscordBot(store, 5, USER)
    register_commands(bot, service)
    assert bot.commands_enabled
    assert {c.name for c in bot.tree.get_commands()} == {
        "status", "run", "pause", "resume", "top", "last-scores", "threshold", "radius", "location"
    }  # fmt: skip
    assert isinstance(bot.tree, GuardedTree)


async def test_discord_tree_only_allows_configured_user(store: Store) -> None:
    bot = DiscordBot(store, 5, USER)
    ok = SimpleNamespace(user=SimpleNamespace(id=USER))
    bad = SimpleNamespace(user=SimpleNamespace(id=USER + 1))
    assert await bot.tree.interaction_check(ok)  # type: ignore[arg-type]
    assert not await bot.tree.interaction_check(bad)  # type: ignore[arg-type]


async def test_discord_command_callback(store: Store) -> None:
    service, _ = make_service(store)
    bot = DiscordBot(store, 5, USER)
    register_commands(bot, service)
    cmd = bot.tree.get_command("pause")
    assert cmd is not None
    interaction = discord_interaction()
    await cmd.callback(interaction)  # type: ignore[arg-type,call-arg]
    assert service.paused
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signal handlers")
async def test_serve_starts_heartbeats_and_stops_on_sigint(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from job_hunter.secrets import Secrets
    from job_hunter.service import serve

    cfg = make_cfg(notifier="discord")
    secrets = Secrets(
        anthropic_api_key=SecretStr("k" * 10),
        discord_webhook_url=SecretStr("https://discord.com/api/webhooks/1/x"),
    )
    asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGINT)
    await asyncio.wait_for(serve(cfg, secrets, tmp_path / "jobs.db", None), timeout=10)
    assert (tmp_path / ".heartbeat").exists()


# --- healthcheck ----------------------------------------------------------------------------------


def test_heartbeat_freshness(tmp_path: Path) -> None:
    from job_hunter.service import heartbeat_file, heartbeat_is_fresh

    hb = heartbeat_file(tmp_path / "jobs.db")
    assert hb == tmp_path / ".heartbeat"
    assert not heartbeat_is_fresh(hb)  # missing
    now = datetime.now(UTC)
    hb.write_text((now - timedelta(seconds=30)).isoformat())
    assert heartbeat_is_fresh(hb, now=now)
    hb.write_text((now - timedelta(seconds=600)).isoformat())
    assert not heartbeat_is_fresh(hb, now=now)  # stale: service stopped or hung
    hb.write_text("garbage")
    assert not heartbeat_is_fresh(hb)


def test_healthcheck_command_needs_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_hunter import __main__ as cli

    monkeypatch.chdir(tmp_path)  # no config, no secrets, no .env here
    monkeypatch.setenv("DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none"))
    assert cli.main(["healthcheck"]) == 1
    (tmp_path / ".heartbeat").write_text(datetime.now(UTC).isoformat())
    assert cli.main(["healthcheck"]) == 0


# --- notification cooldown / outbox ------------------------------------------------------------


def scored(n: int, score: int) -> ScoredJob:
    return ScoredJob(
        make_job(n), ScoreOutcome(result=result(score), model="m", usage=Usage(), cost_usd=0)
    )


def seed(store: Store, scores: list[int]) -> list[ScoredJob]:
    items = [scored(i + 10, s) for i, s in enumerate(scores)]
    for item in items:
        save(store, item.job, item.outcome.result.score)
    return items


async def test_deliver_queues_instead_of_sending_in_service_mode(store: Store) -> None:
    from job_hunter.pipeline import deliver

    rec = Recorder()
    items = seed(store, [95, 80, 60])  # 60 is below the threshold of 70
    summary = RunSummary()
    n = await deliver([rec], items, summary, make_cfg(), store, queue=True)
    assert n == 2 and rec.jobs == [] and store.outbox_count() == 2
    assert summary.sent == 2


async def test_zero_cooldown_sends_immediately_even_in_service_mode(store: Store) -> None:
    from job_hunter.pipeline import deliver

    rec = Recorder()
    items = seed(store, [95, 80])
    cfg = make_cfg(notify={"cooldown_seconds": 0})
    await deliver([rec], items, RunSummary(), cfg, store, queue=True)
    assert rec.jobs == ["Job 10", "Job 11"] and store.outbox_count() == 0


async def test_digest_is_sent_immediately_when_nothing_matches(store: Store) -> None:
    from job_hunter.pipeline import deliver

    rec = Recorder()
    await deliver([rec], seed(store, [10]), RunSummary(), make_cfg(), store, queue=True)
    assert len(rec.texts) == 1 and store.outbox_count() == 0


async def test_drain_releases_one_per_cooldown_best_first(store: Store) -> None:
    from job_hunter.pipeline import deliver

    service, rec = make_service(store)
    rec.jobs.clear()
    await deliver([rec], seed(store, [80, 99, 90]), RunSummary(), make_cfg(), store, queue=True)
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    assert await service.drain_outbox(t0) == 1  # first goes right away
    assert rec.jobs == ["Job 11"]  # the 99
    assert await service.drain_outbox(t0 + timedelta(seconds=299)) == 0  # still cooling down
    assert await service.drain_outbox(t0 + timedelta(seconds=300)) == 1
    assert await service.drain_outbox(t0 + timedelta(seconds=450)) == 0
    assert await service.drain_outbox(t0 + timedelta(seconds=600)) == 1
    assert rec.jobs == ["Job 11", "Job 12", "Job 10"]  # 99, 90, 80
    assert await service.drain_outbox(t0 + timedelta(seconds=900)) == 0
    assert store.outbox_count() == 0


async def test_cooldown_is_tracked_per_notifier(store: Store) -> None:
    from job_hunter.pipeline import deliver

    a, b = Recorder(), Recorder()
    a.name, b.name = "a", "b"
    service = Service(make_cfg(), store, [a, b], lambda: (RunSummary(), []))
    await deliver([a, b], seed(store, [90, 80]), RunSummary(), make_cfg(), store, queue=True)
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert await service.drain_outbox(t0) == 2  # one each
    assert a.jobs == ["Job 10"] and b.jobs == ["Job 10"]
    assert await service.drain_outbox(t0 + timedelta(seconds=60)) == 0


async def test_outbox_survives_a_restart(tmp_path: Path) -> None:
    from job_hunter.pipeline import deliver

    path = tmp_path / "j.db"
    first = Store(path)
    items = seed(first, [90, 80])
    await deliver([Recorder()], items, RunSummary(), make_cfg(), first, queue=True)
    first.close()

    rec = Recorder()
    service = Service(make_cfg(), Store(path), [rec], lambda: (RunSummary(), []))
    assert await service.drain_outbox() == 1 and rec.jobs == ["Job 10"]


async def test_failed_send_stays_queued_waits_cooldown_then_gives_up(store: Store) -> None:
    from job_hunter.pipeline import deliver

    class Flaky(Recorder):
        async def send_job(self, job: Job, r: ScoreResult) -> None:
            raise RuntimeError("telegram down")

    flaky = Flaky()
    service = Service(make_cfg(), store, [flaky], lambda: (RunSummary(), []))
    await deliver([flaky], seed(store, [90]), RunSummary(), make_cfg(), store, queue=True)
    t = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    for attempt in range(5):
        assert await service.drain_outbox(t + timedelta(seconds=300 * attempt)) == 0
        assert store.outbox_count() == (1 if attempt < 4 else 0)  # dropped on the 5th failure
    assert await service.drain_outbox(t + timedelta(seconds=300 * 4 + 10)) == 0


async def test_stale_outbox_items_are_pruned(store: Store) -> None:
    from job_hunter.pipeline import deliver

    service, rec = make_service(store)
    rec.jobs.clear()
    await deliver([rec], seed(store, [90]), RunSummary(), make_cfg(), store, queue=True)
    later = datetime.now(UTC) + timedelta(hours=49)
    assert await service.drain_outbox(later) == 0 and store.outbox_count() == 0


async def test_run_sends_first_match_now_and_queues_the_rest(store: Store) -> None:
    items = seed(store, [95, 85, 75])

    def run_sync() -> tuple[RunSummary, list[ScoredJob]]:
        return RunSummary(scored=3), items

    service, rec = make_service(store, run_sync=run_sync)
    rec.jobs.clear()
    await service.execute("manual")
    assert rec.jobs == ["Job 10"] and store.outbox_count() == 2
    assert "Queued messages: 2 (one every 5 min)" in commands.status_text(service)


def test_notify_config_validation() -> None:
    assert make_cfg().notify.cooldown_seconds == 300
    assert make_cfg(notify={"cooldown_seconds": 0}).notify.cooldown_seconds == 0
    for bad in ({"cooldown_seconds": -1}, {"nope": 1}):
        with pytest.raises(ValueError, match="notify"):
            make_cfg(notify=bad)
