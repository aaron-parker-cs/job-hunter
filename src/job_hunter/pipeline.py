"""fetch -> normalize -> filter -> dedup -> (score -> notify in later milestones)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from job_hunter.config import Config
from job_hunter.fetch import fetch_jobs, plan_fetch
from job_hunter.geo import Coords, Geocoder, haversine_miles
from job_hunter.models import Job, normalize_company
from job_hunter.notify.base import Notifier
from job_hunter.score import FatalScoringError, JobScorer, ScoreOutcome, ScoringError
from job_hunter.store import RunSummary, Store

log = logging.getLogger(__name__)

SEARCH_CURSOR_KEY = "search_cursor"
MAX_CONSECUTIVE_SCORING_FAILURES = 3


def passes_static_filters(job: Job, cfg: Config, hidden: set[str]) -> bool:
    company = normalize_company(job.company)
    excluded = {normalize_company(c) for c in cfg.exclude_companies} | hidden
    if company in excluded:
        return False
    title = job.title.lower()
    for kw in cfg.exclude_title_keywords:
        if re.search(rf"\b{re.escape(kw.lower())}\b", title):
            return False
    return True


def dedup(jobs: list[Job], store: Store) -> list[Job]:
    """Drop jobs already stored or repeated within this batch (by id or url)."""
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        if job.id in seen_ids or job.url in seen_urls or store.is_known(job):
            continue
        seen_ids.add(job.id)
        seen_urls.add(job.url)
        out.append(job)
    return out


def apply_location_filter(
    jobs: list[Job], cfg: Config, geocoder: Geocoder, home: Coords
) -> list[Job]:
    """Keep remote jobs (if enabled) and jobs within radius; flag unknown locations."""
    kept: list[Job] = []
    for job in jobs:
        if job.is_remote:
            if cfg.include_remote:
                kept.append(job)
            continue
        coords = geocoder.geocode(job.location)
        if coords is None:
            job.location_unknown = True
            kept.append(job)
            continue
        job.distance_miles = round(haversine_miles(home, coords), 1)
        if job.distance_miles <= cfg.radius_miles:
            kept.append(job)
    return kept


@dataclass
class ScoredJob:
    job: Job
    outcome: ScoreOutcome


def score_jobs(
    jobs: list[Job],
    cfg: Config,
    store: Store,
    scorer: JobScorer,
    summary: RunSummary,
    *,
    weekly_budget: float | None,
    dry_run: bool,
) -> list[ScoredJob]:
    """Score up to the per-run cap and weekly budget.

    A job is saved (and so deduped in future runs) only once it has been scored, so jobs
    skipped by a guardrail or a transient API failure are picked up again next run.
    """
    spent_week = store.spend_since(datetime.now(UTC) - timedelta(days=7))
    results: list[ScoredJob] = []
    failures = 0
    for job in jobs:
        if summary.scored >= cfg.scoring.max_jobs_scored_per_run:
            log.warning("per-run scoring cap reached (%d)", summary.scored)
            break
        if weekly_budget is not None and spent_week + summary.cost_estimate >= weekly_budget:
            summary.budget_exhausted = True
            log.warning("weekly budget of $%.2f reached; scoring stopped", weekly_budget)
            break
        try:
            outcome = scorer.score(job)
        except FatalScoringError:
            raise
        except ScoringError as exc:
            failures += 1
            log.warning("skipping job after scoring failure: %s", exc)
            if failures >= MAX_CONSECUTIVE_SCORING_FAILURES:
                # Same error over and over (billing, bad request, outage): stop burning calls.
                if not results:
                    raise FatalScoringError(
                        f"{failures} scoring failures in a row; last error: {exc}"
                    ) from None
                log.error("%d scoring failures in a row; stopping scoring for this run", failures)
                break
            continue
        failures = 0
        store.record_spend(outcome.model, outcome.cost_usd)  # ledger counts dry runs too
        summary.scored += 1
        summary.cost_estimate += outcome.cost_usd
        if not dry_run:
            u = outcome.usage
            store.add_job(job)
            store.add_score(
                job.id,
                outcome.result.model_dump_json(),
                outcome.model,
                u.input_tokens,
                u.output_tokens,
                u.cache_write_tokens,
                u.cache_read_tokens,
            )
        results.append(ScoredJob(job, outcome))
    return results


def run_once(
    cfg: Config,
    store: Store,
    geocoder: Geocoder,
    scorer: JobScorer,
    *,
    dry_run: bool,
    weekly_budget: float | None = None,
    fetcher: Callable[[Config], list[Job]] = fetch_jobs,
) -> tuple[RunSummary, list[ScoredJob]]:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    summary = RunSummary()

    home = geocoder.geocode(cfg.home_location)
    if home is None:
        raise RuntimeError(f"Could not geocode home_location {cfg.home_location!r}")

    cursor = int(store.get_state(SEARCH_CURSOR_KEY, "0") or 0)
    run_cfg, next_cursor = plan_fetch(cfg, cursor)
    fetched = fetcher(run_cfg)
    if not dry_run:
        store.set_state(SEARCH_CURSOR_KEY, str(next_cursor))
    summary.fetched = len(fetched)

    hidden = store.hidden_companies()
    candidates = [j for j in fetched if passes_static_filters(j, cfg, hidden)]
    new = dedup(candidates, store)  # before geocoding, to spare Nominatim requests
    summary.new_jobs = len(new)
    in_radius = apply_location_filter(new, cfg, geocoder, home)
    summary.in_radius = len(in_radius)

    try:
        scored = score_jobs(
            in_radius, cfg, store, scorer, summary, weekly_budget=weekly_budget, dry_run=dry_run
        )
    finally:
        if not dry_run:
            summary.run_id = store.record_run(started, summary)
    scored.sort(key=lambda s: s.outcome.result.score, reverse=True)
    log.info(
        "run complete: fetched=%d new=%d in_radius=%d scored=%d cost=$%.4f dry_run=%s",
        summary.fetched,
        summary.new_jobs,
        summary.in_radius,
        summary.scored,
        summary.cost_estimate,
        dry_run,
    )
    return summary, scored


MAX_NOTIFICATIONS_PER_RUN = 15


def digest_text(summary: RunSummary, threshold: int) -> str:
    text = (
        f"job-hunter: fetched {summary.fetched}, new {summary.new_jobs}, in range "
        f"{summary.in_radius}, scored {summary.scored}; none scored {threshold}+ "
        f"(est. ${summary.cost_estimate:.2f})."
    )
    if summary.budget_exhausted:
        text += " Weekly budget reached, so some jobs were not scored."
    return text


async def deliver(
    notifiers: list[Notifier],
    scored: list[ScoredJob],
    summary: RunSummary,
    cfg: Config,
    store: Store,
    *,
    queue: bool = False,
) -> int:
    """Send the best matches (highest first) to every notifier; digest if nothing matched.

    With queue=True (service mode) and a cooldown configured, matches go into the outbox and
    Service.drain_outbox releases them one per cooldown period. Otherwise they are sent now.
    One notifier failing never blocks the others. Returns the number of matches handled.
    """
    threshold = cfg.scoring.min_score_to_notify
    matches = sorted(
        (s for s in scored if s.outcome.result.score >= threshold),
        key=lambda s: s.outcome.result.score,
        reverse=True,
    )[:MAX_NOTIFICATIONS_PER_RUN]

    if queue and cfg.notify.cooldown_seconds > 0:
        for item in matches:
            for notifier in notifiers:
                store.enqueue(notifier.name, item.job.id, item.outcome.result.score)
        summary.sent = len(matches)
        if summary.run_id:
            store.set_run_sent(summary.run_id, summary.sent)
        if not matches:
            await _send_digest(notifiers, summary, threshold)
        return len(matches)

    sent = 0
    for item in matches:
        delivered = False
        for notifier in notifiers:
            try:
                await notifier.send_job(item.job, item.outcome.result)
                delivered = True
            except Exception:
                log.exception("%s failed to send job %s", notifier.name, item.job.id[:8])
        sent += delivered

    if not matches:
        await _send_digest(notifiers, summary, threshold)

    summary.sent = sent
    if summary.run_id:
        store.set_run_sent(summary.run_id, sent)
    return sent


async def _send_digest(notifiers: list[Notifier], summary: RunSummary, threshold: int) -> None:
    for notifier in notifiers:
        try:
            await notifier.send_text(digest_text(summary, threshold))
        except Exception:
            log.exception("%s failed to send digest", notifier.name)
