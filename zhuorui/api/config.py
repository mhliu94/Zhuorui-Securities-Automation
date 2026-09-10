"""API settings extend the existing configuration file."""
from dataclasses import dataclass
import math
from pathlib import Path

from zhuorui.common.config import load_config, nested_config, ZhuoruiAutomationError
from zhuorui.paths import PROJECT_ROOT


@dataclass(frozen=True)
class ApiSettings:
    session_file: Path
    capture_file: Path
    apk_file: Path
    request_timeout_seconds: float = 10.0
    session_max_capture_age_seconds: float = 600.0
    cancel_after_seconds: float = 1.0
    quote_max_age_seconds: float = 30.0


def load_settings(config_path):
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise ZhuoruiAutomationError("Configuration file missing; copy zhuorui_config.example.json and fill in your settings.")
    config = load_config(config_path)
    api = nested_config(config, "api")
    expected = api.get("expected_user_id")
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        raise ZhuoruiAutomationError("api.expected_user_id must be a nonempty broker user ID string or null.")
    capture = nested_config(config, "capture")
    private_value = capture.get("private_dir")
    if private_value:
        if not isinstance(private_value, str):
            raise ZhuoruiAutomationError("capture.private_dir must be a path.")
        private = Path(private_value).expanduser()
        private = private.resolve() if private.is_absolute() else (config_path.parent / private).resolve()
    else:
        private = PROJECT_ROOT / "api_research/private"

    def path(key, default):
        value = api.get(key, str(default))
        if not isinstance(value, str) or not value.strip():
            raise ZhuoruiAutomationError(f"api.{key} must be a file path")
        result = Path(value).expanduser()
        return result.resolve() if result.is_absolute() else (config_path.parent / result).resolve()

    def number(key, default):
        value = api.get(key, default)
        if isinstance(value, bool):
            raise ZhuoruiAutomationError(f"api.{key} must be a positive number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ZhuoruiAutomationError(f"api.{key} must be a positive number") from None
        if not math.isfinite(value) or value <= 0:
            raise ZhuoruiAutomationError(f"api.{key} must be a positive number")
        return value

    settings = ApiSettings(
        session_file=path("session_file", PROJECT_ROOT / "runtime/api/session.dpapi"),
        capture_file=path("capture_file", private / "zhuorui-flows.mitm"),
        apk_file=path("apk_file", private / "zhuorui.apk"),
        request_timeout_seconds=number("request_timeout_seconds", 10),
        session_max_capture_age_seconds=number("session_max_capture_age_seconds", 600),
        cancel_after_seconds=number("cancel_after_seconds", 1),
        quote_max_age_seconds=number("quote_max_age_seconds", 30),
    )
    return config, settings
