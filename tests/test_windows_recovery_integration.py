"""Exercise actual Windows child processes in an isolated, non-trading project."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows is required")
class RecoveryProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zhuorui-recovery-test-")
        self.root = Path(self.temp.name)
        shutil.copytree(SOURCE / "scripts" / "windows", self.root / "scripts" / "windows")
        shutil.copytree(SOURCE / "zhuorui", self.root / "zhuorui", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SOURCE / "monitor_web", self.root / "monitor_web")
        shutil.copyfile(SOURCE / "zhuorui_monitor.py", self.root / "zhuorui_monitor.py")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.root / "runtime" / "python-env")], check=True,
                       capture_output=True)
        self.config = {"account_id": "test", "server_id": "test", "api": {"live_orders_enabled": False}}
        (self.root / "zhuorui_config.json").write_text(json.dumps(self.config))
        recovery = self.root / "runtime" / "recovery"
        recovery.mkdir()
        (recovery / "settings.json").write_text(json.dumps({"version": 1, "enabled": True}))
        (recovery / "monitor.intent.json").write_text(json.dumps({"version": 1, "desired_running": False, "revision": "test", "parameters": {"Port": 443}}))
        # This fake listener has no broker, Kafka, or order-writing code.
        (self.root / "zhuorui_api.py").write_text('''
import json, os, time
from pathlib import Path
from datetime import datetime, timezone
root=Path(__file__).parent
directory=root/'runtime'/'api'
directory.mkdir(parents=True,exist_ok=True)
stop=directory/'listener.stop'
stop.unlink(missing_ok=True)
started=datetime.now(timezone.utc).isoformat()
while not stop.exists():
    if (root/'crash.request').exists():
        (root/'crash.request').unlink()
        os._exit(17)
    value={'running':True,'pid':os.getpid(),'started_at':started,'updated_at':datetime.now(timezone.utc).isoformat(),'session_status':'login_blocked','last_holdings_publish':None}
    tmp=directory/'state.tmp'
    tmp.write_text(json.dumps(value))
    tmp.replace(directory/'listener-state.json')
    time.sleep(.1)
''')

    def tearDown(self):
        for name in ("stop_zhuorui_listener.ps1", "stop_zhuorui_monitor.ps1"):
            with (self.root / "cleanup.log").open("a") as output:
                subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(self.root / "scripts" / "windows" / name)], stdin=subprocess.DEVNULL,
                               stdout=output, stderr=output, timeout=40)
        self.temp.cleanup()

    def ps(self, name, *args):
        output_path = self.root / "control-output.log"
        # Persistent Windows children can inherit pipe handles. A file prevents
        # communicate() waiting for the deliberately long-lived fixture to exit.
        with output_path.open("w") as output:
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                     str(self.root / "scripts" / "windows" / name), *args], cwd=self.root,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=output, timeout=45)
        self.assertEqual(result.returncode, 0, output_path.read_text())
        return result

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8-sig"))

    def test_actual_crash_restart_and_persistent_stop(self):
        self.ps("start_zhuorui_listener.ps1")
        first = self.read("zhuorui_api_listener.current.json")["pid"]
        self.ps("start_zhuorui_listener.ps1")
        self.assertEqual(first, self.read("zhuorui_api_listener.current.json")["pid"])
        self.ps("watch_zhuorui_services.ps1")
        self.assertEqual(self.read("runtime/recovery/status.json")["components"]["api"]["state"], "running")
        (self.root / "crash.request").write_text("crash the test fixture")
        time.sleep(.5)
        self.ps("watch_zhuorui_services.ps1")
        second = self.read("zhuorui_api_listener.current.json")["pid"]
        self.assertNotEqual(first, second)
        # Recovery may stop only the observed process and must preserve intent.
        intent = self.read("runtime/recovery/api.intent.json")
        self.ps("watch_zhuorui_services.ps1")
        observation = self.read("runtime/recovery/status.json")["components"]["api"]
        stop_args = ("-Recovery", "-IntentRevision", intent["revision"],
                     "-ExpectedStartedUtc", observation["started_utc"])
        self.ps("stop_zhuorui_listener.ps1", *stop_args, "-ExpectedPid", str(first))
        self.assertEqual(second, self.read("zhuorui_api_listener.current.json")["pid"])
        self.ps("stop_zhuorui_listener.ps1", *stop_args, "-ExpectedPid", str(second))
        self.assertFalse((self.root / "zhuorui_api_listener.pid").exists())
        self.assertEqual(intent, self.read("runtime/recovery/api.intent.json"))
        self.ps("start_zhuorui_listener.ps1", "-Recovery")
        self.ps("stop_zhuorui_listener.ps1")
        self.ps("watch_zhuorui_services.ps1")
        self.assertFalse((self.root / "zhuorui_api_listener.pid").exists())
        self.assertEqual(self.read("runtime/recovery/status.json")["components"]["api"]["state"], "paused")
        self.ps("start_zhuorui_listener.ps1")
        self.assertTrue(self.read("runtime/recovery/api.intent.json")["desired_running"])

    def test_actual_monitor_graceful_stop_and_duplicate_start(self):
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime, timedelta, timezone
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1)).sign(key, hashes.SHA256()))
        cert_dir = self.root / "certs"
        cert_dir.mkdir()
        (cert_dir / "zhuorui-monitor-cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (cert_dir / "zhuorui-monitor-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        ports = []
        for _ in range(2):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                ports.append(probe.getsockname()[1])
        args = ("-HostAddress", "127.0.0.1", "-PublicHost", "127.0.0.1", "-Port", str(ports[0]), "-RedirectHttpPort", str(ports[1]))
        self.ps("start_zhuorui_monitor.ps1", *args)
        first = self.read("zhuorui_monitor.current.json")["pid"]
        self.ps("start_zhuorui_monitor.ps1", *args)
        self.assertEqual(first, self.read("zhuorui_monitor.current.json")["pid"])
        (self.root / "runtime" / "recovery" / "api.intent.json").write_text(json.dumps({
            "version": 1, "desired_running": False, "revision": "test", "parameters": {},
        }))
        self.ps("watch_zhuorui_services.ps1")
        self.assertEqual(self.read("runtime/recovery/status.json")["components"]["monitor"]["state"], "running")
        intent = self.read("runtime/recovery/monitor.intent.json")
        observation = self.read("runtime/recovery/status.json")["components"]["monitor"]
        self.ps("stop_zhuorui_monitor.ps1", "-Recovery", "-IntentRevision", intent["revision"],
                "-ExpectedPid", str(first), "-ExpectedStartedUtc", observation["started_utc"])
        self.assertFalse((self.root / "zhuorui_monitor.pid").exists())
        self.assertEqual(intent, self.read("runtime/recovery/monitor.intent.json"))
        self.ps("stop_zhuorui_monitor.ps1")
        self.assertFalse(self.read("runtime/recovery/monitor.intent.json")["desired_running"])


if __name__ == "__main__":
    unittest.main()
