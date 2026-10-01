"""Secret loading. Secrets live only in SecretStr; never in config.yaml."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, SecretStr

SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_BOT_TOKEN",
    "DISCORD_CHANNEL_ID",
    "DISCORD_ALLOWED_USER_ID",
    "DISCORD_WEBHOOK_URL",
)


class SecretsError(RuntimeError):
    """Raised when secrets are missing or invalid. Never includes secret values."""


class SecretsProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvProvider:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> str | None:
        value = self._environ.get(name, "").strip()
        return value or None


class AwsSsmProvider:
    """Reads SecureString parameters under SSM_PREFIX (needs `uv sync --extra aws`)."""

    def __init__(self, region: str | None, prefix: str) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise SecretsError(
                "SECRETS_BACKEND=aws_ssm requires boto3: run `uv sync --extra aws`"
            ) from exc
        self._client = boto3.client("ssm", region_name=region)
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            values: dict[str, str] = {}
            paginator = self._client.get_paginator("get_parameters_by_path")
            for page in paginator.paginate(Path=self._prefix, WithDecryption=True):
                for param in page["Parameters"]:
                    values[param["Name"][len(self._prefix) :]] = param["Value"]
            self._cache = values
        return self._cache

    def get(self, name: str) -> str | None:
        return self._load().get(name) or None


def build_provider(environ: Mapping[str, str] | None = None) -> SecretsProvider:
    env = os.environ if environ is None else environ
    backend = env.get("SECRETS_BACKEND", "env").strip().lower() or "env"
    if backend == "env":
        return EnvProvider(env)
    if backend == "aws_ssm":
        return AwsSsmProvider(
            region=env.get("AWS_REGION") or None,
            prefix=env.get("SSM_PREFIX", "/job-hunter/"),
        )
    raise SecretsError(f"SECRETS_BACKEND must be 'env' or 'aws_ssm', got {backend!r}")


class Secrets(BaseModel):
    anthropic_api_key: SecretStr
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    discord_bot_token: SecretStr | None = None
    discord_channel_id: int | None = None
    discord_allowed_user_id: int | None = None
    discord_webhook_url: SecretStr | None = None

    def secret_values(self) -> list[str]:
        """Raw values, for registering with the log redactor."""
        out = [
            self.anthropic_api_key,
            self.telegram_bot_token,
            self.discord_bot_token,
            self.discord_webhook_url,
        ]
        return [s.get_secret_value() for s in out if s is not None]


def load_secrets(
    provider: SecretsProvider, notifier: Literal["telegram", "discord", "both"]
) -> Secrets:
    raw = {name: provider.get(name) for name in SECRET_NAMES}
    use_telegram = notifier in ("telegram", "both")
    use_discord = notifier in ("discord", "both")

    missing: list[str] = []
    if not raw["ANTHROPIC_API_KEY"]:
        missing.append("ANTHROPIC_API_KEY")
    if use_telegram:
        missing += [n for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not raw[n]]
    if use_discord:
        has_webhook = bool(raw["DISCORD_WEBHOOK_URL"])
        has_bot = all(
            raw[n] for n in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_ALLOWED_USER_ID")
        )
        if not (has_bot or has_webhook):
            missing.append(
                "DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID + DISCORD_ALLOWED_USER_ID "
                "(or DISCORD_WEBHOOK_URL)"
            )
    if missing:
        raise SecretsError("Missing required secrets: " + ", ".join(missing))

    return Secrets(
        anthropic_api_key=SecretStr(raw["ANTHROPIC_API_KEY"] or ""),
        telegram_bot_token=_secret(raw["TELEGRAM_BOT_TOKEN"]),
        telegram_chat_id=_int(raw["TELEGRAM_CHAT_ID"], "TELEGRAM_CHAT_ID"),
        discord_bot_token=_secret(raw["DISCORD_BOT_TOKEN"]),
        discord_channel_id=_int(raw["DISCORD_CHANNEL_ID"], "DISCORD_CHANNEL_ID"),
        discord_allowed_user_id=_int(raw["DISCORD_ALLOWED_USER_ID"], "DISCORD_ALLOWED_USER_ID"),
        discord_webhook_url=_secret(raw["DISCORD_WEBHOOK_URL"]),
    )


def _secret(value: str | None) -> SecretStr | None:
    return SecretStr(value) if value else None


def _int(value: str | None, name: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise SecretsError(f"{name} must be an integer") from None
