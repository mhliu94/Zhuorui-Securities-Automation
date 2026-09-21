"""Public, credential-free view of the Windows watchdog's saved state."""
import json
from datetime import datetime, timezone
from pathlib import Path


def recovery_status(root, *, now=None):
    directory = Path(root) / "runtime" / "recovery"
    result = {"enabled": False, "state": "disabled", "message": "Automatic recovery is not configured.", "components": {}}
    try:
        settings = json.loads((directory / "settings.json").read_text(encoding="utf-8-sig"))
        if settings.get("version") != 1 or type(settings.get("enabled")) is not bool:
            raise ValueError()
        if not settings["enabled"]:
            return result
        result["enabled"] = True
        result["boot_enabled"] = settings.get("logon_type") == "Password"
        saved = json.loads((directory / "status.json").read_text(encoding="utf-8-sig"))
        checked = datetime.fromisoformat(saved["checked_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - checked).total_seconds()
        result.update(checked_at=checked.isoformat(), state="active" if -60 <= age < 180 else "stale")
        result["message"] = ("Recovery checks are active; startup before sign-in is enabled." if result["boot_enabled"]
                             else "Recovery checks are active while the Windows user is signed in.")
        if result["state"] == "stale":
            result["message"] = "Recovery checks are overdue; inspect the Windows watchdog task."
        for component in ("api", "monitor"):
            state = saved.get("components", {}).get(component, {})
            # Never expose saved launch parameters, paths, or arbitrary fields.
            public = {key: state.get(key) for key in ("state", "message", "restart_count", "next_attempt_at")}
            intent = json.loads((directory / f"{component}.intent.json").read_text(encoding="utf-8-sig"))
            if type(intent.get("desired_running")) is not bool:
                raise ValueError()
            public["desired_running"] = intent["desired_running"]
            if not public["desired_running"]:
                public.update(state="paused", message="Intentionally stopped; stays stopped across reboots.")
            result["components"][component] = public
        return result
    except FileNotFoundError:
        if not result["enabled"]:
            return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    result.update(state="attention", message="Recovery status is unavailable; check the Windows watchdog setup.")
    return result
