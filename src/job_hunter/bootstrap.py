"""First-run setup: create missing config/resume/profile files from templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

TEMPLATE_MARKER = (
    "<!-- job-hunter template: replace everything in this file, including this line -->"
)

# Keep CONFIG_TEMPLATE and PROFILE_TEMPLATE identical to config.example.yaml and
# profile.example.md (tests/test_bootstrap.py enforces this).
CONFIG_TEMPLATE = """\
home_location: "Your City, ST"
radius_miles: 50
include_remote: true
searches:
  - term: "Systems Engineer"
  - term: "DevOps Engineer"
sites: [indeed, linkedin, zip_recruiter, glassdoor]   # google: opt-in, often empty
hours_old: 24
results_per_search: 30
fetch:
  searches_per_run: 8          # more searches than this? runs rotate through them
  delay_seconds: [3, 8]        # random pause between scrape calls
  linkedin_fetch_description: true   # +1 request per LinkedIn job; needed for good scores
schedule_cron: "0 7,12,18 * * *"
timezone: America/Chicago
exclude_companies: []
exclude_title_keywords: ["intern", "senior director"]
scoring:
  model: "claude-haiku-4-5"   # verify current model id in Anthropic docs
  min_score_to_notify: 70
  max_jobs_scored_per_run: 60
notifier: telegram            # telegram | discord | both
notify:
  cooldown_seconds: 300       # min gap between job messages (0 = all at once)
resume_path: data/resume.md   # .md, .txt or .pdf
profile_path: data/profile.md
"""

PROFILE_TEMPLATE = (
    TEMPLATE_MARKER
    + """
# Profile

## Must-haves
- (e.g. fully remote or hybrid, uses Linux and Kubernetes)

## Dealbreakers
- (e.g. on-call every week, mandatory relocation)

## Salary floor
- (e.g. $110,000 per year)

## Preferences
- (e.g. small teams, infrastructure-as-code, no consulting firms)
"""
)

RESUME_TEMPLATE = (
    TEMPLATE_MARKER + "\n# Resume\n\nPaste your resume here as Markdown or plain text.\n"
)

DEFAULT_RESUME = Path("data/resume.md")
DEFAULT_PROFILE = Path("data/profile.md")


def _write_new(path: Path, content: str) -> bool:
    """Create a file without ever overwriting an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    except FileExistsError:
        return False
    return True


def _paths_from_config(config_path: Path) -> tuple[Path, Path]:
    """Read resume/profile paths from the raw YAML, tolerating an unedited config."""
    try:
        data: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        return DEFAULT_RESUME, DEFAULT_PROFILE
    return (
        Path(str(data.get("resume_path", DEFAULT_RESUME))),
        Path(str(data.get("profile_path", DEFAULT_PROFILE))),
    )


def bootstrap(config_path: Path) -> list[Path]:
    """Create whichever of config, resume and profile are missing; return what was created."""
    created: list[Path] = []
    if _write_new(config_path, CONFIG_TEMPLATE):
        created.append(config_path)
    resume_path, profile_path = _paths_from_config(config_path)
    if resume_path.suffix.lower() in {".md", ".txt"} and _write_new(resume_path, RESUME_TEMPLATE):
        created.append(resume_path)
    if _write_new(profile_path, PROFILE_TEMPLATE):
        created.append(profile_path)
    return created


def ensure_not_template(path: Path, text: str) -> None:
    if TEMPLATE_MARKER in text:
        raise ValueError(f"{path} is still the unedited template; fill it in first")
