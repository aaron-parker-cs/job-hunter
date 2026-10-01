"""`check-anthropic`: explain why the Anthropic API key is (or is not) being accepted.

Prints facts about the key (where it came from, its shape, its last four characters) but never
the key itself, then makes one tiny request and reports the API's own answer.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

import anthropic

from job_hunter import config
from job_hunter.score import Usage, estimate_cost

KEY_VAR = "ANTHROPIC_API_KEY"
EXPECTED_PREFIX = "sk-ant-"
TEST_PROMPT = "Reply with exactly one word: pong"


def key_problems(key: str) -> list[str]:
    """Things that commonly break a pasted key. Never includes the key's contents."""
    problems: list[str] = []
    if key != key.strip():
        problems.append("has leading/trailing whitespace (stray space or Windows line ending?)")
    if any(ch.isspace() for ch in key.strip()):
        problems.append("contains whitespace in the middle (two keys pasted together?)")
    if key.strip()[:1] in "\"'" or key.strip()[-1:] in "\"'":
        problems.append("is wrapped in quote characters")
    if not key.startswith(EXPECTED_PREFIX):
        problems.append(f"does not start with {EXPECTED_PREFIX!r} (is it an Anthropic API key?)")
    if len(key) < 40:
        problems.append(f"is only {len(key)} characters long (truncated paste?)")
    if not key.isascii() or not key.isprintable():
        problems.append("contains non-ASCII or non-printable characters")
    if "..." in key or "your" in key.lower():
        problems.append("looks like a placeholder")
    return problems


def _hint(exc: anthropic.APIStatusError) -> str:
    status = exc.status_code
    if status == 401:
        return (
            "401 means Anthropic does not recognise this key: it is revoked, mistyped, or a "
            "different key than you think is being used (see 'source' above)."
        )
    if status == 403:
        return (
            "403 means the key is valid but not allowed to do this: check that its workspace "
            "has access to the model and that your organisation's API access is active."
        )
    if status == 404:
        return "404 usually means the model id in scoring.model is wrong or not available to you."
    if status == 400 and "credit" in str(exc.message).lower():
        return "Add credit in the Anthropic console (Plans & Billing)."
    return ""


def check_anthropic(
    model: str,
    *,
    environ: Mapping[str, str] | None = None,
    from_file: set[str] | None = None,
    shadowed: list[str] | None = None,
    client_factory: Callable[[str], Any] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Send a real test prompt. 0 = key accepted and a reply came back; 1 = rejected or no
    reply; 2 = no key set."""
    env = os.environ if environ is None else environ
    files = config.ENV_FROM_FILE if from_file is None else from_file
    key = env.get(KEY_VAR, "")
    if not key.strip():
        out(f"{KEY_VAR} is not set (checked the shell environment and .env).")
        return 2

    source = ".env file" if KEY_VAR in files else "shell environment"
    out(f"source: {source}")
    if KEY_VAR in (shadowed if shadowed is not None else config.shadowed_env_keys()):
        out(
            f"WARNING: {KEY_VAR} is also in your .env file with a DIFFERENT value. The shell "
            "value wins. If you meant to use the .env key, run: unset " + KEY_VAR
        )
    out(f"length: {len(key)}, ends with: ...{key.strip()[-4:]} (compare with the console)")
    problems = key_problems(key)
    for problem in problems:
        out(f"problem: the key {problem}")
    if not problems:
        out("shape: looks like a normal Anthropic key")

    factory = client_factory or (
        lambda k: anthropic.Anthropic(api_key=k, max_retries=0, timeout=30.0)
    )
    try:
        response = factory(key.strip()).messages.create(
            model=model, max_tokens=40, messages=[{"role": "user", "content": TEST_PROMPT}]
        )
    except anthropic.APIStatusError as exc:
        out(f"request: REJECTED, HTTP {exc.status_code}: {str(exc.message)[:300]}")
        hint = _hint(exc)
        if hint:
            out(f"hint: {hint}")
        return 1
    except Exception as exc:
        out(f"request: FAILED before getting an answer ({type(exc).__name__}): check your network")
        return 1
    out(f"request: OK (model {model} accepted the key)")
    reply = "".join(
        str(block.text) for block in getattr(response, "content", []) if block.type == "text"
    ).strip()
    if not reply:
        out("response: EMPTY (the key works but the model returned no text)")
        return 1
    out(f"response: {reply[:200]!r}")
    usage = getattr(response, "usage", None)
    if usage is not None:
        tokens = Usage(input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0)
        out(
            f"usage: {tokens.input_tokens} input + {tokens.output_tokens} output tokens "
            f"(about ${estimate_cost(model, tokens):.6f})"
        )
    return 0
