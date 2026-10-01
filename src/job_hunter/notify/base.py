"""Notifier protocol and formatting helpers shared by all notifiers."""

from __future__ import annotations

from typing import Protocol

from job_hunter.models import Job
from job_hunter.score import ScoreResult


class Notifier(Protocol):
    name: str

    async def send_job(self, job: Job, result: ScoreResult) -> None: ...

    async def send_text(self, text: str) -> None: ...

    async def aclose(self) -> None: ...


def where_text(job: Job) -> str:
    if job.is_remote:
        return "Remote"
    if job.distance_miles is not None:
        return f"{job.distance_miles:.0f} mi"
    return f"{job.location or 'unknown location'} (distance unknown)"


def _money(value: float, interval: str | None) -> str:
    if interval in (None, "", "yearly") and value >= 1000:
        return f"${value / 1000:g}k"
    return f"${value:g}"


def format_salary(job: Job) -> str | None:
    lo, hi = job.salary_min, job.salary_max
    if not (lo or hi):
        return None
    unit = {"yearly": "/yr", "hourly": "/hr", "monthly": "/mo", "weekly": "/wk"}.get(
        job.salary_interval or "yearly", ""
    )
    if lo and hi and lo != hi:
        amount = f"{_money(lo, job.salary_interval)}–{_money(hi, job.salary_interval)}"
    else:
        amount = _money(lo or hi or 0, job.salary_interval)
    return amount + unit
