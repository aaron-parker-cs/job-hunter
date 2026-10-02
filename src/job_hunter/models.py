"""Job dataclass and normalization of JobSpy rows."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

MAX_DESCRIPTION_CHARS = 6000


@dataclass
class Job:
    id: str
    url: str
    title: str
    company: str
    location: str
    is_remote: bool
    salary_min: float | None
    salary_max: float | None
    salary_interval: str | None
    date_posted: str | None
    description: str
    site: str
    distance_miles: float | None = None
    location_unknown: bool = False
    search_term: str = ""  # the configured search that found it (first one, if several)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def normalize_company(company: str) -> str:
    return _norm(company)


def city_of(location: str) -> str:
    return location.split(",")[0]


def make_job_id(company: str, title: str, location: str) -> str:
    key = "|".join((_norm(company), _norm(title), _norm(city_of(location))))
    return hashlib.sha256(key.encode()).hexdigest()


def _clean(value: Any) -> Any:
    """Turn pandas NaN/NaT/None into None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if type(value).__name__ in ("NaTType", "NAType"):
        return None
    return value


def _str(value: Any) -> str:
    value = _clean(value)
    return "" if value is None else str(value).strip()


def _float(value: Any) -> float | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value).strip() or None


def normalize_row(row: dict[str, Any]) -> Job | None:
    """Convert one JobSpy record into a Job; None if it lacks a title, company or url."""
    title, company, url = _str(row.get("title")), _str(row.get("company")), _str(row.get("job_url"))
    if not (title and company and url):
        return None
    location = _str(row.get("location"))
    is_remote = bool(_clean(row.get("is_remote")))
    interval = _str(row.get("interval")) or None
    return Job(
        id=make_job_id(company, title, location),
        url=url,
        title=title,
        company=company,
        location=location,
        is_remote=is_remote,
        salary_min=_float(row.get("min_amount")),
        salary_max=_float(row.get("max_amount")),
        salary_interval=interval,
        date_posted=_date(row.get("date_posted")),
        description=_str(row.get("description"))[:MAX_DESCRIPTION_CHARS],
        site=_str(row.get("site")),
    )
