"""Long-running service: scheduled and on-demand runs, pause state, heartbeat, `run` mode."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from job_hunter.bootstrap import ensure_not_template
from job_hunter.config import Config
from job_hunter.geo import Geocoder
from job_hunter.notify.base import Notifier
from job_hunter.pipeline import ScoredJob, deliver, run_once
from job_hunter.score import (
    ClaudeScorer,
    build_feedback_summary,
    build_system_prompt,
    load_text,
    make_scorer,
)
from job_hunter.secrets import Secrets
from job_hunter.store import RunSummary, Store

log = logging.getLogger(__name__)

RunSync = Callable[[], tuple[RunSummary, list[ScoredJob]]]
PAUSED_KEY = "paused"
SCHEDULE_JOB_ID = "pipeline"
HEARTBEAT_SECONDS = 60
DRAIN_SECONDS = 15
OUTBOX_MAX_AGE_HOURS = 48


def heartbeat_file(db_path: Path) -> Path:
    return Path(db_path).parent / ".heartbeat"


def heartbeat_is_fresh(
    path: Path, *, max_age_seconds: float = 180, now: datetime | None = None
) -> bool:
    """True if the running service wrote its heartbeat recently (used by the healthcheck)."""
    try:
        written = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    age = ((now or datetime.now(UTC)) - written).total_seconds()
    return 0 <= age <= max_age_seconds


def prepare_scorer(cfg: Config, secrets: Secrets, store: Store) -> ClaudeScorer:
    """Build a scorer from the current resume, profile and recent feedback.

    Called before every run, so edits to the resume/profile and new feedback take effect
    without restarting the service.
    """
    resume = load_text(cfg.resume_path)
    profile = load_text(cfg.profile_path)
    ensure_not_template(cfg.resume_path, resume)
    ensure_not_template(cfg.profile_path, profile)
    prompt = build_system_prompt(resume, profile, build_feedback_summary(store.recent_feedback()))
    return make_scorer(secrets.anthropic_api_key.get_secret_value(), cfg.scoring.model, prompt)


def make_run_sync(
    cfg: Config, secrets: Secrets, db_path: Path, weekly_budget: float | None
) -> RunSync:
    """A blocking pipeline run for a worker thread, using its own SQLite connection."""

    def run() -> tuple[RunSummary, list[ScoredJob]]:
        store = Store(db_path)
        try:
            scorer = prepare_scorer(cfg, secrets, store)
            return run_once(
                cfg, store, Geocoder(store), scorer, dry_run=False, weekly_budget=weekly_budget
            )
        finally:
            store.close()

    return run


class Service:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        notifiers: list[Notifier],
        run_sync: RunSync,
        *,
        weekly_budget: float | None = None,
        heartbeat_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.weekly_budget = weekly_budget
        self._notifiers = notifiers
        self._run_sync = run_sync
        self._heartbeat_path = heartbeat_path
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_run: Callable[[], datetime | None] = lambda: None

    # --- state --------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self.store.get_state(PAUSED_KEY) == "1"

    def set_paused(self, paused: bool) -> None:
        self.store.set_state(PAUSED_KEY, "1" if paused else "0")

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    def next_run_time(self) -> datetime | None:
        return self._next_run()

    # --- running ------------------------------------------------------------

    def start_run(self, trigger: str) -> bool:
        """Kick off a run in the background; False if one is already in progress."""
        if self.is_running:
            return False
        task = asyncio.create_task(self.execute(trigger))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def scheduled_run(self) -> None:
        if self.paused:
            log.info("scheduled run skipped: paused")
            return
        await self.execute("schedule")

    async def execute(self, trigger: str) -> None:
        if self.is_running:
            log.info("run (%s) skipped: another run is in progress", trigger)
            return
        async with self._lock:
            log.info("run started (%s)", trigger)
            try:
                summary, scored = await asyncio.to_thread(self._run_sync)
                await deliver(self._notifiers, scored, summary, self.cfg, self.store, queue=True)
                await self.drain_outbox()  # first match goes out now, the rest are spaced
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("run failed")
                # RuntimeErrors raised by this project carry vetted, secret-free messages.
                reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                await self._broadcast(f"job-hunter: run failed ({reason}). See the service logs.")

    async def drain_outbox(self, now: datetime | None = None) -> int:
        """Send at most one queued match per notifier, honouring the cooldown.

        Called every few seconds by the scheduler and after each run. /pause only stops
        scheduled *runs*; matches already queued keep trickling out. Failed sends stay queued
        (up to 5 attempts) and wait out the cooldown before the next try.
        """
        now = now or datetime.now(UTC)
        self.store.prune_outbox(now - timedelta(hours=OUTBOX_MAX_AGE_HOURS))
        cooldown = self.cfg.notify.cooldown_seconds
        sent = 0
        for notifier in self._notifiers:
            key = f"last_sent:{notifier.name}"
            last = self.store.get_state(key)
            if last and (now - datetime.fromisoformat(last)).total_seconds() < cooldown:
                continue
            row = self.store.next_outbox(notifier.name)
            if row is None:
                continue
            job = self.store.get_job(row["job_id"])
            result = self.store.get_score(row["job_id"])
            if job is None or result is None:
                self.store.complete_outbox(row["id"])
                continue
            self.store.set_state(key, now.isoformat())  # failures also wait out the cooldown
            try:
                await notifier.send_job(job, result)
            except Exception:
                log.exception("%s failed to send queued job %s", notifier.name, job.id[:8])
                self.store.fail_outbox(row["id"])
            else:
                self.store.complete_outbox(row["id"])
                sent += 1
        return sent

    async def _broadcast(self, text: str) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.send_text(text)
            except Exception:
                log.exception("%s failed to send message", notifier.name)

    async def cancel_runs(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- scheduling ---------------------------------------------------------

    def heartbeat(self) -> None:
        if self._heartbeat_path is not None:
            self._heartbeat_path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")

    def attach_scheduler(self, scheduler: AsyncIOScheduler) -> None:
        trigger = CronTrigger.from_crontab(self.cfg.schedule_cron, timezone=self.cfg.timezone)
        scheduler.add_job(
            self.scheduled_run,
            trigger,
            id=SCHEDULE_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        scheduler.add_job(self.heartbeat, "interval", seconds=HEARTBEAT_SECONDS, id="heartbeat")
        scheduler.add_job(
            self.drain_outbox, "interval", seconds=DRAIN_SECONDS, id="outbox", max_instances=1
        )

        def next_run() -> datetime | None:
            job = scheduler.get_job(SCHEDULE_JOB_ID)
            return getattr(job, "next_run_time", None) if job else None

        self._next_run = next_run


async def serve(cfg: Config, secrets: Secrets, db_path: Path, weekly_budget: float | None) -> None:
    """`run` mode: scheduler plus Telegram/Discord listeners on one event loop."""
    from job_hunter.notify import build_runtime

    store = Store(db_path)
    notifiers, listeners = build_runtime(cfg, secrets, store)
    heartbeat_path = heartbeat_file(db_path)
    service = Service(
        cfg,
        store,
        notifiers,
        make_run_sync(cfg, secrets, Path(db_path), weekly_budget),
        weekly_budget=weekly_budget,
        heartbeat_path=heartbeat_path,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            pass

    scheduler = AsyncIOScheduler(timezone=cfg.timezone)
    service.attach_scheduler(scheduler)
    discord_task: asyncio.Task[None] | None = None
    telegram_app: Any = listeners.telegram_app
    try:
        if telegram_app is not None:
            from job_hunter.notify.telegram_bot import register_commands, set_command_menu

            register_commands(telegram_app, service)
            await telegram_app.initialize()
            await set_command_menu(telegram_app)
            await telegram_app.start()
            await telegram_app.updater.start_polling()
        if listeners.discord_bot is not None and listeners.discord_token:
            from job_hunter.notify.discord_bot import register_commands as discord_commands

            discord_commands(listeners.discord_bot, service)
            discord_task = asyncio.create_task(listeners.discord_bot.start(listeners.discord_token))

            def _discord_done(task: asyncio.Task[None]) -> None:
                if not task.cancelled() and task.exception() is not None:
                    log.error("Discord bot stopped: %s", type(task.exception()).__name__)
                    stop.set()

            discord_task.add_done_callback(_discord_done)
        service.heartbeat()
        scheduler.start()
        log.info("service started; schedule=%r tz=%s", cfg.schedule_cron, cfg.timezone)
        await stop.wait()
    finally:
        log.info("shutting down")
        scheduler.shutdown(wait=False)
        await service.cancel_runs()
        if telegram_app is not None and telegram_app.running:
            await telegram_app.updater.stop()
            await telegram_app.stop()
        if telegram_app is not None:
            await telegram_app.shutdown()
        if listeners.discord_bot is not None:
            await listeners.discord_bot.close()
        if discord_task is not None:
            await asyncio.gather(discord_task, return_exceptions=True)
        store.close()
