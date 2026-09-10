"""Read the existing emulator session. Never log in, root, restart or click the app."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from zhuorui.capture.config import load_capture_settings
from .errors import SessionError
from . import mmkv

PACKAGE = "com.zhuorui.securities"
APP_ROOT = "/data/data/" + PACKAGE
# Exact build whose headers, storage and signatures were verified against traffic.
SUPPORTED_APK_SHA256 = "66ccf60d7e92c1fc97e86c0f9fa0544c3de1df480c5689627d4f1c90aba69b93"
SUPPORTED_SIGNING_SPKI_SHA256 = "d2c340d4e78a7b15a23b7a85dae582f87e5201616538bdb6ef237491f44589d8"
ACCOUNT_KEY = PACKAGE + ".personal.config.LocalAccountConfig"
LANGUAGE_KEY = PACKAGE + ".base2app.language.AppLanguageConfig"


class Adb:
    def __init__(self, executable, device=None):
        self.executable, self.device = str(executable), device

    def run(self, *args, timeout=30):
        command = [self.executable] + (["-s", self.device] if self.device else []) + list(args)
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise SessionError("Cannot read the emulator. Check the configured ADB path, connection and permissions.") from None
        if result.returncode:
            raise SessionError("Emulator read failed. Ensure the selected emulator is connected and permits root reads; otherwise use capture import.")
        return result.stdout

    def text(self, *args):
        try:
            return self.run(*args).decode("utf8").strip()
        except UnicodeError:
            raise SessionError("Unsupported emulator text response.") from None


def select_emulator(settings, *, adb_factory=Adb):
    adb = adb_factory(settings["adb"])
    devices = []
    for line in adb.text("devices").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"emulator-\d+", parts[0]) and parts[1] == "device":
            devices.append(parts[0])
    requested = settings.get("device")
    if requested and requested not in devices:
        raise SessionError("Configured emulator is not online/authorized; refusing to use another device.")
    candidates = []
    for serial in ([requested] if requested else devices):
        selected = adb_factory(settings["adb"], serial)
        name = selected.text("emu", "avd", "name").splitlines()[0]
        if settings.get("avd") and name != settings["avd"]:
            continue
        candidates.append((selected, name))
    if len(candidates) != 1:
        raise SessionError("Cannot identify one matching emulator. Set both device and avd in the shared config.")
    return candidates[0]


def read_store(adb, name):
    path = APP_ROOT + "/files/mmkv/" + name
    # Two identical metadata reads plus CRC validation reject concurrent writes.
    before = adb.run("exec-out", "cat", path + ".crc")
    data = adb.run("exec-out", "cat", path)
    after = adb.run("exec-out", "cat", path + ".crc")
    if before != after:
        raise SessionError("App session changed while being read; repeat the import after the app settles.")
    return mmkv.decode(data, after)


def read_account(adb):
    value = mmkv.json_value(read_store(adb, "LocalAccountConfig"), ACCOUNT_KEY)
    info = value.get("accountInfo")
    if not isinstance(info, dict):
        raise SessionError("No saved logged-in account. Restore login in this emulator first.")
    user, token = info.get("userId"), value.get("token")
    if not all(isinstance(v, str) and v.strip() and len(v) < 8192 and not any(ord(c) < 32 for c in v) for v in (user, token)):
        raise SessionError("The app has no saved valid-looking login. Restore login in this emulator first.")
    return {"userid": user, "token": token}


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def inspect_emulator(config_path):
    settings = load_capture_settings(config_path, require_identity=False)
    adb, name = select_emulator(settings)
    root = adb.text("shell", "id", "-u") == "0"
    result = {"device": adb.device, "avd": name, "root_reads_available": root,
              "network_requests_sent": 0, "login_performed": False}
    if root:
        try:
            account = read_account(adb)
            result.update(saved_session_present=True, account_fingerprint=fingerprint(account["userid"]),
                          server_validity_checked=False)
        except SessionError as exc:
            result.update(saved_session_present=False, session_read_error=str(exc))
    return result


def scoped_device_id(adb):
    if read_store(adb, "mmkv.default").get("cache_agree_privacy_agreement") != b"\x01":
        raise SessionError("The app has not saved privacy consent; use capture import to preserve its actual device identity.")
    users = adb.text("shell", "cmd", "package", "list", "packages", "-U", "--user", "0", PACKAGE)
    uids = re.findall(r"^package:com\.zhuorui\.securities uid:(\d+)$", users, re.M)
    if len(uids) != 1:
        raise SessionError("Cannot identify the app's Android user. Only the main Android profile is supported.")
    raw = adb.run("exec-out", "cat", "/data/system/users/0/settings_ssaid.xml")
    if raw.startswith(b"ABX\x00"):
        raw = adb.run("exec-out", "abx2xml", "/data/system/users/0/settings_ssaid.xml", "-")
    try:
        # Android's XML has sibling settings/namespaceHashes elements.
        raw = re.sub(br"^\s*<\?xml[^?]*\?>", b"", raw)
        tree = ET.fromstring(b"<root>" + raw + b"</root>")
        entries = [e for e in tree.findall("./settings/setting") if e.get("name") == uids[0]]
        if len(entries) != 1 or entries[0].get("package") != PACKAGE:
            raise ValueError()
        value = entries[0].get("value", "")
        if not re.fullmatch(r"[0-9a-f]{16}", value):
            raise ValueError()
        return value
    except (ValueError, ET.ParseError):
        raise SessionError("Cannot verify the app-scoped device ID. Use capture import; a generic Android ID must not be substituted.") from None


def verified_apk(adb, destination):
    paths = adb.text("shell", "pm", "path", PACKAGE).splitlines()
    if len(paths) != 1 or not re.fullmatch(r"package:/data/app/[A-Za-z0-9_./=+~-]+/base\.apk", paths[0]):
        raise SessionError("Unsupported app installation; use capture import for this build.")
    remote = paths[0].removeprefix("package:")
    installed_hash = adb.text("shell", "sha256sum", remote).split()[0]
    if installed_hash != SUPPORTED_APK_SHA256:
        raise SessionError("This app build has not been validated for direct emulator import. Use a fresh traffic capture to validate it first.")
    destination = Path(destination)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != installed_hash:
            raise SessionError("Configured APK differs from the installed app. Use a separate APK path for this installation.")
    else:
        apk = adb.run("exec-out", "cat", remote, timeout=120)
        if hashlib.sha256(apk).hexdigest() != installed_hash:
            raise SessionError("APK changed while reading; repeat import after the app update completes.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".apk.tmp")
        temporary.write_bytes(apk)
        temporary.replace(destination)
    return installed_hash


def known_signing_key(apk_path):
    from cryptography.hazmat.primitives import serialization
    with ZipFile(apk_path) as apk:
        properties = apk.read("assets/config_out.properties").decode("utf-8-sig").replace("\\\r\n", "").replace("\\\n", "")
        for line in properties.splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "private_key":
                key = serialization.load_der_private_key(base64.b64decode(value.strip()), password=None)
                public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                if hashlib.sha256(public).hexdigest() == SUPPORTED_SIGNING_SPKI_SHA256:
                    return key
    raise SessionError("App signing material differs from the validated protocol.")


def device_headers(adb):
    sdk = int(adb.text("shell", "getprop", "ro.build.version.sdk"))
    if sdk < 32:
        raise SessionError("Direct emulator import currently supports Android 12L or newer; use capture import on older versions.")
    def encoded(value):
        return quote(value if value not in ("null", "unknown") else "", safe="_-!.~'()*")
    model = encoded(adb.text("shell", "getprop", "ro.product.model"))
    maker = encoded(adb.text("shell", "getprop", "ro.product.manufacturer"))
    name = encoded(adb.text("shell", "settings", "get", "global", "device_name"))
    # ASCII URI encoding makes the app's garbled-character check false.
    name = f"{maker}-{model}" if not name or name == model else f"{name} ({maker}-{model})"
    lang = mmkv.json_value(read_store(adb, "AppLanguageConfig"), LANGUAGE_KEY).get("appLanguage")
    if lang == "auto":
        locale = (adb.text("shell", "getprop", "persist.sys.locale") or
                  adb.text("shell", "getprop", "ro.product.locale")).replace("_", "-").lower()
        if not locale or not re.fullmatch(r"[a-z0-9-]+", locale):
            raise SessionError("Automatic app language is not mapped for this locale; use capture import.")
        parts = locale.split("-")
        country = next((p for p in parts[1:] if len(p) == 2 or (len(p) == 3 and p.isdigit())), "")
        lang = ("zh_CN" if country in ("", "cn") else "zh_TW") if parts[0] == "zh" else "en_US"
    if lang not in ("en_US", "zh_CN", "zh_TW"):
        raise SessionError("Unsupported saved app language; use capture import.")
    return {"deviceid": scoped_device_id(adb), "devicemodel": model, "devicename": name,
            "osversion": adb.text("shell", "getprop", "ro.build.version.release"),
            "ostype": "android", "appversion": "3.1.5(00001)", "lang": lang,
            "content-type": "application/json; charset=UTF-8", "user-agent": "okhttp/5.1.0"}


def import_emulator_session(config_path, config, settings):
    from cryptography.hazmat.primitives import serialization
    from .session import binding, validate_identity, save_session
    cap = load_capture_settings(config_path, require_identity=False)
    adb, name = select_emulator(cap)
    if adb.text("shell", "id", "-u") != "0":
        raise SessionError("Direct session import requires root reads. Use the validated temporary debug boot or capture import; this command never enables root itself.")
    if adb.text("shell", "am", "get-current-user") != "0":
        raise SessionError("Only the main Android user profile is supported.")
    apk_hash = verified_apk(adb, settings.apk_file)
    account = read_account(adb)
    headers = {**device_headers(adb), **account}
    key = known_signing_key(settings.apk_file)
    if read_account(adb) != account:
        raise SessionError("App account changed during import; retry when login has settled.")
    now = time.time()
    session = {"version": 1, "binding": binding(config), "headers": headers,
        "captured_at": now, "imported_at": now, "source": "emulator",
        "emulator": {"device": adb.device, "avd": name, "apk_sha256": apk_hash},
        "generation": hashlib.sha256(account["token"].encode()).hexdigest(),
        "signing_key": base64.b64encode(key.private_bytes(serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode("ascii")}
    validate_identity(session, config, settings)
    save_session(settings.session_file, session)
    return {"session_imported": True, "source": "emulator", "device": adb.device, "avd": name,
            "account_fingerprint": fingerprint(account["userid"]), "app_build_verified": True,
            "server_validity_checked": False, "network_requests_sent": 0, "login_performed": False}
