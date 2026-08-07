"""Local Windows dashboard for the Zhuorui trading listener and emulator."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "monitor_web"
PID_PATTERN = re.compile(r"^[0-9]+$")
DEFAULT_INTERVAL_SECONDS = 60
MAX_REQUEST_BYTES = 4096
ADMIN_USERNAME = "admin"
PASSWORD_ITERATIONS = 600_000
PASSWORD_SALT = bytes.fromhex("3f890eaa7a2b610e79e4ec76623d6922e55a70e006b774eb88303207509b485c")
PASSWORD_HASH = bytes.fromhex("e18aa15b49e1a8390c956e1d1fb9a25c4f897cda908bdf09736dc816e6a1832e")
SESSION_COOKIE_NAME = "__Host-ZhuoruiSession"
SESSION_LIFETIME_SECONDS = 8 * 60 * 60
LOGIN_ATTEMPT_WINDOW_SECONDS = 5 * 60
LOGIN_LOCKOUT_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def process_probe(pid: int) -> dict[str, Any]:
    """Return liveness and creation time without adding a psutil dependency."""
    if pid <= 0:
        return {"running": False, "started_epoch": None}

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return {"running": False, "started_epoch": None}
        return {"running": True, "started_epoch": None}

    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return {"running": False, "started_epoch": None}

    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return {"running": False, "started_epoch": None}
        if exit_code.value != still_active:
            return {"running": False, "started_epoch": None}

        created = FileTime()
        exited = FileTime()
        kernel = FileTime()
        user = FileTime()
        started_epoch: float | None = None
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            windows_ticks = (created.high << 32) | created.low
            started_epoch = (windows_ticks - 116444736000000000) / 10_000_000
        return {"running": True, "started_epoch": started_epoch}
    finally:
        kernel32.CloseHandle(handle)


def run_hidden(
    arguments: Sequence[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        list(arguments),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **kwargs,
    )


def command_message(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    return output or ("Command completed." if result.returncode == 0 else "Command failed.")


def verify_admin_credentials(username: Any, password: Any) -> bool:
    """Verify the single configured account in constant-time where practical."""
    supplied_username = username if isinstance(username, str) else ""
    supplied_password = password if isinstance(password, str) else ""
    if len(supplied_username) > 128 or len(supplied_password) > 256:
        supplied_password = ""
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        supplied_password.encode("utf-8", errors="replace"),
        PASSWORD_SALT,
        PASSWORD_ITERATIONS,
    )
    username_matches = hmac.compare_digest(supplied_username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    password_matches = hmac.compare_digest(candidate, PASSWORD_HASH)
    return username_matches and password_matches


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


class ZhuoruiController:
    """Collect status and perform the four supported local control actions."""

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        *,
        probe: Callable[[int], dict[str, Any]] = process_probe,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.root = root.resolve()
        self.probe = probe
        self.runner = runner
        self.pid_path = self.root / "zhuorui_listener.pid"
        self.run_path = self.root / "zhuorui_listener.current.json"
        self.emulator_run_path = self.root / "zhuorui_emulator.current.json"
        self.config_path = self.root / "zhuorui_config.json"
        self._action_lock = threading.Lock()

    def public_config(self) -> dict[str, Any]:
        config = read_json(self.config_path)
        return {
            "server_id": str(config.get("server_id") or "Zhuorui server"),
            "account_id": str(config.get("account_id") or "Account not configured"),
            "device": str(config.get("device") or "emulator-5554"),
            "avd": str(config.get("avd") or ""),
        }

    def _config(self) -> dict[str, Any]:
        return read_json(self.config_path)

    def _read_pid(self, path: Path) -> tuple[int | None, str | None]:
        try:
            raw = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"Could not read PID file: {exc}"
        if not PID_PATTERN.fullmatch(raw):
            return None, "PID file is invalid."
        return int(raw), None

    @staticmethod
    def _tracked_process(
        pid: int,
        metadata: Mapping[str, Any],
        probe: Callable[[int], dict[str, Any]],
    ) -> tuple[bool, datetime | None]:
        details = probe(pid)
        if not details.get("running"):
            return False, None

        actual_epoch = details.get("started_epoch")
        actual_start = (
            datetime.fromtimestamp(float(actual_epoch), tz=timezone.utc)
            if isinstance(actual_epoch, (float, int))
            else None
        )
        recorded_start = parse_datetime(metadata.get("started_utc"))
        metadata_pid = metadata.get("pid")
        if metadata_pid not in (None, pid, str(pid)):
            return False, actual_start
        if actual_start and recorded_start and abs((actual_start - recorded_start).total_seconds()) > 30:
            return False, actual_start
        return True, recorded_start or actual_start

    def script_status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        pid, pid_error = self._read_pid(self.pid_path)
        metadata = read_json(self.run_path)
        if pid is None:
            return {
                "state": "stopped" if pid_error is None else "attention",
                "running": False,
                "pid": None,
                "started_at": None,
                "duration_seconds": None,
                "message": pid_error or "The trading listener is not running.",
            }

        running, started_at = self._tracked_process(pid, metadata, self.probe)
        if not running:
            return {
                "state": "stopped",
                "running": False,
                "pid": None,
                "started_at": None,
                "duration_seconds": None,
                "message": f"The listener is not running; PID {pid} is stale.",
            }

        duration = max(0, int((now - started_at).total_seconds())) if started_at else None
        return {
            "state": "running",
            "running": True,
            "pid": pid,
            "started_at": iso_utc(started_at) if started_at else None,
            "duration_seconds": duration,
            "message": "The trading listener is healthy and running.",
        }

    def _adb_path(self, config: Mapping[str, Any]) -> Path | None:
        configured = config.get("adb")
        candidates: list[Path] = []
        if isinstance(configured, str) and configured.strip():
            candidates.append(Path(os.path.expandvars(configured)))
        for environment_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            sdk = os.environ.get(environment_name)
            if sdk:
                candidates.append(Path(sdk) / "platform-tools" / "adb.exe")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
        return self._first_existing_file(candidates)

    @staticmethod
    def _first_existing_file(candidates: Sequence[Path]) -> Path | None:
        for path in candidates:
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def _emulator_path(self, config: Mapping[str, Any], adb_path: Path | None) -> Path | None:
        candidates: list[Path] = []
        configured = config.get("emulator")
        if isinstance(configured, str) and configured.strip():
            candidates.append(Path(os.path.expandvars(configured)))
        if adb_path:
            candidates.append(adb_path.parent.parent / "emulator" / "emulator.exe")
        for environment_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            sdk = os.environ.get(environment_name)
            if sdk:
                candidates.append(Path(sdk) / "emulator" / "emulator.exe")
        return self._first_existing_file(candidates)

    @staticmethod
    def _parse_adb_devices(output: str) -> dict[str, str]:
        devices: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("List of devices") or stripped.startswith("*"):
                continue
            fields = stripped.split()
            if len(fields) >= 2:
                devices[fields[0]] = fields[1]
        return devices

    def _emulator_tracking(self) -> tuple[bool, int | None, datetime | None]:
        metadata = read_json(self.emulator_run_path)
        pid_value = metadata.get("pid")
        try:
            pid = int(pid_value)
        except (TypeError, ValueError):
            return False, None, None
        running, started_at = self._tracked_process(pid, metadata, self.probe)
        return running, pid if running else None, started_at

    def emulator_status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        config = self._config()
        public = self.public_config()
        adb_path = self._adb_path(config)
        tracked, tracked_pid, tracked_start = self._emulator_tracking()
        base = {
            "device": public["device"],
            "avd": public["avd"],
            "pid": tracked_pid,
            "started_at": iso_utc(tracked_start) if tracked_start else None,
            "duration_seconds": max(0, int((now - tracked_start).total_seconds())) if tracked_start else None,
        }

        if not adb_path:
            return {
                **base,
                "state": "unavailable",
                "running": False,
                "message": "ADB was not found. Check the adb path in zhuorui_config.json.",
            }

        try:
            result = self.runner([str(adb_path), "devices"], cwd=self.root, timeout=8)
        except (OSError, subprocess.SubprocessError) as exc:
            if tracked:
                return {
                    **base,
                    "state": "starting",
                    "running": True,
                    "message": "The emulator process is running; ADB is not ready yet.",
                }
            return {
                **base,
                "state": "unavailable",
                "running": False,
                "message": f"ADB status check failed: {exc}",
            }

        if result.returncode != 0:
            return {
                **base,
                "state": "starting" if tracked else "unavailable",
                "running": tracked,
                "message": "The emulator is starting." if tracked else "ADB could not check the emulator.",
            }

        device_state = self._parse_adb_devices(result.stdout).get(public["device"])
        if device_state == "device":
            boot_complete = False
            try:
                boot_result = self.runner(
                    [str(adb_path), "-s", public["device"], "shell", "getprop", "sys.boot_completed"],
                    cwd=self.root,
                    timeout=5,
                )
                boot_complete = boot_result.returncode == 0 and boot_result.stdout.strip() == "1"
            except (OSError, subprocess.SubprocessError):
                boot_complete = False
            return {
                **base,
                "state": "running" if boot_complete else "booting",
                "running": True,
                "message": "The Android emulator is ready." if boot_complete else "Android is finishing its boot sequence.",
            }
        if device_state in {"offline", "unauthorized"}:
            return {
                **base,
                "state": "booting" if device_state == "offline" else "attention",
                "running": True,
                "message": f"ADB reports the emulator as {device_state}.",
            }
        if tracked:
            return {
                **base,
                "state": "starting",
                "running": True,
                "message": "The emulator process is starting; waiting for ADB.",
            }
        return {
            **base,
            "state": "stopped",
            "running": False,
            "message": "The Android emulator is not running.",
        }

    def collect_status(self, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> dict[str, Any]:
        checked_at = utc_now()
        return {
            "account": self.public_config(),
            "script": self.script_status(checked_at),
            "emulator": self.emulator_status(checked_at),
            "checked_at": iso_utc(checked_at),
            "next_check_at": iso_utc(checked_at + timedelta(seconds=interval_seconds)),
            "interval_seconds": interval_seconds,
        }

    def _powershell_script(self, name: str, timeout: float = 30) -> ActionResult:
        script_path = self.root / name
        if not script_path.is_file():
            return ActionResult(False, f"Required control script is missing: {name}")
        arguments = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
        try:
            result = self.runner(arguments, cwd=self.root, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return ActionResult(False, f"Control action failed: {exc}")
        return ActionResult(result.returncode == 0, command_message(result))

    def start_script(self) -> ActionResult:
        status = self.script_status()
        if status["running"]:
            return ActionResult(True, f"The Zhuorui listener is already running with PID {status['pid']}.")
        return self._powershell_script("start_zhuorui_listener.ps1", timeout=30)

    def stop_script(self) -> ActionResult:
        status = self.script_status()
        if not status["running"]:
            return ActionResult(True, "The Zhuorui listener is not running.")
        return self._powershell_script("stop_zhuorui_listener.ps1", timeout=30)

    def start_emulator(self) -> ActionResult:
        config = self._config()
        public = self.public_config()
        current = self.emulator_status()
        if current["running"]:
            return ActionResult(True, "The Android emulator is already running or starting.")
        adb_path = self._adb_path(config)
        emulator_path = self._emulator_path(config, adb_path)
        avd = public["avd"]
        if not emulator_path:
            return ActionResult(False, "emulator.exe was not found. Check the Android SDK installation.")
        if not avd:
            return ActionResult(False, "No AVD is configured. Add avd to zhuorui_config.json.")

        log_dir = self.root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        started_at = utc_now()
        log_path = log_dir / f"emulator_{started_at.strftime('%Y%m%dT%H%M%SZ')}.log"
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            with log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    [str(emulator_path), "-avd", avd],
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                    close_fds=True,
                )
        except OSError as exc:
            return ActionResult(False, f"Could not start the Android emulator: {exc}")

        write_json(
            self.emulator_run_path,
            {
                "pid": process.pid,
                "started_utc": iso_utc(started_at),
                "avd": avd,
                "device": public["device"],
                "log": str(log_path),
            },
        )
        return ActionResult(True, f"Started Android emulator {avd}. It may take a minute to become ready.")

    def stop_emulator(self) -> ActionResult:
        config = self._config()
        public = self.public_config()
        adb_path = self._adb_path(config)
        adb_error = ""
        if adb_path:
            try:
                result = self.runner(
                    [str(adb_path), "-s", public["device"], "emu", "kill"],
                    cwd=self.root,
                    timeout=15,
                )
                if result.returncode == 0:
                    return ActionResult(True, f"Stop signal sent to Android emulator {public['device']}.")
                adb_error = command_message(result)
            except (OSError, subprocess.SubprocessError) as exc:
                adb_error = str(exc)

        tracked, tracked_pid, _ = self._emulator_tracking()
        if tracked and tracked_pid:
            try:
                if os.name == "nt":
                    result = self.runner(
                        ["taskkill.exe", "/PID", str(tracked_pid), "/T", "/F"],
                        cwd=self.root,
                        timeout=15,
                    )
                    if result.returncode != 0:
                        return ActionResult(False, command_message(result))
                else:
                    os.kill(tracked_pid, signal.SIGTERM)
                return ActionResult(True, f"Stopped tracked emulator process {tracked_pid}.")
            except (OSError, subprocess.SubprocessError) as exc:
                return ActionResult(False, f"Could not stop tracked emulator process: {exc}")
        if adb_error:
            return ActionResult(False, f"ADB could not stop the emulator: {adb_error}")
        return ActionResult(True, "The Android emulator is not running.")

    def perform(self, action: str) -> ActionResult:
        actions = {
            "script/start": self.start_script,
            "script/stop": self.stop_script,
            "emulator/start": self.start_emulator,
            "emulator/stop": self.stop_emulator,
        }
        handler = actions.get(action)
        if not handler:
            return ActionResult(False, "Unknown control action.")
        if not self._action_lock.acquire(blocking=False):
            return ActionResult(False, "Another control action is still in progress.")
        try:
            return handler()
        finally:
            self._action_lock.release()


class StatusMonitor:
    def __init__(self, controller: ZhuoruiController, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
        self.controller = controller
        self.interval_seconds = interval_seconds
        self._status: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh(self) -> dict[str, Any]:
        with self._refresh_lock:
            try:
                status = self.controller.collect_status(self.interval_seconds)
            except Exception as exc:  # Keep the web server alive if an external tool fails unexpectedly.
                checked_at = utc_now()
                status = {
                    "account": self.controller.public_config(),
                    "script": {"state": "attention", "running": False, "message": str(exc)},
                    "emulator": {"state": "attention", "running": False, "message": "Status unavailable."},
                    "checked_at": iso_utc(checked_at),
                    "next_check_at": iso_utc(checked_at),
                    "interval_seconds": self.interval_seconds,
                }
            with self._lock:
                self._status = status
            return status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._status))

    def _run(self) -> None:
        self.refresh()
        while not self._stop_event.wait(self.interval_seconds):
            self.refresh()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="zhuorui-status-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


class SessionStore:
    def __init__(self, lifetime_seconds: int = SESSION_LIFETIME_SECONDS) -> None:
        self.lifetime_seconds = lifetime_seconds
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _prune(self, now: float) -> None:
        expired = [token for token, session in self._sessions.items() if session["expires_epoch"] <= now]
        for token in expired:
            self._sessions.pop(token, None)

    def create(self, username: str) -> tuple[str, dict[str, Any]]:
        now = time.time()
        token = secrets.token_urlsafe(48)
        session = {
            "username": username,
            "csrf_token": secrets.token_urlsafe(32),
            "expires_epoch": now + self.lifetime_seconds,
        }
        with self._lock:
            self._prune(now)
            self._sessions[token] = session
        return token, dict(session)

    def get(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(token)
            return dict(session) if session else None

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


class LoginLimiter:
    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._lock = threading.RLock()

    def retry_after(self, client: str) -> int:
        now = time.time()
        with self._lock:
            locked_until = self._lockouts.get(client, 0)
            if locked_until <= now:
                self._lockouts.pop(client, None)
                return 0
            return max(1, int(locked_until - now))

    def record_failure(self, client: str) -> int:
        now = time.time()
        cutoff = now - LOGIN_ATTEMPT_WINDOW_SECONDS
        with self._lock:
            failures = [attempt for attempt in self._failures.get(client, []) if attempt >= cutoff]
            failures.append(now)
            if len(failures) >= MAX_LOGIN_FAILURES:
                self._failures.pop(client, None)
                self._lockouts[client] = now + LOGIN_LOCKOUT_SECONDS
                return LOGIN_LOCKOUT_SECONDS
            self._failures[client] = failures
            if len(self._failures) > 2048:
                self._failures = {
                    address: attempts
                    for address, attempts in self._failures.items()
                    if any(attempt >= cutoff for attempt in attempts)
                }
            return 0

    def clear(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
            self._lockouts.pop(client, None)


class ZhuoruiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], monitor: StatusMonitor) -> None:
        super().__init__(server_address, ZhuoruiRequestHandler)
        self.monitor = monitor
        self.sessions = SessionStore()
        self.login_limiter = LoginLimiter()
        self.is_tls = False
        self.tls_context: ssl.SSLContext | None = None

    def get_request(self):
        connection, client_address = super().get_request()
        if not self.tls_context:
            return connection, client_address
        try:
            connection.settimeout(10)
            # Defer the handshake to the request worker. A client that opens a
            # TCP connection without completing TLS must not block all users.
            secure_connection = self.tls_context.wrap_socket(
                connection,
                server_side=True,
                do_handshake_on_connect=False,
            )
            return secure_connection, client_address
        except Exception:
            connection.close()
            raise


class RedirectServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], public_host: str, https_port: int) -> None:
        super().__init__(server_address, RedirectRequestHandler)
        self.public_host = public_host
        self.https_port = https_port


class RedirectRequestHandler(BaseHTTPRequestHandler):
    server: RedirectServer
    server_version = "ZhuoruiRedirect/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stdout.write(
            f"{self.log_date_time_string()} {self.client_address[0]} redirect {format_string % args}\n"
        )
        sys.stdout.flush()

    def _redirect(self) -> None:
        port = "" if self.server.https_port == 443 else f":{self.server.https_port}"
        parsed = urlparse(self.path)
        request_path = parsed.path if parsed.path.startswith("/") else "/"
        if parsed.query:
            request_path += f"?{parsed.query}"
        location = f"https://{self.server.public_host}{port}{request_path}"
        self.send_response(HTTPStatus.PERMANENT_REDIRECT)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    do_GET = _redirect
    do_HEAD = _redirect
    do_POST = _redirect


class ZhuoruiRequestHandler(BaseHTTPRequestHandler):
    server: ZhuoruiServer
    server_version = "ZhuoruiMonitor/2.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stdout.write(
            f"{self.log_date_time_string()} {self.client_address[0]} {format_string % args}\n"
        )
        sys.stdout.flush()

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.server.is_tls:
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _json(
        self,
        payload: Mapping[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self._send_security_headers()
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()

    def _serve_file(self, path: Path, content_type: str, *, login_page: bool = False) -> None:
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        form_action = "'self'" if login_page else "'none'"
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action {form_action}",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        expected_scheme = "https" if self.server.is_tls else "http"
        return parsed.scheme == expected_scheme and parsed.netloc.casefold() == self.headers.get("Host", "").casefold()

    def _session_token(self) -> str | None:
        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(raw_cookie)
            morsel = cookie.get(SESSION_COOKIE_NAME)
            return morsel.value if morsel else None
        except Exception:
            return None

    def _session(self) -> dict[str, Any] | None:
        return self.server.sessions.get(self._session_token())

    def _require_session(self, *, api: bool) -> dict[str, Any] | None:
        session = self._session()
        if session:
            return session
        if api:
            self._json({"ok": False, "message": "Authentication required."}, HTTPStatus.UNAUTHORIZED)
        else:
            self._redirect("/login")
        return None

    def _read_json_body(self) -> dict[str, Any] | None:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._json({"ok": False, "message": "JSON request required."}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            self._json({"ok": False, "message": "Invalid request size."}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            value = json.loads(self.rfile.read(content_length) if content_length else b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"ok": False, "message": "Invalid JSON request."}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(value, dict):
            self._json({"ok": False, "message": "JSON object required."}, HTTPStatus.BAD_REQUEST)
            return None
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json({"ok": True, "service": "zhuorui-monitor"})
            return

        if parsed.path == "/login":
            if self._session():
                self._redirect("/")
            else:
                self._serve_file(WEB_ROOT / "login.html", "text/html; charset=utf-8", login_page=True)
            return
        if parsed.path == "/login.js":
            self._serve_file(WEB_ROOT / "login.js", "text/javascript; charset=utf-8", login_page=True)
            return
        if parsed.path == "/styles.css":
            self._serve_file(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
            return

        if parsed.path == "/api/session":
            session = self._require_session(api=True)
            if session:
                self._json(
                    {
                        "ok": True,
                        "username": session["username"],
                        "csrf_token": session["csrf_token"],
                        "expires_at": iso_utc(datetime.fromtimestamp(session["expires_epoch"], tz=timezone.utc)),
                    }
                )
            return
        if parsed.path == "/api/status":
            if not self._require_session(api=True):
                return
            status = self.server.monitor.refresh() if parsed.query == "refresh=1" else self.server.monitor.snapshot()
            if not status:
                status = self.server.monitor.refresh()
            self._json(status)
            return

        static_files = {
            "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/index.html": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
        }
        selected = static_files.get(parsed.path)
        if not selected:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._require_session(api=False):
            return
        self._serve_file(*selected)

    def _login(self) -> None:
        if not self._same_origin():
            self._json({"ok": False, "message": "Login request rejected."}, HTTPStatus.FORBIDDEN)
            return
        payload = self._read_json_body()
        if payload is None:
            return
        client = self.client_address[0]
        retry_after = self.server.login_limiter.retry_after(client)
        if retry_after:
            self._json(
                {"ok": False, "message": "Too many failed attempts. Try again later.", "retry_after": retry_after},
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
            )
            return
        if not verify_admin_credentials(payload.get("username"), payload.get("password")):
            retry_after = self.server.login_limiter.record_failure(client)
            status = HTTPStatus.TOO_MANY_REQUESTS if retry_after else HTTPStatus.UNAUTHORIZED
            response: dict[str, Any] = {"ok": False, "message": "Invalid username or password."}
            headers: dict[str, str] = {}
            if retry_after:
                response = {"ok": False, "message": "Too many failed attempts. Try again later.", "retry_after": retry_after}
                headers["Retry-After"] = str(retry_after)
            self._json(response, status, headers=headers)
            return

        self.server.login_limiter.clear(client)
        old_token = self._session_token()
        self.server.sessions.destroy(old_token)
        token, session = self.server.sessions.create(ADMIN_USERNAME)
        cookie = f"{SESSION_COOKIE_NAME}={token}; Path=/; Secure; HttpOnly; SameSite=Strict"
        self._json(
            {"ok": True, "username": ADMIN_USERNAME, "csrf_token": session["csrf_token"]},
            headers={"Set-Cookie": cookie},
        )

    def _logout(self) -> None:
        session = self._require_session(api=True)
        if not session:
            return
        csrf_token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(csrf_token, session["csrf_token"]):
            self._json({"ok": False, "message": "Control request rejected."}, HTTPStatus.FORBIDDEN)
            return
        self.server.sessions.destroy(self._session_token())
        expired_cookie = (
            f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
        )
        self._json(
            {"ok": True, "message": "Signed out."},
            headers={"Set-Cookie": expired_cookie, "Clear-Site-Data": '"cache", "cookies", "storage"'},
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self._login()
            return
        if parsed.path == "/api/logout":
            if not self._same_origin():
                self._json({"ok": False, "message": "Control request rejected."}, HTTPStatus.FORBIDDEN)
                return
            self._logout()
            return
        if not parsed.path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        session = self._require_session(api=True)
        if not session:
            return
        csrf_token = self.headers.get("X-CSRF-Token", "")
        if (
            not self._same_origin()
            or self.headers.get("X-Zhuorui-Action") != "1"
            or not secrets.compare_digest(csrf_token, session["csrf_token"])
        ):
            self._json({"ok": False, "message": "Control request rejected."}, HTTPStatus.FORBIDDEN)
            return
        payload = self._read_json_body()
        if payload is None:
            return
        action = parsed.path.removeprefix("/api/")
        result = self.server.monitor.controller.perform(action)
        status = self.server.monitor.refresh()
        self._json(
            {"ok": result.ok, "message": result.message, "status": status},
            HTTPStatus.OK if result.ok else HTTPStatus.CONFLICT,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the authenticated Zhuorui monitoring dashboard.")
    parser.add_argument("--host", default="0.0.0.0", help="address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8787, help="HTTPS port to bind (default: 8787)")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="status check interval in seconds (minimum: 10, default: 60)",
    )
    parser.add_argument("--cert-file", help="PEM TLS certificate")
    parser.add_argument("--key-file", help="PEM TLS private key")
    parser.add_argument("--redirect-http-port", type=int, default=0, help="optional HTTP port that redirects to HTTPS")
    parser.add_argument("--public-host", default="209.250.240.250", help="public hostname or IP used by the HTTP redirect")
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="allow HTTP only on a loopback address for development tests",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    if args.interval < 10:
        print("Status interval must be at least 10 seconds.", file=sys.stderr)
        return 2
    if args.redirect_http_port and not 1 <= args.redirect_http_port <= 65535:
        print("HTTP redirect port must be between 1 and 65535.", file=sys.stderr)
        return 2
    if args.redirect_http_port == args.port:
        print("HTTP redirect and HTTPS ports must be different.", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", args.public_host):
        print("Public host contains unsupported characters.", file=sys.stderr)
        return 2
    if not (WEB_ROOT / "index.html").is_file() or not (WEB_ROOT / "login.html").is_file():
        print(f"Dashboard assets are missing from {WEB_ROOT}.", file=sys.stderr)
        return 2
    if args.allow_http and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Plain HTTP is allowed only on a loopback address.", file=sys.stderr)
        return 2
    if not args.allow_http and (not args.cert_file or not args.key_file):
        print("HTTPS requires --cert-file and --key-file.", file=sys.stderr)
        return 2

    controller = ZhuoruiController(PROJECT_ROOT)
    monitor = StatusMonitor(controller, args.interval)
    redirect_server: RedirectServer | None = None
    redirect_thread: threading.Thread | None = None
    try:
        server = ZhuoruiServer((args.host, args.port), monitor)
        if not args.allow_http:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.options |= ssl.OP_NO_COMPRESSION
            context.load_cert_chain(certfile=args.cert_file, keyfile=args.key_file)
            server.tls_context = context
            server.is_tls = True
        if args.redirect_http_port:
            redirect_server = RedirectServer(
                (args.host, args.redirect_http_port),
                args.public_host,
                args.port,
            )
    except (OSError, ssl.SSLError) as exc:
        if "server" in locals():
            server.server_close()
        print(f"Could not start dashboard on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    monitor.start()
    if redirect_server:
        redirect_thread = threading.Thread(
            target=redirect_server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="zhuorui-http-redirect",
            daemon=True,
        )
        redirect_thread.start()
    scheme = "http" if args.allow_http else "https"
    display_host = "localhost" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"Zhuorui Control Room: {scheme}://{display_host}:{args.port}")
    print(f"Authentication required. Checking listener and emulator every {args.interval} seconds.")
    if redirect_server:
        print(
            f"HTTP port {args.redirect_http_port} redirects to "
            f"https://{args.public_host}{'' if args.port == 443 else f':{args.port}'}/"
        )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        if redirect_server:
            redirect_server.shutdown()
            redirect_server.server_close()
        if redirect_thread:
            redirect_thread.join(timeout=5)
        monitor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
