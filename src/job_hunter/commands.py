"""Bot command implementations shared by Telegram and Discord.

/status /run /pause /resume /top /last-scores /threshold /radius /location. Each returns the
reply text; the bots handle delivery (and split long replies with split_message).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from job_hunter import settings
from job_hunter.models import Job
from job_hunter.notify.base import format_salary, where_text

if TYPE_CHECKING:
    from job_hunter.service import Service

TOP_DAYS = 7
TOP_COUNT = 5
LAST_SCORES_DEFAULT = 5
LAST_SCORES_MAX = 20


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
    lines.append(
        f"Threshold: {settings.describe(service.base_cfg, store, 'threshold')}; "
        f"radius: {settings.describe(service.base_cfg, store, 'radius')}; "
        f"location: {settings.describe(service.base_cfg, store, 'location')}"
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


# --- /last-scores ---------------------------------------------------------------------------


def last_scores_text(service: Service, arg: str | None = None, *, wrap_links: bool = False) -> str:
    """The top n scores (default 5) from the last run that scored anything, with explanations."""
    try:
        n = int(arg) if arg and arg.strip() else LAST_SCORES_DEFAULT
    except ValueError:
        n = 0
    if not 1 <= n <= LAST_SCORES_MAX:
        return f"Usage: /last_scores [n], with n from 1 to {LAST_SCORES_MAX} (default 5)."

    store, cfg = service.store, service.cfg
    run = store.latest_scored_run()
    if run is None:
        return "No run has scored any jobs yet."
    items, total = store.run_scores(run, n)
    threshold = cfg.scoring.min_score_to_notify
    when = _local(run["started_at"], cfg.timezone)
    header = (
        f"Top {len(items)} of {total} scored in the run at {when} (notify threshold {threshold}):"
    )
    latest = store.last_run()
    if latest is not None and latest["id"] != run["id"]:
        header = "The most recent run scored nothing new; this is the last run that did.\n" + header
    blocks = [header]
    for i, (job, result, explanation) in enumerate(items, start=1):
        mark = "\u2705" if result.score >= threshold else "\u2716"
        link = f"<{job.url}>" if wrap_links else job.url
        why = explanation or result.explanation or _legacy_reasons(result.reasons, result.concerns)
        blocks.append(
            f"{i}. {mark} [{result.score}] {job.title} \u2014 {job.company} "
            f"({_where_and_search(job)})\n{why}\n{link}"
        )
    return "\n\n".join(blocks)


def _where_and_search(job: Job) -> str:
    where = where_text(job)
    return f"{where}; search: {job.search_term}" if job.search_term else where


def _legacy_reasons(reasons: list[str], concerns: list[str]) -> str:
    """Scores saved before explanations existed: summarise what they do have."""
    parts = ["(No explanation stored: scored before explanations were added.)"]
    if reasons:
        parts.append("Reasons: " + "; ".join(reasons))
    if concerns:
        parts.append("Concerns: " + "; ".join(concerns))
    return " ".join(parts)


# --- /threshold /radius /location -------------------------------------------------------------

NOTES = {
    "threshold": (
        "It applies to jobs scored from now on. Recent jobs that now clear it show up in /top."
    ),
    "radius": "It applies from the next run, to both the search and the distance filter.",
    "location": (
        "It applies from the next run, to the search and distances. Jobs already saved keep "
        "the distance they were given."
    ),
}
USAGE = {
    "threshold": "/threshold <0-100>",
    "radius": "/radius <miles>",
    "location": "/location <City, ST>",
}
LABELS = {"threshold": "Notify threshold", "radius": "Search radius", "location": "Home location"}


def _show_or_reset(service: Service, key: str, arg: str | None) -> str | None:
    """Handle the no-argument and 'reset' forms; None means a new value was given."""
    if not arg or not arg.strip():
        current = settings.describe(service.base_cfg, service.store, key)
        return f"{LABELS[key]}: {current}.\nChange it with {USAGE[key]}, or 'reset' to go back."
    if arg.strip().lower() == "reset":
        settings.reset_setting(service.store, key)
        current = settings.describe(service.base_cfg, service.store, key)
        return f"{LABELS[key]} reset to {current}."
    return None


def _set(service: Service, key: str, arg: str) -> str:
    try:
        value = settings.set_setting(service.store, key, arg)
    except settings.SettingError as exc:
        return f"{exc} Usage: {USAGE[key]}, or 'reset'."
    shown = settings.SETTINGS[key].show(value)
    return f"{LABELS[key]} set to {shown}. {NOTES[key]}"


def threshold_text(service: Service, arg: str | None = None) -> str:
    return _show_or_reset(service, "threshold", arg) or _set(service, "threshold", arg or "")


def radius_text(service: Service, arg: str | None = None) -> str:
    return _show_or_reset(service, "radius", arg) or _set(service, "radius", arg or "")


async def location_text(service: Service, arg: str | None = None) -> str:
    shown = _show_or_reset(service, "location", arg)
    if shown is not None:
        return shown
    try:
        value = settings.SETTINGS["location"].parse(arg or "")
    except settings.SettingError as exc:
        return f"{exc} Usage: {USAGE['location']}, or 'reset'."
    try:
        coords = await service.locate(value)
    except Exception:
        return "Couldn't reach the geocoding service to check that location. Try again later."
    if coords is None:
        return f"Couldn't find {value!r} on the map. Try the form 'City, ST'."
    settings.set_setting(service.store, "location", value)
    where = f"{coords[0]:.3f}, {coords[1]:.3f}"
    return f"{LABELS['location']} set to {value} ({where}). {NOTES['location']}"


# --- delivery helper ------------------------------------------------------------------------


def split_message(text: str, limit: int) -> list[str]:
    """Split a reply into chunks under a platform's limit, preferring paragraph boundaries."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # a single oversized paragraph: hard-split it
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:limit])
            para = para[limit:]
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks or [""]
