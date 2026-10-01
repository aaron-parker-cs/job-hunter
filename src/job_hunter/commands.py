"""Bot command implementations shared by Telegram and Discord (/status /run /pause /resume /top)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from job_hunter.notify.base import format_salary, where_text

if TYPE_CHECKING:
    from job_hunter.service import Service

TOP_DAYS = 7
TOP_COUNT = 5


def _local(iso: str, tz: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def status_text(service: Service) -> str:
    cfg, store = service.cfg, service.store
    if service.is_running:
        state = "running now"
    elif service.paused:
        state = "paused (scheduled runs skipped; /run still works)"
    else:
        state = "idle"
    lines = [f"State: {state}"]
    nxt = service.next_run_time()
    if nxt is not None and not service.paused:
        lines.append(f"Next run: {_local(nxt.isoformat(), cfg.timezone)}")

    run = store.last_run()
    if run is None:
        lines.append("Last run: none yet")
    else:
        lines.append(
            f"Last run: {_local(run['started_at'], cfg.timezone)} — fetched {run['fetched']}, "
            f"new {run['new_jobs']}, in range {run['in_radius']}, scored {run['scored']}, "
            f"sent {run['sent']}, est. ${run['cost_estimate']:.2f}"
        )
    queued = store.outbox_count()
    if queued:
        cooldown = service.cfg.notify.cooldown_seconds
        lines.append(f"Queued messages: {queued} (one every {cooldown // 60} min)")
    spent = store.spend_since(datetime.now(UTC) - timedelta(days=7))
    cap = service.weekly_budget
    lines.append(
        f"Spend (7 days): ${spent:.2f}" + (f" of ${cap:.2f}" if cap is not None else " (no cap)")
    )
    return "\n".join(lines)


def top_text(service: Service, *, wrap_links: bool = False) -> str:
    """Best unapplied matches from the last 7 days. wrap_links stops Discord link embeds."""
    since = datetime.now(UTC) - timedelta(days=TOP_DAYS)
    matches = service.store.top_matches(
        since=since, min_score=service.cfg.scoring.min_score_to_notify, limit=TOP_COUNT
    )
    if not matches:
        floor = service.cfg.scoring.min_score_to_notify
        return f"No unapplied matches scored {floor}+ in the last {TOP_DAYS} days."
    lines = [f"Top {len(matches)} unapplied matches (last {TOP_DAYS} days):"]
    for i, (job, result) in enumerate(matches, start=1):
        extra = [where_text(job)]
        salary = format_salary(job)
        if salary:
            extra.append(salary)
        link = f"<{job.url}>" if wrap_links else job.url
        lines.append(
            f"{i}. [{result.score}] {job.title} — {job.company} ({', '.join(extra)})\n   {link}"
        )
    return "\n".join(lines)


def run_text(service: Service) -> str:
    if service.start_run("manual"):
        return "Starting a run now. Matches (or a digest) will arrive here when it finishes."
    return "A run is already in progress."


def pause_text(service: Service) -> str:
    service.set_paused(True)
    return "Paused: scheduled runs are skipped. Use /resume to continue, or /run for a one-off."


def resume_text(service: Service) -> str:
    service.set_paused(False)
    return "Resumed: scheduled runs are back on."
