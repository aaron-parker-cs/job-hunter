"""SQLite storage: schema, versioned migrations, dedup, geocache, runs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from job_hunter.models import Job, normalize_company
from job_hunter.score import ScoreResult

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT NOT NULL,
        is_remote INTEGER NOT NULL,
        salary_min REAL,
        salary_max REAL,
        salary_interval TEXT,
        date_posted TEXT,
        description TEXT NOT NULL,
        site TEXT NOT NULL,
        distance_miles REAL,
        location_unknown INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL
    );
    CREATE TABLE scores (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id),
        score_json TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        channel TEXT NOT NULL,
        message_id TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        UNIQUE (channel, message_id)
    );
    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        action TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        fetched INTEGER NOT NULL DEFAULT 0,
        new_jobs INTEGER NOT NULL DEFAULT 0,
        in_radius INTEGER NOT NULL DEFAULT 0,
        scored INTEGER NOT NULL DEFAULT 0,
        sent INTEGER NOT NULL DEFAULT 0,
        cost_estimate REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE geocache (
        query TEXT PRIMARY KEY,
        lat REAL,
        lon REAL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE hidden_companies (
        company_norm TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    );
    """,
    # 2: cache token accounting + spend ledger (feeds the weekly budget)
    """
    ALTER TABLE scores ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE scores ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE spend (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        model TEXT NOT NULL,
        cost_usd REAL NOT NULL
    );
    """,
    # 3: small key/value state (e.g. paused flag) that survives restarts
    """
    CREATE TABLE state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 4: notification outbox, so matches can be released one per cooldown period
    """
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notifier TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        score INTEGER NOT NULL,
        queued_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        UNIQUE (notifier, job_id)
    );
    """,
]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        is_remote=bool(row["is_remote"]),
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        salary_interval=row["salary_interval"],
        date_posted=row["date_posted"],
        description=row["description"],
        site=row["site"],
        distance_miles=row["distance_miles"],
        location_unknown=bool(row["location_unknown"]),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RunSummary:
    fetched: int = 0
    new_jobs: int = 0
    in_radius: int = 0
    scored: int = 0
    sent: int = 0
    cost_estimate: float = 0.0
    budget_exhausted: bool = False
    run_id: int = 0


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        version: int = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(script)
            self.conn.execute(f"PRAGMA user_version={i}")
        self.conn.commit()

    # --- jobs / dedup ---------------------------------------------------

    def is_known(self, job: Job) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? OR url = ?", (job.id, job.url)
        ).fetchone()
        return row is not None

    def add_job(self, job: Job) -> bool:
        """Insert a job; returns False if its id or url already exists."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO jobs
            (id, url, title, company, location, is_remote, salary_min, salary_max,
             salary_interval, date_posted, description, site, distance_miles,
             location_unknown, first_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.id,
                job.url,
                job.title,
                job.company,
                job.location,
                int(job.is_remote),
                job.salary_min,
                job.salary_max,
                job.salary_interval,
                job.date_posted,
                job.description,
                job.site,
                job.distance_miles,
                int(job.location_unknown),
                _now(),
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # --- scores / spend / feedback ----------------------------------------

    def add_score(
        self,
        job_id: str,
        score_json: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO scores
            (job_id, score_json, model, input_tokens, output_tokens,
             cache_write_tokens, cache_read_tokens, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                job_id,
                score_json,
                model,
                input_tokens,
                output_tokens,
                cache_write_tokens,
                cache_read_tokens,
                _now(),
            ),
        )
        self.conn.commit()

    def record_spend(self, model: str, cost_usd: float) -> None:
        self.conn.execute(
            "INSERT INTO spend (ts, model, cost_usd) VALUES (?,?,?)", (_now(), model, cost_usd)
        )
        self.conn.commit()

    def spend_since(self, since: datetime) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE ts >= ?",
            (since.astimezone(UTC).isoformat(timespec="seconds"),),
        ).fetchone()
        return float(row[0])

    def recent_feedback(self, limit: int = 30) -> list[tuple[str, str, str]]:
        """Latest feedback as (action, title, company), newest first."""
        rows = self.conn.execute(
            """SELECT f.action, j.title, j.company FROM feedback f
            JOIN jobs j ON j.id = f.job_id WHERE j.site != 'test'
            ORDER BY f.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def add_feedback(self, job_id: str, action: str, source: str) -> None:
        """Record feedback; repeating the same (job, action, source) is a no-op."""
        self.conn.execute(
            """INSERT INTO feedback (job_id, action, source, created_at)
            SELECT ?, ?, ?, ? WHERE NOT EXISTS (
                SELECT 1 FROM feedback WHERE job_id = ? AND action = ? AND source = ?)""",
            (job_id, action, source, _now(), job_id, action, source),
        )
        self.conn.commit()

    def remove_feedback(self, job_id: str, action: str, source: str) -> None:
        self.conn.execute(
            "DELETE FROM feedback WHERE job_id = ? AND action = ? AND source = ?",
            (job_id, action, source),
        )
        self.conn.commit()

    # --- notifications / lookups ------------------------------------------

    def add_notification(self, job_id: str, channel: str, message_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications (job_id, channel, message_id, sent_at)"
            " VALUES (?,?,?,?)",
            (job_id, channel, message_id, _now()),
        )
        self.conn.commit()

    def job_for_message(self, channel: str, message_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT job_id FROM notifications WHERE channel = ? AND message_id = ?",
            (channel, message_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _row_to_job(row)

    # --- outbox (cooldown-spaced notifications) -------------------------------

    def enqueue(self, notifier: str, job_id: str, score: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox (notifier, job_id, score, queued_at) VALUES (?,?,?,?)",
            (notifier, job_id, score, _now()),
        )
        self.conn.commit()

    def next_outbox(self, notifier: str) -> sqlite3.Row | None:
        """Highest-scoring queued item for a notifier (oldest first on ties)."""
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM outbox WHERE notifier = ? ORDER BY score DESC, id ASC LIMIT 1",
            (notifier,),
        ).fetchone()
        return row

    def complete_outbox(self, outbox_id: int) -> None:
        self.conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
        self.conn.commit()

    def fail_outbox(self, outbox_id: int, max_attempts: int = 5) -> None:
        """Count a failed send; give up on the item after max_attempts."""
        self.conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (outbox_id,))
        self.conn.execute(
            "DELETE FROM outbox WHERE id = ? AND attempts >= ?", (outbox_id, max_attempts)
        )
        self.conn.commit()

    def prune_outbox(self, older_than: datetime) -> int:
        cur = self.conn.execute(
            "DELETE FROM outbox WHERE queued_at < ?",
            (older_than.astimezone(UTC).isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        return cur.rowcount

    def outbox_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def get_score(self, job_id: str) -> ScoreResult | None:
        row = self.conn.execute(
            "SELECT score_json FROM scores WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else ScoreResult.model_validate_json(row["score_json"])

    # --- state / status / top matches ---------------------------------------

    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row

    def top_matches(
        self, *, since: datetime, min_score: int, limit: int = 5
    ) -> list[tuple[Job, ScoreResult]]:
        """Best-scoring recent jobs the user has not applied to, rejected or hidden."""
        rows = self.conn.execute(
            """SELECT j.*, s.score_json FROM scores s JOIN jobs j ON j.id = s.job_id
            WHERE s.created_at >= ? AND j.site != 'test'
              AND json_extract(s.score_json, '$.score') >= ?
              AND NOT EXISTS (
                SELECT 1 FROM feedback f WHERE f.job_id = j.id
                AND f.action IN ('applied', 'not_fit', 'hide_company'))
            ORDER BY json_extract(s.score_json, '$.score') DESC, s.created_at DESC
            LIMIT ?""",
            (since.astimezone(UTC).isoformat(timespec="seconds"), min_score, limit * 5),
        ).fetchall()
        hidden = self.hidden_companies()
        out: list[tuple[Job, ScoreResult]] = []
        for row in rows:
            job = _row_to_job(row)
            if normalize_company(job.company) in hidden:
                continue
            out.append((job, ScoreResult.model_validate_json(row["score_json"])))
        return out[:limit]

    def set_run_sent(self, run_id: int, sent: int) -> None:
        self.conn.execute("UPDATE runs SET sent = ? WHERE id = ?", (sent, run_id))
        self.conn.commit()

    # --- hidden companies -----------------------------------------------

    def hidden_companies(self) -> set[str]:
        rows = self.conn.execute("SELECT company_norm FROM hidden_companies").fetchall()
        return {r[0] for r in rows}

    def unhide_company(self, company: str) -> None:
        self.conn.execute(
            "DELETE FROM hidden_companies WHERE company_norm = ?", (normalize_company(company),)
        )
        self.conn.commit()

    def hide_company(self, company: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO hidden_companies VALUES (?, ?)",
            (normalize_company(company), _now()),
        )
        self.conn.commit()

    # --- geocache -------------------------------------------------------

    def geocache_get(self, query: str) -> tuple[bool, tuple[float, float] | None]:
        """Returns (hit, coords). A hit with None coords is a cached failed lookup."""
        row = self.conn.execute(
            "SELECT lat, lon FROM geocache WHERE query = ?", (query,)
        ).fetchone()
        if row is None:
            return False, None
        if row["lat"] is None:
            return True, None
        return True, (row["lat"], row["lon"])

    def geocache_put(self, query: str, coords: tuple[float, float] | None) -> None:
        lat, lon = coords if coords else (None, None)
        self.conn.execute(
            "INSERT OR REPLACE INTO geocache VALUES (?, ?, ?, ?)", (query, lat, lon, _now())
        )
        self.conn.commit()

    # --- runs -----------------------------------------------------------

    def record_run(self, started_at: str, summary: RunSummary) -> int:
        cur = self.conn.execute(
            """INSERT INTO runs (started_at, finished_at, fetched, new_jobs, in_radius,
               scored, sent, cost_estimate) VALUES (?,?,?,?,?,?,?,?)""",
            (
                started_at,
                _now(),
                summary.fetched,
                summary.new_jobs,
                summary.in_radius,
                summary.scored,
                summary.sent,
                summary.cost_estimate,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)
