# PLAN.md: job-hunter

## Goal
A self-hosted Python service that, on a schedule, searches job boards with JobSpy,
keeps only new postings within a radius of a home city, scores each against my
resume with Claude, and sends good matches to Telegram and/or a Discord bot (both first-class),
with feedback buttons that improve future scoring.

## Stack
- Latest Python 3, managed with uv (pyproject.toml + uv.lock, pinned versions)
- python-jobspy (job scraping), anthropic (scoring), pydantic + pydantic-settings
  (config/secrets validation), PyYAML, geopy (Nominatim geocoding), httpx,
  python-telegram-bot (v21+, async), discord.py (v2+), APScheduler (AsyncIOScheduler; bots and scheduler share one event loop), SQLite via stdlib sqlite3
- pytest, ruff, mypy; pre-commit with gitleaks
- Docker + docker compose

## Repo layout
job-hunter/
  src/job_hunter/
    __main__.py        # CLI entry: `run` (service), `once`, `once --dry-run`, `init-db`
    config.py          # loads config.yaml + secrets; pydantic models; fails fast if invalid
    secrets.py         # SecretsProvider: EnvProvider (default), AwsSsmProvider (optional)
    fetch.py           # JobSpy wrapper; one call per (search term x site group)
    geo.py             # geocode + haversine; on-disk cache in SQLite
    store.py           # SQLite schema, migrations, dedup, feedback storage
    score.py           # Claude scoring with structured output
    notify/
      base.py          # Notifier protocol
      telegram_bot.py  # messages with inline buttons + callback handler
      discord_bot.py   # bot: embeds with buttons + reactions feeding feedback; webhook fallback
                       # (module names avoid shadowing the telegram/discord packages)
    pipeline.py        # fetch -> normalize -> filter -> dedup -> score -> notify
    logging_setup.py   # structured logs; redacts anything that looks like a secret
  tests/
  config.example.yaml
  profile.example.md
  .env.example
  Dockerfile
  docker-compose.yml
  .dockerignore
  .gitignore
  .pre-commit-config.yaml
  README.md
  data/                # gitignored: resume, profile.md, config.yaml, jobs.db

## Configuration (non-secret, data/config.yaml)
home_location: "Your City, ST"
radius_miles: 50
include_remote: true
searches:
  - term: "Systems Engineer"
  - term: "DevOps Engineer"
sites: [indeed, linkedin, zip_recruiter, glassdoor, google]
hours_old: 24              # only postings from the last N hours
results_per_search: 30
schedule_cron: "0 7,12,18 * * *"   # America/Chicago
timezone: America/Chicago
exclude_companies: []
exclude_title_keywords: ["intern", "senior director"]
scoring:
  model: "claude-haiku-4-5"        # verify current model id in Anthropic docs
  min_score_to_notify: 70
  max_jobs_scored_per_run: 60      # cost guardrail
notifier: telegram                 # telegram | discord | both
resume_path: data/resume.md        # accept .md, .txt or .pdf (extract text with pypdf)
profile_path: data/profile.md      # must-haves, dealbreakers, salary floor, preferences

## Secrets (never in config.yaml, never in the image, never in git)
ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID          # only this chat may receive messages or press buttons
DISCORD_BOT_TOKEN         # when notifier includes discord
DISCORD_CHANNEL_ID        # channel where job posts go
DISCORD_ALLOWED_USER_ID   # only this user's clicks/reactions count
DISCORD_WEBHOOK_URL       # optional one-way fallback, no feedback
SECRETS_BACKEND=env       # env | aws_ssm
AWS_REGION, SSM_PREFIX    # only when SECRETS_BACKEND=aws_ssm, e.g. /job-hunter/

- EnvProvider reads os.environ (docker compose loads .env via env_file).
- AwsSsmProvider uses boto3 to read SecureString parameters under SSM_PREFIX with
  decryption; AWS creds come from the standard chain (env vars or ~/.aws). Make boto3
  an optional extra (`uv sync --extra aws`) so the default image stays small.
- Hold secrets in pydantic SecretStr so they never print in repr or logs.

## Pipeline details
1. Fetch: call jobspy.scrape_jobs per search term with location, distance, hours_old,
   results_wanted, is_remote as configured. Catch and log per-site failures; one site
   failing must not fail the run. Add a small random delay between calls.
2. Normalize into a Job dataclass: id (sha256 of normalized company+title+city),
   url, title, company, location text, is_remote, salary min/max/interval, date_posted,
   description (truncate to ~6k chars), source site.
3. Filter: drop excluded companies/keywords; geocode job location (cache results;
   respect Nominatim 1 req/sec with a descriptive User-Agent); keep if remote (when
   enabled) or haversine distance <= radius_miles; keep unknown locations but flag them.
4. Dedup: skip ids and URLs already in the jobs table (catches cross-board reposts).
5. Score with Claude:
   - System prompt = instructions + resume + profile + a summary of recent feedback
     (last ~30 liked/disliked titles with companies). Mark it for prompt caching.
   - Force structured output via a tool definition returning:
     {score: int 0-100, verdict: "strong"|"maybe"|"weak", reasons: [str] (max 3),
      concerns: [str] (max 3), seniority_match: bool, est_salary_ok: bool|null}
   - Retry with backoff on 429/5xx; stop scoring when max_jobs_scored_per_run is hit.
   - Store score JSON, model, and token usage per job.
6. Notify jobs with score >= threshold, highest first, max 15 per run. Telegram message:
   title, company, distance (or Remote), salary if known, score, 2 reasons, 1 concern,
   link. Inline buttons: Interested / Not a fit / Hide company / Applied.
7. Record run summary (fetched, new, in-radius, scored, sent, cost estimate) and send
   a one-line digest if nothing matched, so silence never means "broken".

## Telegram behavior
- Long polling (no inbound ports needed). Ignore every update whose chat id is not
  TELEGRAM_CHAT_ID.
- Button presses update the feedback table and edit the message to show the choice.
- Commands: /status (last run summary), /run (trigger a run now), /pause, /resume,
  /top (best 5 unapplied matches from the last 7 days).

## Discord behavior
- Outbound gateway connection only (no inbound ports). Intents: guilds, guild
  messages, guild message reactions; no message-content intent needed.
- Each match is an embed in DISCORD_CHANNEL_ID with persistent buttons
  (discord.ui.View, timeout=None, custom_id "fb:<action>:<job_id>" so they still work
  after restarts): Interested / Not a fit / Hide company / Applied.
- Also pre-add reactions (thumbs up, thumbs down, no-entry, check mark) and handle
  on_raw_reaction_add / on_raw_reaction_remove, so reactions on older, uncached
  messages still count. Map message id -> job id via the notifications table.
- Ignore every click or reaction not from DISCORD_ALLOWED_USER_ID (and the bot's own).
- Slash commands mirror Telegram: /status, /run, /pause, /resume, /top.
- If only DISCORD_WEBHOOK_URL is set, post plain embeds with no feedback.
- Both bots write to the same feedback table; with notifier: both, feedback from
  either one counts.

## Storage (SQLite at /data/jobs.db, WAL mode)
Tables: jobs, scores, notifications, feedback, runs, geocache, hidden_companies.
Simple versioned migrations in store.py.

## Security requirements
- .gitignore: .env, .env.*, !.env.example, data/, *.db, *.pdf, resume*, profile.md,
  config.yaml (only config.example.yaml is committed).
- .dockerignore mirrors that plus .git; the image never contains data/ or .env.
- pre-commit: gitleaks, ruff, mypy, end-of-file/trailing-whitespace, check-added-large-files.
- Docker: python:3.12-slim base, multi-stage build, non-root user (uid 10001),
  read_only root filesystem, /data as the only writable volume (plus tmpfs /tmp),
  no-new-privileges, cap_drop ALL, healthcheck, restart unless-stopped.
- Logs never include the resume text, secrets, or full prompts at INFO level.
- README section on rotating each key and what to do if one leaks.

## CLI
- python -m job_hunter run            # scheduler + Telegram listener (container default)
- python -m job_hunter once [--dry-run]  # one pass; dry-run prints instead of sending
- python -m job_hunter init-db
- python -m job_hunter test-notify    # sends one sample message

## Tests
- Unit: normalization, id hashing, haversine, filters, dedup, score JSON parsing,
  secret redaction in logs, Telegram chat-id guard.
- Fixtures: a saved JobSpy DataFrame (CSV) and a canned Claude tool response; no
  network in tests (mock anthropic, jobspy, geopy, Telegram).
- CI: GitHub Actions workflow running ruff, mypy, pytest, and gitleaks on push.

## README must cover
What it does, architecture diagram (mermaid), prerequisites, creating the Telegram bot
and finding the chat id, creating the Discord bot (Developer Portal, intents, invite URL
with minimal permissions, copying channel and user ids), Discord webhook fallback, config reference, secrets
(.env and AWS SSM options), running locally, running with docker compose, cost
estimate and guardrails, troubleshooting (JobSpy site blocks, rate limits), key
rotation, and a note that scraping may conflict with some sites' terms.

## Milestones (stop for review after each)
1. Skeleton: pyproject, layout, config + secrets loading with validation, logging,
   .gitignore/.dockerignore/.env.example/pre-commit. Tests for config and redaction.
2. Fetch + normalize + geo filter + dedup + SQLite; `once --dry-run` prints new jobs.
3. Claude scoring with structured output, caching, cost guardrails; dry-run shows scores.
4. Telegram and Discord bot notifiers with buttons/reactions, shared feedback storage,
   chat/user-id guards; Discord webhook fallback.
5. Scheduler + commands; `run` mode; feedback folded into the scoring prompt.
6. Dockerfile, docker-compose.yml, healthcheck, CI workflow.
7. README and final review against the Security requirements list.