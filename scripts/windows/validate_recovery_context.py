"""Read-only check run by Windows Task Scheduler under its actual logon context."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def validate():
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "ok": False}
    try:
        from zhuorui.api.config import load_settings
        from zhuorui.api.session import load_session
        config, settings = load_settings(ROOT / "zhuorui_config.json")
        load_session(settings.session_file, config)  # Decrypt and validate locally; no broker requests.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        size = wintypes.UINT(0)
        package_result = kernel.GetPackageFamilyName(kernel.GetCurrentProcess(), ctypes.byref(size), None)
        independent = Path(sys.base_prefix).resolve() == ROOT / "runtime" / "python-base"
        result.update(session_decryption_ok=True, independent_python=independent,
                      outside_app_package=package_result == 15700,
                      ok=independent and package_result == 15700)
    except Exception as exc:
        result["error_type"] = type(exc).__name__  # Never record session material or exception bodies.
    target = ROOT / "runtime" / "recovery" / "context-validation.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(target)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(validate())
