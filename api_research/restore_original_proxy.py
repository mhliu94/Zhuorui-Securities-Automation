"""Restore the original capture device's saved proxy fields only.

Refuses unexpected device identity or a proxy that does not belong to this
capture. Does not operate the app, clear its data, or perform network queries.
"""
import json
import subprocess
import time
from pathlib import Path
from capture_settings import arguments

ROOT = Path(__file__).resolve().parent


def main():
    _, settings = arguments()
    session_path = Path(settings["private_dir"]) / "original-capture-session.json"
    if not session_path.exists():
        return
    session = json.loads(session_path.read_text(encoding="utf-8-sig"))
    if session.get("phase") in ("restored", "backed_up", "backing_up"):
        return
    if session["device"] != settings["device"] or session["avd"] != settings["avd"]:
        raise RuntimeError("Saved capture identity differs from the configured emulator")
    adb = [settings["adb"], "-s", settings["device"]]

    def android(*args):
        return subprocess.check_output(adb + list(args), text=True, timeout=20).strip()

    if android("emu", "avd", "name").splitlines()[0] != settings["avd"]:
        raise RuntimeError("Unexpected original device")
    saved = session["proxy_settings"]
    actual = {key: android("shell", "settings", "get", "global", key) for key in saved}
    if actual == saved:
        return
    capture_proxy = session.get("capture_proxy", settings["emulator_proxy"])
    capture_host = session.get("capture_proxy_host", settings["emulator_proxy_host"])
    if actual["http_proxy"] != capture_proxy or actual["global_http_proxy_host"] not in (capture_host, "", "null"):
        raise RuntimeError("Proxy no longer belongs to this capture; refusing to overwrite")
    android("shell", "settings", "put", "global", "http_proxy", ":0")
    for _ in range(30):
        host = android("shell", "settings", "get", "global", "global_http_proxy_host")
        port = android("shell", "settings", "get", "global", "global_http_proxy_port")
        if host in ("", "null") and port in ("0", "null"):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Original proxy is still active")
    for key in [key for key in saved if key != "http_proxy"] + ["http_proxy"]:
        value = saved[key]
        if value == "null":
            android("shell", "settings", "delete", "global", key)
        else:
            android("shell", "settings", "put", "global", key, value or "''")
    actual = {key: android("shell", "settings", "get", "global", key) for key in saved}
    if actual != saved:
        raise RuntimeError("Original proxy fields did not restore exactly")
    print("Original proxy settings restored and verified.")


if __name__ == "__main__":
    main()
