"""Trust the local proxy CA for a verified Android 16 capture session.

Mounts disappear when the selected emulator reboots. Retains all original
system trust anchors. The original AVD additionally requires a saved backup
record and a matching fingerprint from its authorized temporary debug boot.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from cryptography import x509
from capture_settings import arguments

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="One of the three configured emulator serials; defaults to the capture test")
    args, settings = arguments(parser)
    args.device = args.device or settings["test_device"]
    private = Path(settings["private_dir"])
    expected_names = {settings["test_device"]: settings["test_avd"], settings["boot_test_device"]: settings["boot_test_avd"], settings["device"]: settings["avd"]}
    if args.device not in expected_names:
        raise RuntimeError("Device is not a configured capture target")
    adb = [settings["adb"], "-s", args.device]

    def run(*args):
        return subprocess.check_output(adb + list(args), text=True, timeout=30).strip()

    avd_name = run("emu", "avd", "name").splitlines()[0]
    if avd_name != expected_names[args.device]:
        raise RuntimeError(f"Refusing to modify unexpected emulator: {avd_name}")
    if args.device == settings["device"]:
        session = json.loads((private / "original-capture-session.json").read_text(encoding="utf-8-sig"))
        if not session.get("backup_verified") or session["device"] != args.device or session["avd"] != settings["avd"]:
            raise RuntimeError("Original capture session lacks a verified backup")
        if run("shell", "getprop", "ro.build.fingerprint") != session["fingerprint"]:
            raise RuntimeError("Original device fingerprint changed")
        if run("shell", "getprop", "ro.force.debuggable") != "1":
            raise RuntimeError("Original AVD is not in the verified temporary debugging boot")
    if run("shell", "id", "-u") != "0":
        raise RuntimeError(f"Run adb -s {args.device} root first")
    pem = (private / "mitmproxy/mitmproxy-ca-cert.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    # Android's hashed CA filenames use OpenSSL subject_hash_old (MD5/LE).
    digest = hashlib.md5(cert.subject.public_bytes()).digest()
    cert_name = f"{int.from_bytes(digest[:4], 'little'):08x}.0"
    local_cert = private / cert_name
    local_cert.write_bytes(pem)
    run("push", str(local_cert), "/data/local/tmp/zhuorui-capture-ca.pem")
    script = f"""set -eu
store=/data/local/tmp/zhuorui-capture-cacerts
mkdir -p "$store"
cp /apex/com.android.conscrypt/cacerts/* "$store/"
cp /data/local/tmp/zhuorui-capture-ca.pem "$store/{cert_name}"
chmod 755 "$store"
chmod 644 "$store"/*
chcon u:object_r:system_file:s0 "$store" "$store"/*
mount --bind "$store" /apex/com.android.conscrypt/cacerts
for process in $(pidof zygote64) $(pidof zygote); do
    nsenter -t "$process" -m -- mount --bind "$store" /apex/com.android.conscrypt/cacerts
    nsenter -t "$process" -m -- test -f /apex/com.android.conscrypt/cacerts/{cert_name}
done
test -f /apex/com.android.conscrypt/cacerts/{cert_name}
echo 'Temporary proxy CA mounted in the verified capture emulator.'
"""
    local_script = private / "trust-ca.sh"
    local_script.write_text(script, encoding="utf-8", newline="\n")
    run("push", str(local_script), "/data/local/tmp/zhuorui-trust-ca.sh")
    print(run("shell", "sh", "/data/local/tmp/zhuorui-trust-ca.sh"))


if __name__ == "__main__":
    main()
