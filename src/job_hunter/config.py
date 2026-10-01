"""Loads config.yaml (non-secret settings) into validated pydantic models."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Site = Literal["indeed", "linkedin", "zip_recruiter", "glassdoor", "google"]


def _default_sites() -> list[Site]:
    # Google is supported but currently returns nothing through JobSpy, so it is opt-in.
    return ["indeed", "linkedin", "zip_recruiter", "glassdoor"]


class ConfigError(RuntimeError):
    pass


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str = Field(min_length=1)


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "claude-haiku-4-5"
    min_score_to_notify: int = Field(default=70, ge=0, le=100)
    max_jobs_scored_per_run: int = Field(default=60, ge=1)


class Fetch(BaseModel):
    """Politeness limits for scraping job boards."""

    model_config = ConfigDict(extra="forbid")
    searches_per_run: int = Field(default=8, ge=1, le=50)
    delay_seconds: tuple[float, float] = (3.0, 8.0)
    linkedin_fetch_description: bool = True
    max_consecutive_failures: int = Field(default=2, ge=1)

    @field_validator("delay_seconds")
    @classmethod
    def _delay_range(cls, v: tuple[float, float]) -> tuple[float, float]:
        if not 0 <= v[0] <= v[1]:
            raise ValueError("delay_seconds must be [min, max] with 0 <= min <= max")
        return v


class Notify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Minimum gap between job messages per notifier in `run` mode; 0 sends everything at once.
    cooldown_seconds: int = Field(default=300, ge=0)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home_location: str = Field(min_length=1)
    radius_miles: float = Field(default=50, gt=0)
    include_remote: bool = True
    searches: list[Search] = Field(min_length=1)
    sites: list[Site] = Field(default_factory=_default_sites, min_length=1)
    hours_old: int = Field(default=24, gt=0)
    results_per_search: int = Field(default=30, gt=0)
    fetch: Fetch = Field(default_factory=Fetch)
    schedule_cron: str = "0 7,12,18 * * *"
    timezone: str = "America/Chicago"
    exclude_companies: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    scoring: Scoring = Field(default_factory=Scoring)
    notifier: Literal["telegram", "discord", "both"] = "telegram"
    notify: Notify = Field(default_factory=Notify)
    resume_path: Path = Path("data/resume.md")
    profile_path: Path = Path("data/profile.md")

    @field_validator("searches")
    @classmethod
    def _search_count(cls, v: list[Search]) -> list[Search]:
        if len(v) > 200:
            raise ValueError("at most 200 searches are supported")
        return v

    @field_validator("home_location")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        if v.strip() == "Your City, ST":
            raise ValueError("set home_location to your real city")
        return v

    @field_validator("schedule_cron")
    @classmethod
    def _cron_fields(cls, v: str) -> str:
        if len(v.split()) != 5:
            raise ValueError("schedule_cron must have 5 fields (minute hour day month weekday)")
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(v)
        except ValueError as exc:
            raise ValueError(f"invalid schedule_cron: {exc}") from None
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {v!r}") from None
        return v

    @field_validator("resume_path")
    @classmethod
    def _resume_ext(cls, v: Path) -> Path:
        if v.suffix.lower() not in {".md", ".txt", ".pdf"}:
            raise ValueError("resume_path must be .md, .txt or .pdf")
        return v


def load_config(path: Path, *, check_files: bool = True) -> Config:
    """Load and validate config; raises ConfigError with a readable message."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"Invalid config {path}:\n" + "\n".join(lines)) from None
    if check_files:
        for label, p in (("resume_path", cfg.resume_path), ("profile_path", cfg.profile_path)):
            if not p.is_file():
                raise ConfigError(f"{label} not found: {p}")
    return cfg


def weekly_budget_from_env(environ: Mapping[str, str] | None = None) -> float | None:
    """MAX_WEEKLY_BUDGET_USD: cap on estimated Claude spend per rolling 7 days (unset = no cap)."""
    env = os.environ if environ is None else environ
    raw = env.get("MAX_WEEKLY_BUDGET_USD", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("MAX_WEEKLY_BUDGET_USD must be a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError("MAX_WEEKLY_BUDGET_USD must be greater than 0")
    return value


ENV_FROM_FILE: set[str] = set()  # variables load_env_file actually took from the file


def load_env_file(path: Path | None = None) -> int:
    """Load KEY=VALUE pairs from a .env file into os.environ; real env vars take precedence.

    Handles CRLF line endings, quotes and trailing `# comments`. A missing file is fine
    (containers get their environment from compose). Returns how many variables it set.
    """
    from dotenv import dotenv_values

    env_path = path or Path(os.environ.get("ENV_FILE", ".env"))
    if not env_path.is_file():
        return 0
    loaded = 0
    for key, value in dotenv_values(env_path).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value
            ENV_FROM_FILE.add(key)
            loaded += 1
    return loaded


def shadowed_env_keys(path: Path | None = None) -> list[str]:
    """Names set in both the shell and the .env file with different values (the shell wins).

    A stale `export` or an old `source .env` in a terminal silently beats the file, which is a
    classic cause of "I changed the key but nothing changed". Only names are returned.
    """
    from dotenv import dotenv_values

    env_path = path or Path(os.environ.get("ENV_FILE", ".env"))
    if not env_path.is_file():
        return []
    return sorted(
        key
        for key, value in dotenv_values(env_path).items()
        if value is not None and key in os.environ and os.environ[key] != value
    )
