"""Score explanations, /last-scores, and chat-adjustable settings (/threshold /radius /location)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from job_hunter import commands, settings
from job_hunter.config import Config
from job_hunter.feedback import apply_feedback
from job_hunter.geo import Geocoder
from job_hunter.models import Job
from job_hunter.notify import telegram_bot
from job_hunter.notify.discord_bot import DiscordBot, register_commands
from job_hunter.pipeline import ScoredJob, deliver, run_once
from job_hunter.score import (
    EXPLANATION_MAX_CHARS,
    SCORE_SCHEMA,
    ClaudeScorer,
    ScoreOutcome,
    ScoreResult,
    ScoringError,
    Usage,
)
from job_hunter.service import Service
from job_hunter.store import RunSummary, Store

AUSTIN = (30.27, -97.74)


def make_cfg(**over: Any) -> Config:
    base = {"home_location": "Austin, TX", "searches": [{"term": "x"}], "sites": ["indeed"]}
    return Config.model_validate(base | over)


def make_job(n: int, **over: Any) -> Job:
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


def result(score: int, explanation: str = "") -> ScoreResult:
    return ScoreResult(
        score=score,
        verdict="maybe",
        reasons=["good stack"],
        concerns=["long commute"],
        seniority_match=True,
        explanation=explanation or f"Explanation for a {score}.",
    )


def save_scored(store: Store, n: int, score: int, run_id: int | None, explanation: str = "") -> Job:
    job = make_job(n)
    store.add_job(job)
    r = result(score, explanation)
    store.add_score(
        job.id, r.model_dump_json(), "m", 1, 1, 0, 0, explanation=r.explanation, run_id=run_id
    )
    return job


def finish(store: Store, run_id: int, scored: int) -> None:
    store.finish_run(run_id, RunSummary(scored=scored))


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


def make_service(store: Store, **kw: Any) -> Service:
    return Service(make_cfg(), store, [], lambda: (RunSummary(), []), **kw)


# --- schema and explanation enforcement ------------------------------------------------------


def _walk(schema: Any) -> list[dict[str, Any]]:
    if isinstance(schema, dict):
        return [schema] + [x for v in schema.values() for x in _walk(v)]
    if isinstance(schema, list):
        return [x for v in schema for x in _walk(v)]
    return []


def test_schema_is_valid_for_structured_outputs() -> None:
    nodes = _walk(SCORE_SCHEMA)
    unsupported = {"minimum", "maximum", "minLength", "maxLength", "maxItems", "minItems"}
    assert not any(unsupported & node.keys() for node in nodes)  # the API rejects these
    assert SCORE_SCHEMA["additionalProperties"] is False
    assert set(SCORE_SCHEMA["required"]) == set(SCORE_SCHEMA["properties"])
    assert {"score", "explanation"} <= set(SCORE_SCHEMA["required"])
    assert next(iter(SCORE_SCHEMA["properties"])) == "explanation"  # reasons before the number


def test_explanation_capped_at_500_chars_and_whitespace_normalised() -> None:
    r = result(50, "word   " * 200)
    assert len(r.explanation) == EXPLANATION_MAX_CHARS and r.explanation.endswith("\u2026")
    assert "  " not in r.explanation
    assert result(50, "  short\n reason ").explanation == "short reason"


def test_scores_saved_before_explanations_still_load() -> None:
    legacy = (
        '{"score": 64, "verdict": "maybe", "reasons": [], "concerns": [], '
        '"seniority_match": true, "est_salary_ok": null}'
    )
    assert ScoreResult.model_validate_json(legacy).explanation == ""


class OneReply:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.messages = self

    def create(self, **kw: Any) -> Any:
        return self.reply


def reply(payload: Any, stop: str = "end_turn") -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        stop_reason=stop,
    )


GOOD = {
    "explanation": "Good match.",
    "score": 75,
    "verdict": "strong",
    "reasons": [],
    "concerns": [],
    "seniority_match": True,
    "est_salary_ok": None,
}


def test_missing_or_blank_explanation_is_rejected() -> None:
    for payload in (
        {**GOOD, "explanation": "   "},
        {k: v for k, v in GOOD.items() if k != "explanation"},
    ):
        with pytest.raises(ScoringError, match="no explanation"):
            ClaudeScorer(OneReply(reply(payload)), "m", "S").score(make_job(1))


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_or_truncation_is_a_scoring_error(stop: str) -> None:
    with pytest.raises(ScoringError, match=stop):
        ClaudeScorer(OneReply(reply(GOOD, stop=stop)), "m", "S").score(make_job(1))


def test_valid_json_reply_is_parsed() -> None:
    out = ClaudeScorer(OneReply(reply(GOOD)), "m", "S").score(make_job(1))
    assert out.result.score == 75 and out.result.explanation == "Good match."


# --- storage: runs, run ids, last-run queries --------------------------------------------------


def test_migration_adds_columns_and_settings_table(store: Store) -> None:
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(scores)")}
    assert {"explanation", "run_id"} <= cols
    assert store.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_run_in_progress_is_not_the_last_run(store: Store) -> None:
    done = store.start_run("2026-10-01T12:00:00+00:00")
    finish(store, done, 3)
    store.start_run("2026-10-01T18:00:00+00:00")  # still running
    last = store.last_run()
    assert last is not None and last["id"] == done


def test_run_scores_orders_limits_and_counts(store: Store) -> None:
    run = store.start_run(datetime.now(UTC).isoformat(timespec="seconds"))
    for n, score in enumerate([40, 90, 65, 72], start=1):
        save_scored(store, n, score, run)
    finish(store, run, 4)
    latest = store.latest_scored_run()
    assert latest is not None
    items, total = store.run_scores(latest, 2)
    assert total == 4 and [r.score for _, r, _ in items] == [90, 72]
    assert items[0][2] == "Explanation for a 90."


def test_legacy_scores_without_run_id_are_matched_by_time(store: Store) -> None:
    start = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    run = store.start_run(start)
    save_scored(store, 1, 61, run_id=None)  # saved by the old code: no run id, no explanation
    store.conn.execute("UPDATE scores SET explanation = NULL")
    finish(store, run, 1)
    latest = store.latest_scored_run()
    assert latest is not None
    items, total = store.run_scores(latest, 5)
    assert total == 1 and items[0][2] is None


def test_runs_that_scored_nothing_are_skipped(store: Store) -> None:
    first = store.start_run("2026-10-01T12:00:00+00:00")
    save_scored(store, 1, 55, first)
    finish(store, first, 1)
    empty = store.start_run("2026-10-01T18:00:00+00:00")
    finish(store, empty, 0)
    latest = store.latest_scored_run()
    assert latest is not None and latest["id"] == first


class FixedScorer:
    def __init__(self, scores: dict[str, int]) -> None:
        self.scores = scores

    def score(self, job: Job) -> ScoreOutcome:
        r = result(self.scores.get(job.company, 50), f"Why {job.company} got its score.")
        return ScoreOutcome(result=r, model="m", usage=Usage(), cost_usd=0.001)


def run_pipeline(store: Store, cfg: Config, jobs: list[Job], dry_run: bool = False) -> Any:
    geocoder = Geocoder(store, lookup=lambda q: AUSTIN, sleep=lambda s: None)
    return run_once(
        cfg, store, geocoder, FixedScorer({"Co1": 88, "Co2": 60}), dry_run=dry_run,
        fetcher=lambda c: jobs,
    )  # fmt: skip


def test_pipeline_saves_explanation_and_run_id(store: Store) -> None:
    summary, _ = run_pipeline(store, make_cfg(), [make_job(1), make_job(2)])
    rows = store.conn.execute(
        "SELECT explanation, run_id FROM scores ORDER BY explanation"
    ).fetchall()
    assert [r[0] for r in rows] == ["Why Co1 got its score.", "Why Co2 got its score."]
    assert {r[1] for r in rows} == {summary.run_id}
    run = store.last_run()
    assert run is not None and run["scored"] == 2 and run["finished_at"]


def test_dry_run_records_no_run_or_scores(store: Store) -> None:
    run_pipeline(store, make_cfg(), [make_job(1)], dry_run=True)
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


# --- /last-scores ---------------------------------------------------------------------------------


def seed_run(store: Store, scores: list[int]) -> int:
    run = store.start_run((datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"))
    for n, score in enumerate(scores, start=1):
        save_scored(store, n, score, run)
    finish(store, run, len(scores))
    return run


def test_last_scores_default_five_with_explanations(store: Store) -> None:
    seed_run(store, [30, 91, 64, 69, 55, 72, 40])
    text = commands.last_scores_text(make_service(store))
    assert "Top 5 of 7 scored" in text and "notify threshold 70" in text
    order = [int(line.split("[")[1].split("]")[0]) for line in text.splitlines() if "] Job" in line]
    assert order == [91, 72, 69, 64, 55]
    assert "Explanation for a 69." in text
    assert "\u2705 [91]" in text and "\u2716 [69]" in text  # cleared vs missed the bar


def test_last_scores_n_and_validation(store: Store) -> None:
    seed_run(store, [30, 91, 64])
    service = make_service(store)
    assert "Top 2 of 3" in commands.last_scores_text(service, "2")
    assert "Top 3 of 3" in commands.last_scores_text(service, "20")
    for bad in ("0", "21", "five", "-3"):
        assert commands.last_scores_text(service, bad).startswith("Usage: /last_scores")


def test_last_scores_uses_current_threshold_and_wraps_links(store: Store) -> None:
    seed_run(store, [64])
    service = make_service(store)
    settings.set_setting(store, "threshold", "60")
    text = commands.last_scores_text(service, wrap_links=True)
    assert "notify threshold 60" in text and "\u2705 [64]" in text
    assert "<https://x.example/1>" in text


def test_last_scores_when_nothing_scored_or_latest_run_empty(store: Store) -> None:
    service = make_service(store)
    assert commands.last_scores_text(service) == "No run has scored any jobs yet."
    seed_run(store, [50])
    empty = store.start_run(datetime.now(UTC).isoformat(timespec="seconds"))
    finish(store, empty, 0)
    assert "most recent run scored nothing new" in commands.last_scores_text(service)


def test_last_scores_legacy_rows_show_reasons_instead(store: Store) -> None:
    run = seed_run(store, [58])
    store.conn.execute("UPDATE scores SET explanation = NULL, run_id = NULL")
    store.conn.execute("UPDATE scores SET score_json = json_remove(score_json, '$.explanation')")
    store.conn.execute(
        "UPDATE scores SET created_at = (SELECT started_at FROM runs WHERE id = ?)", (run,)
    )
    text = commands.last_scores_text(make_service(store))
    assert "No explanation stored" in text and "Reasons: good stack" in text


# --- settings overlay ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("threshold", "65", 65),
        ("threshold", " 0 ", 0),
        ("radius", "30", 30.0),
        ("radius", "12.5 mi", 12.5),
        ("radius", "40 miles", 40.0),
        ("location", "  Tacoma,   WA ", "Tacoma, WA"),
    ],
)
def test_setting_parsing(key: str, raw: str, expected: Any) -> None:
    assert settings.SETTINGS[key].parse(raw) == expected


@pytest.mark.parametrize(
    "key, raw",
    [
        ("threshold", "101"),
        ("threshold", "-1"),
        ("threshold", "high"),
        ("threshold", "65.5"),
        ("radius", "0"),
        ("radius", "501"),
        ("radius", "far"),
        ("location", ""),
        ("location", "Your City, ST"),
        ("location", "x" * 101),
    ],
)
def test_setting_validation(key: str, raw: str) -> None:
    with pytest.raises(settings.SettingError):
        settings.SETTINGS[key].parse(raw)


def test_effective_config_overlays_and_resets(store: Store) -> None:
    cfg = make_cfg(radius_miles=50)
    assert settings.effective_config(cfg, store) is cfg  # no overrides: unchanged
    settings.set_setting(store, "threshold", "55")
    settings.set_setting(store, "radius", "25")
    settings.set_setting(store, "location", "Tacoma, WA")
    eff = settings.effective_config(cfg, store)
    assert eff.scoring.min_score_to_notify == 55
    assert eff.radius_miles == 25 and eff.home_location == "Tacoma, WA"
    assert eff.searches == cfg.searches and eff.scoring.model == cfg.scoring.model
    settings.reset_setting(store, "radius")
    assert settings.effective_config(cfg, store).radius_miles == 50


def test_config_yaml_edits_still_apply_when_not_overridden(store: Store) -> None:
    settings.set_setting(store, "threshold", "55")
    edited = make_cfg(radius_miles=80)  # e.g. the user edited config.yaml and restarted
    eff = settings.effective_config(edited, store)
    assert eff.radius_miles == 80 and eff.scoring.min_score_to_notify == 55


def test_invalid_stored_values_are_ignored(store: Store) -> None:
    store.set_setting("threshold", "banana")
    store.set_setting("unknown_key", "1")
    assert settings.effective_config(make_cfg(), store).scoring.min_score_to_notify == 70


def test_describe(store: Store) -> None:
    cfg = make_cfg()
    assert settings.describe(cfg, store, "threshold") == "70 (config.yaml)"
    settings.set_setting(store, "threshold", "60")
    assert settings.describe(cfg, store, "threshold") == "60 (set from chat; config.yaml: 70)"
    assert settings.describe(cfg, store, "radius") == "50 mi (config.yaml)"


# --- /threshold /radius /location commands ------------------------------------------------------


def test_threshold_command_show_set_reset(store: Store) -> None:
    service = make_service(store)
    assert "Notify threshold: 70 (config.yaml)" in commands.threshold_text(service)
    out = commands.threshold_text(service, "60")
    assert out.startswith("Notify threshold set to 60.") and "/top" in out
    assert service.cfg.scoring.min_score_to_notify == 60
    assert "must be from 0 to 100" in commands.threshold_text(service, "150")
    assert service.cfg.scoring.min_score_to_notify == 60  # rejected value changed nothing
    assert "reset to 70 (config.yaml)" in commands.threshold_text(service, "RESET")
    assert service.cfg.scoring.min_score_to_notify == 70


def test_radius_command(store: Store) -> None:
    service = make_service(store)
    assert commands.radius_text(service, "35").startswith("Search radius set to 35 mi.")
    assert service.cfg.radius_miles == 35
    assert "number of miles" in commands.radius_text(service, "lots")


async def test_location_command_geocodes_before_saving(store: Store) -> None:
    lookups: list[str] = []

    def lookup(q: str) -> tuple[float, float] | None:
        lookups.append(q)
        return (47.25, -122.44) if q == "tacoma, wa" else None

    service = make_service(store, geocode_lookup=lookup)
    out = await commands.location_text(service, "Tacoma, WA")
    assert out.startswith("Home location set to Tacoma, WA (47.250, -122.440).")
    assert service.cfg.home_location == "Tacoma, WA"
    await commands.location_text(service, "Tacoma, WA")
    assert lookups == ["tacoma, wa"]  # second time served from the geocache

    out = await commands.location_text(service, "Atlantis")
    assert "Couldn't find 'Atlantis'" in out and service.cfg.home_location == "Tacoma, WA"
    assert "reset to Austin, TX" in await commands.location_text(service, "reset")


async def test_location_command_survives_geocoder_outage(store: Store) -> None:
    def down(q: str) -> None:
        raise TimeoutError

    service = make_service(store, geocode_lookup=down)
    out = await commands.location_text(service, "Tacoma, WA")
    assert "Couldn't reach the geocoding service" in out
    assert service.cfg.home_location == "Austin, TX"


def test_status_shows_settings_and_their_source(store: Store) -> None:
    service = make_service(store)
    settings.set_setting(store, "threshold", "65")
    text = commands.status_text(service)
    assert "Threshold: 65 (set from chat; config.yaml: 70)" in text
    assert "radius: 50 mi (config.yaml)" in text and "location: Austin, TX (config.yaml)" in text


async def test_threshold_change_affects_delivery_without_restart(store: Store) -> None:
    class Rec:
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

    rec = Rec()
    service = Service(
        make_cfg(notify={"cooldown_seconds": 0}), store, [rec], lambda: (RunSummary(), [])
    )
    job = make_job(1)
    store.add_job(job)
    item = ScoredJob(job, ScoreOutcome(result=result(66), model="m", usage=Usage(), cost_usd=0))
    await deliver([rec], [item], RunSummary(), service.cfg, store, queue=True)
    assert rec.jobs == []  # 66 < 70
    commands.threshold_text(service, "65")
    await deliver([rec], [item], RunSummary(), service.cfg, store, queue=True)
    assert rec.jobs == ["Job 1"]


def test_top_respects_chat_threshold(store: Store) -> None:
    service = make_service(store)
    save_scored(store, 1, 66, None)
    assert "No unapplied matches scored 70+" in commands.top_text(service)
    commands.threshold_text(service, "60")
    assert "[66] Job 1" in commands.top_text(service)
    apply_feedback(store, make_job(1).id, "applied", "t")
    assert "No unapplied" in commands.top_text(service)


def test_service_run_uses_chat_settings(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from job_hunter import service as service_module
    from job_hunter.secrets import Secrets

    db = tmp_path / "jobs.db"
    store = Store(db)
    settings.set_setting(store, "radius", "12")
    settings.set_setting(store, "threshold", "42")
    store.close()

    seen: dict[str, Any] = {}

    def fake_run_once(cfg: Config, *a: Any, **k: Any) -> Any:
        seen["radius"] = cfg.radius_miles
        seen["threshold"] = cfg.scoring.min_score_to_notify
        return RunSummary(), []

    resume, profile = tmp_path / "r.md", tmp_path / "p.md"
    resume.write_text("resume")
    profile.write_text("profile")
    cfg = make_cfg(resume_path=str(resume), profile_path=str(profile))
    secrets = Secrets(anthropic_api_key=SecretStr("sk-ant-" + "x" * 40))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service_module, "run_once", fake_run_once)
        service_module.make_run_sync(cfg, secrets, db, None)()
    assert seen == {"radius": 12.0, "threshold": 42}


# --- delivery to the bots -----------------------------------------------------------------------


def test_split_message() -> None:
    blocks = [f"block {i} " + "x" * 50 for i in range(10)]
    chunks = commands.split_message("\n\n".join(blocks), 200)
    assert all(len(c) <= 200 for c in chunks)
    assert "\n\n".join(chunks).replace("\n\n", "") == "".join(blocks)
    assert commands.split_message("short", 100) == ["short"]
    huge = commands.split_message("y" * 450, 200)
    assert [len(c) for c in huge] == [200, 200, 50]


async def test_telegram_last_scores_parses_argument_and_splits_long_replies(store: Store) -> None:
    seed_run(store, list(range(50, 70)))
    for n in range(1, 21):
        store.conn.execute(
            "UPDATE scores SET explanation = ? WHERE job_id = ?", ("e" * 480, make_job(n).id)
        )
    service = make_service(store)
    handlers: list[Any] = []
    app = SimpleNamespace(add_handler=lambda h, group=0: handlers.append(h))
    telegram_bot.register_commands(app, service)  # type: ignore[arg-type]
    handler = next(h for h in handlers if "last_scores" in h.commands)
    message = SimpleNamespace(reply_text=AsyncMock())
    await handler.callback(SimpleNamespace(effective_message=message), SimpleNamespace(args=["12"]))
    sent = [c.args[0] for c in message.reply_text.call_args_list]
    assert len(sent) >= 2 and all(len(t) <= 4096 for t in sent)
    assert "Top 12 of 20" in sent[0]


async def test_telegram_location_takes_multi_word_argument(store: Store) -> None:
    service = make_service(store, geocode_lookup=lambda q: (47.2, -122.4))
    handlers: list[Any] = []
    app = SimpleNamespace(add_handler=lambda h, group=0: handlers.append(h))
    telegram_bot.register_commands(app, service)  # type: ignore[arg-type]
    handler = next(h for h in handlers if "location" in h.commands)
    message = SimpleNamespace(reply_text=AsyncMock())
    ctx = SimpleNamespace(args=["Tacoma,", "WA"])
    await handler.callback(SimpleNamespace(effective_message=message), ctx)
    assert service.cfg.home_location == "Tacoma, WA"


def discord_interaction() -> Any:
    state = {"done": False}

    async def done(*a: Any, **k: Any) -> None:
        state["done"] = True

    response = SimpleNamespace(
        send_message=AsyncMock(side_effect=done),
        defer=AsyncMock(side_effect=done),
        is_done=lambda: state["done"],
    )
    return SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))


async def test_discord_last_scores_splits_under_2000_chars(store: Store) -> None:
    seed_run(store, list(range(50, 60)))
    for n in range(1, 11):
        store.conn.execute(
            "UPDATE scores SET explanation = ? WHERE job_id = ?", ("e" * 480, make_job(n).id)
        )
    service = make_service(store)
    bot = DiscordBot(store, 5, 7)
    register_commands(bot, service)
    cmd = bot.tree.get_command("last-scores")
    assert cmd is not None
    interaction = discord_interaction()
    await cmd.callback(interaction, n=8)  # type: ignore[arg-type,call-arg]
    first = interaction.response.send_message.call_args
    rest = [c.args[0] for c in interaction.followup.send.call_args_list]
    assert "Top 8 of 10" in first.args[0] and first.kwargs["ephemeral"] is True
    assert rest and all(len(t) <= 2000 for t in [first.args[0], *rest])


async def test_discord_location_defers_then_follows_up(store: Store) -> None:
    service = make_service(store, geocode_lookup=lambda q: (47.2, -122.4))
    bot = DiscordBot(store, 5, 7)
    register_commands(bot, service)
    cmd = bot.tree.get_command("location")
    assert cmd is not None
    interaction = discord_interaction()
    await cmd.callback(interaction, value="Tacoma, WA")  # type: ignore[arg-type,call-arg]
    interaction.response.defer.assert_awaited_once()
    assert "Home location set to Tacoma, WA" in interaction.followup.send.call_args.args[0]
    assert service.cfg.home_location == "Tacoma, WA"


async def test_discord_threshold_command(store: Store) -> None:
    service = make_service(store)
    bot = DiscordBot(store, 5, 7)
    register_commands(bot, service)
    cmd = bot.tree.get_command("threshold")
    assert cmd is not None
    interaction = discord_interaction()
    await cmd.callback(interaction, value="55")  # type: ignore[arg-type,call-arg]
    assert service.cfg.scoring.min_score_to_notify == 55
