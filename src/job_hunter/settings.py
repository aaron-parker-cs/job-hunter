"""Chat-adjustable settings (/threshold, /radius, /location) layered over config.yaml.

config.yaml stays the source of the initial values. The settings table only holds what was
changed from chat, and those overrides win. Keeping them as overrides rather than copying
the whole config into the database means later edits to config.yaml still take effect for
anything not overridden, and `reset` returns a setting to its config.yaml value.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from job_hunter.config import Config
from job_hunter.store import Store

log = logging.getLogger(__name__)

RADIUS_MAX_MILES = 500


class SettingError(ValueError):
    """A value from a chat command was rejected; the message is shown to the user."""


@dataclass(frozen=True)
class Setting:
    key: str
    parse: Callable[[str], Any]  # raises SettingError
    read: Callable[[Config], Any]  # current value from a Config
    write: Callable[[dict[str, Any], Any], None]  # put a value into Config.model_dump()
    show: Callable[[Any], str]


def _parse_threshold(raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        raise SettingError("The threshold must be a whole number from 0 to 100.") from None
    if not 0 <= value <= 100:
        raise SettingError("The threshold must be from 0 to 100.")
    return value


def _parse_radius(raw: str) -> float:
    text = raw.strip().lower().removesuffix("miles").removesuffix("mi").strip()
    try:
        value = float(text)
    except ValueError:
        raise SettingError(
            f"The radius must be a number of miles, 1 to {RADIUS_MAX_MILES}."
        ) from None
    if not 1 <= value <= RADIUS_MAX_MILES:
        raise SettingError(f"The radius must be from 1 to {RADIUS_MAX_MILES} miles.")
    return value


def _parse_location(raw: str) -> str:
    value = " ".join(raw.split())
    if not value or len(value) > 100:
        raise SettingError("Give a location like 'Seattle, WA'.")
    if value == "Your City, ST":
        raise SettingError("That is the template placeholder; give a real city.")
    return value


def _set_threshold(data: dict[str, Any], value: Any) -> None:
    data["scoring"]["min_score_to_notify"] = value


SETTINGS: dict[str, Setting] = {
    "threshold": Setting(
        "threshold",
        _parse_threshold,
        lambda c: c.scoring.min_score_to_notify,
        _set_threshold,
        str,
    ),
    "radius": Setting(
        "radius",
        _parse_radius,
        lambda c: c.radius_miles,
        lambda d, v: d.__setitem__("radius_miles", v),
        lambda v: f"{v:g} mi",
    ),
    "location": Setting(
        "location",
        _parse_location,
        lambda c: c.home_location,
        lambda d, v: d.__setitem__("home_location", v),
        str,
    ),
}


def overrides(store: Store) -> dict[str, Any]:
    """Valid overrides from the database; anything unparseable is ignored with a warning."""
    values: dict[str, Any] = {}
    for key, raw in store.all_settings().items():
        setting = SETTINGS.get(key)
        if setting is None:
            continue
        try:
            values[key] = setting.parse(raw)
        except SettingError:
            log.warning("ignoring invalid stored setting %s", key)
    return values


def effective_config(cfg: Config, store: Store) -> Config:
    """config.yaml with the chat overrides applied (and re-validated)."""
    values = overrides(store)
    if not values:
        return cfg
    data = cfg.model_dump()
    for key, value in values.items():
        SETTINGS[key].write(data, value)
    try:
        return Config.model_validate(data)
    except ValidationError:
        log.warning("stored settings produce an invalid config; using config.yaml values")
        return cfg


def set_setting(store: Store, key: str, raw: str) -> Any:
    """Validate and save a value; returns the parsed value. Raises SettingError."""
    value = SETTINGS[key].parse(raw)
    store.set_setting(key, str(value))
    return value


def reset_setting(store: Store, key: str) -> None:
    store.delete_setting(key)


def describe(cfg: Config, store: Store, key: str) -> str:
    """'65 (set from chat; config.yaml: 70)' or '70 (config.yaml)'."""
    setting = SETTINGS[key]
    base = setting.show(setting.read(cfg))
    current = overrides(store).get(key)
    if current is None:
        return f"{base} (config.yaml)"
    return f"{setting.show(current)} (set from chat; config.yaml: {base})"
