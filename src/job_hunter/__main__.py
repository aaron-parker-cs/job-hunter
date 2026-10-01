"""CLI entry point. Commands: run, once, init-db, test-notify, check-anthropic, healthcheck."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from job_hunter.bootstrap import bootstrap, ensure_not_template
from job_hunter.config import (
    Config,
    ConfigError,
    load_config,
    load_env_file,
    shadowed_env_keys,
    weekly_budget_from_env,
)
from job_hunter.diagnose import check_anthropic
from job_hunter.geo import Geocoder
from job_hunter.logging_setup import register_secrets, setup_logging
from job_hunter.models import Job, make_job_id
from job_hunter.notify import build_notifiers
from job_hunter.pipeline import ScoredJob, deliver, run_once
from job_hunter.score import ScoreResult, load_text
from job_hunter.secrets import Secrets, SecretsError, build_provider, load_secrets
from job_hunter.service import heartbeat_file, heartbeat_is_fresh, prepare_scorer, serve
from job_hunter.store import RunSummary, Store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="job_hunter")
    p.add_argument(
        "--config", type=Path, default=Path(os.environ.get("CONFIG_PATH", "data/config.yaml"))
    )
    p.add_argument(
        "--test-anthropic",
        action="store_true",
        help="send a test prompt to Claude to check the API key works, print the reply, and exit",
    )
    p.add_argument("--model", help="model for --test-anthropic (default: scoring.model)")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="scheduler + bot listeners")
    once = sub.add_parser("once", help="one pass")
    once.add_argument("--dry-run", action="store_true")
    sub.add_parser("init-db", help="create the SQLite schema")
    sub.add_parser("test-notify", help="send one sample message")
    sub.add_parser("healthcheck", help="exit 0 if the running service is alive")
    sub.add_parser("check-anthropic", help="same as --test-anthropic")
    return p


def _print_scored(scored: list[ScoredJob], threshold: int) -> None:
    for item in scored:
        job, r = item.job, item.outcome.result
        if job.is_remote:
            where = "Remote"
        elif job.distance_miles is not None:
            where = f"{job.distance_miles:.0f} mi"
        else:
            where = f"{job.location or '?'} (location unknown)"
        mark = "*" if r.score >= threshold else " "
        print(f"{mark}[{r.score:3d} {r.verdict}] {job.title} @ {job.company} [{where}]")
        print(f"      {job.url}")
        for reason in r.reasons[:2]:
            print(f"      + {reason}")
        for concern in r.concerns[:1]:
            print(f"      - {concern}")


async def _deliver(
    cfg: Config, secrets: Secrets, store: Store, scored: list[ScoredJob], summary: RunSummary
) -> None:
    notifiers = build_notifiers(cfg, secrets, store)
    try:
        await deliver(notifiers, scored, summary, cfg, store)
    finally:
        for n in notifiers:
            await n.aclose()


async def _test_notify(cfg: Config, secrets: Secrets, store: Store) -> int:
    """Send one sample job (with working buttons) to every configured notifier."""
    job = Job(
        id=make_job_id("Job Hunter Test Co", "Sample Job", "Testville"),
        url="https://example.com/sample-job",
        title="Sample Job (test-notify)",
        company="Job Hunter Test Co",
        location="Testville",
        is_remote=False,
        salary_min=100000,
        salary_max=130000,
        salary_interval="yearly",
        date_posted=None,
        description="Sample posting used to test notifications.",
        site="test",  # excluded from feedback calibration
        distance_miles=12.0,
    )
    result = ScoreResult(
        score=88,
        verdict="strong",
        reasons=["Matches your skills", "Good salary"],
        concerns=["This is only a test"],
        seniority_match=True,
    )
    store.add_job(job)
    notifiers = build_notifiers(cfg, secrets, store)
    failures = 0
    try:
        for n in notifiers:
            try:
                await n.send_job(job, result)
                print(f"sent sample message via {n.name}")
            except Exception as exc:
                failures += 1
                print(f"error: {n.name} failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        for n in notifiers:
            await n.aclose()
        store.close()
    return 1 if failures else 0


def _test_model(args: argparse.Namespace) -> str:
    """--model, else scoring.model from the config if it loads, else the default."""
    if args.model:
        return str(args.model)
    try:
        return load_config(args.config, check_files=False).scoring.model
    except ConfigError:
        return "claude-haiku-4-5"


def main(argv: list[str] | None = None) -> int:
    load_env_file()  # before anything reads CONFIG_PATH, DB_PATH, LOG_LEVEL or secrets
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None and not args.test_anthropic:
        parser.error("a command is required (or use --test-anthropic)")
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    shadowed = shadowed_env_keys()
    if shadowed and args.command != "healthcheck":
        print(
            f"warning: {', '.join(shadowed)} is set in your shell and differs from .env; "
            "the shell value wins. Run `unset NAME` to use the .env value.",
            file=sys.stderr,
        )
    db_path = Path(os.environ.get("DB_PATH", "data/jobs.db"))

    if args.command == "init-db":
        Store(db_path).close()
        print(f"database ready at {db_path}")
        return 0

    if args.test_anthropic or args.command == "check-anthropic":  # needs no config
        register_secrets([os.environ.get("ANTHROPIC_API_KEY", "")])
        return check_anthropic(_test_model(args), shadowed=shadowed)
    if args.command == "healthcheck":  # needs no config or secrets
        return 0 if heartbeat_is_fresh(heartbeat_file(db_path)) else 1

    created = bootstrap(args.config)
    if created:
        print("Created starter files from the built-in templates:", file=sys.stderr)
        for path in created:
            print(f"  {path}", file=sys.stderr)
        print("Edit them (home_location, resume, profile), then run again.", file=sys.stderr)
        return 2

    dry_run = args.command == "once" and args.dry_run
    try:
        cfg = load_config(args.config)
        secrets = load_secrets(build_provider(), cfg.notifier)
        register_secrets(secrets.secret_values())
        budget = weekly_budget_from_env()
        for path in (cfg.resume_path, cfg.profile_path):  # fail fast on unedited templates
            ensure_not_template(path, load_text(path))
    except (ConfigError, SecretsError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "test-notify":
        return asyncio.run(_test_notify(cfg, secrets, Store(db_path)))
    if args.command == "run":
        try:
            asyncio.run(serve(cfg, secrets, db_path, budget))
        except KeyboardInterrupt:
            pass
        return 0

    store = Store(db_path)
    try:
        scorer = prepare_scorer(cfg, secrets, store)
        summary, scored = run_once(
            cfg, store, Geocoder(store), scorer, dry_run=dry_run, weekly_budget=budget
        )
        if not dry_run:
            asyncio.run(_deliver(cfg, secrets, store, scored, summary))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    _print_scored(scored, cfg.scoring.min_score_to_notify)
    print(
        f"\nfetched={summary.fetched} new={summary.new_jobs} in_radius={summary.in_radius} "
        f"scored={summary.scored} est_cost=${summary.cost_estimate:.4f}"
        + (" (dry run, jobs not saved)" if dry_run else "")
    )
    if summary.budget_exhausted:
        print("weekly budget reached: remaining jobs will be scored next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
