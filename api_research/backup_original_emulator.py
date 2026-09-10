"""Stop the explicitly named original AVD and verify a complete private backup.

Does not delete/move originals, change their config, restart a listener, or
operate the app. Requires the listener to have been checked stopped by caller.
"""
import ctypes
import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from capture_settings import arguments, verify_boot_evidence

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"


def exclusive_read_available(path):
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main():
    _, settings = arguments()
    private = Path(settings["private_dir"])
    private.mkdir(parents=True, exist_ok=True)
    adb = [settings["adb"], "-s", settings["device"]]

    def android(*args):
        return subprocess.check_output(adb + list(args), text=True, timeout=20).strip()

    if android("emu", "avd", "name").splitlines()[0] != settings["avd"]:
        raise RuntimeError("Original emulator identity mismatch")
    source = Path(settings["original_avd_dir"]).resolve(strict=True)
    from zhuorui.capture.config import read_ini
    ini = read_ini(source / "config.ini")
    if ini.get("AvdId") != settings["avd"]:
        raise RuntimeError("Original AVD directory does not match the configured emulator")
    if private.is_relative_to(source):
        raise RuntimeError("Private backup directory cannot be inside the original AVD")
    verify_boot_evidence(settings)
    keys = ["http_proxy", "global_http_proxy_host", "global_http_proxy_port", "global_http_proxy_exclusion_list", "global_proxy_pac_url"]
    saved = {key: android("shell", "settings", "get", "global", key) for key in keys}
    if saved["http_proxy"] not in ("null", "", ":0") or saved["global_http_proxy_host"] not in ("null", ""):
        raise RuntimeError("Unexpected existing proxy; original left running")
    now = datetime.now(timezone.utc)
    backup = private / ("original-avd-backup-" + now.strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(exist_ok=False)
    session = {
        "device": settings["device"], "avd": settings["avd"], "original_avd": str(source),
        "capture_proxy": settings["emulator_proxy"], "capture_proxy_host": settings["emulator_proxy_host"],
        "backup": str(backup), "started_utc": now.isoformat(), "backup_verified": False,
        "fingerprint": android("shell", "getprop", "ro.build.fingerprint"),
        "android_id": android("shell", "settings", "get", "secure", "android_id"),
        "proxy_settings": saved, "phase": "backing_up",
    }
    session_path = private / "original-capture-session.json"
    session_path.write_text(json.dumps(session, indent=2))
    print(f"Stopping only {settings['avd']} for its private backup.", flush=True)
    android("emu", "kill")
    deadline = time.monotonic() + 60
    disk = source / "userdata-qemu.img.qcow2"
    while time.monotonic() < deadline:
        if exclusive_read_available(disk):
            break
        time.sleep(1)
    else:
        raise RuntimeError("Original data disk is still open; no backup copy attempted")
    manifest = []
    files = [p for p in source.rglob("*") if p.is_file() and not any(part.endswith(".lock") or part == "tmpAdbCmds" for part in p.relative_to(source).parts)]
    total = sum(p.stat().st_size for p in files)
    print(f"Backing up {len(files)} files ({total / 1024**3:.1f} GiB), including app data, encryption metadata, and snapshots.", flush=True)
    for path in files:
        relative = path.relative_to(source)
        destination = backup / "avd" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_hash = hashlib.sha256()
        with path.open("rb") as reader, destination.open("xb") as writer:
            for block in iter(lambda: reader.read(8 * 1024 * 1024), b""):
                source_hash.update(block)
                writer.write(block)
        if source_hash.hexdigest() != digest(destination):
            raise RuntimeError(f"Backup verification failed: {relative}")
        manifest.append({"path": str(relative), "bytes": path.stat().st_size, "sha256": source_hash.hexdigest()})
        if path.stat().st_size > 100 * 1024 * 1024:
            print(f"Verified backup: {relative}", flush=True)
    shutil.copy2(Path(settings["avd_home"]) / (settings["avd"] + ".ini"), backup / (settings["avd"] + ".ini"))
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    session.update(backup_verified=True, phase="backed_up", backup_completed_utc=datetime.now(timezone.utc).isoformat())
    session_path.write_text(json.dumps(session, indent=2))
    print("Original AVD backup verified; original files and configuration were not changed.", flush=True)


if __name__ == "__main__":
    main()
