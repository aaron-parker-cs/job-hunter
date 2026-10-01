from pathlib import Path

import pytest

from job_hunter.config import ConfigError, load_config
from job_hunter.secrets import EnvProvider, SecretsError, load_secrets

VALID = """
home_location: "Austin, TX"
searches:
  - term: "DevOps Engineer"
resume_path: {resume}
profile_path: {profile}
"""


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    resume = tmp_path / "resume.md"
    profile = tmp_path / "profile.md"
    resume.write_text("resume")
    profile.write_text("profile")
    return resume, profile


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_valid_config_with_defaults(tmp_path: Path, files: tuple[Path, Path]) -> None:
    cfg = load_config(_write(tmp_path, VALID.format(resume=files[0], profile=files[1])))
    assert cfg.radius_miles == 50
    assert cfg.scoring.min_score_to_notify == 70
    assert cfg.notifier == "telegram"


def test_example_config_rejected_until_edited() -> None:
    example = Path(__file__).parent.parent / "config.example.yaml"
    with pytest.raises(ConfigError, match="home_location"):
        load_config(example, check_files=False)


@pytest.mark.parametrize(
    "extra, needle",
    [
        ("radius_miles: -1", "radius_miles"),
        ("notifier: sms", "notifier"),
        ("timezone: Mars/Base", "timezone"),
        ("schedule_cron: '* *'", "schedule_cron"),
        ("sites: [monster]", "sites"),
        ("bogus_key: 1", "bogus_key"),
        ("scoring: {min_score_to_notify: 101}", "min_score_to_notify"),
    ],
)
def test_invalid_config_fails_fast(
    tmp_path: Path, files: tuple[Path, Path], extra: str, needle: str
) -> None:
    body = VALID.format(resume=files[0], profile=files[1]) + extra + "\n"
    with pytest.raises(ConfigError, match=needle):
        load_config(_write(tmp_path, body))


def test_missing_resume_file(tmp_path: Path, files: tuple[Path, Path]) -> None:
    body = VALID.format(resume=tmp_path / "nope.md", profile=files[1])
    with pytest.raises(ConfigError, match="resume_path not found"):
        load_config(_write(tmp_path, body))


def test_resume_extension_checked(tmp_path: Path, files: tuple[Path, Path]) -> None:
    body = VALID.format(resume=tmp_path / "resume.docx", profile=files[1])
    with pytest.raises(ConfigError, match="resume_path"):
        load_config(_write(tmp_path, body), check_files=False)


def test_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(_write(tmp_path, "a: [unclosed"))


def test_secrets_telegram_ok_and_hidden_in_repr() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-abcdef123456",
        "TELEGRAM_BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123",
        "TELEGRAM_CHAT_ID": "42",
    }
    s = load_secrets(EnvProvider(env), "telegram")
    assert s.telegram_chat_id == 42
    assert "sk-ant" not in repr(s)
    assert "ABCDEFGH" not in str(s)
    assert "sk-ant-abcdef123456" in s.secret_values()


def test_secrets_missing_named_without_values() -> None:
    with pytest.raises(SecretsError) as ei:
        load_secrets(EnvProvider({"ANTHROPIC_API_KEY": "k" * 10}), "both")
    msg = str(ei.value)
    assert "TELEGRAM_BOT_TOKEN" in msg and "DISCORD" in msg


def test_discord_webhook_only_is_enough() -> None:
    env = {
        "ANTHROPIC_API_KEY": "k" * 10,
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc",
    }
    assert load_secrets(EnvProvider(env), "discord").discord_webhook_url is not None


def test_non_integer_chat_id() -> None:
    env = {"ANTHROPIC_API_KEY": "k" * 10, "TELEGRAM_BOT_TOKEN": "t" * 10, "TELEGRAM_CHAT_ID": "x"}
    with pytest.raises(SecretsError, match="TELEGRAM_CHAT_ID must be an integer"):
        load_secrets(EnvProvider(env), "telegram")


def test_aws_ssm_provider_reads_prefix_with_decryption(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import SimpleNamespace
    from typing import Any

    from job_hunter.secrets import AwsSsmProvider, build_provider

    calls: dict[str, Any] = {}

    class Paginator:
        def paginate(self, **kw: Any) -> list[dict[str, Any]]:
            calls["paginate"] = kw
            return [
                {
                    "Parameters": [
                        {"Name": "/job-hunter/ANTHROPIC_API_KEY", "Value": "sk-ant-x" * 3}
                    ]
                },
                {"Parameters": [{"Name": "/job-hunter/TELEGRAM_CHAT_ID", "Value": "42"}]},
            ]

    class Client:
        def get_paginator(self, name: str) -> Paginator:
            calls["op"] = name
            return Paginator()

    fake = SimpleNamespace(
        client=lambda svc, region_name=None: calls.update(region=region_name) or Client()
    )
    monkeypatch.setitem(sys.modules, "boto3", fake)

    provider = build_provider(
        {"SECRETS_BACKEND": "aws_ssm", "AWS_REGION": "us-east-2", "SSM_PREFIX": "/job-hunter"}
    )
    assert isinstance(provider, AwsSsmProvider)
    assert provider.get("TELEGRAM_CHAT_ID") == "42"
    assert provider.get("DISCORD_BOT_TOKEN") is None
    assert calls["paginate"] == {"Path": "/job-hunter/", "WithDecryption": True}
    assert calls["op"] == "get_parameters_by_path" and calls["region"] == "us-east-2"
    # an SSM-backed provider plugs into load_secrets like the env one
    with pytest.raises(SecretsError, match="TELEGRAM_BOT_TOKEN"):
        load_secrets(provider, "telegram")


def test_aws_backend_without_boto3_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from job_hunter.secrets import build_provider

    monkeypatch.setitem(sys.modules, "boto3", None)  # makes `import boto3` raise ImportError
    with pytest.raises(SecretsError, match="uv sync --extra aws"):
        build_provider({"SECRETS_BACKEND": "aws_ssm"})


def test_unknown_secrets_backend() -> None:
    from job_hunter.secrets import build_provider

    with pytest.raises(SecretsError, match="SECRETS_BACKEND"):
        build_provider({"SECRETS_BACKEND": "vault"})
