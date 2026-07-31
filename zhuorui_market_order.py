#!/usr/bin/env python3
"""
Submit Zhuorui orders through the Android emulator UI.

Default behavior is a dry run: the script prepares the order ticket but does not
tap the final trade button. Live submission requires --confirm-live-order.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Iterable, Optional


PACKAGE = "com.zhuorui.securities"
LAUNCH_ACTIVITY = f"{PACKAGE}/.ui.SplashActivity"
LOGGED_OUT_BOTTOM_TAB = "Open A/C"
LOGGED_IN_BOTTOM_TAB = "Assets"
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
LOGIN_DELAY_START_HOUR = 9
LOGIN_DELAY_END_HOUR = 16
LOGIN_DELAY_SECONDS = 180.0
LOGIN_ME_SWITCH_DELAY = 0.5
REMOTE_DUMP = "/sdcard/codex-zhuorui-window.xml"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_NAMES = ("zhuorui_config.json", "config.json")
DEFAULT_WAIT_TIMEOUT = 4.0
ADB_COMMAND_TIMEOUT = 5.0
ADB_DUMP_TIMEOUT = 2.5
ADB_PULL_TIMEOUT = 2.0
FAST_POLL = 0.12
SHORT_SETTLE = 0.15
FIELD_FOCUS_SETTLE = 0.35
PASSWORD_FOCUS_SETTLE = 0.3
FAST_SCREEN_SIZE = (1080, 2424)
FAST_TRADE_SHEET_SIDE_BUTTONS = {
    "buy": (197, 2124),
    "sell": (540, 2124),
}
FAST_TICKET_ORDER_TYPE_FIELD = (671, 1556)
FAST_TICKET_ORDER_TYPE_OPTIONS = {
    "limit": (540, 2109),
    "market": (539, 2277),
}
FAST_TICKET_PRICE_FIELD = (608, 1703)
FAST_TICKET_QUANTITY_FIELD = (608, 1850)
FAST_TICKET_SUBMIT_BUTTON = (820, 2266)
FAST_QUOTES_TAB_RIGHT = (175, 2285)
FAST_ASSETS_TAB = (270, 2285)
FAST_NET_ASSETS_TILE_MIDDLE_LEFT = (170, 1300)
FAST_ASSETS_POSITIONS_SECTION_TAB = (165, 430)
FAST_ASSETS_ORDERS_SECTION_TAB = (430, 430)
FAST_ASSETS_TODAYS_ORDERS_TAB = (230, 558)
FAST_ASSETS_FIRST_ORDER_ROW = (540, 760)
FAST_ASSETS_ORDER_CANCEL_BUTTON = (412, 873)
FAST_CANCEL_ORDER_CONFIRM_BUTTON = (745, 1355)
# Tap just inside the right edge of the first bottom-bar tab. Tapping near
# the left edge can accidentally open Android's side menu.
QUOTES_TAB_RIGHT_X_RATIO = (1 / 6) - 0.005
ASSETS_TAB_X_RATIO = 0.25
BOTTOM_TAB_Y_RATIO = 0.943
APP_BACK_X_RATIO = 0.063
APP_BACK_Y_RATIO = 0.082
SUCCESS_REVOKE_X_RATIO = 0.29
SUCCESS_REVOKE_Y_RATIO = 0.895
WATCHLIST_SYMBOL_SCORE_THRESHOLD = 0.70
HOME_SCREEN_REQUIRED_LABELS = ("Quotes", "Assets", "S-Invest", "Wealth", "News")
HOME_SCREEN_LABEL_SCORE_THRESHOLD = 0.48
EMPTY_POSITIONS_LABEL = "No positions yet"
POSITION_TABLE_MAX_SCROLLS = 8
POSITION_LANDING_BACK_TAPS = 5
POSITION_LANDING_BACK_DELAY = 0.45
ORDER_WATCHLIST_BACK_TAPS = 5
ORDER_WATCHLIST_BACK_DELAY = 0.45
FILL_OR_KILL_REVOKE_DELAY = 3.0
CANCEL_ORDER_SETTLE_SECONDS = 2.0
MAX_CANCEL_ORDER_ATTEMPTS = 50
ANDROID_ROBOTO_FONT = "/system/fonts/Roboto-Regular.ttf"
KEYCODE_ENTER = 66
MARKET_BUY_LIMIT_MULTIPLIER = Decimal("1.05")
MARKET_SELL_LIMIT_MULTIPLIER = Decimal("0.95")
MARKET_LIMIT_PRICE_QUANTUM = Decimal("0.0001")
DEFAULT_KAFKA_COMMAND_TOPIC = "commands"
DEFAULT_KAFKA_HOLDINGS_TOPIC = "holdings"
DEFAULT_KAFKA_ORDER_STATUS_TOPIC = "order-status"
DEFAULT_HOLDINGS_INTERVAL_SECONDS = 30.0
DEFAULT_KAFKA_POLL_SECONDS = 1.0

KNOWN_ADB_PATHS = [
    Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe",
    Path(os.environ.get("ANDROID_SDK_ROOT", "")) / "platform-tools" / "adb.exe",
    Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
]


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


def login_delay_seconds(now: Optional[datetime] = None) -> float:
    beijing_now = now.astimezone(BEIJING_TIMEZONE) if now is not None else datetime.now(BEIJING_TIMEZONE)
    if LOGIN_DELAY_START_HOUR <= beijing_now.hour < LOGIN_DELAY_END_HOUR:
        return LOGIN_DELAY_SECONDS
    return 0.0


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass
class UiNode:
    text: str
    hint: str
    content_desc: str
    resource_id: str
    klass: str
    clickable: bool
    focusable: bool
    focused: bool
    password: bool
    bounds: Bounds


def parse_bounds(raw: str) -> Bounds:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
    if not match:
        raise ZhuoruiAutomationError(f"Cannot parse bounds: {raw!r}")
    left, top, right, bottom = map(int, match.groups())
    return Bounds(left, top, right, bottom)


class Adb:
    def __init__(self, adb_path: Optional[str] = None, device: Optional[str] = None, verbose: bool = False):
        self.adb = self._resolve_adb(adb_path)
        self.verbose = verbose
        self.device = self._resolve_device(device)
        self._wm_size: Optional[tuple[int, int]] = None
        self._dump_transport: Optional[str] = None

    @staticmethod
    def _resolve_adb(adb_path: Optional[str]) -> str:
        if adb_path:
            path = Path(adb_path)
            if path.exists():
                return str(path)
            raise ZhuoruiAutomationError(f"adb was not found at {path}")

        found = shutil.which("adb")
        if found:
            return found

        for path in KNOWN_ADB_PATHS:
            if path and path.exists():
                return str(path)

        raise ZhuoruiAutomationError(
            "adb was not found. Pass --adb PATH or set ANDROID_HOME/ANDROID_SDK_ROOT."
        )

    def _resolve_device(self, requested: Optional[str]) -> Optional[str]:
        devices = self._online_devices()
        if not requested:
            return devices[0] if len(devices) == 1 else requested
        if requested in devices:
            return requested
        if len(devices) == 1:
            actual = devices[0]
            print(
                f"Configured ADB device {requested!r} is not connected; using {actual!r}.",
                file=sys.stderr,
            )
            return actual
        if devices:
            raise ZhuoruiAutomationError(
                f"Configured ADB device {requested!r} is not connected. "
                f"Connected devices: {', '.join(devices)}"
            )
        return requested

    def _online_devices(self) -> list[str]:
        result = subprocess.run(
            [self.adb, "devices"],
            text=True,
            capture_output=True,
            timeout=ADB_COMMAND_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            return []
        devices: list[str] = []
        for line in result.stdout.splitlines():
            match = re.match(r"^(\S+)\s+device(?:\s|$)", line)
            if match:
                devices.append(match.group(1))
        return devices

    def cmd(self, *args: str, timeout: float = ADB_COMMAND_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [self.adb]
        if self.device:
            command.extend(["-s", self.device])
        command.extend(args)
        if self.verbose:
            print("+", " ".join(command), file=sys.stderr)
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if check:
                raise ZhuoruiAutomationError(
                    f"adb command timed out after {timeout:g}s: {' '.join(args)}"
                ) from exc
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else f"timed out after {timeout:g}s"
            return subprocess.CompletedProcess(command, 124, stdout, stderr)
        if check and result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise ZhuoruiAutomationError(f"adb command failed: {' '.join(args)}\n{details}")
        return result

    def shell(self, *args: str, timeout: float = ADB_COMMAND_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.cmd("shell", *args, timeout=timeout, check=check)

    def foreground_package(self) -> Optional[str]:
        result = self.shell("dumpsys", "window", timeout=ADB_COMMAND_TIMEOUT, check=False)
        output = result.stdout or ""
        for pattern in (
            r"mCurrentFocus=.*?\s([A-Za-z0-9_.]+)/",
            r"mFocusedApp=.*?\s([A-Za-z0-9_.]+)/",
        ):
            match = re.search(pattern, output)
            if match:
                return match.group(1)
        return None

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def tap_node(self, node: UiNode) -> None:
        x, y = node.bounds.center
        self.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 450) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def keyevent(self, *codes: str | int) -> None:
        self.shell("input", "keyevent", *(str(code) for code in codes))

    def input_text(self, text: str) -> None:
        self.shell("input", "text", escape_adb_input_text(text))

    def input_key_text(self, text: str) -> None:
        keycodes: list[int] = []
        for char in text:
            keycode = adb_keycode_for_char(char)
            if keycode is None:
                raise ZhuoruiAutomationError(
                    "Trade password entry through the secure keypad only supports letters and digits."
                )
            keycodes.append(keycode)
        if keycodes:
            self.keyevent(*keycodes)
            time.sleep(SHORT_SETTLE)

    def disable_animations(self) -> None:
        for name in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            self.shell("settings", "put", "global", name, "0", timeout=2, check=False)

    def wm_size(self, refresh: bool = False) -> tuple[int, int]:
        if self._wm_size is not None and not refresh:
            return self._wm_size
        output = self.shell("wm", "size").stdout
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if not match:
            raise ZhuoruiAutomationError(f"Could not read screen size from: {output!r}")
        self._wm_size = int(match.group(1)), int(match.group(2))
        return self._wm_size

    def dump_xml(self) -> list[UiNode]:
        direct_errors: list[str] = []
        if self._dump_transport != "file":
            for dump_args in (
                ("exec-out", "uiautomator", "dump", "/dev/tty"),
                ("exec-out", "uiautomator", "dump", "--compressed", "/dev/tty"),
            ):
                dumped = self.cmd(*dump_args, timeout=ADB_DUMP_TIMEOUT, check=False)
                raw = dumped.stdout or ""
                if dumped.returncode == 0 and raw:
                    try:
                        nodes = parse_ui(extract_ui_xml(raw))
                    except ZhuoruiAutomationError as exc:
                        direct_errors.append(str(exc))
                    else:
                        self._dump_transport = "direct"
                        return nodes
                else:
                    direct_errors.append((dumped.stderr or dumped.stdout or "").strip())
            self._dump_transport = "file"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as tmp:
            local_path = Path(tmp.name)
        try:
            errors: list[str] = direct_errors
            for dump_args in (
                ("uiautomator", "dump", REMOTE_DUMP),
                ("uiautomator", "dump", "--compressed", REMOTE_DUMP),
            ):
                for _ in range(1):
                    self.shell("rm", "-f", REMOTE_DUMP, timeout=1, check=False)
                    dumped = self.shell(*dump_args, timeout=ADB_DUMP_TIMEOUT, check=False)
                    pulled = self.cmd("pull", REMOTE_DUMP, str(local_path), timeout=ADB_PULL_TIMEOUT, check=False)
                    if pulled.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0:
                        raw = local_path.read_text(encoding="utf-8", errors="replace")
                        return parse_ui(raw)
                    errors.append((dumped.stderr or dumped.stdout or pulled.stderr or pulled.stdout or "").strip())
                    time.sleep(0.1)
            raise ZhuoruiAutomationError("Could not dump Android UI XML: " + " | ".join(error for error in errors if error))
        finally:
            try:
                local_path.unlink()
            except FileNotFoundError:
                pass

    def screenshot(self, path: Path) -> None:
        remote = "/sdcard/codex-zhuorui-screen.png"
        self.shell("screencap", "-p", remote)
        self.cmd("pull", remote, str(path))

    def pull(self, remote: str, local: Path) -> None:
        self.cmd("pull", remote, str(local))


def escape_adb_input_text(text: str) -> str:
    # `adb shell input text` treats spaces specially and has a small shell-like
    # parser on-device. Keep supported order symbols simple; this also handles
    # passwords containing spaces.
    return (
        text.replace("\\", "\\\\")
        .replace(" ", "%s")
        .replace("&", "\\&")
        .replace("|", "\\|")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace(";", "\\;")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("$", "\\$")
        .replace("`", "\\`")
        .replace('"', '\\"')
        .replace("'", "\\'")
    )


def adb_keycode_for_char(char: str) -> Optional[int]:
    if re.fullmatch(r"[a-zA-Z]", char):
        return 29 + (ord(char.lower()) - ord("a"))
    if re.fullmatch(r"\d", char):
        return 7 + int(char)
    return None


def extract_ui_xml(raw: str) -> str:
    start = raw.find("<?xml")
    if start < 0:
        start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise ZhuoruiAutomationError("uiautomator output did not contain a UI hierarchy.")
    return raw[start : end + len("</hierarchy>")]


def parse_ui(raw_xml: str) -> list[UiNode]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise ZhuoruiAutomationError(f"Could not parse UI XML: {exc}") from exc

    nodes: list[UiNode] = []
    for elem in root.iter("node"):
        bounds_raw = elem.attrib.get("bounds", "")
        if not bounds_raw:
            continue
        nodes.append(
            UiNode(
                text=elem.attrib.get("text", ""),
                hint=elem.attrib.get("hint", ""),
                content_desc=elem.attrib.get("content-desc", ""),
                resource_id=elem.attrib.get("resource-id", ""),
                klass=elem.attrib.get("class", ""),
                clickable=elem.attrib.get("clickable") == "true",
                focusable=elem.attrib.get("focusable") == "true",
                focused=elem.attrib.get("focused") == "true",
                password=elem.attrib.get("password") == "true",
                bounds=parse_bounds(bounds_raw),
            )
        )
    return nodes


def nodes_by_id(nodes: Iterable[UiNode], resource_suffix: str) -> list[UiNode]:
    return [node for node in nodes if node.resource_id.endswith(resource_suffix)]


def first_by_id(nodes: Iterable[UiNode], resource_suffix: str) -> Optional[UiNode]:
    return next(iter(nodes_by_id(nodes, resource_suffix)), None)


def first_text(nodes: Iterable[UiNode], text: str) -> Optional[UiNode]:
    return next((node for node in nodes if node.text == text), None)


def any_text_contains(nodes: Iterable[UiNode], needle: str) -> bool:
    needle_lower = needle.lower()
    return any(needle_lower in node.text.lower() for node in nodes if node.text)


def wait_for(predicate, timeout: float, interval: float = 0.4):
    deadline = time.monotonic() + timeout
    last_nodes: list[UiNode] = []
    while time.monotonic() < deadline:
        last_nodes = predicate()
        if last_nodes:
            return last_nodes
        time.sleep(interval)
    return last_nodes


def contiguous_runs(values: list[int], max_gap: int = 1) -> list[tuple[int, int]]:
    if not values:
        return []
    runs: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value <= previous + max_gap:
            previous = value
            continue
        runs.append((start, previous))
        start = previous = value
    runs.append((start, previous))
    return runs


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ZhuoruiAutomationError(message)


class NumericOcr:
    CHARS = "0123456789,."
    NORMALIZED_SIZE = (20, 30)

    def __init__(self, font_path: Path):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise ZhuoruiAutomationError(
                "Pillow is required for reading Zhuorui's drawn position table numbers."
            ) from exc

        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont
        self.templates = self._build_templates(font_path)

    @classmethod
    def from_adb(cls, adb: Adb, temp_dir: Path) -> "NumericOcr":
        font_path = temp_dir / "Roboto-Regular.ttf"
        adb.pull(ANDROID_ROBOTO_FONT, font_path)
        return cls(font_path)

    def _build_templates(self, font_path: Path) -> dict[str, list[tuple[int, tuple[int, int], bytes]]]:
        templates: dict[str, list[tuple[int, tuple[int, int], bytes]]] = {char: [] for char in self.CHARS}
        for size in range(22, 42):
            font = self.ImageFont.truetype(str(font_path), size)
            for char in self.CHARS:
                bbox = font.getbbox(char)
                width = max(1, bbox[2] - bbox[0]) + 12
                height = max(1, bbox[3] - bbox[1]) + 12
                image = self.Image.new("L", (width, height), 255)
                draw = self.ImageDraw.Draw(image)
                draw.text((6 - bbox[0], 6 - bbox[1]), char, font=font, fill=0)
                glyph = self._trim_binary_image(image)
                if glyph is None:
                    continue
                templates[char].append((size, glyph.size, self._normalize(glyph)))
        return templates

    def recognize_lower_line(self, screenshot_path: Path, bounds: Bounds) -> str:
        image = self.Image.open(screenshot_path).convert("RGB")
        line_top = bounds.top + max(0, bounds.height // 2 - 4)
        crop = image.crop((bounds.left, line_top, bounds.right, bounds.bottom))
        return self.recognize_crop(crop)

    def recognize_crop(self, image) -> str:
        glyphs = self._segment_glyphs(image)
        return "".join(self._recognize_glyph(glyph) for glyph in glyphs).strip(",.")

    def _segment_glyphs(self, image) -> list:
        mask_image = self._text_mask_image(image)
        if mask_image is None:
            return []

        width, height = mask_image.size
        pixels = mask_image.load()
        columns: list[tuple[int, int]] = []
        in_run = False
        start = 0
        blank_gap = 0
        for x in range(width):
            count = sum(1 for y in range(height) if pixels[x, y] == 0)
            has_ink = count > 0
            if has_ink and not in_run:
                start = x
                in_run = True
                blank_gap = 0
            elif has_ink:
                blank_gap = 0
            elif in_run:
                blank_gap += 1
                if blank_gap > 1:
                    columns.append((start, x - blank_gap + 1))
                    in_run = False
                    blank_gap = 0
        if in_run:
            columns.append((start, width))

        glyphs = []
        for left, right in columns:
            if right - left <= 0:
                continue
            glyph = self._trim_binary_image(mask_image.crop((left, 0, right, height)))
            if glyph is not None and glyph.size[0] > 0 and glyph.size[1] > 0:
                glyphs.append(glyph)
        return glyphs

    def _text_mask_image(self, image):
        rgb = image.convert("RGB")
        width, height = rgb.size
        pixels = rgb.load()
        xs: list[int] = []
        ys: list[int] = []
        mask = self.Image.new("L", (width, height), 255)
        mask_pixels = mask.load()
        for y in range(height):
            for x in range(width):
                red, green, blue = pixels[x, y]
                is_text = min(red, green, blue) < 210 and not (
                    red > 230 and green > 230 and blue > 230
                )
                if is_text:
                    xs.append(x)
                    ys.append(y)
                    mask_pixels[x, y] = 0
        if not xs:
            return None
        return mask.crop((min(xs), min(ys), max(xs) + 1, max(ys) + 1))

    def _trim_binary_image(self, image):
        gray = image.convert("L")
        width, height = gray.size
        pixels = gray.load()
        xs: list[int] = []
        ys: list[int] = []
        for y in range(height):
            for x in range(width):
                if pixels[x, y] < 225:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return gray.crop((min(xs), min(ys), max(xs) + 1, max(ys) + 1)).point(
            lambda px: 0 if px < 225 else 255
        )

    def _normalize(self, image) -> bytes:
        resized = image.convert("L").resize(self.NORMALIZED_SIZE, self.Image.Resampling.BILINEAR)
        return bytes(1 if px < 180 else 0 for px in resized.tobytes())

    def _recognize_glyph(self, glyph) -> str:
        normalized = self._normalize(glyph)
        best_char = ""
        best_score: Optional[float] = None
        glyph_width, glyph_height = glyph.size
        for char, variants in self.templates.items():
            for _, (template_width, template_height), template in variants:
                distance = sum(a != b for a, b in zip(normalized, template))
                width_penalty = abs(glyph_width - template_width) * 1.8
                height_penalty = abs(glyph_height - template_height) * 1.2
                score = distance + width_penalty + height_penalty
                if best_score is None or score < best_score:
                    best_score = score
                    best_char = char
        return best_char


@dataclass(frozen=True)
class WatchlistSymbolMatch:
    score: float
    tap_point: tuple[int, int]
    last_price: Optional[Decimal]


class WatchlistSymbolMatcher:
    def __init__(self, font_path: Path):
        try:
            from PIL import Image, ImageDraw, ImageFont
            import numpy as np
        except ImportError as exc:
            raise ZhuoruiAutomationError(
                "Pillow and numpy are required for screenshot-based watchlist symbol detection."
            ) from exc

        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont
        self.np = np
        self.font_path = font_path

    @classmethod
    def from_adb(cls, adb: Adb, temp_dir: Path) -> "WatchlistSymbolMatcher":
        font_path = temp_dir / "Roboto-Regular.ttf"
        adb.pull(ANDROID_ROBOTO_FONT, font_path)
        return cls(font_path)

    def find_symbol(
        self,
        screenshot_path: Path,
        symbol: str,
        price_ocr: Optional[NumericOcr] = None,
    ) -> Optional["WatchlistSymbolMatch"]:
        image = self.Image.open(screenshot_path).convert("RGB")
        width, height = image.size
        target = symbol.upper()
        templates = self.render_target_templates(target)
        best: Optional[WatchlistSymbolMatch] = None
        for line_top, line_bottom in self.symbol_line_runs(image):
            crop = image.crop(
                (
                    round(width * 0.07),
                    max(0, line_top - 8),
                    round(width * 0.30),
                    min(height, line_bottom + 8),
                )
            )
            score = self.score_symbol_crop(crop, templates)
            center_y = (line_top + line_bottom) // 2
            tap_point = (round(width * 0.14), center_y)
            last_price = self.recognize_last_price(image, center_y, price_ocr)
            candidate = WatchlistSymbolMatch(score=score, tap_point=tap_point, last_price=last_price)
            if best is None or score > best.score:
                best = candidate
        if best and best.score >= WATCHLIST_SYMBOL_SCORE_THRESHOLD:
            return best
        return None

    def recognize_last_price(self, image, symbol_center_y: int, price_ocr: Optional[NumericOcr]) -> Optional[Decimal]:
        if price_ocr is None:
            return None
        width, _ = image.size
        crop = image.crop(
            (
                round(width * 0.52),
                max(0, symbol_center_y - 100),
                round(width * 0.74),
                max(0, symbol_center_y - 25),
            )
        )
        price_text = price_ocr.recognize_crop(crop)
        if "." not in price_text:
            return None
        price = parse_decimal_text(price_text)
        if price is None or price <= 0:
            return None
        return price

    def symbol_line_runs(self, image) -> list[tuple[int, int]]:
        width, height = image.size
        x_start = round(width * 0.02)
        x_end = round(width * 0.23)
        y_start = round(height * 0.25)
        y_end = round(height * 0.90)
        active_rows: list[int] = []
        min_pixels = max(8, round((x_end - x_start) * 0.04))

        for y in range(y_start, y_end):
            count = 0
            for x in range(x_start, x_end):
                red, green, blue = image.getpixel((x, y))
                brightness = (red + green + blue) / 3
                if (
                    brightness > 70
                    and not (red > 245 and green > 245 and blue > 245)
                    and not (abs(red - green) < 5 and abs(green - blue) < 5 and brightness < 100)
                ):
                    count += 1
            if count >= min_pixels:
                active_rows.append(y)

        runs = contiguous_runs(active_rows)
        text_runs = [
            run
            for run in runs
            if 12 <= run[1] - run[0] + 1 <= 42
        ]
        symbol_runs: list[tuple[int, int]] = []
        previous: Optional[tuple[int, int]] = None
        for run in text_runs:
            if previous is not None:
                gap = run[0] - previous[1]
                if 18 <= gap <= 65:
                    symbol_runs.append(run)
            previous = run
        return symbol_runs

    def render_target_templates(self, target: str) -> list[tuple[object, int, int, float]]:
        templates: list[tuple[object, int, int, float]] = []
        for size in range(18, 36):
            font = self.ImageFont.truetype(str(self.font_path), size)
            bbox = font.getbbox(target)
            image = self.Image.new(
                "L",
                (max(1, bbox[2] - bbox[0]) + 8, max(1, bbox[3] - bbox[1]) + 8),
                255,
            )
            draw = self.ImageDraw.Draw(image)
            draw.text((4 - bbox[0], 4 - bbox[1]), target, font=font, fill=0)
            array = self.np.array(image)
            ys, xs = self.np.where(array < 220)
            if len(xs) == 0:
                continue
            mask = (array[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1] < 220).astype(self.np.float32)
            templates.append((mask, mask.shape[1], mask.shape[0], float(mask.sum())))
        return templates

    def score_symbol_crop(self, crop, templates: list[tuple[object, int, int, float]]) -> float:
        array = self.np.array(crop.convert("RGB"))
        mask = (
            (array[:, :, 0] < 230)
            | (array[:, :, 1] < 230)
            | (array[:, :, 2] < 230)
        ).astype(self.np.float32)
        ys, xs = self.np.where(mask > 0)
        if len(xs) == 0:
            return -1.0
        mask = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        padded = self.np.pad(mask, ((8, 8), (8, 8)), constant_values=0)
        best = -1.0
        for template, template_width, template_height, template_count in templates:
            if template_count <= 0:
                continue
            if template_height > padded.shape[0] or template_width > padded.shape[1]:
                continue
            for y in range(0, padded.shape[0] - template_height + 1):
                for x in range(0, padded.shape[1] - template_width + 1):
                    patch = padded[y : y + template_height, x : x + template_width]
                    intersection = float((patch * template).sum())
                    missed = (template_count - intersection) / template_count
                    extra = max(0.0, (float(patch.sum()) - intersection) / template_count)
                    score = intersection / template_count - 0.2 * extra - 0.2 * missed
                    if score > best:
                        best = score
        return best


class HomeScreenTextOcr:
    SEGMENT_COUNT = 6
    LABEL_BANDS = ((0.946, 0.975), (0.938, 0.984))

    def __init__(self, font_path: Path):
        try:
            from PIL import Image, ImageDraw, ImageFont
            import numpy as np
        except ImportError as exc:
            raise ZhuoruiAutomationError(
                "Pillow and numpy are required for screenshot-based home screen OCR."
            ) from exc

        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont
        self.np = np
        self.font_path = font_path
        self.templates = {
            label: self.render_label_templates(label)
            for label in HOME_SCREEN_REQUIRED_LABELS
        }

    @classmethod
    def from_adb(cls, adb: Adb, temp_dir: Path) -> "HomeScreenTextOcr":
        font_path = temp_dir / "Roboto-Regular.ttf"
        adb.pull(ANDROID_ROBOTO_FONT, font_path)
        return cls(font_path)

    def recognize_home_text(self, screenshot_path: Path) -> str:
        image = self.Image.open(screenshot_path).convert("RGB")
        detected: list[str] = []
        for index, label in enumerate(HOME_SCREEN_REQUIRED_LABELS):
            score = self.score_expected_label(image, index, label)
            if score >= HOME_SCREEN_LABEL_SCORE_THRESHOLD:
                detected.append(label)
        return " ".join(detected)

    def score_expected_label(self, image, index: int, label: str) -> float:
        width, height = image.size
        x0 = round(width * index / self.SEGMENT_COUNT)
        x1 = round(width * (index + 1) / self.SEGMENT_COUNT)
        best = -1.0
        for top_ratio, bottom_ratio in self.LABEL_BANDS:
            crop = image.crop((x0, round(height * top_ratio), x1, round(height * bottom_ratio)))
            score = self.score_text_crop(crop, self.templates[label])
            if score > best:
                best = score
        return best

    def render_label_templates(self, label: str) -> list[tuple[object, int, int, float]]:
        templates: list[tuple[object, int, int, float]] = []
        for size in range(12, 30):
            font = self.ImageFont.truetype(str(self.font_path), size)
            bbox = font.getbbox(label)
            image = self.Image.new(
                "L",
                (max(1, bbox[2] - bbox[0]) + 8, max(1, bbox[3] - bbox[1]) + 8),
                255,
            )
            draw = self.ImageDraw.Draw(image)
            draw.text((4 - bbox[0], 4 - bbox[1]), label, font=font, fill=0)
            array = self.np.array(image)
            ys, xs = self.np.where(array < 220)
            if len(xs) == 0:
                continue
            mask = (array[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1] < 220).astype(self.np.float32)
            templates.append((mask, mask.shape[1], mask.shape[0], float(mask.sum())))
        return templates

    def score_text_crop(self, crop, templates: list[tuple[object, int, int, float]]) -> float:
        array = self.np.array(crop.convert("RGB"))
        mask = (
            (array[:, :, 0] < 235)
            | (array[:, :, 1] < 235)
            | (array[:, :, 2] < 235)
        ).astype(self.np.float32)
        ys, xs = self.np.where(mask > 0)
        if len(xs) == 0:
            return -1.0
        mask = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        padded = self.np.pad(mask, ((8, 8), (8, 8)), constant_values=0)
        best = -1.0
        for template, template_width, template_height, template_count in templates:
            if template_count <= 0:
                continue
            if template_height > padded.shape[0] or template_width > padded.shape[1]:
                continue
            for y in range(0, padded.shape[0] - template_height + 1):
                for x in range(0, padded.shape[1] - template_width + 1):
                    patch = padded[y : y + template_height, x : x + template_width]
                    intersection = float((patch * template).sum())
                    missed = (template_count - intersection) / template_count
                    extra = max(0.0, (float(patch.sum()) - intersection) / template_count)
                    score = intersection / template_count - 0.2 * extra - 0.2 * missed
                    if score > best:
                        best = score
        return best


class ZhuoruiTrader:
    def __init__(
        self,
        adb: Adb,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        artifact_dir: Optional[Path] = None,
        fast_path: bool = True,
        login_phone: Optional[str] = None,
        login_password: Optional[str] = None,
    ):
        self.adb = adb
        self.wait_timeout = wait_timeout
        self.artifact_dir = artifact_dir
        self.fast_path = fast_path
        self.login_phone = login_phone
        self.login_password = login_password
        self.prepared_submit: Optional[UiNode] = None
        self.prepared_order_type_name: Optional[str] = None
        self.prepared_limit_price: Optional[Decimal] = None
        self.market_reference_price: Optional[Decimal] = None

    def launch(self) -> None:
        self.adb.shell("am", "start", "-n", LAUNCH_ACTIVITY, timeout=5)
        self.wait_for_app_foreground(timeout=7.0)
        time.sleep(0.2)
        if self.fast_path:
            return
        try:
            self.dismiss_known_dialogs()
        except ZhuoruiAutomationError:
            # Watchlists can animate continuously and prevent uiautomator from
            # reaching idle. Navigation falls back to coordinate search below.
            pass

    def wait_for_app_foreground(self, timeout: Optional[float] = None) -> None:
        wait_seconds = timeout if timeout is not None else self.wait_timeout
        deadline = time.monotonic() + wait_seconds
        last_package: Optional[str] = None
        while time.monotonic() < deadline:
            last_package = self.adb.foreground_package()
            if last_package == PACKAGE:
                return
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(
            f"Zhuorui did not come to the foreground after {wait_seconds:g}s. "
            f"Current foreground app: {last_package or '<unknown>'}."
        )

    def ensure_app_foreground(self, launch_if_needed: bool) -> None:
        foreground = self.adb.foreground_package()
        if foreground == PACKAGE:
            return
        if not launch_if_needed:
            raise ZhuoruiAutomationError(
                f"Zhuorui is not the foreground app; current foreground app is {foreground or '<unknown>'}. "
                "Open the desired Zhuorui quote page, or omit --assume-current-symbol."
            )
        if foreground:
            print(f"Zhuorui is not foreground ({foreground}); launching Zhuorui.", file=sys.stderr)
        self.launch()

    def current_nodes(self) -> list[UiNode]:
        last_error: Optional[ZhuoruiAutomationError] = None
        for _ in range(3):
            try:
                return self.adb.dump_xml()
            except ZhuoruiAutomationError as exc:
                last_error = exc
                time.sleep(0.35)
        raise last_error or ZhuoruiAutomationError("Could not dump Android UI XML.")

    def dismiss_known_dialogs(self) -> None:
        for _ in range(1):
            nodes = self.current_nodes()
            got_it = first_text(nodes, "I got it") or first_text(nodes, "Got it")
            if got_it:
                self.adb.tap_node(got_it)
                time.sleep(SHORT_SETTLE)
                continue
            break

    def dismiss_transient_overlay(self, nodes: list[UiNode]) -> bool:
        if first_by_id(nodes, ":id/searchView"):
            return False

        share_cancel = first_by_id(nodes, ":id/view_cancel")
        if share_cancel and share_cancel.clickable:
            self.adb.keyevent(4)
            time.sleep(0.3)
            return True

        _, height = self.adb.wm_size()
        for node in nodes:
            if node.text == "Cancel" and node.clickable and node.bounds.center[1] > round(height * 0.80):
                self.adb.keyevent(4)
                time.sleep(0.3)
                return True
        return False

    def is_quote_page(self, nodes: list[UiNode]) -> bool:
        return bool(first_by_id(nodes, ":id/tvSubTitle")) and not first_by_id(nodes, ":id/searchView")

    def is_fast_screen(self) -> bool:
        return self.fast_path and self.adb.wm_size() == FAST_SCREEN_SIZE

    def tap_ratio(self, x_ratio: float, y_ratio: float) -> None:
        width, height = self.adb.wm_size()
        self.adb.tap(round(width * x_ratio), round(height * y_ratio))

    def screenshot_shows_main_landing_page(self) -> bool:
        try:
            with tempfile.TemporaryDirectory(prefix="zhuorui-home-ocr-") as temp_name:
                temp_dir = Path(temp_name)
                screenshot_path = temp_dir / "home.png"
                ocr = HomeScreenTextOcr.from_adb(self.adb, temp_dir)
                self.adb.screenshot(screenshot_path)
                text = self.home_screen_ocr_text(screenshot_path, ocr)
                return self.home_screen_text_has_required_labels(text)
        except ZhuoruiAutomationError:
            return False

    def home_screen_ocr_text(self, screenshot_path: Path, ocr: HomeScreenTextOcr) -> str:
        try:
            if self.image_shows_navigation_drawer(screenshot_path):
                return ""
            return ocr.recognize_home_text(screenshot_path)
        except ZhuoruiAutomationError:
            return ""

    def home_screen_text_has_required_labels(self, text: str) -> bool:
        return all(label in text for label in HOME_SCREEN_REQUIRED_LABELS)

    def image_shows_main_landing_page(self, screenshot_path: Path) -> bool:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ZhuoruiAutomationError(
                "Pillow is required for screenshot-based landing page detection."
            ) from exc

        image = Image.open(screenshot_path).convert("RGB")
        width, height = image.size
        bottom_band = image.crop((0, round(height * 0.90), width, round(height * 0.985)))
        _, white_ratio, dark_ratio = self.image_region_stats(bottom_band)
        if white_ratio < 0.62 or dark_ratio > 0.08:
            return False

        label_band_top = round(height * 0.948)
        label_band_bottom = round(height * 0.972)
        segment_hits = 0
        for index in range(6):
            x0 = round(width * index / 6)
            x1 = round(width * (index + 1) / 6)
            if self.image_segment_has_bottom_tab_label(
                image.crop((x0, label_band_top, x1, label_band_bottom))
            ):
                segment_hits += 1
        return segment_hits >= 5

    def image_segment_has_bottom_tab_label(self, image) -> bool:
        data = image.tobytes()
        nonwhite = 0
        for index in range(0, len(data), 3):
            red = data[index]
            green = data[index + 1]
            blue = data[index + 2]
            if not (red > 245 and green > 245 and blue > 245):
                nonwhite += 1
        ratio = nonwhite / max(1, len(data) // 3)
        return 0.015 <= ratio <= 0.095

    def tap_app_back_button(self) -> None:
        self.tap_ratio(APP_BACK_X_RATIO, APP_BACK_Y_RATIO)

    def is_logged_out_landing_page(self, nodes: list[UiNode]) -> bool:
        if not first_text(nodes, LOGGED_OUT_BOTTOM_TAB):
            return False
        if first_by_id(nodes, ":id/bottomBar"):
            return True
        logged_out_tabs = {"Quotes", "Open A/C", "S-Invest", "Wealth", "News", "Me"}
        return len({node.text for node in nodes if node.text in logged_out_tabs}) >= 3

    def is_main_landing_page(self, nodes: list[UiNode]) -> bool:
        # Authentication state takes precedence over the generic bottom-bar shape.
        if self.is_logged_out_landing_page(nodes):
            return False
        if first_by_id(nodes, ":id/bottomBar"):
            return True
        logged_in_tabs = {"Quotes", "Assets", "S-Invest", "Wealth", "News", "Me"}
        return len({node.text for node in nodes if node.text in logged_in_tabs}) >= 3

    def is_logged_in_landing_page(self, nodes: list[UiNode]) -> bool:
        return (
            bool(first_text(nodes, LOGGED_IN_BOTTOM_TAB))
            and not first_text(nodes, LOGGED_OUT_BOTTOM_TAB)
            and self.is_main_landing_page(nodes)
        )

    def ensure_logged_in(self, nodes: Optional[list[UiNode]] = None) -> None:
        current = nodes if nodes is not None else self.current_nodes()
        if not self.is_logged_out_landing_page(current):
            return

        print(
            'Detected logged-out Zhuorui account (bottom bar contains "Open A/C").',
            file=sys.stderr,
            flush=True,
        )
        if not self.login_phone or not self.login_password:
            raise ZhuoruiAutomationError(
                "Zhuorui account is not logged in and login credentials are not configured. "
                "Set login.phone and login.password in the config or use "
                "ZHUORUI_LOGIN_PHONE and ZHUORUI_LOGIN_PASSWORD."
            )

        delay = login_delay_seconds()
        if delay:
            print(
                "Beijing time is between 09:00 and 16:00; waiting 3 minutes before login.",
                file=sys.stderr,
                flush=True,
            )
            deadline = time.monotonic() + delay
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(30.0, remaining))
            current = self.current_nodes()
            if self.is_logged_in_landing_page(current):
                return
            if not self.is_logged_out_landing_page(current):
                raise ZhuoruiAutomationError(
                    "Zhuorui left the logged-out home page while waiting to log in; aborting automation."
                )

        self.login_from_landing_page(current)

    def text_click_target(
        self,
        nodes: list[UiNode],
        text: str,
        *,
        prefer_bottom: bool = False,
    ) -> Optional[UiNode]:
        targets: list[UiNode] = []
        for text_node in nodes:
            if text_node.text.strip().lower() != text.strip().lower():
                continue
            target = text_node if text_node.clickable else self.clickable_container_for(text_node, nodes)
            targets.append(target or text_node)
        if not targets:
            return None
        targets.sort(key=lambda node: node.bounds.center[1], reverse=prefer_bottom)
        return targets[0]

    def wait_for_text(self, text: str, timeout: Optional[float] = None) -> list[UiNode]:
        deadline = time.monotonic() + (timeout if timeout is not None else self.wait_timeout)
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            if self.text_click_target(nodes, text):
                return nodes
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(f'Login flow could not find "{text}".')

    def login_from_landing_page(self, nodes: list[UiNode]) -> None:
        me = self.text_click_target(nodes, "Me", prefer_bottom=True)
        if not me:
            raise ZhuoruiAutomationError('Login flow could not find the "Me" bottom tab.')
        self.adb.tap_node(me)
        time.sleep(LOGIN_ME_SWITCH_DELAY)

        nodes = self.wait_for_text("Login/Sign Up")
        login_link = self.text_click_target(nodes, "Login/Sign Up")
        require(login_link is not None, 'Login flow could not find the top "Login/Sign Up" link.')
        self.adb.tap_node(login_link)

        nodes = self.wait_for_text("Password Login")
        password_login = self.text_click_target(nodes, "Password Login")
        require(password_login is not None, 'Login flow could not find "Password Login".')
        self.adb.tap_node(password_login)

        nodes = self.wait_for_login_inputs()
        phone_input, password_input = self.find_login_inputs(nodes)
        require(phone_input is not None, "Login flow could not find the phone number input.")
        require(password_input is not None, "Login flow could not find the password input.")
        self.replace_text(phone_input, self.login_phone or "", clear_chars=32)
        self.replace_text(password_input, self.login_password or "", clear_chars=64)
        self.adb.keyevent(4)  # Hide the keyboard so the large login button is visible.
        time.sleep(SHORT_SETTLE)

        nodes = self.current_nodes()
        login_button = self.text_click_target(nodes, "Login/Sign Up", prefer_bottom=True)
        require(login_button is not None, 'Login flow could not find the blue "Login/Sign Up" button.')
        self.adb.tap_node(login_button)
        self.complete_user_agreement_and_wait_for_login()

    def find_login_inputs(self, nodes: list[UiNode]) -> tuple[Optional[UiNode], Optional[UiNode]]:
        inputs = sorted(
            (node for node in nodes if node.klass == "android.widget.EditText"),
            key=lambda node: node.bounds.top,
        )
        password_input = next(
            (
                node
                for node in inputs
                if node.password
                or "password" in f"{node.hint} {node.content_desc}".lower()
            ),
            None,
        )
        phone_input = next(
            (
                node
                for node in inputs
                if node is not password_input
                and any(word in f"{node.hint} {node.content_desc}".lower() for word in ("phone", "mobile"))
            ),
            None,
        )
        if phone_input is None:
            phone_input = next((node for node in inputs if node is not password_input), None)
        if password_input is None:
            password_input = next((node for node in reversed(inputs) if node is not phone_input), None)
        return phone_input, password_input

    def wait_for_login_inputs(self) -> list[UiNode]:
        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            phone_input, password_input = self.find_login_inputs(nodes)
            if phone_input and password_input:
                return nodes
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError("Login flow could not find the phone number and password inputs.")

    def complete_user_agreement_and_wait_for_login(self) -> None:
        deadline = time.monotonic() + max(10.0, self.wait_timeout)
        agreement_confirmed = False
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            if self.is_logged_in_landing_page(nodes):
                print("Zhuorui login completed.", flush=True)
                return

            agreement_visible = any(
                any_text_contains(nodes, word) for word in ("agreement", "terms", "privacy")
            )
            if not agreement_confirmed and agreement_visible:
                for label in (
                    "Agree",
                    "I Agree",
                    "Agree and Continue",
                    "Agree & Continue",
                    "Agree and Login",
                    "Accept",
                    "Confirm",
                ):
                    agree = self.text_click_target(nodes, label, prefer_bottom=True)
                    if agree:
                        self.adb.tap_node(agree)
                        agreement_confirmed = True
                        time.sleep(0.5)
                        break
                else:
                    checkbox = next(
                        (
                            node
                            for node in nodes
                            if node.klass.endswith("CheckBox")
                            or "checkbox" in node.resource_id.lower()
                        ),
                        None,
                    )
                    if checkbox:
                        self.adb.tap_node(checkbox)
                        time.sleep(SHORT_SETTLE)
                        refreshed = self.current_nodes()
                        login_button = self.text_click_target(
                            refreshed,
                            "Login/Sign Up",
                            prefer_bottom=True,
                        )
                        if login_button:
                            self.adb.tap_node(login_button)
                            agreement_confirmed = True
            time.sleep(FAST_POLL)

        raise ZhuoruiAutomationError(
            "Zhuorui login did not complete after submitting credentials and confirming the user agreement."
        )

    def return_to_landing_page(self, max_taps: int = POSITION_LANDING_BACK_TAPS) -> None:
        self.return_to_home_screen_by_back(
            max_presses=max_taps,
            delay=POSITION_LANDING_BACK_DELAY,
            screen_name="Zhuorui's main screen",
        )

    def return_to_landing_page_fast(self, max_taps: int = POSITION_LANDING_BACK_TAPS) -> None:
        self.return_to_home_screen_by_back(
            max_presses=max_taps,
            delay=POSITION_LANDING_BACK_DELAY,
            screen_name="Zhuorui's main screen",
        )

    def return_to_home_screen_by_back(
        self,
        max_presses: int = 5,
        delay: float = 0.45,
        screen_name: str = "Zhuorui's home screen",
    ) -> None:
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            nodes = []
        if nodes and self.is_logged_out_landing_page(nodes):
            self.ensure_logged_in(nodes)
            return

        last_text = ""
        with tempfile.TemporaryDirectory(prefix="zhuorui-home-ocr-") as temp_name:
            temp_dir = Path(temp_name)
            screenshot_path = temp_dir / "home.png"
            ocr = HomeScreenTextOcr.from_adb(self.adb, temp_dir)
            for attempt in range(max_presses + 1):
                try:
                    nodes = self.current_nodes()
                except ZhuoruiAutomationError:
                    nodes = []
                if nodes and self.is_logged_out_landing_page(nodes):
                    self.ensure_logged_in(nodes)
                    return

                self.adb.screenshot(screenshot_path)
                last_text = self.home_screen_ocr_text(screenshot_path, ocr)
                if self.home_screen_text_has_required_labels(last_text) and self.current_screen_has_main_bottom_bar():
                    return
                if attempt >= max_presses:
                    break
                self.adb.keyevent(4)
                time.sleep(delay)
        required = ", ".join(HOME_SCREEN_REQUIRED_LABELS)
        raise ZhuoruiAutomationError(
            f"Could not return to {screen_name} after {max_presses} Back presses. "
            f"Home OCR text must contain all of: {required}. Last OCR text: {last_text!r}"
        )

    def current_screen_has_main_bottom_bar(self) -> bool:
        try:
            return self.is_main_landing_page(self.current_nodes())
        except ZhuoruiAutomationError:
            return False

    def is_assets_page(self, nodes: list[UiNode]) -> bool:
        return bool(
            first_by_id(nodes, ":id/cardNetValue")
            or first_by_id(nodes, ":id/myPositionRecyclerView")
            or (first_text(nodes, "Assets") and first_text(nodes, "Net Assets"))
        )

    def is_cash_details_page(self, nodes: list[UiNode]) -> bool:
        return bool(
            first_by_id(nodes, ":id/tvHKDCash")
            or first_by_id(nodes, ":id/tvUSDCash")
            or first_by_id(nodes, ":id/tvCNHCash")
            or first_text(nodes, "Account details")
        )

    def open_assets(self) -> list[UiNode]:
        last_text: list[str] = []
        for attempt in range(8):
            nodes = self.current_nodes()
            last_text = [node.text for node in nodes if node.text][:12]
            if self.is_assets_page(nodes):
                return nodes

            if self.is_cash_details_page(nodes):
                self.adb.keyevent(4)
                time.sleep(SHORT_SETTLE)
                continue

            if self.dismiss_transient_overlay(nodes):
                continue

            if first_by_id(nodes, ":id/imgClose"):
                self.adb.keyevent(4)
                time.sleep(SHORT_SETTLE)
                continue

            if first_by_id(nodes, ":id/searchView"):
                self.adb.keyevent(4)
                time.sleep(SHORT_SETTLE)
                continue

            if self.is_quote_page(nodes):
                width, height = self.adb.wm_size()
                self.adb.tap(round(width * 0.06), round(height * 0.052))
                time.sleep(0.3)
                continue

            self.tap_ratio(ASSETS_TAB_X_RATIO, BOTTOM_TAB_Y_RATIO)
            deadline = time.monotonic() + min(self.wait_timeout, 2.0)
            while time.monotonic() < deadline:
                nodes = self.current_nodes()
                if self.is_assets_page(nodes):
                    return nodes
                time.sleep(FAST_POLL)

            if attempt >= 3:
                self.adb.keyevent(4)
                time.sleep(SHORT_SETTLE)

        raise ZhuoruiAutomationError(
            f"Could not reach Zhuorui's Assets tab. Visible text: {last_text}"
        )

    def open_cash_details(self) -> list[UiNode]:
        nodes = self.open_assets()
        for _ in range(5):
            amount = first_by_id(nodes, ":id/tvAmount")
            card = first_by_id(nodes, ":id/cardNetValue")
            if amount:
                self.adb.tap_node(amount)
                break
            if card:
                self.adb.tap(card.bounds.left + 170, card.bounds.top + 130)
                break
            self.scroll_assets_toward_top()
            time.sleep(0.35)
            nodes = self.current_nodes()
        else:
            raise ZhuoruiAutomationError("Net Assets tile was not found on the Assets tab.")

        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            if self.is_cash_details_page(nodes):
                return nodes
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError("Cash Details did not open from the Net Assets tile.")

    def collect_positions(self) -> dict[str, list[dict[str, str]]]:
        if self.is_fast_screen():
            return self.collect_positions_fast()

        self.return_to_landing_page()
        assets_nodes = self.open_assets()
        securities = self.collect_security_positions(assets_nodes)
        cash_nodes = self.open_cash_details()
        cash = self.collect_cash_positions(cash_nodes)
        return {"cash": cash, "securities": securities}

    def collect_positions_fast(self) -> dict[str, list[dict[str, str]]]:
        self.return_to_landing_page_fast()
        self.tap_assets_tab_fast()
        time.sleep(0.8)
        self.scroll_assets_to_position_bottom()
        time.sleep(0.7)
        securities = self.collect_visible_security_positions_once()
        self.scroll_assets_toward_top()
        time.sleep(0.8)
        self.tap_net_assets_tile_fast()
        time.sleep(0.9)
        cash = self.collect_cash_positions()
        return {"cash": cash, "securities": securities}

    def tap_assets_tab_fast(self) -> None:
        if self.is_fast_screen():
            self.adb.tap(*FAST_ASSETS_TAB)
            return
        self.tap_ratio(ASSETS_TAB_X_RATIO, BOTTOM_TAB_Y_RATIO)

    def tap_quotes_tab_fast(self) -> None:
        if self.is_fast_screen():
            self.adb.tap(*FAST_QUOTES_TAB_RIGHT)
            return
        self.tap_ratio(QUOTES_TAB_RIGHT_X_RATIO, BOTTOM_TAB_Y_RATIO)

    def tap_fast_point_or_ratio(self, fast_point: tuple[int, int], x_ratio: float, y_ratio: float) -> None:
        if self.is_fast_screen():
            self.adb.tap(*fast_point)
            return
        self.tap_ratio(x_ratio, y_ratio)

    def scroll_assets_to_position_bottom(self) -> None:
        width, height = self.adb.wm_size()
        self.adb.swipe(width // 2, round(height * 0.88), width // 2, round(height * 0.18), 700)

    def collect_visible_security_positions_once(self) -> list[dict[str, str]]:
        nodes = self.current_nodes()
        if self.empty_positions_visible(nodes):
            return []

        with tempfile.TemporaryDirectory(prefix="zhuorui-positions-") as temp_name:
            temp_dir = Path(temp_name)
            ocr = NumericOcr.from_adb(self.adb, temp_dir)
            screenshot_path = temp_dir / "positions.png"
            self.adb.screenshot(screenshot_path)
            securities = self.extract_visible_security_positions(nodes, screenshot_path, ocr)
        if not securities:
            nodes = self.current_nodes()
            if self.empty_positions_visible(nodes):
                return []
            visible = [node.text for node in nodes if node.text]
            raise ZhuoruiAutomationError(f"Positions table rows were not found. Visible text: {visible[:16]}")
        return securities

    def tap_net_assets_tile_fast(self) -> None:
        self.adb.tap(*FAST_NET_ASSETS_TILE_MIDDLE_LEFT)

    def cancel_all_pending_orders(self, dry_run: bool = False) -> dict:
        cancelled_count = 0
        for _ in range(MAX_CANCEL_ORDER_ATTEMPTS):
            self.open_today_orders_from_landing()
            if not self.cancel_first_visible_pending_order(dry_run=dry_run):
                self.tap_assets_positions_section_tab()
                return {
                    "cancelled_orders": cancelled_count,
                    "dry_run": dry_run,
                    "dry_run_confirmation_reached": False,
                }
            if dry_run:
                self.tap_assets_positions_section_tab()
                return {
                    "cancelled_orders": cancelled_count,
                    "dry_run": True,
                    "dry_run_confirmation_reached": True,
                }
            cancelled_count += 1
            time.sleep(CANCEL_ORDER_SETTLE_SECONDS)
        self.tap_assets_positions_section_tab()
        raise ZhuoruiAutomationError(
            f"Stopped after cancelling {cancelled_count} order(s); "
            "the Orders list still appeared cancellable."
        )

    def open_today_orders_from_landing(self) -> None:
        self.return_to_landing_page_fast()
        self.tap_quotes_tab_fast()
        time.sleep(0.7)
        self.tap_assets_tab_fast()
        time.sleep(0.8)
        self.scroll_assets_to_position_bottom()
        time.sleep(0.7)
        self.tap_assets_orders_section_tab()
        time.sleep(0.35)
        self.tap_assets_todays_orders_tab()
        time.sleep(0.25)

    def tap_assets_orders_section_tab(self) -> None:
        self.tap_fast_point_or_ratio(FAST_ASSETS_ORDERS_SECTION_TAB, 0.398, 0.177)

    def tap_assets_positions_section_tab(self) -> None:
        self.tap_fast_point_or_ratio(FAST_ASSETS_POSITIONS_SECTION_TAB, 0.153, 0.177)
        time.sleep(0.3)

    def tap_assets_todays_orders_tab(self) -> None:
        self.tap_fast_point_or_ratio(FAST_ASSETS_TODAYS_ORDERS_TAB, 0.213, 0.230)

    def tap_assets_first_order_row(self) -> None:
        self.tap_fast_point_or_ratio(FAST_ASSETS_FIRST_ORDER_ROW, 0.500, 0.314)

    def tap_assets_order_cancel_button(self) -> None:
        self.tap_fast_point_or_ratio(FAST_ASSETS_ORDER_CANCEL_BUTTON, 0.381, 0.360)

    def tap_cancel_order_confirm_button(self) -> None:
        self.tap_fast_point_or_ratio(FAST_CANCEL_ORDER_CONFIRM_BUTTON, 0.690, 0.559)

    def cancel_first_visible_pending_order(self, dry_run: bool = False) -> bool:
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            nodes = []
        cancel_button = self.find_visible_order_cancel_button(nodes)
        if not cancel_button:
            row = self.find_first_pending_order_row(nodes)
            if row:
                self.adb.tap_node(row)
            else:
                self.tap_assets_first_order_row()
            time.sleep(0.3)
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                nodes = []
            cancel_button = self.find_visible_order_cancel_button(nodes)
        if cancel_button:
            self.adb.tap_node(cancel_button)
        elif self.has_visible_pending_order(nodes):
            self.tap_assets_order_cancel_button()
        else:
            return False
        time.sleep(0.45)
        return self.confirm_cancel_order_dialog(dry_run=dry_run)

    def find_first_pending_order_row(self, nodes: list[UiNode]) -> Optional[UiNode]:
        order_name = first_by_id(nodes, ":id/tvProductName")
        if order_name:
            row = self.clickable_container_for(order_name, nodes)
            if row:
                return row
        pending = first_by_id(nodes, ":id/tvOrderStateDesc")
        if pending and pending.text.strip().lower() == "pending":
            row = self.clickable_container_for(pending, nodes)
            if row:
                return row
        return None

    def has_visible_pending_order(self, nodes: list[UiNode]) -> bool:
        state = first_by_id(nodes, ":id/tvOrderStateDesc")
        if state and state.text.strip().lower() == "pending":
            return True
        return bool(first_by_id(nodes, ":id/tvProductName"))

    def find_visible_order_cancel_button(self, nodes: list[UiNode]) -> Optional[UiNode]:
        _, height = self.adb.wm_size()
        min_y = round(height * 0.68)
        max_y = round(height * 0.92)
        icon_cancel = first_by_id(nodes, ":id/fvOrderCancel")
        if icon_cancel and icon_cancel.clickable:
            return icon_cancel
        for node in nodes:
            text = self.node_label(node).strip().lower()
            if text != "cancel":
                continue
            _, cy = node.bounds.center
            if node.clickable and min_y <= cy <= max_y:
                return node
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            if text != "cancel":
                continue
            _, cy = text_node.bounds.center
            if not (min_y <= cy <= max_y):
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                return parent
            return text_node
        return None

    def confirm_cancel_order_dialog(self, dry_run: bool = False) -> bool:
        deadline = time.monotonic() + min(self.wait_timeout, 4.0)
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                time.sleep(FAST_POLL)
                continue
            if self.looks_cancel_order_confirmation(nodes):
                if dry_run:
                    self.dismiss_cancel_order_dialog(nodes)
                    return True
                confirm = self.find_cancel_order_confirm_button(nodes)
                if confirm:
                    self.adb.tap_node(confirm)
                else:
                    self.tap_cancel_order_confirm_button()
                return True
            if self.looks_error(nodes):
                visible = [node.text for node in nodes if node.text]
                raise ZhuoruiAutomationError(f"The app reported a cancellation error: {visible[:12]}")
            time.sleep(FAST_POLL)
        return False

    def dismiss_cancel_order_dialog(self, nodes: list[UiNode]) -> None:
        _, height = self.adb.wm_size()
        min_y = round(height * 0.35)
        max_y = round(height * 0.70)
        for node in nodes:
            text = self.node_label(node).strip().lower()
            _, cy = node.bounds.center
            if node.clickable and text == "cancel" and min_y <= cy <= max_y:
                self.adb.tap_node(node)
                time.sleep(SHORT_SETTLE)
                return
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            _, cy = text_node.bounds.center
            if text != "cancel" or not (min_y <= cy <= max_y):
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                self.adb.tap_node(parent)
            else:
                self.adb.tap_node(text_node)
            time.sleep(SHORT_SETTLE)
            return
        self.adb.keyevent(4)
        time.sleep(SHORT_SETTLE)

    def looks_cancel_order_confirmation(self, nodes: list[UiNode]) -> bool:
        for node in nodes:
            text = self.node_label(node).strip().lower()
            if text == "cancel order" or "are you sure to cancel this order" in text:
                return True
        return False

    def find_cancel_order_confirm_button(self, nodes: list[UiNode]) -> Optional[UiNode]:
        _, height = self.adb.wm_size()
        min_y = round(height * 0.35)
        max_y = round(height * 0.70)
        for node in nodes:
            text = self.node_label(node).strip().lower()
            _, cy = node.bounds.center
            if node.clickable and text == "confirm" and min_y <= cy <= max_y:
                return node
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            _, cy = text_node.bounds.center
            if text != "confirm" or not (min_y <= cy <= max_y):
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                return parent
            return text_node
        return None

    def collect_cash_positions(self, nodes: Optional[list[UiNode]] = None) -> list[dict[str, str]]:
        nodes = nodes or self.current_nodes()
        cash_ids = {
            "HKD": ":id/tvHKDCash",
            "USD": ":id/tvUSDCash",
            "CNH": ":id/tvCNHCash",
        }
        cash: list[dict[str, str]] = []
        for currency, resource_suffix in cash_ids.items():
            node = first_by_id(nodes, resource_suffix)
            if node:
                cash.append({"currency": currency, "amount": node.text})
        if not cash:
            visible = [node.text for node in nodes if node.text]
            raise ZhuoruiAutomationError(f"Cash balances were not found. Visible text: {visible[:12]}")
        return cash

    def collect_security_positions(self, nodes: Optional[list[UiNode]] = None) -> list[dict[str, str]]:
        self.ensure_positions_visible(nodes)
        nodes = self.current_nodes()
        if self.empty_positions_visible(nodes):
            return []

        positions: dict[tuple[str, str], dict[str, str]] = {}
        previous_signature: Optional[tuple[tuple[str, str], ...]] = None

        with tempfile.TemporaryDirectory(prefix="zhuorui-positions-") as temp_name:
            temp_dir = Path(temp_name)
            ocr = NumericOcr.from_adb(self.adb, temp_dir)
            screenshot_path = temp_dir / "positions.png"

            for _ in range(POSITION_TABLE_MAX_SCROLLS):
                nodes = self.current_nodes()
                self.adb.screenshot(screenshot_path)
                visible_positions = self.extract_visible_security_positions(nodes, screenshot_path, ocr)
                for position in visible_positions:
                    positions[(position["market"], position["symbol"])] = position

                signature = tuple((row["market"], row["symbol"]) for row in visible_positions)
                if previous_signature == signature:
                    break
                previous_signature = signature
                if not visible_positions:
                    self.scroll_assets_content()
                    time.sleep(0.35)
                    continue

                self.scroll_positions_table(nodes)
                time.sleep(0.35)

        return list(positions.values())

    def ensure_positions_visible(self, nodes: Optional[list[UiNode]] = None) -> None:
        nodes = nodes or self.open_assets()
        if nodes_by_id(nodes, ":id/tvStockCode") or self.empty_positions_visible(nodes):
            return
        for _ in range(4):
            self.scroll_assets_content()
            time.sleep(0.35)
            nodes = self.current_nodes()
            if nodes_by_id(nodes, ":id/tvStockCode") or self.empty_positions_visible(nodes):
                return
        visible = [node.text for node in nodes if node.text]
        raise ZhuoruiAutomationError(f"Positions table rows were not found. Visible text: {visible[:12]}")

    def empty_positions_visible(self, nodes: Iterable[UiNode]) -> bool:
        expected = EMPTY_POSITIONS_LABEL.casefold()
        return any(node.text.strip().casefold() == expected for node in nodes if node.text)

    def extract_visible_security_positions(
        self,
        nodes: list[UiNode],
        screenshot_path: Path,
        ocr: NumericOcr,
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for code_node in nodes_by_id(nodes, ":id/tvStockCode"):
            code_parts = code_node.text.split(maxsplit=1)
            if len(code_parts) != 2:
                continue
            market, symbol = code_parts[0].upper(), code_parts[1].upper()
            row = self.position_row_for(code_node, nodes)
            if row is None:
                continue
            name = self.node_in_bounds(nodes, ":id/tvStockName", row.bounds)
            quantity_cell = self.node_in_bounds(nodes, ":id/mvAndBuyNumber", row.bounds)
            average_cost_cell = self.node_in_bounds(nodes, ":id/lastAndBuyPrice", row.bounds)
            if quantity_cell is None or average_cost_cell is None:
                continue
            quantity = ocr.recognize_lower_line(screenshot_path, quantity_cell.bounds)
            average_cost = ocr.recognize_lower_line(screenshot_path, average_cost_cell.bounds)
            rows.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "name": name.text if name else "",
                    "quantity": quantity,
                    "average_cost": average_cost,
                }
            )
        return rows

    def position_row_for(self, child: UiNode, nodes: list[UiNode]) -> Optional[UiNode]:
        cx, cy = child.bounds.center
        candidates = [
            node
            for node in nodes_by_id(nodes, ":id/parent_view")
            if node.bounds.left <= cx <= node.bounds.right and node.bounds.top <= cy <= node.bounds.bottom
        ]
        candidates.sort(key=lambda node: node.bounds.width * node.bounds.height)
        return candidates[0] if candidates else None

    def node_in_bounds(self, nodes: list[UiNode], resource_suffix: str, bounds: Bounds) -> Optional[UiNode]:
        candidates = []
        for node in nodes_by_id(nodes, resource_suffix):
            cx, cy = node.bounds.center
            if bounds.left <= cx <= bounds.right and bounds.top <= cy <= bounds.bottom:
                candidates.append(node)
        candidates.sort(key=lambda node: node.bounds.left)
        return candidates[0] if candidates else None

    def scroll_positions_table(self, nodes: list[UiNode]) -> None:
        table = first_by_id(nodes, ":id/myPositionRecyclerView")
        width, height = self.adb.wm_size()
        if table:
            x = min(max(table.bounds.center[0], 1), width - 1)
            y1 = min(table.bounds.bottom - 70, height - 150)
            y2 = max(table.bounds.top + 140, 250)
        else:
            x = width // 2
            y1 = round(height * 0.88)
            y2 = round(height * 0.73)
        self.adb.swipe(x, y1, x, y2, 450)

    def scroll_assets_content(self) -> None:
        width, height = self.adb.wm_size()
        self.adb.swipe(width // 2, round(height * 0.84), width // 2, round(height * 0.55), 450)

    def scroll_assets_toward_top(self) -> None:
        width, height = self.adb.wm_size()
        self.adb.swipe(width // 2, round(height * 0.30), width // 2, round(height * 0.88), 650)

    def open_symbol_from_watchlist(self, symbol: str) -> Optional[Decimal]:
        self.return_to_watchlist_landing()
        with tempfile.TemporaryDirectory(prefix="zhuorui-watchlist-") as temp_name:
            temp_dir = Path(temp_name)
            screenshot_path = temp_dir / "watchlist.png"
            matcher = WatchlistSymbolMatcher.from_adb(self.adb, temp_dir)
            price_ocr = NumericOcr(matcher.font_path)
            for attempt in range(2):
                self.tap_quotes_tab_fast()
                time.sleep(0.25 if attempt == 0 else 0.5)
                self.adb.screenshot(screenshot_path)
                match = matcher.find_symbol(screenshot_path, symbol, price_ocr=price_ocr)
                if match:
                    x, y = match.tap_point
                    if self.fast_path:
                        price_note = (
                            f"; last price {decimal_to_input_text(match.last_price)}"
                            if match.last_price is not None
                            else ""
                        )
                        print(
                            f"Watchlist OCR matched {symbol.upper()} at score {match.score:.2f}{price_note}; tapping row.",
                            file=sys.stderr,
                        )
                    self.adb.tap(x, y)
                    time.sleep(0.8)
                    return match.last_price
            raise ZhuoruiAutomationError(
                f"{symbol.upper()} was not found in the visible watchlist. "
                "Add it to the watchlist and keep it visible without scrolling."
            )

    def return_to_watchlist_landing(self) -> None:
        self.return_to_home_screen_by_back(
            max_presses=ORDER_WATCHLIST_BACK_TAPS,
            delay=ORDER_WATCHLIST_BACK_DELAY,
            screen_name="Zhuorui's main watchlist screen",
        )

    def read_quote_last_price(self) -> Optional[Decimal]:
        with tempfile.TemporaryDirectory(prefix="zhuorui-quote-price-") as temp_name:
            temp_dir = Path(temp_name)
            screenshot_path = temp_dir / "quote.png"
            ocr = NumericOcr.from_adb(self.adb, temp_dir)
            self.adb.screenshot(screenshot_path)
            image = ocr.Image.open(screenshot_path).convert("RGB")
            width, height = image.size
            boxes = [
                (
                    round(width * 0.03),
                    round(height * 0.15),
                    round(width * 0.38),
                    round(height * 0.25),
                ),
                (
                    round(width * 0.03),
                    round(height * 0.16),
                    round(width * 0.35),
                    round(height * 0.23),
                ),
            ]
            for box in boxes:
                price_text = ocr.recognize_crop(image.crop(box))
                if "." not in price_text:
                    continue
                price = parse_decimal_text(price_text)
                if price is not None and price > 0:
                    return price
        return None

    def screenshot_shows_navigation_drawer(self) -> bool:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            screenshot_path = Path(tmp.name)
        try:
            self.adb.screenshot(screenshot_path)
            return self.image_shows_navigation_drawer(screenshot_path)
        except ZhuoruiAutomationError:
            return False
        finally:
            try:
                screenshot_path.unlink()
            except FileNotFoundError:
                pass

    def image_shows_navigation_drawer(self, screenshot_path: Path) -> bool:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ZhuoruiAutomationError(
                "Pillow is required for screenshot-based drawer detection."
            ) from exc

        image = Image.open(screenshot_path).convert("RGB")
        width, height = image.size
        left_panel = image.crop((0, round(height * 0.05), round(width * 0.72), round(height * 0.90)))
        right_scrim = image.crop((round(width * 0.80), round(height * 0.05), width, round(height * 0.90)))
        left_average, left_white, _ = self.image_region_stats(left_panel)
        right_average, _, right_dark = self.image_region_stats(right_scrim)
        return left_average > 210 and left_white > 0.55 and (right_average < 170 or right_dark > 0.30)

    def wait_for_quote_page(self) -> None:
        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            self.dismiss_known_dialogs()
            if self.is_quote_page(nodes):
                return
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError("Timed out waiting for the quote page.")

    def open_trade_sheet(self, trade_password: Optional[str] = None) -> None:
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            if self.maybe_enter_trading_password_from_screenshot(trade_password):
                time.sleep(0.6)
                nodes = self.current_nodes()
            else:
                raise
        if self.nodes_show_trading_password_prompt(nodes):
            self.maybe_enter_trading_password_from_screenshot(trade_password, nodes=nodes)
            time.sleep(0.6)
            nodes = self.current_nodes()
        if first_by_id(nodes, ":id/layoutBuy") and first_by_id(nodes, ":id/layoutSell"):
            return
        if first_by_id(nodes, ":id/sbTrade"):
            return

        if not self.is_quote_page(nodes):
            raise ZhuoruiAutomationError("Not on a quote page; cannot open Trade sheet.")

        width, height = self.adb.wm_size()
        self.adb.tap(round(width * 0.835), round(height * 0.943))
        deadline = time.monotonic() + self.wait_timeout
        entered_trade_password = False
        trade_password_entered_at: Optional[float] = None
        retried_after_password = False
        time.sleep(0.25)
        if self.maybe_enter_trading_password_from_screenshot(trade_password):
            entered_trade_password = True
            trade_password_entered_at = time.monotonic()
            time.sleep(0.6)
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                if not entered_trade_password and self.maybe_enter_trading_password_from_screenshot(trade_password):
                    entered_trade_password = True
                    trade_password_entered_at = time.monotonic()
                    time.sleep(0.6)
                    continue
                if (
                    entered_trade_password
                    and trade_password_entered_at is not None
                    and time.monotonic() - trade_password_entered_at < 3
                ):
                    time.sleep(FAST_POLL)
                    continue
                if not trade_password and not entered_trade_password:
                    raise ZhuoruiAutomationError(
                        "Zhuorui is asking for the trading password before opening the Trade sheet. "
                        "Add trade_password to zhuorui_config.json."
                    )
                raise
            if first_by_id(nodes, ":id/layoutBuy") and first_by_id(nodes, ":id/layoutSell"):
                return
            if self.nodes_show_trading_password_prompt(nodes):
                self.maybe_enter_trading_password_from_screenshot(trade_password, nodes=nodes)
                entered_trade_password = True
                trade_password_entered_at = time.monotonic()
                time.sleep(0.6)
                continue
            if entered_trade_password and not retried_after_password and self.is_quote_page(nodes):
                self.adb.tap(round(width * 0.835), round(height * 0.943))
                retried_after_password = True
                time.sleep(SHORT_SETTLE)
                continue
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError("Trade sheet did not open.")

    def try_fast_choose_side(self, side: str, trade_password: Optional[str] = None) -> bool:
        if not self.is_fast_screen():
            return False

        self.tap_ratio(0.835, 0.943)
        time.sleep(0.25)
        if self.maybe_enter_trading_password_from_screenshot(trade_password):
            time.sleep(0.6)
            self.tap_ratio(0.835, 0.943)
            time.sleep(0.25)

        self.adb.tap(*FAST_TRADE_SHEET_SIDE_BUTTONS[side])
        time.sleep(0.5)
        return True

    def choose_side(self, side: str, trade_password: Optional[str]) -> None:
        if self.try_fast_choose_side(side, trade_password=trade_password):
            return

        self.open_trade_sheet(trade_password=trade_password)
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            if self.maybe_enter_trading_password_from_screenshot(trade_password):
                time.sleep(0.6)
                nodes = self.current_nodes()
            else:
                raise
        if self.nodes_show_trading_password_prompt(nodes):
            self.maybe_enter_trading_password_from_screenshot(trade_password, nodes=nodes)
            time.sleep(0.6)
            nodes = self.current_nodes()
        if first_by_id(nodes, ":id/sbTrade") and first_by_id(nodes, ":id/tvOrderType"):
            return
        target_id = ":id/layoutBuy" if side == "buy" else ":id/layoutSell"
        target = first_by_id(nodes, target_id)
        require(target is not None, f"{side.title()} entry not found on Trade sheet.")
        self.adb.tap_node(target)

        deadline = time.monotonic() + self.wait_timeout
        entered_trade_password = False
        trade_password_entered_at: Optional[float] = None
        retried_after_password = False
        time.sleep(0.25)
        if self.maybe_enter_trading_password_from_screenshot(trade_password):
            entered_trade_password = True
            trade_password_entered_at = time.monotonic()
            time.sleep(0.6)
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                if not entered_trade_password and self.maybe_enter_trading_password_from_screenshot(trade_password):
                    entered_trade_password = True
                    trade_password_entered_at = time.monotonic()
                    time.sleep(0.6)
                    continue
                if (
                    entered_trade_password
                    and trade_password_entered_at is not None
                    and time.monotonic() - trade_password_entered_at < 3
                ):
                    time.sleep(FAST_POLL)
                    continue
                if not trade_password and not entered_trade_password:
                    raise ZhuoruiAutomationError(
                        "Zhuorui is asking for the trading password before opening the order ticket. "
                        "Add trade_password to zhuorui_config.json."
                    )
                raise
            if first_by_id(nodes, ":id/sbTrade") and first_by_id(nodes, ":id/tvOrderType"):
                return
            if self.nodes_show_trading_password_prompt(nodes):
                self.maybe_enter_trading_password_from_screenshot(trade_password, nodes=nodes)
                entered_trade_password = True
                trade_password_entered_at = time.monotonic()
                time.sleep(0.6)
                continue
            if entered_trade_password and not retried_after_password:
                self.open_trade_sheet()
                nodes = self.current_nodes()
                target = first_by_id(nodes, target_id)
                require(target is not None, f"{side.title()} entry not found after trading password.")
                self.adb.tap_node(target)
                retried_after_password = True
                time.sleep(SHORT_SETTLE)
                continue
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(f"{side.title()} order ticket did not open.")

    def select_order_type(self, order_type_name: str) -> list[UiNode]:
        if self.try_fast_select_order_type(order_type_name):
            return []

        wanted = order_type_name.title()
        nodes = self.current_nodes()
        order_type = first_by_id(nodes, ":id/tvOrderType")
        require(order_type is not None, "Order type selector not found.")
        if order_type.text.lower() == wanted.lower():
            return nodes

        self.adb.tap_node(order_type)
        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            option = first_text(nodes, wanted)
            if option:
                self.adb.tap_node(option)
                time.sleep(SHORT_SETTLE)
                return self.current_nodes()
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(f"{wanted} order option was not found.")

    def try_fast_select_order_type(self, order_type_name: str) -> bool:
        if not self.is_fast_screen() or order_type_name != "limit":
            return False
        self.adb.tap(*FAST_TICKET_ORDER_TYPE_FIELD)
        time.sleep(0.18)
        self.adb.tap(*FAST_TICKET_ORDER_TYPE_OPTIONS[order_type_name])
        time.sleep(0.25)
        return True

    def price_input(self, nodes: list[UiNode]) -> Optional[UiNode]:
        return next(
            (
                node
                for node in nodes
                if node.klass == "android.widget.EditText"
                and node.hint.lower().startswith("please enter price")
            ),
            None,
        )

    def quantity_input(self, nodes: list[UiNode]) -> Optional[UiNode]:
        return next(
            (
                node
                for node in nodes
                if node.klass == "android.widget.EditText" and node.hint.lower().startswith("minimum")
            ),
            None,
        )

    def set_limit_price(self, price: Decimal, nodes: Optional[list[UiNode]] = None) -> None:
        price_text = decimal_to_input_text(price)
        if self.is_fast_screen():
            self.replace_text_at(*FAST_TICKET_PRICE_FIELD, price_text, clear_chars=16)
            self.press_keyboard_enter()
            return

        nodes = nodes or self.current_nodes()
        price_input = self.wait_for_ticket_input(self.price_input, "Limit price input not found.", nodes=nodes)
        self.replace_text(price_input, price_text, clear_chars=max(16, len(price_input.text) + 5))
        self.press_keyboard_enter()
        self.restore_order_ticket_position("price", price_input)

    def set_quantity(
        self,
        quantity: int,
        nodes: Optional[list[UiNode]] = None,
        enter_presses_after_input: int = 0,
    ) -> None:
        if self.is_fast_screen() and enter_presses_after_input > 0:
            self.replace_text_at(*FAST_TICKET_QUANTITY_FIELD, str(quantity), clear_chars=10)
            self.press_keyboard_enter(enter_presses_after_input, delay_between=0.5)
            return

        nodes = nodes or self.current_nodes()
        quantity_input = self.wait_for_ticket_input(self.quantity_input, "Quantity input not found.", nodes=nodes)
        self.replace_text(quantity_input, str(quantity), clear_chars=max(10, len(quantity_input.text) + 5))
        self.press_keyboard_enter(enter_presses_after_input, delay_between=0.5)
        self.restore_order_ticket_position("quantity", quantity_input)

    def wait_for_ticket_input(self, finder, message: str, nodes: Optional[list[UiNode]] = None) -> UiNode:
        found = finder(nodes) if nodes is not None else None
        if found:
            return found
        deadline = time.monotonic() + min(self.wait_timeout, 2.0)
        last_visible: list[str] = []
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            last_visible = [node.text for node in nodes if node.text]
            found = finder(nodes)
            if found:
                return found
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(f"{message} Visible text: {last_visible[:12]}")

    def replace_text(self, node: UiNode, text: str, clear_chars: int = 20) -> None:
        self.adb.tap_node(node)
        # Some Zhuorui fields animate after focus; typing too early can be dropped.
        time.sleep(FIELD_FOCUS_SETTLE)
        self.adb.keyevent(123, *([67] * clear_chars))  # MOVE_END, then DEL.
        if text:
            self.adb.input_text(text)
            time.sleep(0.1)

    def replace_text_at(self, x: int, y: int, text: str, clear_chars: int = 20) -> None:
        self.adb.tap(x, y)
        time.sleep(FIELD_FOCUS_SETTLE)
        self.adb.keyevent(123, *([67] * clear_chars))  # MOVE_END, then DEL.
        if text:
            self.adb.input_text(text)
            time.sleep(0.1)

    def press_keyboard_enter(self, count: int = 1, delay_between: float = 0.0) -> None:
        if count <= 0:
            return
        for index in range(count):
            self.adb.keyevent(KEYCODE_ENTER)
            if delay_between > 0 and index < count - 1:
                time.sleep(delay_between)
        time.sleep(SHORT_SETTLE)

    def focus_trade_password_boxes(self) -> None:
        width, height = self.adb.wm_size()
        self.adb.tap(round(width * 0.115), round(height * 0.862))
        time.sleep(PASSWORD_FOCUS_SETTLE)

    def enter_trade_password_blind(self, password: str) -> None:
        self.focus_trade_password_boxes()
        self.adb.input_key_text(password)

    def maybe_enter_trading_password_from_screenshot(
        self,
        password: Optional[str],
        nodes: Optional[list[UiNode]] = None,
    ) -> bool:
        if nodes is not None and self.nodes_show_trading_password_prompt(nodes):
            if not password:
                raise ZhuoruiAutomationError(
                    "Zhuorui is asking for the trading password. Add trade_password to "
                    "zhuorui_config.json, pass --password, or set ZHUORUI_TRADE_PASSWORD."
                )
            self.enter_trade_password_blind(password)
            return True

        if not self.screenshot_shows_trading_password_prompt():
            return False
        if not password:
            raise ZhuoruiAutomationError(
                "Zhuorui is asking for the trading password. Add trade_password to "
                "zhuorui_config.json, pass --password, or set ZHUORUI_TRADE_PASSWORD."
            )
        self.enter_trade_password_blind(password)
        return True

    def nodes_show_trading_password_prompt(self, nodes: list[UiNode]) -> bool:
        return any("trading password" in self.node_label(node).lower() for node in nodes)

    def screenshot_shows_trading_password_prompt(self) -> bool:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            screenshot_path = Path(tmp.name)
        try:
            self.adb.screenshot(screenshot_path)
            return self.image_shows_trading_password_prompt(screenshot_path)
        except ZhuoruiAutomationError:
            return False
        finally:
            try:
                screenshot_path.unlink()
            except FileNotFoundError:
                pass

    def image_shows_trading_password_prompt(self, screenshot_path: Path) -> bool:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ZhuoruiAutomationError(
                "Pillow is required for screenshot-based trading password detection."
            ) from exc

        image = Image.open(screenshot_path).convert("RGB")
        width, height = image.size
        upper = image.crop(
            (
                round(width * 0.10),
                round(height * 0.12),
                round(width * 0.90),
                round(height * 0.68),
            )
        )
        sheet = image.crop(
            (
                round(width * 0.05),
                round(height * 0.72),
                round(width * 0.95),
                round(height * 0.97),
            )
        )

        upper_average, _, upper_dark = self.image_region_stats(upper)
        sheet_average, sheet_white, _ = self.image_region_stats(sheet)
        return (
            (upper_average < 200 or upper_dark > 0.08)
            and sheet_average > 220
            and sheet_white > 0.55
            and self.image_has_password_boxes(image)
        )

    def image_has_password_boxes(self, image) -> bool:
        width, height = image.size
        x_start = round(width * 0.05)
        x_end = round(width * 0.95)
        y_start = round(height * 0.76)
        y_end = round(height * 0.92)
        min_column_pixels = max(8, round((y_end - y_start) * 0.15))

        columns: list[int] = []
        for x in range(x_start, x_end):
            gray_count = 0
            for y in range(y_start, y_end):
                red, green, blue = image.getpixel((x, y))
                if abs(red - green) < 10 and abs(green - blue) < 10 and 215 <= red <= 248:
                    gray_count += 1
            if gray_count >= min_column_pixels:
                columns.append(x)

        runs = contiguous_runs(columns, max_gap=2)
        box_runs = [
            run
            for run in runs
            if round(width * 0.045) <= run[1] - run[0] + 1 <= round(width * 0.14)
        ]
        return len(box_runs) >= 5

    def image_region_stats(self, image) -> tuple[float, float, float]:
        data = image.tobytes()
        pixel_count = max(1, len(data) // 3)
        brightness_total = 0
        white_count = 0
        dark_count = 0
        for index in range(0, len(data), 3):
            red = data[index]
            green = data[index + 1]
            blue = data[index + 2]
            brightness_total += (red + green + blue) / 3
            if red > 245 and green > 245 and blue > 245:
                white_count += 1
            if red < 110 and green < 110 and blue < 110:
                dark_count += 1
        return brightness_total / pixel_count, white_count / pixel_count, dark_count / pixel_count

    def restore_order_ticket_position(self, field_name: str, edited_input: UiNode) -> None:
        width, height = self.adb.wm_size()
        if self.wait_for_ready_submit_button(timeout=0.35):
            return
        if self.is_fast_screen():
            self.tap_left_of_ticket_input(edited_input, width, height)
            if self.wait_for_ready_submit_button(timeout=0.6):
                return

        label_id = {
            "price": ":id/tvOrderPriceTitle",
            "quantity": ":id/tvOrderNumberTitle",
        }[field_name]
        input_finder = {
            "price": self.price_input,
            "quantity": self.quantity_input,
        }[field_name]

        for _ in range(2):
            try:
                nodes = self.current_nodes()
                current_input = input_finder(nodes) or edited_input
                label = first_by_id(nodes, label_id)
                if label:
                    self.adb.tap_node(label)
                else:
                    self.tap_left_of_ticket_input(current_input, width, height)
            except ZhuoruiAutomationError:
                self.tap_left_of_ticket_input(edited_input, width, height)

            deadline = time.monotonic() + 0.9
            while time.monotonic() < deadline:
                try:
                    nodes = self.current_nodes()
                except ZhuoruiAutomationError:
                    time.sleep(FAST_POLL)
                    continue
                if self.ticket_submit_button_ready(nodes, height):
                    return
                time.sleep(FAST_POLL)

        try:
            nodes = self.current_nodes()
            close = first_by_id(nodes, ":id/imgClose")
            if close:
                # A blank spot in the ticket header dismisses the field shift
                # without hitting the backdrop, which closes the ticket.
                self.adb.tap(round(width * 0.14), close.bounds.center[1])
                time.sleep(SHORT_SETTLE)
                nodes = self.current_nodes()
                if self.ticket_submit_button_ready(nodes, height):
                    return
        except ZhuoruiAutomationError:
            pass

        self.adb.keyevent(4)
        deadline = time.monotonic() + min(self.wait_timeout, 2)
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                time.sleep(FAST_POLL)
                continue
            if self.ticket_submit_button_ready(nodes, height):
                return
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError("Order ticket did not restore after leaving the input field.")

    def tap_left_of_ticket_input(self, input_node: UiNode, width: int, height: int) -> None:
        _, input_y = input_node.bounds.center
        safe_y = min(max(input_y, round(height * 0.62)), round(height * 0.86))
        self.adb.tap(round(width * 0.10), safe_y)

    def wait_for_ready_submit_button(self, timeout: float) -> Optional[UiNode]:
        _, height = self.adb.wm_size()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                time.sleep(FAST_POLL)
                continue
            submit = first_by_id(nodes, ":id/sbTrade")
            if submit and self.ticket_submit_button_ready(nodes, height):
                return submit
            time.sleep(FAST_POLL)
        return None

    def ticket_submit_button_ready(self, nodes: list[UiNode], screen_height: int) -> bool:
        submit = first_by_id(nodes, ":id/sbTrade")
        order_type = first_by_id(nodes, ":id/tvOrderType")
        if not submit or not order_type or not submit.clickable:
            return False
        return submit.bounds.center[1] >= round(screen_height * 0.86)

    def prepare_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type_name: str,
        limit_price: Optional[Decimal],
        trade_password: Optional[str],
        assume_current_symbol: bool,
    ) -> None:
        self.prepared_submit = None
        self.prepared_order_type_name = None
        self.prepared_limit_price = None
        self.market_reference_price = None
        market_reference_price: Optional[Decimal] = None
        if not assume_current_symbol:
            market_reference_price = self.open_symbol_from_watchlist(symbol)
        else:
            nodes = self.current_nodes()
            if not self.is_quote_page(nodes):
                raise ZhuoruiAutomationError("--assume-current-symbol requires the desired quote page to be open.")

        effective_order_type_name = order_type_name
        effective_limit_price = limit_price
        if order_type_name == "market":
            market_reference_price = market_reference_price or self.read_quote_last_price()
            require(
                market_reference_price is not None,
                "Could not OCR the last traded price needed to synthesize a market order.",
            )
            effective_order_type_name = "limit"
            effective_limit_price = through_market_limit_price(market_reference_price, side)
            self.market_reference_price = market_reference_price

        self.prepared_order_type_name = effective_order_type_name
        self.prepared_limit_price = effective_limit_price
        self.choose_side(side, trade_password=trade_password)
        ticket_nodes = self.select_order_type(effective_order_type_name)
        if effective_order_type_name == "limit":
            require(effective_limit_price is not None, "Limit orders require --limit-price.")
            self.set_limit_price(effective_limit_price, nodes=ticket_nodes if self.is_fast_screen() else None)
        quantity_enter_presses = 3 if effective_order_type_name == "limit" else 0
        self.set_quantity(quantity, enter_presses_after_input=quantity_enter_presses)
        self.prepared_submit = self.verify_ticket_ready(
            side,
            quantity,
            effective_order_type_name,
            effective_limit_price,
        )

    def verify_ticket_ready(
        self,
        side: str,
        quantity: int,
        order_type_name: str,
        limit_price: Optional[Decimal],
    ) -> UiNode:
        if self.is_fast_screen() and order_type_name == "limit":
            return self.fast_submit_node()

        nodes = self.current_nodes()
        order_type = first_by_id(nodes, ":id/tvOrderType")
        submit = first_by_id(nodes, ":id/sbTrade")
        qty = self.quantity_input(nodes)
        require(
            order_type is not None and order_type.text.lower() == order_type_name,
            f"Order type is not {order_type_name.title()}.",
        )
        if order_type_name == "limit":
            price = self.price_input(nodes)
            require(
                price is not None and limit_price is not None and parse_decimal_text(price.text) == limit_price,
                f"Limit price is not set; UI shows {price.text if price else '<missing>'!r}.",
            )
        require(qty is not None and qty.text.replace(",", "") == str(quantity), "Quantity is not set.")
        _, height = self.adb.wm_size()
        require(
            submit is not None and self.ticket_submit_button_ready(nodes, height),
            f"Final {side} button is not restored to the tappable position.",
        )
        return submit

    def fast_submit_node(self) -> UiNode:
        x, y = FAST_TICKET_SUBMIT_BUTTON
        return UiNode(
            text="",
            hint="",
            content_desc="",
            resource_id=f"{PACKAGE}:id/sbTrade",
            klass="android.widget.Button",
            clickable=True,
            focusable=True,
            focused=False,
            password=False,
            bounds=Bounds(x - 1, y - 1, x + 1, y + 1),
        )

    def submit_prepared_order(self, password: Optional[str], dismiss_success: bool = True) -> None:
        if self.is_fast_screen():
            self.adb.tap(*FAST_TICKET_SUBMIT_BUTTON)
            self.handle_confirmation_flow(password=password, dismiss_success=dismiss_success)
            return

        submit = self.wait_for_ready_submit_button(timeout=1.5)
        require(submit is not None, "Prepared order submit button is not restored to the tappable position.")
        self.adb.tap_node(submit)
        self.handle_confirmation_flow(password=password, dismiss_success=dismiss_success)

    def submit_fill_or_kill_order(self, password: Optional[str], revoke_delay: float = FILL_OR_KILL_REVOKE_DELAY) -> None:
        self.submit_prepared_order(password=password, dismiss_success=False)
        time.sleep(revoke_delay)
        self.revoke_visible_order()

    def revoke_visible_order(self) -> None:
        deadline = time.monotonic() + self.wait_timeout
        last_visible: list[str] = []
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            last_visible = [self.node_label(node) for node in nodes if self.node_label(node)]
            revoke = self.find_revoke_button(nodes)
            if revoke:
                self.adb.tap_node(revoke)
                time.sleep(SHORT_SETTLE)
                self.handle_revoke_confirmation()
                return
            if self.looks_successful(nodes):
                self.tap_success_dialog_revoke()
                time.sleep(SHORT_SETTLE)
                self.handle_revoke_confirmation()
                return
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(f"Revoke button was not found after submission. Visible text: {last_visible[:16]}")

    def find_revoke_button(self, nodes: list[UiNode]) -> Optional[UiNode]:
        for node in nodes:
            text = self.node_label(node).strip().lower()
            if node.clickable and self.is_revoke_text(text):
                return node
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            if not self.is_revoke_text(text):
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                return parent
            return text_node
        return None

    def is_revoke_text(self, text: str) -> bool:
        return "revoke" in text or "withdraw" in text or text in {"cancel order", "cancel orders"}

    def tap_success_dialog_revoke(self) -> None:
        self.tap_ratio(SUCCESS_REVOKE_X_RATIO, SUCCESS_REVOKE_Y_RATIO)

    def node_label(self, node: UiNode) -> str:
        return node.text or node.content_desc

    def handle_revoke_confirmation(self) -> None:
        deadline = time.monotonic() + min(self.wait_timeout, 4.0)
        tapped_confirm = False
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            if self.looks_revoke_successful(nodes):
                return
            if self.looks_error(nodes):
                visible = [node.text for node in nodes if node.text]
                raise ZhuoruiAutomationError(f"The app reported a revoke error: {visible[:12]}")
            if not tapped_confirm and self.looks_revoke_confirmation(nodes):
                confirm = self.find_revoke_confirmation_button(nodes)
                if confirm:
                    self.adb.tap_node(confirm)
                    tapped_confirm = True
                    time.sleep(SHORT_SETTLE)
                    continue
            time.sleep(FAST_POLL)
        return

    def looks_revoke_confirmation(self, nodes: list[UiNode]) -> bool:
        confirmation_needles = [
            "confirm revoke",
            "confirm cancellation",
            "cancel order?",
            "revoke order?",
            "order cancellation",
            "are you sure",
        ]
        return any(any(needle in self.node_label(node).lower() for needle in confirmation_needles) for node in nodes)

    def find_revoke_confirmation_button(self, nodes: list[UiNode]) -> Optional[UiNode]:
        confirm_words = {"confirm", "ok", "yes", "revoke", "cancel order", "withdraw"}
        for node in nodes:
            text = self.node_label(node).strip().lower()
            if node.clickable and text in confirm_words:
                return node
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            if text not in confirm_words:
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                return parent
        return None

    def handle_confirmation_flow(self, password: Optional[str], dismiss_success: bool = True) -> None:
        password = password or os.environ.get("ZHUORUI_TRADE_PASSWORD")
        deadline = time.monotonic() + self.wait_timeout
        tapped_confirm = False
        entered_password = False
        time.sleep(0.2)
        if password and self.maybe_enter_trading_password_from_screenshot(password):
            entered_password = True
            time.sleep(SHORT_SETTLE)

        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                if not entered_password and self.maybe_enter_trading_password_from_screenshot(password):
                    entered_password = True
                    time.sleep(SHORT_SETTLE)
                    continue
                raise
            if self.artifact_dir:
                self.artifact_dir.mkdir(parents=True, exist_ok=True)
                self.adb.screenshot(self.artifact_dir / f"post-submit-{int(time.time())}.png")

            password_input = next(
                (
                    node
                    for node in nodes
                    if node.klass == "android.widget.EditText"
                    and (node.password or "password" in (node.hint + " " + node.text).lower())
                ),
                None,
            )
            if self.nodes_show_trading_password_prompt(nodes) and not entered_password:
                self.maybe_enter_trading_password_from_screenshot(password, nodes=nodes)
                entered_password = True
                time.sleep(SHORT_SETTLE)
                continue
            if password_input and not entered_password:
                if not password:
                    raise ZhuoruiAutomationError(
                        "Trade password is required by the app. Add trade_password to "
                        "zhuorui_config.json, pass --password, or set ZHUORUI_TRADE_PASSWORD."
                    )
                self.adb.tap_node(password_input)
                time.sleep(PASSWORD_FOCUS_SETTLE)
                self.adb.input_key_text(password)
                entered_password = True
                time.sleep(SHORT_SETTLE)
                continue

            if self.looks_successful(nodes):
                if dismiss_success:
                    self.dismiss_order_success_dialog(nodes)
                return

            if self.looks_error(nodes):
                visible = [node.text for node in nodes if node.text]
                raise ZhuoruiAutomationError(f"The app reported an order error: {visible[:12]}")

            confirm = self.find_confirmation_button(nodes)
            if confirm and not tapped_confirm:
                self.adb.tap_node(confirm)
                tapped_confirm = True
                time.sleep(SHORT_SETTLE)
                continue

            time.sleep(FAST_POLL)

        visible = [node.text for node in self.current_nodes() if node.text]
        raise ZhuoruiAutomationError(
            "Timed out after tapping the trade button. Inspect the emulator for final status. "
            f"Visible text: {visible[:16]}"
        )

    def dismiss_order_success_dialog(self, nodes: Optional[list[UiNode]] = None) -> None:
        nodes = nodes or self.current_nodes()
        deadline = time.monotonic() + min(self.wait_timeout, 3.0)
        tapped = False
        while time.monotonic() < deadline:
            if not self.looks_successful(nodes):
                return
            dismiss = self.find_success_dialog_dismiss_button(nodes)
            if dismiss and not tapped:
                self.adb.tap_node(dismiss)
                tapped = True
                time.sleep(SHORT_SETTLE)
            elif not tapped:
                close = first_by_id(nodes, ":id/imgClose")
                if close:
                    self.adb.tap_node(close)
                    tapped = True
                    time.sleep(SHORT_SETTLE)
                else:
                    self.adb.keyevent(4)
                    tapped = True
                    time.sleep(SHORT_SETTLE)
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                time.sleep(FAST_POLL)
        return

    def find_success_dialog_dismiss_button(self, nodes: list[UiNode]) -> Optional[UiNode]:
        dismiss_words = {
            "ok",
            "done",
            "close",
            "confirm",
            "got it",
            "i got it",
            "view order",
            "orders",
        }
        for node in nodes:
            text = self.node_label(node).strip().lower()
            if node.clickable and text in dismiss_words:
                return node
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            if text not in dismiss_words:
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                return parent
        return None

    def find_confirmation_button(self, nodes: list[UiNode]) -> Optional[UiNode]:
        confirm_words = {
            "confirm",
            "submit",
            "place order",
            "buy",
            "sell",
            "done",
            "ok",
        }
        for node in nodes:
            text = self.node_label(node).strip().lower()
            if node.clickable and text in confirm_words:
                return node
        # Text often sits inside a clickable parent in this app.
        for text_node in nodes:
            text = self.node_label(text_node).strip().lower()
            if text not in confirm_words:
                continue
            parent = self.clickable_container_for(text_node, nodes)
            if parent:
                return parent
        return None

    def clickable_container_for(self, child: UiNode, nodes: list[UiNode]) -> Optional[UiNode]:
        cx, cy = child.bounds.center
        candidates = [
            node
            for node in nodes
            if node.clickable
            and node.bounds.left <= cx <= node.bounds.right
            and node.bounds.top <= cy <= node.bounds.bottom
        ]
        candidates.sort(key=lambda node: node.bounds.width * node.bounds.height)
        return candidates[0] if candidates else None

    def looks_successful(self, nodes: list[UiNode]) -> bool:
        success_needles = [
            "submitted",
            "success",
            "order placed",
            "entrustment succeeded",
            "entrusted successfully",
        ]
        return any(any(needle in self.node_label(node).lower() for needle in success_needles) for node in nodes)

    def looks_revoke_successful(self, nodes: list[UiNode]) -> bool:
        success_needles = [
            "revoked",
            "cancelled",
            "canceled",
            "withdrawn",
            "cancel order submitted",
            "revoke submitted",
            "revocation submitted",
        ]
        return any(any(needle in self.node_label(node).lower() for needle in success_needles) for node in nodes)

    def looks_error(self, nodes: list[UiNode]) -> bool:
        error_needles = [
            "failed",
            "rejected",
            "insufficient",
            "not enough",
            "error",
            "invalid",
        ]
        return any(any(needle in self.node_label(node).lower() for needle in error_needles) for node in nodes)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("quantity must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("quantity must be a positive integer")
    return parsed


def positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value.replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise argparse.ArgumentTypeError("price must be a positive decimal") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("price must be a positive decimal")
    return parsed


def parse_decimal_text(value: str) -> Optional[Decimal]:
    try:
        return Decimal((value or "").replace(",", "").strip())
    except InvalidOperation:
        return None


def decimal_to_input_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def through_market_limit_price(last_price: Decimal, side: str) -> Decimal:
    multiplier = MARKET_BUY_LIMIT_MULTIPLIER if side == "buy" else MARKET_SELL_LIMIT_MULTIPLIER
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    price = (last_price * multiplier).quantize(MARKET_LIMIT_PRICE_QUANTUM, rounding=rounding)
    if price <= 0:
        raise ZhuoruiAutomationError(f"Computed market-order limit price is not positive: {price}")
    return price


@dataclass(frozen=True)
class KafkaConfig:
    bootstrap_servers: str
    command_topic: str
    holdings_topic: str
    order_status_topic: str
    group_id: str
    client_id: str
    server_id: str
    auto_offset_reset: str
    holdings_interval_seconds: float
    poll_seconds: float
    publish_order_status: bool = False


@dataclass(frozen=True)
class TradingCommand:
    command_id: str
    symbol: str
    side: str
    quantity: Optional[int]
    order_type: str
    limit_price: Optional[Decimal]
    notional_usd: Optional[Decimal] = None


@dataclass(frozen=True)
class CancelCommand:
    command_id: str


@dataclass
class AutomationRuntime:
    config: dict
    adb: "Adb"
    trader: "ZhuoruiTrader"
    trade_password: Optional[str]
    launch_app: bool


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


def load_kafka_config(args: argparse.Namespace, config: dict) -> KafkaConfig:
    kafka = nested_config(config, "kafka")
    server_id = (
        args.server_id
        or read_config_string(kafka, None, "server_id")
        or read_config_string(config, None, "server_id")
        or socket.gethostname()
    )
    bootstrap_servers = (
        args.kafka_bootstrap_servers
        or read_config_string(kafka, None, "bootstrap_servers")
        or read_config_string(kafka, None, "server")
        or read_config_string(config, None, "kafka_bootstrap_servers")
    )
    if not bootstrap_servers:
        raise ZhuoruiAutomationError(
            "Kafka bootstrap servers are required for server mode. "
            "Pass --kafka-bootstrap-servers or set kafka.bootstrap_servers in config."
        )
    return KafkaConfig(
        bootstrap_servers=bootstrap_servers,
        command_topic=(
            args.kafka_command_topic
            or read_config_string(kafka, DEFAULT_KAFKA_COMMAND_TOPIC, "command_topic")
            or DEFAULT_KAFKA_COMMAND_TOPIC
        ),
        holdings_topic=(
            args.kafka_holdings_topic
            or read_config_string(kafka, DEFAULT_KAFKA_HOLDINGS_TOPIC, "holdings_topic")
            or DEFAULT_KAFKA_HOLDINGS_TOPIC
        ),
        order_status_topic=(
            args.kafka_order_status_topic
            or read_config_string(kafka, DEFAULT_KAFKA_ORDER_STATUS_TOPIC, "order_status_topic")
            or DEFAULT_KAFKA_ORDER_STATUS_TOPIC
        ),
        group_id=(
            args.kafka_group_id
            or read_config_string(kafka, f"zhuorui-{server_id}", "group_id")
            or f"zhuorui-{server_id}"
        ),
        client_id=(
            args.kafka_client_id
            or read_config_string(kafka, f"zhuorui-{server_id}", "client_id")
            or f"zhuorui-{server_id}"
        ),
        server_id=server_id,
        auto_offset_reset=(
            args.kafka_auto_offset_reset
            or read_config_string(kafka, "latest", "auto_offset_reset")
            or "latest"
        ),
        holdings_interval_seconds=(
            args.holdings_interval
            if args.holdings_interval is not None
            else config_number(kafka, "holdings_interval_seconds", DEFAULT_HOLDINGS_INTERVAL_SECONDS)
        ),
        poll_seconds=(
            args.kafka_poll_seconds
            if args.kafka_poll_seconds is not None
            else config_number(kafka, "poll_seconds", DEFAULT_KAFKA_POLL_SECONDS)
        ),
        publish_order_status=config_bool(kafka, "publish_order_status", False),
    )


def build_automation_runtime(args: argparse.Namespace) -> AutomationRuntime:
    config = load_config(args.config.expanduser())
    adb_path = args.adb or config_string(config, "adb")
    device = args.device or config_string(config, "device")
    wait_timeout = args.wait_timeout if args.wait_timeout is not None else config_float(
        config, "wait_timeout", DEFAULT_WAIT_TIMEOUT
    )
    artifact_dir = args.artifact_dir or config_path(config, "artifact_dir")
    expected_screen_size = config_screen_size(config)
    fast_path = config_bool(config, "fast_path", True) and not args.no_fast_path
    launch_app = args.launch_app or config_bool(config, "launch_app", False)

    adb = Adb(adb_path=adb_path, device=device, verbose=args.verbose)
    if not args.no_stabilize_ui:
        adb.disable_animations()
    validate_screen_size(adb, expected_screen_size)
    trader = ZhuoruiTrader(
        adb,
        wait_timeout=wait_timeout,
        artifact_dir=artifact_dir,
        fast_path=fast_path,
        login_phone=config_login_phone(config),
        login_password=config_login_password(config),
    )
    trade_password = (
        getattr(args, "password", None)
        or config_trade_password(config)
        or os.environ.get("ZHUORUI_TRADE_PASSWORD")
    )
    return AutomationRuntime(
        config=config,
        adb=adb,
        trader=trader,
        trade_password=trade_password,
        launch_app=launch_app,
    )


def json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return decimal_to_input_text(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def kafka_value_serializer(value: dict) -> bytes:
    return json.dumps(value, default=json_default, separators=(",", ":")).encode("utf-8")


def kafka_value_deserializer(value: bytes) -> dict:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZhuoruiAutomationError(f"Kafka command value must be a JSON object: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ZhuoruiAutomationError("Kafka command value must be a JSON object.")
    return decoded


def command_text(payload: dict, *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def command_int(payload: dict, *keys: str) -> int:
    value = command_text(payload, *keys)
    if value is None:
        raise ZhuoruiAutomationError(f"Command is missing one of: {', '.join(keys)}")
    try:
        return positive_int(value.replace(",", ""))
    except argparse.ArgumentTypeError as exc:
        raise ZhuoruiAutomationError(str(exc)) from exc


def command_optional_int(payload: dict, *keys: str) -> Optional[int]:
    value = command_text(payload, *keys)
    if value is None:
        return None
    try:
        return positive_int(value.replace(",", ""))
    except argparse.ArgumentTypeError as exc:
        raise ZhuoruiAutomationError(str(exc)) from exc


def command_decimal(payload: dict, *keys: str) -> Optional[Decimal]:
    value = command_text(payload, *keys)
    if value is None:
        return None
    try:
        return positive_decimal(value)
    except argparse.ArgumentTypeError as exc:
        raise ZhuoruiAutomationError(str(exc)) from exc


def normalize_command_value(value: str) -> str:
    with_camel_breaks = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    return re.sub(r"[^A-Za-z0-9]+", "_", with_camel_breaks).strip("_").lower()


CANCEL_COMMAND_TYPES = {
    "cancel",
    "cancel_all",
    "cancel_order",
    "cancel_orders",
    "cancel_open_order",
    "cancel_open_orders",
    "cancel_pending_order",
    "cancel_pending_orders",
    "cancel_all_orders",
    "cancel_all_open_orders",
    "cancel_all_pending_order",
    "cancel_all_pending_orders",
}


def normalized_command_values(payload: dict) -> list[str]:
    values: list[str] = []
    for key in (
        "action",
        "cmd",
        "command",
        "type",
        "command_type",
        "commandType",
        "event",
        "event_type",
        "eventType",
        "order_type",
        "orderType",
        "request_type",
        "requestType",
    ):
        value = command_text(payload, key)
        if value:
            values.append(normalize_command_value(value))
    return values


def is_cancel_command_payload(payload: dict) -> bool:
    return any(value in CANCEL_COMMAND_TYPES for value in normalized_command_values(payload))


def normalize_cancel_command(payload: dict) -> CancelCommand:
    if not is_cancel_command_payload(payload):
        raise ZhuoruiAutomationError("Command is not a cancel command.")
    command_id = command_text(payload, "id", "command_id", "commandId", "order_id", "orderId") or str(time.time_ns())
    return CancelCommand(command_id=command_id)


def normalize_order_type(payload: dict) -> str:
    raw_order_type = command_text(payload, "order_type", "type", "orderType")
    raw_time_in_force = command_text(payload, "time_in_force", "tif", "timeInForce")
    order_type = normalize_command_value(raw_order_type or "market")
    time_in_force = normalize_command_value(raw_time_in_force or "")
    aliases = {
        "market": "market",
        "market_order": "market",
        "limit": "limit",
        "limit_order": "limit",
        "fok": "fok",
        "fill_or_kill": "fok",
        "limit_order_fok": "fok",
    }
    if order_type == "delayed_market_order":
        raise ZhuoruiAutomationError("DELAYED_MARKET_ORDER is not supported by the Zhuorui listener yet.")
    normalized = aliases.get(order_type)
    if normalized is None:
        raise ZhuoruiAutomationError(
            f"Unsupported KTrader command type: {raw_order_type!r}. "
            "Supported order commands are MARKET_ORDER, LIMIT_ORDER, and LIMIT_ORDER_FOK."
        )
    if normalized == "limit" and time_in_force in {"fok", "fill_or_kill"}:
        return "fok"
    return normalized


def is_supported_order_payload(payload: dict) -> bool:
    raw_order_type = command_text(payload, "order_type", "type", "orderType")
    if not raw_order_type:
        return True
    normalized = normalize_command_value(raw_order_type)
    if normalized in {
        "market",
        "market_order",
        "limit",
        "limit_order",
        "fok",
        "fill_or_kill",
        "limit_order_fok",
        "delayed_market_order",
    }:
        return True
    return False


def is_supported_command_payload(payload: dict) -> bool:
    return is_cancel_command_payload(payload) or is_supported_order_payload(payload)


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


def account_selector_matches(command_value: str, account_id: Optional[str], account_num_id: Optional[int]) -> bool:
    normalized = command_value.strip()
    if account_id and normalized == account_id:
        return True
    if account_num_id is not None and normalized == str(account_num_id):
        return True
    return False


def account_num_selector_matches(command_value: str, account_num_id: Optional[int]) -> bool:
    if account_num_id is None:
        return False
    try:
        return int(command_value.strip()) == account_num_id
    except ValueError:
        return False


def command_targets_configured_account(payload: dict, config: dict) -> bool:
    target_account_id = configured_account_id(config)
    target_account_num_id = configured_account_num_id(config)
    command_account_id = command_text(payload, "account_id", "accountId", "account")
    if command_account_id and not account_selector_matches(command_account_id, target_account_id, target_account_num_id):
        return False
    command_account_num_id = command_text(
        payload,
        "account_num_id",
        "accountNumId",
        "account_numeric_id",
        "accountNumericId",
        "account_num",
        "accountNum",
    )
    if command_account_num_id and not account_num_selector_matches(command_account_num_id, target_account_num_id):
        return False
    return True


def normalize_command(payload: dict) -> TradingCommand:
    action = (command_text(payload, "action", "command") or "order").lower().replace("-", "_")
    if action not in {"order", "place_order", "submit_order", "trade"}:
        raise ZhuoruiAutomationError(f"Unsupported command action: {action}")

    symbol = command_text(payload, "symbol", "ticker", "code")
    if not symbol:
        raise ZhuoruiAutomationError("Command is missing symbol.")
    symbol = symbol.upper()
    if not re.fullmatch(r"[A-Z0-9.=\-]{1,16}", symbol):
        raise ZhuoruiAutomationError("Command symbol must be 1-16 characters: letters, numbers, dot, dash, or equals")

    side = (command_text(payload, "side", "direction") or "").lower()
    if side not in {"buy", "sell"}:
        raise ZhuoruiAutomationError("Command side must be buy or sell.")

    order_type = normalize_order_type(payload)
    limit_price = command_decimal(payload, "limit_price", "price", "limitPrice", "limit")
    quantity = command_optional_int(payload, "qty_shares", "quantity", "qty", "shares")
    notional_usd = command_decimal(payload, "notional_usd", "notionalUsd", "dollar_amount", "dollars")
    if order_type in {"limit", "fok"} and limit_price is None:
        raise ZhuoruiAutomationError(f"{order_type.upper()} commands require limit_price.")
    if order_type in {"limit", "fok"} and quantity is None:
        raise ZhuoruiAutomationError(f"{order_type.upper()} commands require qty_shares.")
    if order_type == "market" and limit_price is not None:
        raise ZhuoruiAutomationError("Market commands cannot include limit_price.")
    if order_type == "market" and quantity is None and notional_usd is None:
        raise ZhuoruiAutomationError("MARKET_ORDER commands require qty_shares or notional_usd.")

    command_id = command_text(payload, "id", "command_id", "commandId", "order_id", "orderId") or str(time.time_ns())
    return TradingCommand(
        command_id=command_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        notional_usd=notional_usd,
    )


def status_event(
    kafka_config: KafkaConfig,
    command: Optional[TradingCommand | CancelCommand],
    status: str,
    message: str,
    extra: Optional[dict] = None,
) -> dict:
    event = {
        "server_id": kafka_config.server_id,
        "status": status,
        "message": message,
        "timestamp": time.time(),
    }
    if isinstance(command, CancelCommand):
        event.update(
            {
                "command_id": command.command_id,
                "order_type": "cancel",
            }
        )
    elif command:
        event.update(
            {
                "command_id": command.command_id,
                "symbol": command.symbol,
                "side": command.side,
                "order_type": command.order_type,
            }
        )
        if command.quantity is not None:
            event["quantity"] = command.quantity
        if command.notional_usd is not None:
            event["notional_usd"] = command.notional_usd
        if command.limit_price is not None:
            event["limit_price"] = command.limit_price
    if extra:
        event.update(extra)
    return event


def publish_status(
    producer: object,
    kafka_config: KafkaConfig,
    event: dict,
    key: str,
) -> None:
    if not kafka_config.publish_order_status or not kafka_config.order_status_topic:
        return
    try:
        producer.send(kafka_config.order_status_topic, event, key=key.encode("utf-8"))
        producer.flush()
    except Exception as exc:
        print(f"WARNING: could not publish order status event: {exc}", file=sys.stderr)


def command_log_summary(command: TradingCommand) -> str:
    parts = [
        f"id={command.command_id}",
        f"type={command.order_type}",
        f"side={command.side}",
        f"symbol={command.symbol}",
    ]
    if command.quantity is not None:
        parts.append(f"qty={command.quantity}")
    if command.notional_usd is not None:
        parts.append(f"notional_usd={decimal_to_input_text(command.notional_usd)}")
    if command.limit_price is not None:
        parts.append(f"limit_price={decimal_to_input_text(command.limit_price)}")
    return " ".join(parts)


def order_operation_name(order_type: str) -> str:
    names = {
        "market": "market order",
        "limit": "limit order",
        "fok": "fill-or-kill order",
    }
    return names.get(order_type, f"{order_type} order")


def elapsed_seconds(started_at: float) -> float:
    return time.monotonic() - started_at


def quantity_from_notional(notional_usd: Decimal, reference_price: Decimal) -> int:
    require(reference_price > 0, "Cannot resolve notional order quantity from a non-positive reference price.")
    quantity = int((notional_usd / reference_price).to_integral_value(rounding=ROUND_FLOOR))
    if quantity <= 0:
        raise ZhuoruiAutomationError(
            f"Notional order amount {decimal_to_input_text(notional_usd)} is too small "
            f"for reference price {decimal_to_input_text(reference_price)}."
        )
    return quantity


def submit_trading_command(trader: "ZhuoruiTrader", command: TradingCommand, trade_password: Optional[str]) -> dict:
    operation_name = order_operation_name(command.order_type)
    started_at = time.monotonic()
    succeeded = False
    if command.order_type == "fok":
        app_order_type = "limit"
        fill_or_kill = True
    else:
        app_order_type = command.order_type
        fill_or_kill = False

    try:
        trader.ensure_app_foreground(launch_if_needed=True)
        quantity = command.quantity
        assume_current_symbol = False
        notional_reference_price: Optional[Decimal] = None
        if quantity is None:
            require(
                command.order_type == "market" and command.notional_usd is not None,
                "Only MARKET_ORDER supports notional_usd without qty_shares.",
            )
            notional_reference_price = trader.open_symbol_from_watchlist(command.symbol) or trader.read_quote_last_price()
            require(
                notional_reference_price is not None,
                "Could not OCR the last traded price needed to convert notional_usd to qty_shares.",
            )
            quantity = quantity_from_notional(command.notional_usd, notional_reference_price)
            assume_current_symbol = True

        trader.prepare_order(
            symbol=command.symbol,
            side=command.side,
            quantity=quantity,
            order_type_name=app_order_type,
            limit_price=command.limit_price,
            trade_password=trade_password,
            assume_current_symbol=assume_current_symbol,
        )
        if fill_or_kill:
            trader.submit_fill_or_kill_order(password=trade_password)
        else:
            trader.submit_prepared_order(password=trade_password)

        result = {
            "prepared_order_type": trader.prepared_order_type_name,
            "resolved_quantity": quantity,
            "submitted_limit_price": trader.prepared_limit_price,
        }
        if notional_reference_price is not None:
            result["notional_reference_price"] = notional_reference_price
        if trader.market_reference_price is not None:
            result["market_reference_price"] = trader.market_reference_price
        succeeded = True
        return result
    finally:
        duration = elapsed_seconds(started_at)
        if succeeded:
            print(f"{operation_name.title()} completed in {duration:.3f} seconds.", flush=True)
        else:
            print(f"{operation_name.title()} failed after {duration:.3f} seconds.", file=sys.stderr, flush=True)


def submit_cancel_command(trader: "ZhuoruiTrader", command: CancelCommand, dry_run: bool = False) -> dict:
    started_at = time.monotonic()
    succeeded = False
    cancelled_orders = 0
    result: dict = {
        "cancelled_orders": 0,
        "dry_run": dry_run,
        "dry_run_confirmation_reached": False,
    }
    try:
        trader.ensure_app_foreground(launch_if_needed=True)
        result = trader.cancel_all_pending_orders(dry_run=dry_run)
        cancelled_orders = int(result.get("cancelled_orders", 0))
        succeeded = True
        return result
    finally:
        duration = elapsed_seconds(started_at)
        if succeeded:
            if dry_run:
                reached = bool(result.get("dry_run_confirmation_reached"))
                print(
                    f"Cancel orders dry run completed in {duration:.3f} seconds; "
                    f"confirmation_reached={reached}.",
                    flush=True,
                )
            else:
                print(
                    f"Cancel orders completed in {duration:.3f} seconds; "
                    f"cancelled {cancelled_orders} order(s).",
                    flush=True,
                )
        else:
            print(f"Cancel orders failed after {duration:.3f} seconds.", file=sys.stderr, flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decimal_text_to_float(value: object, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def account_snapshot_config(config: dict, kafka_config: KafkaConfig) -> tuple[str, int, bool]:
    account_id = configured_account_id(config)
    if not account_id:
        raise ZhuoruiAutomationError(
            "Config value account_id is required for KTrader account-details snapshots."
        )

    account_num_id = configured_account_num_id(config)
    if account_num_id is None:
        raise ZhuoruiAutomationError(
            "Config value account_num_id is required for KTrader account-details snapshots."
        )
    if account_num_id <= 0:
        raise ZhuoruiAutomationError("Config value account_num_id must be a positive integer.")

    trading_enabled = config_bool(config, "trading_enabled", True)
    account_config = nested_config(config, "account")
    if account_config:
        trading_enabled = config_bool(account_config, "trading_enabled", trading_enabled)
    return account_id, account_num_id, trading_enabled


def ktrader_account_snapshot(config: dict, kafka_config: KafkaConfig, holdings: dict) -> dict:
    account_id, account_num_id, trading_enabled = account_snapshot_config(config, kafka_config)
    cash_by_currency = {
        str(row.get("currency", "")).strip().upper(): decimal_text_to_float(row.get("amount"))
        for row in holdings.get("cash", [])
        if str(row.get("currency", "")).strip()
    }
    usd_cash = cash_by_currency.get("USD", 0.0)

    positions = []
    for row in holdings.get("securities", []):
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        qty = decimal_text_to_float(row.get("quantity"))
        if qty == 0.0:
            continue
        positions.append(
            {
                "symbol": symbol,
                "qty": qty,
                "avg_price": decimal_text_to_float(row.get("average_cost")),
            }
        )

    return {
        "account_id": account_id,
        "account_num_id": account_num_id,
        "cash": usd_cash,
        "cash_by_currency": cash_by_currency,
        "positions": positions,
        "ts": utc_now_iso(),
        "trading_enabled": trading_enabled,
    }


def publish_holdings(
    producer: object,
    topic: str,
    kafka_config: KafkaConfig,
    config: dict,
    trader: "ZhuoruiTrader",
) -> None:
    started_at = time.monotonic()
    holdings_collected = False
    try:
        trader.ensure_app_foreground(launch_if_needed=True)
        holdings = trader.collect_positions()
        holdings_collected = True
    finally:
        duration = elapsed_seconds(started_at)
        if holdings_collected:
            print(f"Holdings query completed in {duration:.3f} seconds.", flush=True)
        else:
            print(f"Holdings query failed after {duration:.3f} seconds.", file=sys.stderr, flush=True)
    snapshot = ktrader_account_snapshot(config, kafka_config, holdings)
    producer.send(
        topic,
        snapshot,
        key=snapshot["account_id"].encode("utf-8"),
    )
    producer.flush()
    print("Published account details message.")


def run_trading_server(args: argparse.Namespace, runtime: AutomationRuntime) -> int:
    kafka_config = load_kafka_config(args, runtime.config)
    account_snapshot_config(runtime.config, kafka_config)
    try:
        from kafka import KafkaConsumer, KafkaProducer
    except ImportError as exc:
        raise ZhuoruiAutomationError(
            "Server mode requires the kafka-python package. Install it with: pip install kafka-python"
        ) from exc

    producer = KafkaProducer(
        bootstrap_servers=kafka_config.bootstrap_servers,
        client_id=kafka_config.client_id,
        value_serializer=kafka_value_serializer,
    )
    consumer = KafkaConsumer(
        kafka_config.command_topic,
        bootstrap_servers=kafka_config.bootstrap_servers,
        client_id=kafka_config.client_id,
        group_id=kafka_config.group_id,
        auto_offset_reset=kafka_config.auto_offset_reset,
        enable_auto_commit=True,
    )

    print(
        f"Zhuorui trading server {kafka_config.server_id} consuming {kafka_config.command_topic} "
        f"and publishing holdings to {kafka_config.holdings_topic}."
    )
    next_holdings_at = 0.0
    try:
        while True:
            now = time.monotonic()
            if now >= next_holdings_at:
                try:
                    publish_holdings(
                        producer,
                        kafka_config.holdings_topic,
                        kafka_config,
                        runtime.config,
                        runtime.trader,
                    )
                    next_holdings_at = now + kafka_config.holdings_interval_seconds
                except ZhuoruiAutomationError as exc:
                    print(f"ERROR publishing holdings: {exc}", file=sys.stderr, flush=True)
                    publish_status(
                        producer,
                        kafka_config,
                        status_event(kafka_config, None, "holdings_error", str(exc)),
                        key=kafka_config.server_id,
                    )
                    next_holdings_at = now + kafka_config.holdings_interval_seconds

            records = consumer.poll(timeout_ms=max(1, int(kafka_config.poll_seconds * 1000)), max_records=1)
            for messages in records.values():
                for message in messages:
                    command: Optional[TradingCommand | CancelCommand] = None
                    try:
                        payload = kafka_value_deserializer(message.value)
                        if not command_targets_configured_account(payload, runtime.config):
                            continue
                        if not is_supported_command_payload(payload):
                            continue
                        if is_cancel_command_payload(payload):
                            command = normalize_cancel_command(payload)
                            print(f"Received cancel command: id={command.command_id}", flush=True)
                            publish_status(
                                producer,
                                kafka_config,
                                status_event(kafka_config, command, "accepted", "Command accepted."),
                                key=command.command_id,
                            )
                            result = submit_cancel_command(runtime.trader, command)
                            print(f"Completed cancel command: {command.command_id}", flush=True)
                            publish_status(
                                producer,
                                kafka_config,
                                status_event(kafka_config, command, "submitted", "Cancel command completed.", result),
                                key=command.command_id,
                            )
                            next_holdings_at = 0.0
                            continue

                        command = normalize_command(payload)
                        print(f"Received trading command: {command_log_summary(command)}", flush=True)
                        publish_status(
                            producer,
                            kafka_config,
                            status_event(kafka_config, command, "accepted", "Command accepted."),
                            key=command.command_id,
                        )
                        result = submit_trading_command(runtime.trader, command, runtime.trade_password)
                        print(f"Submitted trading command: {command.command_id}", flush=True)
                        publish_status(
                            producer,
                            kafka_config,
                            status_event(kafka_config, command, "submitted", "Order submitted.", result),
                            key=command.command_id,
                        )
                        next_holdings_at = 0.0
                    except ZhuoruiAutomationError as exc:
                        command_id = command.command_id if command else "<unparsed>"
                        print(f"ERROR processing trading command {command_id}: {exc}", file=sys.stderr, flush=True)
                        publish_status(
                            producer,
                            kafka_config,
                            status_event(kafka_config, command, "error", str(exc)),
                            key=command.command_id if command else kafka_config.server_id,
                        )
    except KeyboardInterrupt:
        print("Stopping Zhuorui trading server.")
        return 0
    finally:
        consumer.close()
        producer.flush()
        producer.close()


def add_common_automation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="JSON config file; defaults to zhuorui_config.json, then config.json",
    )
    parser.add_argument("--adb", help="path to adb.exe")
    parser.add_argument("--device", help="adb device serial, e.g. emulator-5554")
    parser.add_argument("--wait-timeout", type=float, help="seconds to wait for each UI transition")
    parser.add_argument("--artifact-dir", type=Path, help="optional directory for post-submit screenshots")
    parser.add_argument(
        "--no-stabilize-ui",
        action="store_true",
        help="do not disable Android animation scales before automation",
    )
    parser.add_argument(
        "--no-fast-path",
        action="store_true",
        help="disable the coordinate hot path and use XML navigation throughout",
    )
    parser.add_argument(
        "--launch-app",
        action="store_true",
        help="launch Zhuorui before automation; otherwise use the current app if it is already foreground",
    )
    parser.add_argument("--verbose", action="store_true", help="print adb commands")


def parse_positions_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} positions",
        description="Collect Zhuorui cash and securities positions from the Android emulator UI.",
    )
    add_common_automation_args(parser)
    parser.add_argument(
        "--compact-json",
        action="store_true",
        help="print positions JSON on one line",
    )
    args = parser.parse_args(argv)
    args.command = "positions"
    return args


def add_kafka_server_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server-id", help="trading server id included in Kafka events")
    parser.add_argument("--kafka-bootstrap-servers", help="Kafka bootstrap servers, e.g. 127.0.0.1:9092")
    parser.add_argument("--kafka-command-topic", help="Kafka topic to consume trading commands from")
    parser.add_argument("--kafka-holdings-topic", help="Kafka topic to publish cash and positions to")
    parser.add_argument("--kafka-order-status-topic", help="Kafka topic to publish order status events to")
    parser.add_argument("--kafka-group-id", help="Kafka consumer group id")
    parser.add_argument("--kafka-client-id", help="Kafka client id")
    parser.add_argument(
        "--kafka-auto-offset-reset",
        choices=["earliest", "latest"],
        help="where to start if the consumer group has no committed offset",
    )
    parser.add_argument(
        "--holdings-interval",
        type=float,
        help="seconds between periodic holdings publications",
    )
    parser.add_argument(
        "--kafka-poll-seconds",
        type=float,
        help="seconds to wait for commands between server loop ticks",
    )


def parse_server_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} server",
        description="Run Zhuorui as a long-running Kafka trading server.",
    )
    add_common_automation_args(parser)
    add_kafka_server_args(parser)
    parser.add_argument("--password", help="trade password override; normally read from config")
    args = parser.parse_args(argv)
    args.command = "server"
    return args


def parse_cancel_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} cancel",
        description="Cancel all visible pending Zhuorui orders once through the Android emulator UI.",
    )
    add_common_automation_args(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="navigate to the cancel confirmation, then dismiss it without confirming",
    )
    args = parser.parse_args(argv)
    args.command = "cancel"
    return args


def parse_args(argv: list[str]) -> argparse.Namespace:
    if argv and argv[0] in {"positions", "get-positions"}:
        return parse_positions_args(argv[1:])
    if argv and argv[0] in {"server", "serve", "trading-server"}:
        return parse_server_args(argv[1:])
    if argv and argv[0] in {"cancel", "cancel-orders", "cancel-open-orders"}:
        return parse_cancel_args(argv[1:])
    if "--server" in argv:
        server_argv = [value for value in argv if value != "--server"]
        return parse_server_args(server_argv)

    parser = argparse.ArgumentParser(
        description="Prepare or submit a Zhuorui order through the Android emulator UI."
    )
    parser.add_argument("symbol", help="US stock symbol, e.g. BILI")
    parser.add_argument("side", choices=["buy", "sell"], help="order side")
    parser.add_argument("quantity", type=positive_int, help="share quantity")
    parser.add_argument(
        "--order-type",
        choices=["market", "limit"],
        default="market",
        help="order type to prepare; market is submitted as a 5%% through-market limit order",
    )
    parser.add_argument(
        "--limit-price",
        "--price",
        "--limit",
        dest="limit_price",
        type=positive_decimal,
        help="limit price; required when --order-type limit",
    )
    add_common_automation_args(parser)
    parser.add_argument(
        "--assume-current-symbol",
        action="store_true",
        help="skip watchlist navigation and use the quote page currently open in the app",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="tap the final order button after preparing the order; also requires --confirm-live-order",
    )
    parser.add_argument(
        "--confirm-live-order",
        action="store_true",
        help="submit the prepared order live; kept as the required acknowledgement for real orders",
    )
    parser.add_argument(
        "--fill-or-kill",
        action="store_true",
        help="submit a live limit order, wait 3 seconds, then tap Revoke",
    )
    parser.add_argument(
        "--revoke-delay",
        type=float,
        default=FILL_OR_KILL_REVOKE_DELAY,
        help="seconds to wait before tapping Revoke with --fill-or-kill; defaults to 3",
    )
    parser.add_argument("--password", help="trade password override; normally read from config")
    args = parser.parse_args(argv)
    args.command = "order"
    if args.confirm_live_order:
        args.live = True
    return args


def validate_args(args: argparse.Namespace) -> None:
    if getattr(args, "command", "order") == "positions":
        return
    if getattr(args, "command", "order") == "cancel":
        return
    if getattr(args, "command", "order") == "server":
        if args.holdings_interval is not None and args.holdings_interval <= 0:
            raise ZhuoruiAutomationError("--holdings-interval must be greater than zero")
        if args.kafka_poll_seconds is not None and args.kafka_poll_seconds <= 0:
            raise ZhuoruiAutomationError("--kafka-poll-seconds must be greater than zero")
        return
    if not re.fullmatch(r"[A-Za-z0-9.=\-]{1,16}", args.symbol):
        raise ZhuoruiAutomationError("symbol must be 1-16 characters: letters, numbers, dot, dash, or equals")
    if args.order_type == "limit" and args.limit_price is None:
        raise ZhuoruiAutomationError("--order-type limit requires --limit-price")
    if args.order_type == "market" and args.limit_price is not None:
        raise ZhuoruiAutomationError("--limit-price can only be used with --order-type limit")
    if args.live and not args.confirm_live_order:
        raise ZhuoruiAutomationError("--live requires --confirm-live-order")
    if args.fill_or_kill:
        if args.order_type != "limit" or args.limit_price is None:
            raise ZhuoruiAutomationError("--fill-or-kill requires --order-type limit and --limit-price")
        if not args.confirm_live_order:
            raise ZhuoruiAutomationError("--fill-or-kill submits a live order and requires --confirm-live-order")
        if args.revoke_delay < 0:
            raise ZhuoruiAutomationError("--revoke-delay must be zero or greater")


def validate_screen_size(adb: Adb, expected: Optional[tuple[int, int]]) -> None:
    if not expected:
        return
    actual = adb.wm_size()
    if actual != expected:
        raise ZhuoruiAutomationError(
            f"Configured screen_size is {expected[0]}x{expected[1]}, "
            f"but the emulator reports {actual[0]}x{actual[1]}. "
            "Switch the emulator resolution or update zhuorui_config.json."
        )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        runtime = build_automation_runtime(args)

        if args.command == "server":
            return run_trading_server(args, runtime)

        if args.command == "positions":
            runtime.trader.ensure_app_foreground(launch_if_needed=True)
            positions = runtime.trader.collect_positions()
            if args.compact_json:
                print(json.dumps(positions, separators=(",", ":")))
            else:
                print(json.dumps(positions, indent=2))
            return 0

        if args.command == "cancel":
            result = submit_cancel_command(
                runtime.trader,
                CancelCommand(command_id="one-shot-cancel"),
                dry_run=args.dry_run,
            )
            if args.dry_run:
                print(f"Dry run confirmation reached: {result['dry_run_confirmation_reached']}.")
            else:
                print(f"Cancelled {result['cancelled_orders']} order(s).")
            return 0

        runtime.trader.ensure_app_foreground(launch_if_needed=runtime.launch_app or not args.assume_current_symbol)
        runtime.trader.prepare_order(
            symbol=args.symbol.upper(),
            side=args.side,
            quantity=args.quantity,
            order_type_name=args.order_type,
            limit_price=args.limit_price,
            trade_password=runtime.trade_password,
            assume_current_symbol=args.assume_current_symbol,
        )

        order_summary = f"{args.order_type.upper()} {args.side.upper()} {args.quantity} {args.symbol.upper()}"
        if args.order_type == "market":
            require(
                runtime.trader.prepared_limit_price is not None,
                "Synthesized market-order limit price was not recorded.",
            )
            order_summary += f" as LIMIT @ {decimal_to_input_text(runtime.trader.prepared_limit_price)}"
            if runtime.trader.market_reference_price is not None:
                order_summary += f" from last {decimal_to_input_text(runtime.trader.market_reference_price)}"
        elif args.order_type == "limit":
            order_summary += f" @ {decimal_to_input_text(args.limit_price)}"

        if not args.live and not args.fill_or_kill:
            print(
                f"Dry run complete: prepared {order_summary}. "
                "The final trade button was not tapped."
            )
            return 0

        if args.fill_or_kill:
            runtime.trader.submit_fill_or_kill_order(password=runtime.trade_password, revoke_delay=args.revoke_delay)
            print(f"Submitted {order_summary}, waited {args.revoke_delay:g}s, and tapped Revoke.")
            return 0

        runtime.trader.submit_prepared_order(password=runtime.trade_password)
        print(f"Submitted {order_summary}.")
        return 0
    except ZhuoruiAutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
