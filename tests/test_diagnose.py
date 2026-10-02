import os
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from job_hunter import config
from job_hunter.config import load_env_file, shadowed_env_keys
from job_hunter.diagnose import check_anthropic, key_problems

GOOD = "sk-ant-api03-" + "x" * 60 + "WXYZ"


def status_error(cls: type[anthropic.APIStatusError], code: int, msg: str) -> Exception:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(msg, response=httpx.Response(code, request=req), body=None)


def reply(text: str | None) -> Any:
    from types import SimpleNamespace

    blocks = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=20, output_tokens=2))


def scoring_reply() -> Any:
    import json
    from types import SimpleNamespace

    payload = {
        "explanation": "Remote DevOps role matching Kubernetes and Terraform experience.",
        "score": 82,
        "verdict": "strong",
        "reasons": ["fits"],
        "concerns": [],
        "seniority_match": True,
        "est_salary_ok": True,
    }
    blocks = [
        SimpleNamespace(type="text", text=json.dumps(payload)),
        SimpleNamespace(type="tool_use", name="record_score", input=payload),
    ]
    usage = SimpleNamespace(input_tokens=900, output_tokens=60)
    return SimpleNamespace(content=blocks, usage=usage, stop_reason="end_turn")


class Client:
    def __init__(self, outcome: Exception | None) -> None:
        self.outcome = outcome
        self.messages = self

    def create(self, **kw: Any) -> object:
        self.calls = getattr(self, "calls", []) + [kw]
        self.last = self.calls[0]  # the plain test prompt, not the later scoring request
        if self.outcome:
            raise self.outcome
        if "system" in kw:  # the scoring request (the plain ping has no system prompt)
            return scoring_reply()
        return reply("pong")


def run(env: dict[str, str], outcome: Exception | None = None, **kw: Any) -> tuple[int, str]:
    lines: list[str] = []
    code = check_anthropic(
        "claude-haiku-4-5",
        environ=env,
        client_factory=lambda k: Client(outcome),
        out=lines.append,
        **kw,
    )
    return code, "\n".join(lines)


def test_good_key_has_no_problems() -> None:
    assert key_problems(GOOD) == []


@pytest.mark.parametrize(
    "key, needle",
    [
        (GOOD + "\r", "whitespace"),
        (GOOD + " ", "whitespace"),
        ("sk-ant-api03-aaaa bbbb" + "c" * 40, "middle"),
        ('"' + GOOD + '"', "quote"),
        ("sk-proj-" + "x" * 60, "does not start with"),
        ("sk-ant-short", "characters long"),
        ("sk-ant-" + "\u2013" * 50, "non-ASCII"),
        ("sk-ant-your-key-here" + "x" * 30, "placeholder"),
    ],
)
def test_common_paste_problems_detected(key: str, needle: str) -> None:
    assert any(needle in p for p in key_problems(key))


def test_report_never_contains_the_key() -> None:
    code, text = run({"ANTHROPIC_API_KEY": GOOD})
    assert code == 0 and "request: OK" in text
    assert GOOD not in text and "x" * 20 not in text
    assert "ends with: ...WXYZ" in text and "length: 77" in text


def test_missing_key() -> None:
    code, text = run({})
    assert code == 2 and "not set" in text


def test_401_explains_and_reports_source_and_shadowing() -> None:
    err = status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    code, text = run(
        {"ANTHROPIC_API_KEY": GOOD}, err, from_file=set(), shadowed=["ANTHROPIC_API_KEY"]
    )
    assert code == 1
    assert "source: shell environment" in text
    assert "DIFFERENT value" in text and "unset ANTHROPIC_API_KEY" in text
    assert "HTTP 401: invalid x-api-key" in text and "does not recognise" in text


def test_source_is_env_file_when_loaded_from_it() -> None:
    _, text = run({"ANTHROPIC_API_KEY": GOOD}, None, from_file={"ANTHROPIC_API_KEY"}, shadowed=[])
    assert "source: .env file" in text and "DIFFERENT" not in text


def test_403_hint_mentions_workspace_and_credit_hint() -> None:
    code, text = run(
        {"ANTHROPIC_API_KEY": GOOD},
        status_error(anthropic.PermissionDeniedError, 403, "not allowed"),
        from_file=set(),
        shadowed=[],
    )
    assert code == 1 and "workspace" in text
    _, text = run(
        {"ANTHROPIC_API_KEY": GOOD},
        status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"),
        from_file=set(),
        shadowed=[],
    )
    assert "Add credit" in text


def test_network_failure_is_reported_not_raised() -> None:
    code, text = run(
        {"ANTHROPIC_API_KEY": GOOD}, ConnectionError("dns"), from_file=set(), shadowed=[]
    )
    assert code == 1 and "check your network" in text


# --- shadowing --------------------------------------------------------------------------------


def test_shadowed_env_keys_detects_stale_shell_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("A_KEY=from-file\nB_KEY=same\nC_KEY=only-in-file\n")
    monkeypatch.setenv("A_KEY", "stale-from-shell")
    monkeypatch.setenv("B_KEY", "same")
    monkeypatch.delenv("C_KEY", raising=False)
    assert shadowed_env_keys(env) == ["A_KEY"]  # names only, never values
    assert shadowed_env_keys(tmp_path / "missing") == []


def test_load_env_file_tracks_what_it_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENV_FROM_FILE", set())
    monkeypatch.delenv("FILE_KEY", raising=False)
    monkeypatch.setenv("SHELL_KEY", "shell")
    env = tmp_path / ".env"
    env.write_text("FILE_KEY=1\nSHELL_KEY=2\n")
    load_env_file(env)
    assert config.ENV_FROM_FILE == {"FILE_KEY"}
    assert os.environ["SHELL_KEY"] == "shell"
    monkeypatch.delenv("FILE_KEY", raising=False)


def test_scoring_auth_error_includes_status_and_pointer() -> None:
    from job_hunter.models import Job
    from job_hunter.score import ClaudeScorer, FatalScoringError

    class Boom:
        messages = None

        def __init__(self) -> None:
            self.messages = self

        def create(self, **kw: Any) -> None:
            raise status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")

    job = Job("a" * 64, "u", "t", "c", "l", False, None, None, None, None, "d", "s")
    with pytest.raises(FatalScoringError) as ei:
        ClaudeScorer(Boom(), "m", "S").score(job)
    text = str(ei.value)
    assert "401" in text and "invalid x-api-key" in text and "check-anthropic" in text


def test_success_prints_the_models_reply_and_usage() -> None:
    code, text = run({"ANTHROPIC_API_KEY": GOOD}, from_file=set(), shadowed=[])
    assert code == 0
    assert "response: 'pong'" in text
    assert "usage: 20 input + 2 output tokens" in text
    assert "scoring request: OK (sample job scored 82/100, strong; output=json_schema" in text
    assert "explanation: 'Remote DevOps role" in text


def test_sends_a_real_prompt_not_a_one_token_ping() -> None:
    clients: list[Client] = []

    def factory(k: str) -> Client:
        clients.append(Client(None))
        return clients[0]

    check_anthropic(
        "claude-haiku-4-5",
        environ={"ANTHROPIC_API_KEY": GOOD},
        client_factory=factory,
        out=lambda s: None,
        from_file=set(),
        shadowed=[],
    )
    sent = clients[0].last
    assert sent["max_tokens"] > 1 and "pong" in sent["messages"][0]["content"]


def test_empty_reply_counts_as_failure() -> None:
    class Empty(Client):
        def create(self, **kw: Any) -> object:
            return reply(None)

    lines: list[str] = []
    code = check_anthropic(
        "m",
        environ={"ANTHROPIC_API_KEY": GOOD},
        client_factory=lambda k: Empty(None),
        out=lines.append,
        from_file=set(),
        shadowed=[],
    )
    assert code == 1 and any("EMPTY" in line for line in lines)


def test_cli_flag_needs_no_subcommand_and_uses_config_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from job_hunter import __main__ as cli

    seen: dict[str, Any] = {}

    def fake(model: str, **kw: Any) -> int:
        seen["model"] = model
        return 0

    monkeypatch.setattr(cli, "check_anthropic", fake)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", GOOD)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        'home_location: "Austin, TX"\nsearches: [{term: x}]\nscoring: {model: my-model}\n'
    )

    assert cli.main(["--test-anthropic", "--config", str(cfg)]) == 0
    assert seen["model"] == "my-model"
    assert cli.main(["--test-anthropic", "--model", "other"]) == 0 and seen["model"] == "other"
    assert cli.main(["--config", str(tmp_path / "missing.yaml"), "--test-anthropic"]) == 0
    assert seen["model"] == "claude-haiku-4-5"  # config unreadable -> default
    assert cli.main(["check-anthropic"]) == 0  # the subcommand is an alias

    with pytest.raises(SystemExit):  # no command and no flag is still an error
        cli.main([])
    assert "required" in capsys.readouterr().err


def test_scoring_request_failure_fails_the_check_even_if_ping_works() -> None:
    """The failure mode that bit Sonnet 5.5: a plain prompt works, the scoring request does not."""

    class PingOnly(Client):
        def create(self, **kw: Any) -> object:
            if "system" in kw:
                raise status_error(anthropic.BadRequestError, 400, "messages.0: not supported")
            return reply("pong")

    lines: list[str] = []
    code = check_anthropic(
        "some-model",
        environ={"ANTHROPIC_API_KEY": GOOD},
        client_factory=lambda k: PingOnly(None),
        out=lines.append,
        from_file=set(),
        shadowed=[],
    )
    text = "\n".join(lines)
    assert code == 1 and "request: OK" in text
    assert "scoring request: FAILED" in text and "messages.0: not supported" in text


def test_check_adapts_to_a_model_without_structured_outputs_or_forced_tools() -> None:
    class OldModel(Client):
        def create(self, **kw: Any) -> object:
            self.calls = getattr(self, "calls", []) + [kw]
            if "system" not in kw:
                return reply("pong")
            if "format" in kw.get("output_config", {}):
                raise status_error(
                    anthropic.BadRequestError, 400, "output_config.format: not supported"
                )
            if kw["tool_choice"]["type"] != "auto":
                raise status_error(
                    anthropic.BadRequestError,
                    400,
                    'tool_choice: type "tool" and "any" are not supported for this model.',
                )
            return scoring_reply()

    lines: list[str] = []
    code = check_anthropic(
        "claude-sonnet-5-5",
        environ={"ANTHROPIC_API_KEY": GOOD},
        client_factory=lambda k: OldModel(None),
        out=lines.append,
        from_file=set(),
        shadowed=[],
    )
    assert code == 0
    assert any("output=tool (auto)" in line and "effort=low" in line for line in lines)
