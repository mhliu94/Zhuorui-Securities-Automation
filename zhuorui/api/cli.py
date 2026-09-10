"""API account queries, offline order plans and the Kafka trading listener."""
import argparse
import json
import sys

from zhuorui.common.config import default_config_path, ZhuoruiAutomationError
from .config import load_settings
from .errors import ApiError
from .signing import canonical


def parser():
    main = argparse.ArgumentParser(description=__doc__)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", default=default_config_path(), help="Shared zhuorui_config.json path")
    commands = main.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "status": "Check local setup without network access",
        "import-session": "Import a recently captured existing app session; no login",
        "inspect-emulator": "Read emulator identity and saved-login availability; no login",
        "import-emulator-session": "Read the existing login directly from a supported root-readable emulator",
        "capture-settings": "Show resolved non-secret capture settings; no emulator or network calls",
        "listen": "Receive Kafka commands and publish account snapshots",
        "check-session": "Query account and trading-authorization state directly",
        "holdings": "Read holdings directly from the broker",
        "cash": "Read raw broker cash fields",
        "orders": "Read today's broker order records",
    }.items():
        commands.add_parser(name, parents=[shared], help=help_text)
    order = commands.add_parser("plan-order", parents=[shared], help="Create an unsigned order plan; sends nothing")
    order.add_argument("symbol")
    order.add_argument("side", choices=["buy", "sell"])
    order.add_argument("quantity", type=int)
    order.add_argument("--type", choices=["market", "limit", "timed-cancel"], default="market")
    order.add_argument("--price")
    order.add_argument("--allow-pre-post", action="store_true")
    cancel = commands.add_parser("plan-cancel", parents=[shared], help="Create an unsigned cancellation plan; sends nothing")
    cancel.add_argument("order_reference")
    return main


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config, settings = load_settings(args.config)
        if args.command == "listen":
            from .listener import run_listener
            return run_listener(args.config)
        elif args.command == "status":
            result = {"mode": "api_trading_and_account_queries", "config_loaded": True,
                      "session_file_present": settings.session_file.is_file(),
                      "capture_file_present": settings.capture_file.is_file(),
                      "apk_file_present": settings.apk_file.is_file(),
                      "live_api_orders_available": True, "kafka_api_listener_available": True,
                      "live_orders_enabled_in_config": config.get("api", {}).get("live_orders_enabled", True),
                      "network_requests_sent": 0}
        elif args.command == "import-session":
            from .session import import_session
            result = import_session(config, settings)
        elif args.command == "capture-settings":
            from zhuorui.capture.config import load_capture_settings
            result = load_capture_settings(args.config)
        elif args.command == "inspect-emulator":
            from .emulator import inspect_emulator
            result = inspect_emulator(args.config)
        elif args.command == "import-emulator-session":
            from .emulator import import_emulator_session
            result = import_emulator_session(args.config, config, settings)
        elif args.command in {"plan-order", "plan-cancel"}:
            from .orders import plan_cancel, plan_order
            result = plan_cancel(args.order_reference) if args.command == "plan-cancel" else plan_order(
                args.symbol, args.side, args.quantity, args.type, price=args.price,
                allow_pre_post=args.allow_pre_post, cancel_after=settings.cancel_after_seconds)
        else:
            from .session import load_session
            from .client import ApiClient
            client = ApiClient(load_session(settings.session_file, config), settings)
            if args.command == "check-session":
                client.query("account")
                auth = client.query("trade-auth")
                result = {"account_session_valid": True, "trading_auth_populated": isinstance(auth.get("data"), dict) and bool(auth["data"]),
                          "network_requests_sent": 2}
            else:
                result = client.query(args.command)
        print(canonical(result).decode("utf-8"))
        return 0
    except (ApiError, ZhuoruiAutomationError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    except ImportError:
        print(json.dumps({"error": "Missing dependency. Install requirements-api.txt; import-session additionally needs api_research/requirements.txt."}), file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError):
        # Captures, broker bodies, tokens and local credentials must not leak
        # through exception messages from third-party libraries.
        print(json.dumps({"error": "API command failed. Check local setup and use a fresh account capture."}), file=sys.stderr)
        return 2
