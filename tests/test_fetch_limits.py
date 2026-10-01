from typing import Any

import pandas as pd
import pytest

from job_hunter.config import Config, Search
from job_hunter.fetch import average_gap_hours, fetch_jobs, plan_fetch, select_searches
from job_hunter.geo import Geocoder
from job_hunter.pipeline import SEARCH_CURSOR_KEY, run_once
from job_hunter.store import Store


def rows_for(kw: dict[str, Any]) -> pd.DataFrame:
    """One result per requested site, as a healthy JobSpy call would return."""
    return pd.DataFrame(
        [{"site": s, "title": "t", "company": "c", "job_url": "u"} for s in kw["site_name"]]
    )


def cfg_with(n_searches: int, **over: Any) -> Config:
    base: dict[str, Any] = {
        "home_location": "Austin, TX",
        "searches": [{"term": f"t{i}"} for i in range(n_searches)],
        "schedule_cron": "0 7,12,18 * * *",
        "hours_old": 24,
    }
    return Config.model_validate(base | over)


def terms(searches: list[Search]) -> list[str]:
    return [s.term for s in searches]


def test_few_searches_are_never_rotated() -> None:
    cfg = cfg_with(5)
    chosen, nxt = select_searches(cfg.searches, 8, cursor=3)
    assert terms(chosen) == ["t0", "t1", "t2", "t3", "t4"] and nxt == 0


def test_rotation_covers_every_term_and_wraps() -> None:
    cfg = cfg_with(20)
    seen: list[str] = []
    cursor = 0
    for _ in range(5):  # 20 terms / 8 per run -> 3-run cycle, plus wraparound
        chosen, cursor = select_searches(cfg.searches, 8, cursor)
        assert len(chosen) == 8
        seen += terms(chosen)
    assert set(seen) == {f"t{i}" for i in range(20)}
    first_three = seen[:24]
    assert first_three[:8] == [f"t{i}" for i in range(8)]
    assert first_three[16:24] == [f"t{i}" for i in (16, 17, 18, 19, 0, 1, 2, 3)]


def test_cursor_survives_shrinking_search_list() -> None:
    cfg = cfg_with(10)
    chosen, _ = select_searches(cfg.searches, 4, cursor=57)
    assert len(chosen) == 4


def test_average_gap() -> None:
    assert average_gap_hours("0 7,12,18 * * *", "America/Chicago") == pytest.approx(8, abs=0.1)
    assert average_gap_hours("0 9 * * *", "America/Chicago") == pytest.approx(24, abs=0.1)


def test_hours_old_widened_only_when_rotating() -> None:
    small, _ = plan_fetch(cfg_with(5), 0)
    assert small.hours_old == 24 and len(small.searches) == 5
    big, nxt = plan_fetch(cfg_with(100), 0)  # 13-run cycle at ~8h per run
    assert len(big.searches) == 8 and nxt == 8
    assert big.hours_old >= 100  # lookback must cover the whole rotation
    assert cfg_with(100).hours_old == 24  # original config untouched


def test_hours_old_never_shrinks() -> None:
    cfg = cfg_with(20, hours_old=500)
    assert plan_fetch(cfg, 0)[0].hours_old == 500


def test_circuit_breaker_stops_hammering_a_blocked_site() -> None:
    cfg = cfg_with(6, sites=["indeed", "google"])
    calls: list[tuple[str, ...]] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        calls.append(tuple(kw["site_name"]))
        if kw["site_name"] == ["indeed"]:
            raise RuntimeError("429")
        return rows_for(kw)

    fetch_jobs(cfg, scrape=scrape, sleep=lambda s: None)
    assert calls.count(("indeed",)) == 2  # gave up after 2 consecutive failures
    assert calls.count(("google",)) == 6  # the healthy group is unaffected


def test_success_resets_failure_count() -> None:
    cfg = cfg_with(6, sites=["indeed"])
    outcomes = iter([True, False, True, False, True, False])

    def scrape(**kw: Any) -> pd.DataFrame:
        if not next(outcomes):
            raise RuntimeError("flaky")
        return rows_for(kw)

    calls = []

    def counting(**kw: Any) -> pd.DataFrame:
        calls.append(1)
        return scrape(**kw)

    fetch_jobs(cfg, scrape=counting, sleep=lambda s: None)
    assert len(calls) == 6  # alternating failures never hit 2 in a row


def test_delay_comes_from_config_and_linkedin_flag_is_respected() -> None:
    cfg = cfg_with(
        2,
        sites=["linkedin"],
        fetch={"delay_seconds": [5, 5], "linkedin_fetch_description": False},
    )
    sleeps: list[float] = []
    seen: list[dict[str, Any]] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        seen.append(kw)
        return pd.DataFrame()

    fetch_jobs(cfg, scrape=scrape, sleep=sleeps.append)
    assert sleeps == [5.0]
    assert seen[0]["linkedin_fetch_description"] is False


def test_call_volume_is_bounded_by_searches_per_run() -> None:
    cfg = cfg_with(150)  # a user with 150 job titles
    run_cfg, _ = plan_fetch(cfg, 0)
    calls: list[Any] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        calls.append(kw)
        return rows_for(kw)

    fetch_jobs(run_cfg, scrape=scrape, sleep=lambda s: None)
    assert len(calls) == 8  # 8 terms x 1 group, not 150 x 2


def test_run_once_advances_cursor_but_not_in_dry_run() -> None:
    store = Store(":memory:")
    cfg = cfg_with(20)
    geocoder = Geocoder(store, lookup=lambda q: (30.0, -97.0), sleep=lambda s: None)
    seen: list[list[str]] = []

    def fetcher(c: Config) -> list[Any]:
        seen.append(terms(c.searches))
        return []

    class NoScorer:
        def score(self, job: Any) -> Any:
            raise AssertionError("nothing to score")

    run_once(cfg, store, geocoder, NoScorer(), dry_run=True, fetcher=fetcher)
    assert store.get_state(SEARCH_CURSOR_KEY, "0") == "0"
    run_once(cfg, store, geocoder, NoScorer(), dry_run=False, fetcher=fetcher)
    run_once(cfg, store, geocoder, NoScorer(), dry_run=False, fetcher=fetcher)
    assert seen[1] == seen[0] == [f"t{i}" for i in range(8)]  # dry run did not advance
    assert seen[2][0] == "t8"
    assert store.get_state(SEARCH_CURSOR_KEY) == "16"


@pytest.mark.parametrize(
    "fetch",
    [{"delay_seconds": [9, 3]}, {"delay_seconds": [-1, 3]}, {"searches_per_run": 0}, {"bogus": 1}],
)
def test_invalid_fetch_settings_rejected(fetch: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="fetch"):
        cfg_with(2, fetch=fetch)


def test_too_many_searches_rejected() -> None:
    with pytest.raises(ValueError, match="200"):
        cfg_with(201)


def test_site_that_keeps_returning_nothing_is_skipped_for_the_rest_of_the_run() -> None:
    cfg = cfg_with(5, sites=["indeed", "zip_recruiter", "glassdoor"])
    requested: list[list[str]] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        requested.append(list(kw["site_name"]))
        # zip_recruiter is blocked (JobSpy logs it and returns nothing); glassdoor flaky once
        live = [s for s in kw["site_name"] if s != "zip_recruiter"]
        return rows_for({"site_name": live})

    fetch_jobs(cfg, scrape=scrape, sleep=lambda s: None)
    assert "zip_recruiter" in requested[0] and "zip_recruiter" in requested[1]
    assert all("zip_recruiter" not in group for group in requested[2:])  # dropped after 2 empties
    assert all("indeed" in group for group in requested)  # healthy sites untouched


def test_one_empty_result_does_not_disable_a_site() -> None:
    cfg = cfg_with(4, sites=["indeed", "glassdoor"])
    n = {"calls": 0}
    requested: list[list[str]] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        n["calls"] += 1
        requested.append(list(kw["site_name"]))
        # glassdoor is empty only on the 1st and 3rd call: never twice in a row
        live = [s for s in kw["site_name"] if not (s == "glassdoor" and n["calls"] in (1, 3))]
        return rows_for({"site_name": live})

    fetch_jobs(cfg, scrape=scrape, sleep=lambda s: None)
    assert all("glassdoor" in group for group in requested)


def test_default_sites_exclude_google_and_no_custom_google_query() -> None:
    assert "google" not in cfg_with(1).sites
    cfg = cfg_with(1, sites=["google"])
    seen: list[dict[str, Any]] = []

    def scrape(**kw: Any) -> pd.DataFrame:
        seen.append(kw)
        return rows_for(kw)

    fetch_jobs(cfg, scrape=scrape, sleep=lambda s: None)
    assert "google_search_term" not in seen[0]  # JobSpy builds the query itself
