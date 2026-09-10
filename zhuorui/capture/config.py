"""Resolve capture paths and identities without touching an emulator."""
import ipaddress
import json
import os
import re
from pathlib import Path

from zhuorui.common.config import load_config, nested_config, ZhuoruiAutomationError
from zhuorui.paths import PROJECT_ROOT


def read_ini(path):
    if not path.is_file():
        return {}
    return {k.strip(): v.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
            if not line.lstrip().startswith(("#", ";"))
            for k, sep, v in [line.partition("=")] if sep}


def load_capture_settings(config_path, *, require_identity=True):
    """Return only non-secret, resolved settings. Explicit paths are config-relative."""
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise ZhuoruiAutomationError("Configuration file missing.")
    config = load_config(config_path)
    cap = nested_config(config, "capture")

    def path(value, default, label):
        value = default if value in (None, "") else value
        if not isinstance(value, (str, Path)) or any(c in str(value) for c in '\r\n\0"'):
            raise ZhuoruiAutomationError(f"{label} must be a valid path.")
        p = Path(value).expanduser()
        return p.resolve() if p.is_absolute() else (config_path.parent / p).resolve()

    def cap_path(key, default):
        return path(cap.get(key), default, "capture." + key)

    def name(value, label, allow_empty=False):
        if allow_empty and value in (None, ""):
            return ""
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
            raise ZhuoruiAutomationError(f"{label} must be an emulator name using letters, digits, _, - or .")
        return value

    def number(key, default, low, high):
        value = cap.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ZhuoruiAutomationError(f"capture.{key} must be an integer from {low} to {high}.")
        return value

    sdk_default = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
                       or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Android/Sdk"))
    adb = path(config.get("adb"), sdk_default / "platform-tools/adb.exe", "adb")
    sdk = cap_path("sdk_root", adb.parent.parent)
    private = cap_path("private_dir", PROJECT_ROOT / "api_research/private")
    avd = name(config.get("avd"), "avd", allow_empty=not require_identity)
    device = config.get("device") or ""
    match = re.fullmatch(r"emulator-(\d+)", device) if isinstance(device, str) else None
    if not match and (device or require_identity):
        raise ZhuoruiAutomationError("device must be an emulator serial, for example emulator-5554.")
    port = int(match[1]) if match else None
    if port is not None and (port % 2 or not 5554 <= port <= 5682):
        raise ZhuoruiAutomationError("Emulator console port must be even, from 5554 to 5682.")
    avd_home = cap_path("avd_home", Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android/avd"))
    avd_ini = read_ini(avd_home / f"{avd}.ini") if avd else {}
    avd_dir = cap_path("original_avd_dir", avd_ini.get("path") or avd_home / f"{avd}.avd")
    if avd_ini.get("path") and avd_dir != Path(avd_ini["path"]).resolve():
        raise ZhuoruiAutomationError("capture.original_avd_dir differs from the AVD registered in capture.avd_home.")
    source_config = read_ini(avd_dir / "config.ini")
    image_value = source_config.get("image.sysdir.1")
    image_default = (sdk / image_value).resolve() if image_value else None
    image_dir = cap_path("system_image_dir", image_default) if (cap.get("system_image_dir") or image_default) else None
    if image_default and image_dir != image_default:
        raise ZhuoruiAutomationError("capture.system_image_dir differs from the original AVD's registered system image.")
    test_default = image_dir.parent.parent / "google_apis" / image_dir.name if image_dir else None
    test_image = cap_path("test_system_image_dir", test_default) if (cap.get("test_system_image_dir") or test_default) else None
    test_avd = name(cap.get("test_avd", "ZhuoruiCapture"), "capture.test_avd")
    boot_avd = name(cap.get("boot_test_avd", "ZhuoruiBootTest"), "capture.boot_test_avd")
    test_port = number("test_port", 5580, 5554, 5682)
    boot_port = number("boot_test_port", 5582, 5554, 5682)
    if test_port % 2 or boot_port % 2 or len({p for p in (port, test_port, boot_port) if p is not None}) != (3 if port else 2):
        raise ZhuoruiAutomationError("Original, capture-test and boot-test emulator ports must be distinct and even.")
    if len({n for n in (avd, test_avd, boot_avd) if n}) != (3 if avd else 2):
        raise ZhuoruiAutomationError("Original, capture-test and boot-test AVD names must be distinct.")
    host = cap.get("proxy_listen_host", "127.0.0.1")
    emulator_host = cap.get("emulator_proxy_host", "10.0.2.2")
    try:
        if not ipaddress.IPv4Address(host).is_loopback:
            raise ValueError()
        ipaddress.IPv4Address(emulator_host)
    except (ValueError, TypeError):
        raise ZhuoruiAutomationError("Use a loopback IPv4 proxy_listen_host and an IPv4 emulator_proxy_host.") from None
    proxy_port = number("proxy_port", 8082, 1024, 65535)
    if proxy_port in {p + offset for p in (port, test_port, boot_port) if p for offset in (0, 1)}:
        raise ZhuoruiAutomationError("Proxy port conflicts with an emulator console/ADB port.")
    result = dict(config_path=config_path, adb=adb, sdk_root=sdk, device=device, avd=avd, port=port,
        emulator_executable=cap_path("emulator_executable", sdk / "emulator/emulator.exe"),
        private_dir=private, avd_home=avd_home, original_avd_dir=avd_dir,
        system_image_dir=image_dir, test_system_image_dir=test_image,
        debug_policy_file=cap_path("debug_policy_file", private / "userdebug_plat_sepolicy.cil"),
        debug_ramdisk_file=cap_path("debug_ramdisk_file", private / "capture-debug-ramdisk.img"),
        python_executable=cap_path("python_executable", PROJECT_ROOT / "api_research/.venv/Scripts/python.exe"),
        mitmdump_executable=cap_path("mitmdump_executable", PROJECT_ROOT / "api_research/.venv/Scripts/mitmdump.exe"),
        proxy_listen_host=host, proxy_port=proxy_port, emulator_proxy_host=emulator_host,
        emulator_proxy=f"{emulator_host}:{proxy_port}",
        test_avd=test_avd, test_port=test_port, test_device=f"emulator-{test_port}",
        boot_test_avd=boot_avd, boot_test_port=boot_port, boot_test_device=f"emulator-{boot_port}",
        boot_timeout_seconds=number("boot_timeout_seconds", 180, 10, 1800))
    return {k: str(v) if isinstance(v, Path) else v for k, v in result.items()}


def verify_boot_evidence(settings):
    import hashlib
    private = Path(settings["private_dir"])
    evidence = json.loads((private / "debug-ramdisk-evidence.json").read_text(encoding="utf-8-sig"))
    if not evidence.get("boot_test_passed") or not evidence.get("certificate_mount_passed"):
        raise ZhuoruiAutomationError("Temporary boot and certificate mount have not been validated.")
    if not settings["system_image_dir"]:
        raise ZhuoruiAutomationError("Configure capture.system_image_dir or the original AVD path first.")
    source = Path(settings["system_image_dir"]) / "ramdisk.img"
    if Path(evidence["source"]).resolve() != source.resolve():
        raise ZhuoruiAutomationError("Boot evidence belongs to a different system image.")
    for file, field in [(source, "source_sha256"), (Path(settings["debug_ramdisk_file"]), "output_sha256"),
                        (Path(settings["debug_policy_file"]), "policy_sha256")]:
        if hashlib.sha256(file.read_bytes()).hexdigest() != evidence.get(field):
            raise ZhuoruiAutomationError("Boot evidence no longer matches the configured image or policy.")
