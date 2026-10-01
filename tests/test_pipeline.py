from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from job_hunter.config import Config
from job_hunter.fetch import fetch_jobs, site_groups
from job_hunter.geo import Coords, Geocoder, clean_location, haversine_miles
from job_hunter.models import MAX_DESCRIPTION_CHARS, Job, make_job_id, normalize_row
from job_hunter.pipeline import dedup, passes_static_filters, run_once
from job_hunter.score import ScoreOutcome, ScoreResult, ScoringError, Usage
from job_hunter.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "jobspy_sample.csv"
AUSTIN: Coords = (30.2672, -97.7431)
DALLAS: Coords = (32.7767, -96.7970)
PLACES: dict[str, Coords | None] = {
    "austin, tx": AUSTIN,
    "dallas, tx": DALLAS,
    "somewhere weird": None,
}


def make_cfg(**over: Any) -> Config:
    base: dict[str, Any] = {
        "home_location": "Austin, TX",
        "radius_miles": 50,
        "searches": [{"term": "DevOps Engineer"}, {"term": "SRE"}],
        "exclude_title_keywords": ["intern", "senior director"],
    }
    return Config.model_validate(base | over)


class FakeScorer:
    def __init__(self, score: int = 80, cost: float = 0.01, fail_on: str | None = None) -> None:
        self.score_value, self.cost, self.fail_on = score, cost, fail_on
        self.calls: list[str] = []

    def score(self, job: Job) -> ScoreOutcome:
        self.calls.append(job.company)
        if job.company == self.fail_on:
            raise ScoringError("boom")
        result = ScoreResult(
            score=self.score_value,
            verdict="strong",
            reasons=["r"],
            concerns=["c"],
            seniority_match=True,
        )
        return ScoreOutcome(
            result=result, model="claude-haiku-4-5", usage=Usage(), cost_usd=self.cost
        )


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def geocoder(store: Store) -> Geocoder:
    return Geocoder(store, lookup=lambda q: PLACES.get(q), sleep=lambda s: None)


def fixture_df() -> pd.DataFrame:
    return pd.read_csv(FIXTURE)


def fixture_jobs() -> list[Job]:
    jobs = [normalize_row(r) for r in fixture_df().to_dict("records")]
    return [j for j in jobs if j]


# --- normalization / ids ---------------------------------------------------


def test_normalize_handles_nan_and_drops_incomplete() -> None:
    jobs = fixture_jobs()
    assert len(jobs) == 7  # the row without a title is dropped
    acme = jobs[0]
    assert acme.salary_min == 110000 and acme.salary_interval == "yearly"
    assert not acme.is_remote
    mystery = next(j for j in jobs if j.company == "Mystery LLC")
    assert mystery.date_posted is None and mystery.salary_min is None


def test_id_is_stable_across_boards_and_formatting() -> None:
    a = make_job_id("Acme Corp", "DevOps Engineer", "Austin, TX, US")
    b = make_job_id("ACME Corp.", "devops  engineer", "austin, TX")
    assert a == b and len(a) == 64
    assert a != make_job_id("Acme Corp", "DevOps Engineer", "Dallas, TX")


def test_description_truncated() -> None:
    row = {"title": "t", "company": "c", "job_url": "u", "description": "x" * 10000}
    job = normalize_row(row)
    assert job and len(job.description) == MAX_DESCRIPTION_CHARS


# --- geo --------------------------------------------------------------------


def test_haversine_austin_dallas() -> None:
    assert haversine_miles(AUSTIN, DALLAS) == pytest.approx(182, abs=3)
    assert haversine_miles(AUSTIN, AUSTIN) == 0


def test_clean_location() -> None:
    assert clean_location("  Austin,  TX, US ") == "austin, tx"


def test_geocoder_caches_including_misses_and_rate_limits(store: Store) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def lookup(q: str) -> Coords | None:
        calls.append(q)
        return PLACES.get(q)

    g = Geocoder(store, lookup=lookup, sleep=sleeps.append, clock=lambda: 100.0)
    assert g.geocode("Austin, TX, US") == AUSTIN
    assert g.geocode("Austin, TX") == AUSTIN  # cache hit
    assert g.geocode("Somewhere Weird") is None
    assert g.geocode("Somewhere Weird") is None  # negative cache hit
    assert calls == ["austin, tx", "somewhere weird"]
    assert sleeps == [1.0]  # second network call had to wait out the 1s interval


def test_geocoder_does_not_cache_errors(store: Store) -> None:
    def boom(q: str) -> Coords | None:
        raise TimeoutError

    g = Geocoder(store, lookup=boom, sleep=lambda s: None)
    assert g.geocode("Austin, TX") is None
    assert store.geocache_get("austin, tx") == (False, None)


# --- fetch --------------------------------------------------------------------


def test_site_groups_splits_google() -> None:
    assert site_groups(["indeed", "google", "linkedin"]) == [["indeed", "linkedin"], ["google"]]
    assert site_groups(["google"]) == [["google"]]


def test_fetch_one_call_per_term_and_group_and_survives_failure() -> None:
    cfg = make_cfg(sites=["indeed", "google"])
    calls: list[dict[str, Any]] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        calls.append(kw)
        if kw["site_name"] == ["google"] and kw["search_term"] == "SRE":
            raise RuntimeError("blocked")
        return fixture_df().head(1)

    sleeps: list[float] = []
    jobs = fetch_jobs(cfg, scrape=scrape, sleep=sleeps.append)
    assert len(calls) == 4 and len(sleeps) == 3
    assert len(jobs) == 3  # one of four calls failed
    assert calls[0]["location"] == "Austin, TX" and calls[0]["distance"] == 50
    assert calls[0]["site_name"] == ["indeed"] and calls[1]["site_name"] == ["google"]


# --- filters / dedup ----------------------------------------------------------


def test_static_filters_use_word_boundaries() -> None:
    cfg = make_cfg(exclude_companies=["BigCo"])
    jobs = fixture_jobs()
    kept = {j.url for j in jobs if passes_static_filters(j, cfg, hidden=set())}
    assert "https://indeed.example/2" not in kept  # title keyword
    assert "https://indeed.example/5" not in kept  # 'intern'
    intl = Job("x", "u", "International Sales", "C", "", False, None, None, None, None, "", "s")
    assert passes_static_filters(intl, cfg, set())
    assert not passes_static_filters(jobs[0], cfg, {"acme corp"})  # hidden company


def test_dedup_within_batch_and_against_store(store: Store) -> None:
    jobs = fixture_jobs()
    assert jobs[0].id == jobs[1].id  # cross-board repost
    assert [j.url for j in dedup(jobs[:2], store)] == [jobs[0].url]
    store.add_job(jobs[0])
    assert dedup(jobs[:2], store) == []
    same_url = Job(**{**jobs[3].__dict__, "id": "different", "url": jobs[0].url})
    assert dedup([same_url], store) == []


# --- end to end -----------------------------------------------------------------


def test_run_once_dry_run_then_real(store: Store, geocoder: Geocoder) -> None:
    cfg = make_cfg()

    def fetcher(c: Config) -> list[Job]:
        return fixture_jobs()

    summary, scored = run_once(cfg, store, geocoder, FakeScorer(), dry_run=True, fetcher=fetcher)
    by_company = {x.job.company: x.job for x in scored}
    assert set(by_company) == {"Acme Corp", "RemoteCo", "Mystery LLC"}
    assert by_company["Acme Corp"].distance_miles == 0
    assert by_company["Mystery LLC"].location_unknown
    assert by_company["RemoteCo"].is_remote
    assert (summary.fetched, summary.new_jobs, summary.in_radius) == (7, 4, 3)
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # dry run: no save

    run_once(cfg, store, geocoder, FakeScorer(), dry_run=False, fetcher=fetcher)
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    summary, scored = run_once(cfg, store, geocoder, FakeScorer(), dry_run=False, fetcher=fetcher)
    assert scored == [] and summary.new_jobs == 1  # only the out-of-radius job is re-evaluated


def test_remote_dropped_when_disabled(store: Store, geocoder: Geocoder) -> None:
    cfg = make_cfg(include_remote=False)
    _, scored = run_once(
        cfg, store, geocoder, FakeScorer(), dry_run=True, fetcher=lambda c: fixture_jobs()
    )
    jobs = [x.job for x in scored]
    assert "RemoteCo" not in {j.company for j in jobs}


def test_migrations_idempotent_and_versioned(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "jobs.db"
    Store(path).close()
    s = Store(path)  # reopening must not re-run migrations
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"jobs", "scores", "notifications", "feedback", "runs", "geocache", "spend"}
    assert expected | {"hidden_companies"} <= tables


def test_home_location_geocode_failure(store: Store) -> None:
    g = Geocoder(store, lookup=lambda q: None, sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="home_location"):
        run_once(make_cfg(), store, g, FakeScorer(), dry_run=True, fetcher=lambda c: [])


# --- scoring guardrails ------------------------------------------------------------


def count(store: Store, table: str) -> int:
    return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def run(store: Store, geocoder: Geocoder, scorer: FakeScorer, **kw: Any) -> Any:
    cfg = make_cfg(**kw.pop("cfg", {}))
    return run_once(cfg, store, geocoder, scorer, fetcher=lambda c: fixture_jobs(), **kw)


def test_scored_jobs_saved_with_scores_and_spend(store: Store, geocoder: Geocoder) -> None:
    summary, scored = run(store, geocoder, FakeScorer(cost=0.02), dry_run=False)
    assert summary.scored == 3 and summary.cost_estimate == pytest.approx(0.06)
    assert count(store, "scores") == 3 and count(store, "spend") == 3
    assert scored[0].outcome.result.score == 80


def test_dry_run_scores_and_records_spend_but_saves_no_jobs(
    store: Store, geocoder: Geocoder
) -> None:
    run(store, geocoder, FakeScorer(), dry_run=True)
    assert count(store, "jobs") == 0 and count(store, "spend") == 3


def test_per_run_cap_leaves_unscored_jobs_for_next_run(store: Store, geocoder: Geocoder) -> None:
    cfg = {"scoring": {"max_jobs_scored_per_run": 2}}
    summary, _ = run(store, geocoder, FakeScorer(), dry_run=False, cfg=cfg)
    assert summary.scored == 2 and count(store, "jobs") == 2
    summary, _ = run(store, geocoder, FakeScorer(), dry_run=False, cfg=cfg)
    assert summary.scored == 1  # the job skipped last time is scored now
    assert count(store, "jobs") == 3


def test_weekly_budget_stops_scoring(store: Store, geocoder: Geocoder) -> None:
    scorer = FakeScorer(cost=0.04)
    summary, _ = run(store, geocoder, scorer, dry_run=False, weekly_budget=0.05)
    assert summary.scored == 2 and summary.budget_exhausted  # checked before each call
    # spend from this run counts against the next one
    summary, _ = run(store, geocoder, FakeScorer(), dry_run=False, weekly_budget=0.05)
    assert summary.scored == 0 and summary.budget_exhausted


def test_old_spend_does_not_count(store: Store, geocoder: Geocoder) -> None:
    store.conn.execute(
        "INSERT INTO spend (ts, model, cost_usd) VALUES ('2000-01-01T00:00:00+00:00', 'm', 99)"
    )
    summary, _ = run(store, geocoder, FakeScorer(), dry_run=True, weekly_budget=1.0)
    assert summary.scored == 3 and not summary.budget_exhausted


def test_scoring_failure_skips_job_and_does_not_save_it(store: Store, geocoder: Geocoder) -> None:
    summary, scored = run(store, geocoder, FakeScorer(fail_on="RemoteCo"), dry_run=False)
    assert summary.scored == 2 and count(store, "jobs") == 2
    assert "RemoteCo" not in {x.job.company for x in scored}


def test_repeated_scoring_failures_abort_instead_of_burning_calls(
    store: Store, geocoder: Geocoder
) -> None:
    from job_hunter.score import FatalScoringError

    class AlwaysFails(FakeScorer):
        def score(self, job: Job) -> ScoreOutcome:
            self.calls.append(job.company)
            raise ScoringError("BadRequestError 400: nope")

    scorer = AlwaysFails()
    with pytest.raises(FatalScoringError, match="3 scoring failures in a row"):
        run(store, geocoder, scorer, dry_run=False)
    assert len(scorer.calls) == 3
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1  # run still recorded


def test_failures_after_some_successes_stop_scoring_but_keep_results(
    store: Store, geocoder: Geocoder
) -> None:
    class Dies(FakeScorer):
        def score(self, job: Job) -> ScoreOutcome:
            if self.calls:  # first job scores, then everything fails
                self.calls.append(job.company)
                raise ScoringError("outage")
            return super().score(job)

    cfg = {"sites": ["indeed"]}
    jobs = [j for j in fixture_jobs() if j.company in {"Acme Corp", "RemoteCo", "Mystery LLC"}]
    summary, scored = run_once(
        make_cfg(**cfg), store, geocoder, Dies(), dry_run=False, fetcher=lambda c: jobs
    )
    assert summary.scored == 1 and len(scored) == 1  # partial results are kept, not discarded
