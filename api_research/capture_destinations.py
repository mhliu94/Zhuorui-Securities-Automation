"""Bounded TLS passthrough capture; restores the Android proxy on exit.

Run with this folder's isolated Python environment. This does not decrypt TLS,
restart the app, operate its UI, or issue trading/account requests.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from capture_settings import arguments

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=140)
    parser.add_argument("--port", type=int, help="Override capture.proxy_port")
    args, settings = arguments(parser)
    args.port = args.port or settings["proxy_port"]
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if not 1 <= args.seconds <= 600:
        parser.error("seconds must be between 1 and 600")
    adb = [settings["adb"], "-s", settings["device"]]

    def android(*command):
        return subprocess.check_output(adb + list(command), text=True, timeout=15).strip()

    if android("emu", "avd", "name").splitlines()[0] != settings["avd"]:
        raise RuntimeError("Configured emulator identity mismatch")

    proxy_keys = ["http_proxy", "global_http_proxy_host", "global_http_proxy_port",
                  "global_http_proxy_exclusion_list", "global_proxy_pac_url"]
    saved = {key: android("shell", "settings", "get", "global", key) for key in proxy_keys}
    original = saved["http_proxy"]
    if original not in {"null", "", ":0"} or saved["global_http_proxy_host"] not in {"null", ""} or saved["global_proxy_pac_url"] not in {"null", ""}:
        raise RuntimeError("An existing proxy requires manual capture setup; no settings changed")
    private = Path(settings["private_dir"])
    private.mkdir(parents=True, exist_ok=True)
    (private / "proxy-backup.json").write_text(json.dumps({"device": settings["device"], "settings": saved}), encoding="utf-8")
    with socket.socket() as check:
        check.bind((settings["proxy_listen_host"], args.port))
    command = [settings["mitmdump_executable"],
               "--listen-host", settings["proxy_listen_host"], "--listen-port", str(args.port),
               "--ignore-hosts", ".*", "--set", "connection_strategy=lazy",
               "--set", f"confdir={private / 'mitmproxy'}", "-q",
               "-s", str(ROOT / "capture_metadata.py")]
    proxy = None
    changed = False
    with (private / "proxy-process.log").open("w", encoding="utf-8") as log:
        try:
            proxy = subprocess.Popen(command, stdout=log, stderr=log,
                                     env={**os.environ, "ZHUORUI_CAPTURE_PRIVATE": str(private)},
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for _ in range(100):
                if proxy.poll() is not None:
                    raise RuntimeError("Proxy exited; inspect private/proxy-process.log")
                try:
                    with socket.create_connection((settings["proxy_listen_host"], args.port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Proxy did not start")
            changed = True
            android("shell", "settings", "put", "global", "http_proxy", f"{settings['emulator_proxy_host']}:{args.port}")
            print(f"TLS passthrough capture active for {args.seconds}s; original proxy={original}", flush=True)
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                if proxy.poll() is not None:
                    raise RuntimeError("Proxy exited during capture")
                time.sleep(0.5)
        finally:
            # Restore connectivity before stopping the listening proxy.
            try:
                if changed:
                    # Removing http_proxy alone leaves Android's derived proxy
                    # fields and active ProxyInfo populated. Clear active state
                    # first, verify it, then restore every persisted field.
                    android("shell", "settings", "put", "global", "http_proxy", ":0")
                    for _ in range(30):
                        host = android("shell", "settings", "get", "global", "global_http_proxy_host")
                        port = android("shell", "settings", "get", "global", "global_http_proxy_port")
                        if host in {"null", ""} and port in {"null", "0"}:
                            break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("Android still has an active proxy; inspect private/proxy-backup.json")
                    for key in proxy_keys[1:] + ["http_proxy"]:
                        value = saved[key]
                        if value == "null":
                            android("shell", "settings", "delete", "global", key)
                        else:
                            android("shell", "settings", "put", "global", key, value or "''")
                    actual = {key: android("shell", "settings", "get", "global", key) for key in proxy_keys}
                    if actual != saved:
                        raise RuntimeError("Proxy restoration mismatch; inspect private/proxy-backup.json")
                    print("Original Android proxy and all derived settings restored.", flush=True)
            finally:
                if proxy is not None:
                    # Windows console entry points can have child interpreters.
                    if sys.platform == "win32" and proxy.poll() is None:
                        subprocess.run(["taskkill.exe", "/PID", str(proxy.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
                    elif proxy.poll() is None:
                        proxy.terminate()
                    try:
                        proxy.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proxy.kill()
                        proxy.wait(timeout=5)


if __name__ == "__main__":
    main()
