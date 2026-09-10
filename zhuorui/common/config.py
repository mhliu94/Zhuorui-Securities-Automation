"""Configuration shared by the UI and API entry points."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Optional
from zhuorui.paths import PROJECT_ROOT

SCRIPT_DIR = PROJECT_ROOT
DEFAULT_CONFIG_NAMES = ("zhuorui_config.json", "config.json")

class ZhuoruiAutomationError(RuntimeError):
    pass


def default_config_path() -> Path:
    for name in DEFAULT_CONFIG_NAMES:
        path = SCRIPT_DIR / name
        if path.exists():
            return path
    return SCRIPT_DIR / DEFAULT_CONFIG_NAMES[0]


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ZhuoruiAutomationError(f"Could not parse config file {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ZhuoruiAutomationError(f"Config file {path} must contain a JSON object.")
    return config


def config_string(config: dict, *keys: str) -> Optional[str]:
    value: object = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ZhuoruiAutomationError(f"Config value {'.'.join(keys)} must be a string.")
    stripped = value.strip()
    return stripped or None


def config_float(config: dict, key: str, default: float) -> float:
    value = config.get(key)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ZhuoruiAutomationError(f"Config value {key} must be a number.") from exc


def config_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ZhuoruiAutomationError(f"Config value {key} must be a boolean.")


def config_screen_size(config: dict) -> Optional[tuple[int, int]]:
    value = config.get("screen_size") or config.get("resolution")
    if value in (None, ""):
        return None

    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*[xX,*]\s*(\d+)\s*", value)
        if not match:
            raise ZhuoruiAutomationError("Config value screen_size must look like WIDTHxHEIGHT.")
        return int(match.group(1)), int(match.group(2))

    if isinstance(value, dict):
        width = value.get("width")
        height = value.get("height")
    elif isinstance(value, list) and len(value) == 2:
        width, height = value
    else:
        raise ZhuoruiAutomationError("Config value screen_size must be a string, object, or two-item array.")

    try:
        parsed = int(width), int(height)
    except (TypeError, ValueError) as exc:
        raise ZhuoruiAutomationError("Config value screen_size must contain integer width and height.") from exc
    if parsed[0] <= 0 or parsed[1] <= 0:
        raise ZhuoruiAutomationError("Config value screen_size must contain positive width and height.")
    return parsed


def config_path(config: dict, key: str) -> Optional[Path]:
    value = config_string(config, key)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path


def config_trade_password(config: dict) -> Optional[str]:
    return (
        config_string(config, "trade_password")
        or config_string(config, "trade", "password")
        or config_string(config, "password")
    )


def config_login_phone(config: dict) -> Optional[str]:
    return (
        config_string(config, "login", "phone")
        or config_string(config, "login", "phone_number")
        or config_string(config, "login_phone")
        or os.environ.get("ZHUORUI_LOGIN_PHONE")
    )


def config_login_password(config: dict) -> Optional[str]:
    return (
        config_string(config, "login", "password")
        or config_string(config, "login_password")
        or os.environ.get("ZHUORUI_LOGIN_PASSWORD")
    )


def nested_config(config: dict, key: str) -> dict:
    value = config.get(key)
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ZhuoruiAutomationError(f"Config value {key} must be an object.")
    return value


def config_number(config: dict, key: str, default: float) -> float:
    value = config.get(key)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ZhuoruiAutomationError(f"Config value {key} must be a number.") from exc


def optional_config_int(config: dict, *keys: str) -> Optional[int]:
    value: object = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ZhuoruiAutomationError(f"Config value {'.'.join(keys)} must be an integer.") from exc


def read_config_string(config: dict, default: Optional[str], *keys: str) -> Optional[str]:
    value = config_string(config, *keys)
    return value if value is not None else default


def configured_account_id(config: dict) -> Optional[str]:
    return (
        config_string(config, "account_id")
        or config_string(config, "account", "id")
        or config_string(config, "account", "account_id")
    )


def configured_account_num_id(config: dict) -> Optional[int]:
    return (
        optional_config_int(config, "account_num_id")
        or optional_config_int(config, "account", "num_id")
        or optional_config_int(config, "account", "numeric_id")
        or optional_config_int(config, "account", "account_num_id")
    )
