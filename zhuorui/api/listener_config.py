"""Kafka listener configuration, extending the shared UI configuration."""
from dataclasses import dataclass
import math
from pathlib import Path

from zhuorui.common.config import config_bool, config_string, configured_account_id, configured_account_num_id, nested_config
from zhuorui.paths import PROJECT_ROOT
from .errors import ApiError


@dataclass(frozen=True)
class ListenerSettings:
    bootstrap_servers: str
    command_topic: str
    holdings_topic: str
    order_status_topic: str
    group_id: str
    client_id: str
    server_id: str
    holdings_interval_seconds: float
    poll_seconds: float
    command_max_age_seconds: float
    live_orders_enabled: bool
    journal_file: Path
    state_file: Path
    stop_file: Path
    auto_import_session: bool
    auto_login_enabled: bool = True
    login_retry_seconds: float = 300


def load_listener_settings(config_path, config):
    kafka, api = nested_config(config, "kafka"), nested_config(config, "api")
    from .snapshot import snapshot_identity
    account, account_number, enabled = snapshot_identity(config)
    def text(value, default=None):
        value = value if value is not None else default
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ApiError("Kafka connection/topic/server settings must be nonempty text.")
        return value.strip()
    def number(section, key, default):
        value = section.get(key, default)
        if isinstance(value, bool):
            raise ApiError(f"{key} must be a positive finite number.")
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ApiError(f"{key} must be a positive finite number.") from None
        if not math.isfinite(value) or value <= 0:
            raise ApiError(f"{key} must be a positive finite number.")
        return value
    def path(key, default):
        value = api.get(key, str(PROJECT_ROOT / default))
        if not isinstance(value, str) or not value.strip():
            raise ApiError(f"api.{key} must be a file path.")
        value = Path(value).expanduser()
        return value.resolve() if value.is_absolute() else (Path(config_path).resolve().parent / value).resolve()
    server = text(kafka.get("server_id") or config.get("server_id"))
    # Each account must see every command. A shared consumer group would assign
    # another account's command to one consumer, which then filters it away.
    group = text(api.get("kafka_group_id"), text(kafka.get("group_id"), "zhuorui-trading") + f".api.account-{account_number}")
    return ListenerSettings(
        bootstrap_servers=text(kafka.get("bootstrap_servers") or kafka.get("server") or config.get("kafka_bootstrap_servers")),
        command_topic=text(kafka.get("command_topic"), "trading-commands"),
        holdings_topic=text(kafka.get("holdings_topic"), "account-details"),
        order_status_topic=text(kafka.get("order_status_topic"), "order-status"),
        group_id=group, client_id=text(kafka.get("client_id"), server) + "-api", server_id=server,
        holdings_interval_seconds=number(kafka, "holdings_interval_seconds", 30),
        poll_seconds=min(number(kafka, "poll_seconds", 1), 1),
        command_max_age_seconds=number(api, "command_max_age_seconds", 120),
        live_orders_enabled=config_bool(api, "live_orders_enabled", True) and enabled,
        journal_file=path("journal_file", "runtime/api/commands.sqlite3"),
        state_file=path("state_file", "runtime/api/listener-state.json"),
        stop_file=path("stop_file", "runtime/api/listener.stop"),
        auto_import_session=config_bool(api, "auto_import_session", False),
        auto_login_enabled=config_bool(api, "auto_login_enabled", True),
        login_retry_seconds=number(api, "login_retry_seconds", 300))
