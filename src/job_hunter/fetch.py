"""JobSpy wrapper: one call per (search term x site group)."""

from __future__ import annotations

import logging
import math
import random
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from job_hunter.config import Config, Search
from job_hunter.models import Job, normalize_row

log = logging.getLogger(__name__)

# Google ignores most search params and needs google_search_term, so it gets its own call.
SiteGroup = list[str]


def site_groups(sites: list[str]) -> list[SiteGroup]:
    groups = [[s for s in sites if s != "google"], [s for s in sites if s == "google"]]
    return [g for g in groups if g]


def _default_scrape(**kwargs: Any) -> Any:
    from jobspy import scrape_jobs

    return scrape_jobs(**kwargs)


def select_searches(searches: list[Search], per_run: int, cursor: int) -> tuple[list[Search], int]:
    """Pick this run's slice of the searches, rotating so every term gets its turn."""
    n = len(searches)
    if n <= per_run:
        return list(searches), 0
    start = cursor % n
    chosen = [searches[(start + i) % n] for i in range(per_run)]
    return chosen, (start + per_run) % n


def average_gap_hours(cron: str, timezone: str) -> float:
    """Average hours between scheduled runs, measured over the next 7 days."""
    trigger = CronTrigger.from_crontab(cron, timezone=timezone)
    now = datetime.now(ZoneInfo(timezone))
    end = now + timedelta(days=7)
    fires = 0
    prev: datetime | None = None
    cursor = now
    while True:
        nxt = trigger.get_next_fire_time(prev, cursor)
        if nxt is None or nxt >= end:
            break
        fires += 1
        prev, cursor = nxt, nxt
    return 7 * 24 / max(fires, 1)


def plan_fetch(cfg: Config, cursor: int) -> tuple[Config, int]:
    """Config for this run: a rotating slice of searches, with hours_old widened to match.

    When searches rotate, each term is only searched every `cycles` runs, so the lookback
    window must cover that gap or postings would slip through unseen. Dedup keeps the wider
    window from causing re-scoring.
    """
    searches, next_cursor = select_searches(cfg.searches, cfg.fetch.searches_per_run, cursor)
    cycles = math.ceil(len(cfg.searches) / cfg.fetch.searches_per_run)
    hours_old = cfg.hours_old
    if cycles > 1:
        needed = math.ceil(average_gap_hours(cfg.schedule_cron, cfg.timezone) * cycles)
        hours_old = max(cfg.hours_old, needed)
        log.info(
            "rotating searches: %d of %d this run (%d-run cycle), hours_old=%d",
            len(searches),
            len(cfg.searches),
            cycles,
            hours_old,
        )
    return cfg.model_copy(update={"searches": searches, "hours_old": hours_old}), next_cursor


def fetch_jobs(
    cfg: Config,
    *,
    scrape: Callable[..., Any] = _default_scrape,
    sleep: Callable[[float], None] = time.sleep,
    delay_range: tuple[float, float] | None = None,
) -> list[Job]:
    """Scrape every configured search, politely.

    A failing call is logged and skipped. After `max_consecutive_failures` in a row for one
    site group (likely blocked or rate limited), that group is skipped for the rest of the run
    instead of being hit again.
    """
    delay = delay_range or cfg.fetch.delay_seconds
    limit = cfg.fetch.max_consecutive_failures
    records: list[tuple[str, dict[str, Any]]] = []  # (search term, JobSpy row)
    errors: dict[tuple[str, ...], int] = {}  # consecutive exceptions per site group
    empty_streak: dict[str, int] = {}  # consecutive zero-row calls per site
    disabled: set[str] = set()
    first = True
    for search in cfg.searches:
        for base_group in site_groups(list(cfg.sites)):
            group = [site for site in base_group if site not in disabled]
            key = tuple(base_group)
            if not group or errors.get(key, 0) >= limit:
                continue
            if not first:
                sleep(random.uniform(*delay))  # noqa: S311 - jitter, not crypto
            first = False
            kwargs: dict[str, Any] = {
                "site_name": group,
                "search_term": search.term,
                "location": cfg.home_location,
                "distance": int(cfg.radius_miles),
                "hours_old": cfg.hours_old,
                "results_wanted": cfg.results_per_search,
                "is_remote": False,  # remote postings still come back and are flagged
                "country_indeed": "USA",
                "linkedin_fetch_description": "linkedin" in group
                and cfg.fetch.linkedin_fetch_description,
                "verbose": 0,
            }
            try:
                df = scrape(**kwargs)
            except Exception:
                errors[key] = errors.get(key, 0) + 1
                log.exception("scrape failed: term=%r sites=%s", search.term, group)
                if errors[key] >= limit:
                    log.warning(
                        "skipping %s for the rest of this run after repeated failures", group
                    )
                continue
            errors[key] = 0
            rows: list[dict[str, Any]] = df.to_dict("records") if df is not None else []
            by_site = Counter(str(row.get("site", "")) for row in rows)
            log.info("scrape ok: term=%r rows=%d by_site=%s", search.term, len(rows), dict(by_site))
            # JobSpy logs a blocked site and carries on, so the only sign of a block is a
            # site that keeps returning nothing. Stop asking it for the rest of this run.
            for site in group:
                if by_site.get(site, 0) > 0:
                    empty_streak[site] = 0
                    continue
                empty_streak[site] = empty_streak.get(site, 0) + 1
                if empty_streak[site] >= limit and site not in disabled:
                    disabled.add(site)
                    log.warning(
                        "%s returned no rows %d times in a row; skipping it for the rest of "
                        "this run (blocked, rate limited or broken upstream?)",
                        site,
                        empty_streak[site],
                    )
            records.extend((search.term, row) for row in rows)
    if disabled:
        log.warning("sites skipped this run after returning no rows: %s", sorted(disabled))

    jobs: list[Job] = []
    for term, row in records:
        job = normalize_row(row)
        if job is not None:
            job.search_term = term
            jobs.append(job)
    return jobs
