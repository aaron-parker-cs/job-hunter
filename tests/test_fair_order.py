"""Scoring order is fair across search terms, so one title can't use up the scoring cap."""

from collections import Counter
from typing import Any

import pandas as pd

from job_hunter import commands
from job_hunter.config import Config
from job_hunter.fetch import fetch_jobs
from job_hunter.geo import Geocoder
from job_hunter.models import Job
from job_hunter.pipeline import interleave_by_search, run_once
from job_hunter.score import ScoreOutcome, ScoreResult, Usage
from job_hunter.service import Service
from job_hunter.store import RunSummary, Store

TERMS = ["Software Engineer", "Cloud Engineer", "DevOps Engineer", "SRE"]


def job(term: str, n: int, site: str = "indeed") -> Job:
    return Job(
        id=f"{term}-{site}-{n}".ljust(64, "0"),
        url=f"https://x/{term}/{site}/{n}",
        title=f"{term} {n}",
        company=f"Co {term} {site} {n}",
        location="Austin, TX",
        is_remote=False,
        salary_min=None,
        salary_max=None,
        salary_interval=None,
        date_posted=None,
        description="d",
        site=site,
        search_term=term,
    )


def make_cfg(**over: Any) -> Config:
    base = {
        "home_location": "Austin, TX",
        "searches": [{"term": t} for t in TERMS],
        "sites": ["indeed", "linkedin"],
    }
    return Config.model_validate(base | over)


class CountingScorer:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def score(self, j: Job) -> ScoreOutcome:
        self.seen.append(j.search_term)
        r = ScoreResult(
            score=50, verdict="maybe", reasons=[], concerns=[], seniority_match=True,
            explanation="e",
        )  # fmt: skip
        return ScoreOutcome(result=r, model="m", usage=Usage(), cost_usd=0.0)


def test_round_robin_across_terms_preserves_term_order() -> None:
    jobs = [job("A", i) for i in range(4)] + [job("B", i) for i in range(1)]
    jobs += [job("C", i) for i in range(2)]
    order = [j.search_term for j in interleave_by_search(jobs)]
    assert order == ["A", "B", "C", "A", "C", "A", "A"]


def test_sites_are_interleaved_within_a_term() -> None:
    jobs = [job("A", i, "indeed") for i in range(3)] + [job("A", i, "linkedin") for i in range(3)]
    sites = [j.site for j in interleave_by_search(jobs)]
    assert sites == ["indeed", "linkedin"] * 3


def test_interleave_keeps_every_job_exactly_once() -> None:
    jobs = [job(t, i, s) for t in TERMS for s in ("indeed", "linkedin") for i in range(7)]
    out = interleave_by_search(jobs)
    assert sorted(j.id for j in out) == sorted(j.id for j in jobs)
    assert interleave_by_search([]) == []


def run(store: Store, cfg: Config, jobs: list[Job]) -> tuple[RunSummary, CountingScorer]:
    scorer = CountingScorer()
    geocoder = Geocoder(store, lookup=lambda q: (30.27, -97.74), sleep=lambda s: None)
    summary, _ = run_once(cfg, store, geocoder, scorer, dry_run=False, fetcher=lambda c: jobs)
    return summary, scorer


def test_scoring_cap_is_shared_fairly_between_titles() -> None:
    # 120 candidates from the first title, 30 from each of the others; cap of 60.
    jobs = [job(TERMS[0], i, s) for s in ("indeed", "linkedin") for i in range(60)]
    jobs += [job(t, i) for t in TERMS[1:] for i in range(30)]
    cfg = make_cfg(scoring={"max_jobs_scored_per_run": 60})
    summary, scorer = run(Store(":memory:"), cfg, jobs)
    assert summary.scored == 60
    assert Counter(scorer.seen) == {t: 15 for t in TERMS}  # not 60 Software Engineer jobs


def test_unused_share_goes_to_the_other_titles() -> None:
    jobs = [job("Software Engineer", i) for i in range(40)] + [job("SRE", 0)]
    cfg = make_cfg(scoring={"max_jobs_scored_per_run": 10})
    _, scorer = run(Store(":memory:"), cfg, jobs)
    assert Counter(scorer.seen) == {"Software Engineer": 9, "SRE": 1}


def test_fetch_tags_jobs_with_their_search_term() -> None:
    def scrape(**kw: Any) -> pd.DataFrame:
        term = kw["search_term"]
        rows = [
            {
                "site": s,
                "title": f"{term} role",
                "company": f"{term} Co",
                "job_url": f"u/{term}/{s}",
            }
            for s in kw["site_name"]
        ]
        return pd.DataFrame(rows)

    jobs = fetch_jobs(make_cfg(), scrape=scrape, sleep=lambda s: None)
    assert {j.search_term for j in jobs} == set(TERMS)
    assert all(j.title.startswith(j.search_term) for j in jobs)


def test_search_term_is_stored_and_shown_in_last_scores() -> None:
    store = Store(":memory:")
    run(store, make_cfg(), [job("DevOps Engineer", 1)])
    saved = store.get_job(job("DevOps Engineer", 1).id)
    assert saved is not None and saved.search_term == "DevOps Engineer"
    service = Service(make_cfg(), store, [], lambda: (RunSummary(), []))
    assert "search: DevOps Engineer" in commands.last_scores_text(service)


def test_jobs_saved_before_the_column_existed_load_without_a_term() -> None:
    store = Store(":memory:")
    j = job("SRE", 1)
    store.add_job(j)
    store.conn.execute("UPDATE jobs SET search_term = NULL")
    loaded = store.get_job(j.id)
    assert loaded is not None and loaded.search_term == ""
