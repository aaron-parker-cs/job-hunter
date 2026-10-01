import io
import logging

import pytest

from job_hunter import logging_setup
from job_hunter.logging_setup import JsonFormatter, RedactingFilter, redact, register_secrets


def test_registered_secret_is_redacted() -> None:
    register_secrets(["hunter2-very-secret"])
    assert "hunter2" not in redact("value is hunter2-very-secret ok")


def test_pattern_based_redaction() -> None:
    text = (
        "key sk-ant-api03-abcdefghijkl "
        "tg 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1 "
        "hook https://discord.com/api/webhooks/123/abcDEF "
        "aws AKIAABCDEFGHIJKLMNOP "
        "TOKEN=abc123"
    )
    out = redact(text)
    for leaked in ("sk-ant", "AAHdq", "webhooks/123", "AKIAABC", "abc123"):
        assert leaked not in out
    assert "[REDACTED]" in out


def test_ordinary_text_untouched() -> None:
    assert redact("fetched 12 jobs from indeed") == "fetched 12 jobs from indeed"


def test_logger_end_to_end_including_args_and_exceptions() -> None:
    register_secrets(["supersecretvalue"])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test_redaction")
    logger.handlers[:] = [handler]
    logger.propagate = False
    try:
        raise ValueError("boom supersecretvalue")
    except ValueError:
        logger.error("failed with %s", "supersecretvalue", exc_info=True)
    assert "supersecretvalue" not in stream.getvalue()
    assert logging_setup.REDACTED in stream.getvalue()


def test_library_loggers_stay_quiet_even_at_debug() -> None:
    # anthropic/httpx log full request bodies (resume, prompts) at DEBUG
    logging_setup.setup_logging("DEBUG")
    try:
        for name in ("anthropic", "httpx", "httpx2", "httpcore", "telegram", "discord", "urllib3"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("job_hunter").getEffectiveLevel() == logging.DEBUG
        assert logging.getLogger("job_hunter.fetch").getEffectiveLevel() == logging.DEBUG
    finally:
        logging_setup.setup_logging("INFO")


def test_jobspy_logs_go_through_redacting_handler() -> None:
    import io

    pytest.importorskip("jobspy")
    from jobspy.util import create_logger

    logging_setup.setup_logging("INFO")
    register_secrets(["jobspy-leaked-secret"])
    try:
        for site in logging_setup.JOBSPY_LOGGERS:
            lg = create_logger(site)  # what JobSpy does on every call
            assert len(lg.handlers) == 1 and any(
                isinstance(f, RedactingFilter) for f in lg.handlers[0].filters
            )
        lg = create_logger("Glassdoor")
        stream = io.StringIO()
        lg.handlers[0].stream = stream  # type: ignore[attr-defined]
        lg.error("Glassdoor failed with jobspy-leaked-secret")
        lg.info("finished scraping")  # INFO is noise: suppressed
        out = stream.getvalue()
        assert "jobspy-leaked-secret" not in out and "[REDACTED]" in out
        assert '"logger": "JobSpy:Glassdoor"' in out and "finished scraping" not in out
    finally:
        logging_setup.setup_logging("INFO")
