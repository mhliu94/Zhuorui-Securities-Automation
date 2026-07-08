#!/usr/bin/env python3
"""
Submit Zhuorui orders through the Android emulator UI.

Default behavior is a dry run: the script prepares the order ticket but does not
tap the final trade button. Live submission requires both --live and
--confirm-live-order.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional


PACKAGE = "com.zhuorui.securities"
LAUNCH_ACTIVITY = f"{PACKAGE}/.ui.SplashActivity"
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
QUOTES_TAB_X_RATIO = 1 / 12
ASSETS_TAB_X_RATIO = 0.25
BOTTOM_TAB_Y_RATIO = 0.943
TOP_SEARCH_X_RATIO = 0.85
TOP_SEARCH_Y_RATIO = 0.0825
APP_BACK_X_RATIO = 0.063
APP_BACK_Y_RATIO = 0.082
SUCCESS_REVOKE_X_RATIO = 0.29
SUCCESS_REVOKE_Y_RATIO = 0.895
POSITION_TABLE_MAX_SCROLLS = 8
POSITION_LANDING_BACK_TAPS = 5
POSITION_LANDING_BACK_DELAY = 1.0
FILL_OR_KILL_REVOKE_DELAY = 3.0
ANDROID_ROBOTO_FONT = "/system/fonts/Roboto-Regular.ttf"

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


class ZhuoruiTrader:
    def __init__(
        self,
        adb: Adb,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        artifact_dir: Optional[Path] = None,
        fast_path: bool = True,
    ):
        self.adb = adb
        self.wait_timeout = wait_timeout
        self.artifact_dir = artifact_dir
        self.fast_path = fast_path
        self.prepared_submit: Optional[UiNode] = None

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

    def open_symbol_search(self) -> list[UiNode]:
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            return self.open_search_from_visible_top_bar()
        if first_by_id(nodes, ":id/searchView"):
            return nodes
        if self.is_main_landing_page(nodes):
            return self.open_search_from_landing_page()

        # If a trade sheet or menu is open, close it first.
        for _ in range(3):
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                return self.open_search_from_visible_top_bar()
            if first_by_id(nodes, ":id/searchView"):
                return nodes
            if self.dismiss_transient_overlay(nodes):
                continue
            if first_by_id(nodes, ":id/imgClose"):
                self.adb.tap_node(first_by_id(nodes, ":id/imgClose"))  # type: ignore[arg-type]
                time.sleep(SHORT_SETTLE)
                continue
            if first_by_id(nodes, ":id/sbTrade") or first_by_id(nodes, ":id/recyclerView"):
                self.adb.keyevent(4)
                time.sleep(SHORT_SETTLE)
                continue
            break

        # On a quote page opened from search, the top-left app back button
        # returns directly to the search result screen.
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            return self.open_search_from_visible_top_bar()
        if self.is_quote_page(nodes):
            width, height = self.adb.wm_size()
            self.adb.tap(round(width * 0.06), round(height * 0.052))
            deadline = time.monotonic() + min(self.wait_timeout, 2.5)
            while time.monotonic() < deadline:
                try:
                    nodes = self.current_nodes()
                except ZhuoruiAutomationError:
                    time.sleep(FAST_POLL)
                    continue
                if first_by_id(nodes, ":id/searchView"):
                    return nodes
                if not self.is_quote_page(nodes):
                    break
                time.sleep(FAST_POLL)
            else:
                self.adb.keyevent(4)
                time.sleep(SHORT_SETTLE)

            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                return self.open_search_from_visible_top_bar()
            if first_by_id(nodes, ":id/searchView"):
                return nodes
            if self.is_quote_page(nodes):
                raise ZhuoruiAutomationError(
                    "Could not leave Zhuorui's quote page to reach symbol search."
                )

        # Some Android builds route KEYCODE_SEARCH to the in-app search box.
        self.adb.keyevent(84)
        time.sleep(SHORT_SETTLE)
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            return self.open_search_from_visible_top_bar()
        if first_by_id(nodes, ":id/searchView"):
            return nodes

        return self.open_search_from_visible_top_bar()

    def open_search_from_landing_page(self) -> list[UiNode]:
        self.tap_ratio(QUOTES_TAB_X_RATIO, BOTTOM_TAB_Y_RATIO)
        time.sleep(0.2)
        return self.open_search_from_visible_top_bar(timeout=min(self.wait_timeout, 2.0))

    def open_search_from_visible_top_bar(self, timeout: Optional[float] = None) -> list[UiNode]:
        wait_seconds = timeout if timeout is not None else self.wait_timeout
        self.tap_ratio(TOP_SEARCH_X_RATIO, TOP_SEARCH_Y_RATIO)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                time.sleep(FAST_POLL)
                continue
            if first_by_id(nodes, ":id/searchView"):
                return nodes
            time.sleep(FAST_POLL)

        raise ZhuoruiAutomationError(
            "Could not reach Zhuorui's symbol search screen. Open the app search screen manually, "
            "then run the script again, or use --assume-current-symbol from the desired quote page."
        )

    def is_quote_page(self, nodes: list[UiNode]) -> bool:
        return bool(first_by_id(nodes, ":id/tvSubTitle")) and not first_by_id(nodes, ":id/searchView")

    def is_fast_screen(self) -> bool:
        return self.fast_path and self.adb.wm_size() == FAST_SCREEN_SIZE

    def tap_ratio(self, x_ratio: float, y_ratio: float) -> None:
        width, height = self.adb.wm_size()
        self.adb.tap(round(width * x_ratio), round(height * y_ratio))

    def try_fast_search_symbol_from_landing(self, symbol: str) -> bool:
        if not self.fast_path:
            return False
        try:
            nodes = self.current_nodes()
        except ZhuoruiAutomationError:
            return False
        if not self.is_main_landing_page(nodes) or self.is_quote_page(nodes):
            return False
        self.tap_ratio(QUOTES_TAB_X_RATIO, BOTTOM_TAB_Y_RATIO)
        time.sleep(0.15)
        self.tap_ratio(TOP_SEARCH_X_RATIO, TOP_SEARCH_Y_RATIO)
        time.sleep(0.45)
        self.adb.input_text(symbol.upper())
        time.sleep(SHORT_SETTLE)
        return True

    def tap_app_back_button(self) -> None:
        self.tap_ratio(APP_BACK_X_RATIO, APP_BACK_Y_RATIO)

    def is_main_landing_page(self, nodes: list[UiNode]) -> bool:
        bottom_bar = first_by_id(nodes, ":id/bottomBar")
        if bottom_bar:
            return True
        bottom_tabs = {"Quotes", "Assets", "S-Invest", "Wealth", "News", "Me"}
        return len({node.text for node in nodes if node.text in bottom_tabs}) >= 3

    def return_to_landing_page(self, max_taps: int = POSITION_LANDING_BACK_TAPS) -> None:
        for _ in range(max_taps):
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                nodes = []
            if nodes and self.is_main_landing_page(nodes):
                return
            self.tap_app_back_button()
            time.sleep(POSITION_LANDING_BACK_DELAY)

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
        self.return_to_landing_page()
        assets_nodes = self.open_assets()
        securities = self.collect_security_positions(assets_nodes)
        cash_nodes = self.open_cash_details()
        cash = self.collect_cash_positions(cash_nodes)
        return {"cash": cash, "securities": securities}

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
        if nodes_by_id(nodes, ":id/tvStockCode"):
            return
        for _ in range(4):
            self.scroll_assets_content()
            time.sleep(0.35)
            nodes = self.current_nodes()
            if nodes_by_id(nodes, ":id/tvStockCode"):
                return
        visible = [node.text for node in nodes if node.text]
        raise ZhuoruiAutomationError(f"Positions table rows were not found. Visible text: {visible[:12]}")

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
        self.adb.swipe(width // 2, round(height * 0.45), width // 2, round(height * 0.82), 450)

    def search_symbol(self, symbol: str) -> None:
        if self.try_fast_search_symbol_from_landing(symbol):
            return
        nodes = self.open_symbol_search()
        search = first_by_id(nodes, ":id/searchView")
        require(search is not None, "Search field not found.")
        self.replace_text(search, symbol.upper(), clear_chars=max(20, len(search.text) + 5))
        time.sleep(SHORT_SETTLE)

    def select_symbol_result(self, symbol: str) -> None:
        target = f"US {symbol.upper()}"
        deadline = time.monotonic() + self.wait_timeout
        last_symbols: list[str] = []
        while time.monotonic() < deadline:
            nodes = self.current_nodes()
            last_symbols = [node.text for node in nodes if re.fullmatch(r"[A-Z]{2} .+", node.text)]
            for code_node in nodes:
                if code_node.text.upper() == target:
                    row = self.row_container_for(code_node, nodes)
                    self.adb.tap_node(row or code_node)
                    self.wait_for_quote_page()
                    return
            time.sleep(FAST_POLL)
        raise ZhuoruiAutomationError(
            f"Could not find exact US symbol result {target!r}. Visible symbol rows: {last_symbols[:8]}"
        )

    def row_container_for(self, child: UiNode, nodes: list[UiNode]) -> Optional[UiNode]:
        child_center_y = child.bounds.center[1]
        rows = [
            node
            for node in nodes
            if node.resource_id.endswith(":id/rl_stock")
            and node.bounds.top <= child_center_y <= node.bounds.bottom
        ]
        return rows[0] if rows else None

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

    def try_fast_choose_side(self, side: str) -> bool:
        if not self.is_fast_screen():
            return False

        self.tap_ratio(0.835, 0.943)
        time.sleep(0.2)

        target_id = ":id/layoutBuy" if side == "buy" else ":id/layoutSell"
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                return False
            if first_by_id(nodes, ":id/sbTrade") and first_by_id(nodes, ":id/tvOrderType"):
                return True
            target = first_by_id(nodes, target_id)
            if target:
                self.adb.tap_node(target)
                time.sleep(0.25)
                break
            time.sleep(FAST_POLL)
        else:
            return False

        deadline = time.monotonic() + 0.9
        while time.monotonic() < deadline:
            try:
                nodes = self.current_nodes()
            except ZhuoruiAutomationError:
                return False
            if first_by_id(nodes, ":id/sbTrade") and first_by_id(nodes, ":id/tvOrderType"):
                return True
            time.sleep(FAST_POLL)
        return False

    def choose_side(self, side: str, trade_password: Optional[str]) -> None:
        if self.try_fast_choose_side(side):
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
        nodes = nodes or self.current_nodes()
        price_input = self.price_input(nodes)
        require(price_input is not None, "Limit price input not found.")
        price_text = decimal_to_input_text(price)
        self.replace_text(price_input, price_text, clear_chars=max(16, len(price_input.text) + 5))
        self.restore_order_ticket_position("price", price_input)

    def set_quantity(self, quantity: int, nodes: Optional[list[UiNode]] = None) -> None:
        nodes = nodes or self.current_nodes()
        quantity_input = self.quantity_input(nodes)
        require(quantity_input is not None, "Quantity input not found.")
        self.replace_text(quantity_input, str(quantity), clear_chars=max(10, len(quantity_input.text) + 5))
        self.restore_order_ticket_position("quantity", quantity_input)

    def replace_text(self, node: UiNode, text: str, clear_chars: int = 20) -> None:
        self.adb.tap_node(node)
        # Some Zhuorui fields animate after focus; typing too early can be dropped.
        time.sleep(FIELD_FOCUS_SETTLE)
        self.adb.keyevent(123, *([67] * clear_chars))  # MOVE_END, then DEL.
        if text:
            self.adb.input_text(text)
            time.sleep(0.1)

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
        if self.is_fast_screen():
            self.tap_left_of_ticket_input(edited_input, width, height)
            time.sleep(SHORT_SETTLE)
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
        if not assume_current_symbol:
            self.search_symbol(symbol)
            self.select_symbol_result(symbol)
        else:
            nodes = self.current_nodes()
            if not self.is_quote_page(nodes):
                raise ZhuoruiAutomationError("--assume-current-symbol requires the desired quote page to be open.")

        self.choose_side(side, trade_password=trade_password)
        ticket_nodes = self.select_order_type(order_type_name)
        if order_type_name == "limit":
            require(limit_price is not None, "Limit orders require --limit-price.")
            self.set_limit_price(limit_price, nodes=ticket_nodes if self.is_fast_screen() else None)
        self.set_quantity(quantity)
        self.prepared_submit = self.verify_ticket_ready(side, quantity, order_type_name, limit_price)

    def verify_ticket_ready(
        self,
        side: str,
        quantity: int,
        order_type_name: str,
        limit_price: Optional[Decimal],
    ) -> UiNode:
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
        require(submit is not None and submit.clickable, f"Final {side} button is not available.")
        return submit

    def submit_prepared_order(self, password: Optional[str], dismiss_success: bool = True) -> None:
        submit = self.prepared_submit
        if submit is None:
            nodes = self.current_nodes()
            submit = first_by_id(nodes, ":id/sbTrade")
        require(submit is not None, "Prepared order submit button not found.")
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    if argv and argv[0] in {"positions", "get-positions"}:
        return parse_positions_args(argv[1:])

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
        help="order type to prepare; defaults to market",
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
        help="skip search and use the quote page currently open in the app",
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
        )

        if args.command == "positions":
            trader.ensure_app_foreground(launch_if_needed=True)
            positions = trader.collect_positions()
            if args.compact_json:
                print(json.dumps(positions, separators=(",", ":")))
            else:
                print(json.dumps(positions, indent=2))
            return 0

        trade_password = (
            args.password
            or config_trade_password(config)
            or os.environ.get("ZHUORUI_TRADE_PASSWORD")
        )

        trader.ensure_app_foreground(launch_if_needed=launch_app or not args.assume_current_symbol)
        trader.prepare_order(
            symbol=args.symbol.upper(),
            side=args.side,
            quantity=args.quantity,
            order_type_name=args.order_type,
            limit_price=args.limit_price,
            trade_password=trade_password,
            assume_current_symbol=args.assume_current_symbol,
        )

        order_summary = f"{args.order_type.upper()} {args.side.upper()} {args.quantity} {args.symbol.upper()}"
        if args.order_type == "limit":
            order_summary += f" @ {decimal_to_input_text(args.limit_price)}"

        if not args.live and not args.fill_or_kill:
            print(
                f"Dry run complete: prepared {order_summary}. "
                "The final trade button was not tapped."
            )
            return 0

        if args.fill_or_kill:
            trader.submit_fill_or_kill_order(password=trade_password, revoke_delay=args.revoke_delay)
            print(f"Submitted {order_summary}, waited {args.revoke_delay:g}s, and tapped Revoke.")
            return 0

        trader.submit_prepared_order(password=trade_password)
        print(f"Submitted {order_summary}.")
        return 0
    except ZhuoruiAutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
