"""Structured logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

REDACTED = "[REDACTED]"
JOBSPY_LOGGERS = ("Indeed", "LinkedIn", "ZipRecruiter", "Glassdoor", "Google")

# Patterns for things that look like secrets, even when not registered explicitly.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic keys
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}"),  # Telegram bot tokens
    re.compile(r"[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,7}\.[A-Za-z0-9_\-]{27,}"),  # Discord
    re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key ids
]
_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[=:]\s*)\S+")

_known: set[str] = set()


def register_secrets(values: list[str]) -> None:
    """Register exact secret values to be scrubbed from all log output."""
    _known.update(v for v in values if v and len(v) >= 4)


def redact(text: str) -> str:
    for value in sorted(_known, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(REDACTED, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            # Render and scrub the traceback now, since formatters render it later.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    # Third-party libraries stay at WARNING whatever LOG_LEVEL is: at INFO/DEBUG they log
    # request URLs (Telegram puts the bot token in the path) and full request bodies (which
    # would include the resume and prompts). Only our own code follows LOG_LEVEL.
    root.setLevel(logging.WARNING)
    logging.getLogger("job_hunter").setLevel(level.upper())
    # JobSpy attaches its own plain-text handler and stops propagation, which would bypass
    # the redaction above. Giving its loggers our handler up front means it adds none.
    for name in JOBSPY_LOGGERS:
        jobspy_logger = logging.getLogger(f"JobSpy:{name}")
        jobspy_logger.handlers[:] = [handler]
        jobspy_logger.propagate = False
        jobspy_logger.setLevel(logging.WARNING)
