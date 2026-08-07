import json
import http.client
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zhuorui_monitor import (
    LoginLimiter,
    RedirectServer,
    ZhuoruiController,
    ZhuoruiServer,
    parse_android_app_meminfo,
    parse_android_memory_summary,
    parse_datetime,
    verify_admin_credentials,
)


class FakeRunner:
    def __init__(self, device_output: str = "List of devices attached\n") -> None:
        self.device_output = device_output
        self.calls: list[list[str]] = []

    def __call__(self, arguments, *, cwd, timeout):
        args = list(arguments)
        self.calls.append(args)
        if "getprop" in args:
            return subprocess.CompletedProcess(args, 0, "1\n", "")
        if args[-1:] == ["devices"]:
            return subprocess.CompletedProcess(args, 0, self.device_output, "")
        return subprocess.CompletedProcess(args, 0, "completed\n", "")


class ZhuoruiControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)

    def tearDown(self):
        self.temp_directory.cleanup()

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def make_adb(self):
        adb = self.root / "sdk" / "platform-tools" / "adb.exe"
        adb.parent.mkdir(parents=True)
        adb.write_bytes(b"")
        return adb

    def test_running_script_reports_pid_start_and_duration(self):
        started = datetime.now(timezone.utc) - timedelta(hours=2, minutes=3)
        (self.root / "zhuorui_listener.pid").write_text("4321", encoding="ascii")
        self.write_json(
            "zhuorui_listener.current.json",
            {"pid": 4321, "started_utc": started.isoformat()},
        )
        controller = ZhuoruiController(
            self.root,
            probe=lambda pid: {"running": True, "started_epoch": started.timestamp()},
            runner=FakeRunner(),
        )

        status = controller.script_status(started + timedelta(hours=2, minutes=3))

        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], 4321)
        self.assertEqual(status["duration_seconds"], 7380)
        self.assertIsNotNone(parse_datetime(status["started_at"]))

    def test_stale_pid_is_not_reported_as_running(self):
        (self.root / "zhuorui_listener.pid").write_text("4321", encoding="ascii")
        controller = ZhuoruiController(
            self.root,
            probe=lambda pid: {"running": False, "started_epoch": None},
            runner=FakeRunner(),
        )

        status = controller.script_status()

        self.assertFalse(status["running"])
        self.assertEqual(status["state"], "stopped")
        self.assertIn("stale", status["message"])

    def test_reused_pid_is_rejected_using_creation_time(self):
        recorded = datetime.now(timezone.utc) - timedelta(days=1)
        actual = datetime.now(timezone.utc)
        (self.root / "zhuorui_listener.pid").write_text("4321", encoding="ascii")
        self.write_json(
            "zhuorui_listener.current.json",
            {"pid": 4321, "started_utc": recorded.isoformat()},
        )
        controller = ZhuoruiController(
            self.root,
            probe=lambda pid: {"running": True, "started_epoch": actual.timestamp()},
            runner=FakeRunner(),
        )

        self.assertFalse(controller.script_status()["running"])

    def test_public_config_never_contains_credentials(self):
        self.write_json(
            "zhuorui_config.json",
            {
                "server_id": "zhuorui-1",
                "account_id": "account-7",
                "trade_password": "secret",
                "login": {"phone": "123", "password": "secret"},
            },
        )
        controller = ZhuoruiController(self.root, runner=FakeRunner())

        public = controller.public_config()

        self.assertEqual(public["account_id"], "account-7")
        self.assertNotIn("trade_password", public)
        self.assertNotIn("login", public)

    def test_emulator_running_when_adb_device_is_booted(self):
        adb = self.make_adb()
        self.write_json(
            "zhuorui_config.json",
            {"adb": str(adb), "device": "emulator-5554", "avd": "Pixel_10_2"},
        )
        runner = FakeRunner("List of devices attached\nemulator-5554\tdevice\n")
        controller = ZhuoruiController(self.root, runner=runner)

        status = controller.emulator_status()

        self.assertTrue(status["running"])
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["device"], "emulator-5554")

    def test_start_listener_uses_existing_powershell_control(self):
        (self.root / "start_zhuorui_listener.ps1").write_text("", encoding="utf-8")
        control_runner = FakeRunner()
        controller = ZhuoruiController(
            self.root,
            runner=FakeRunner(),
            control_runner=control_runner,
        )

        result = controller.start_script()

        self.assertTrue(result.ok)
        self.assertIn("start_zhuorui_listener.ps1", control_runner.calls[0][-1])

    def test_holdings_performance_uses_only_last_ten_from_current_session(self):
        logs = self.root / "logs"
        logs.mkdir()
        previous_log = logs / "previous.out.log"
        previous_log.write_text("Holdings query completed in 999.000 seconds.\n", encoding="utf-8")
        current_log = logs / "current.out.log"
        current_log.write_text(
            "\n".join(f"Holdings query completed in {value:.3f} seconds." for value in range(1, 13)),
            encoding="utf-8",
        )
        self.write_json(
            "zhuorui_listener.current.json",
            {"pid": 4321, "stdout": str(current_log)},
        )
        controller = ZhuoruiController(self.root, runner=FakeRunner())

        performance = controller.holdings_query_performance(
            {"running": True, "pid": 4321, "started_at": "2026-08-07T18:00:00Z"}
        )

        self.assertTrue(performance["available"])
        self.assertEqual(performance["attempts_in_session"], 12)
        self.assertEqual(performance["sample_count"], 10)
        self.assertEqual(performance["average_seconds"], 7.5)
        self.assertEqual(performance["fastest_seconds"], 3.0)
        self.assertEqual(performance["slowest_seconds"], 12.0)

    def test_holdings_performance_averages_all_available_when_under_ten(self):
        logs = self.root / "logs"
        logs.mkdir()
        current_log = logs / "current.out.log"
        current_log.write_text(
            "Holdings query completed in 2.000 seconds.\n"
            "Holdings query completed in 4.000 seconds.\n"
            "Holdings query completed in 6.000 seconds.\n",
            encoding="utf-8",
        )
        self.write_json(
            "zhuorui_listener.current.json",
            {"pid": 4321, "stdout": str(current_log)},
        )
        controller = ZhuoruiController(self.root, runner=FakeRunner())

        performance = controller.holdings_query_performance(
            {"running": True, "pid": 4321, "started_at": "2026-08-07T18:00:00Z"}
        )

        self.assertEqual(performance["sample_count"], 3)
        self.assertEqual(performance["average_seconds"], 4.0)
        self.assertEqual(performance["fastest_seconds"], 2.0)
        self.assertEqual(performance["slowest_seconds"], 6.0)

    def test_holdings_performance_does_not_reuse_stopped_session(self):
        controller = ZhuoruiController(self.root, runner=FakeRunner())

        performance = controller.holdings_query_performance(
            {"running": False, "pid": None, "started_at": None}
        )

        self.assertFalse(performance["available"])
        self.assertEqual(performance["sample_count"], 0)

    def test_health_status_reports_all_five_healthy_metrics(self):
        controller = ZhuoruiController(
            self.root,
            runner=FakeRunner(),
            machine_sampler=lambda: {
                "cpu_percent": 24.0,
                "memory_percent": 48.0,
                "memory_total_bytes": 16 * 1024**3,
                "memory_available_bytes": 8 * 1024**3,
            },
        )
        controller._android_memory_status = lambda emulator: {
            "total_bytes": 12 * 1024**3,
            "used_bytes": int(2.5 * 1024**3),
            "free_reclaimable_bytes": int(9.5 * 1024**3),
            "status": "normal",
            "swap_used_bytes": 500 * 1024**2,
            "swap_total_bytes": 9 * 1024**3,
            "app_pss_bytes": 300 * 1024**2,
            "app_rss_bytes": 500 * 1024**2,
            "app_swap_bytes": 0,
        }

        health = controller.health_status(
            {
                "running": True,
                "state": "running",
                "adb_state": "device",
                "adb_latency_ms": 80,
                "shell_latency_ms": 120,
            },
            datetime.now(timezone.utc),
        )

        self.assertEqual(health["overall_level"], "healthy")
        self.assertEqual(
            [metric["id"] for metric in health["metrics"]],
            ["machine_cpu", "machine_memory", "emulator_memory", "adb_health", "android_response"],
        )
        self.assertTrue(all(metric["level_label"] == "Healthy" for metric in health["metrics"]))

    def test_health_status_recommends_restart_for_critical_memory_pressure(self):
        controller = ZhuoruiController(
            self.root,
            runner=FakeRunner(),
            machine_sampler=lambda: {
                "cpu_percent": 35.0,
                "memory_percent": 95.0,
                "memory_total_bytes": 8 * 1024**3,
                "memory_available_bytes": 400 * 1024**2,
            },
        )
        controller._android_memory_status = lambda emulator: {
            "total_bytes": 12 * 1024**3,
            "used_bytes": int(2.5 * 1024**3),
            "free_reclaimable_bytes": int(9.5 * 1024**3),
            "status": "normal",
            "swap_used_bytes": 500 * 1024**2,
            "swap_total_bytes": 9 * 1024**3,
            "app_pss_bytes": 300 * 1024**2,
            "app_rss_bytes": 500 * 1024**2,
            "app_swap_bytes": 0,
        }

        health = controller.health_status(
            {
                "running": True,
                "state": "running",
                "adb_state": "device",
                "adb_latency_ms": 90,
                "shell_latency_ms": 140,
            },
            datetime.now(timezone.utc),
        )

        memory_metric = next(metric for metric in health["metrics"] if metric["id"] == "machine_memory")
        self.assertEqual(memory_metric["level"], "restart_recommended")
        self.assertEqual(health["overall_label"], "Restart recommended")

    def test_android_memory_parsers_use_guest_and_app_values(self):
        system = parse_android_memory_summary(
            """
            Total RAM: 12,246,832K (status normal)
             Free RAM: 10,041,759K (cached and free)
             Used RAM: 2,490,360K (pss and kernel)
             Lost RAM: 183,789K
                 ZRAM: 218,068K physical used for 506,312K in swap (9,185,120K total swap)
            """
        )
        app = parse_android_app_meminfo(
            "TOTAL PSS: 297820 TOTAL RSS: 507044 TOTAL SWAP (KB): 0"
        )

        self.assertEqual(system["status"], "normal")
        self.assertEqual(system["used_bytes"], 2_490_360 * 1024)
        self.assertEqual(system["swap_used_bytes"], 506_312 * 1024)
        self.assertEqual(app["app_pss_bytes"], 297_820 * 1024)
        self.assertEqual(app["app_rss_bytes"], 507_044 * 1024)

    def test_android_memory_pressure_drives_restart_level(self):
        controller = ZhuoruiController(
            self.root,
            runner=FakeRunner(),
            machine_sampler=lambda: {
                "cpu_percent": 20.0,
                "memory_percent": 50.0,
                "memory_total_bytes": 32 * 1024**3,
                "memory_available_bytes": 16 * 1024**3,
            },
        )
        controller._android_memory_status = lambda emulator: {
            "total_bytes": 12 * 1024**3,
            "used_bytes": 11 * 1024**3,
            "free_reclaimable_bytes": 1 * 1024**3,
            "status": "critical",
            "swap_used_bytes": 7 * 1024**3,
            "swap_total_bytes": 9 * 1024**3,
            "app_pss_bytes": 2 * 1024**3,
            "app_swap_bytes": 512 * 1024**2,
        }

        health = controller.health_status(
            {
                "running": True,
                "state": "running",
                "adb_state": "device",
                "adb_latency_ms": 80,
                "shell_latency_ms": 120,
            },
            datetime.now(timezone.utc),
        )

        android_metric = next(metric for metric in health["metrics"] if metric["id"] == "emulator_memory")
        self.assertEqual(android_metric["level"], "restart_recommended")
        self.assertEqual(android_metric["value"], "92% used")


class DummyController:
    def __init__(self):
        self.actions = []

    def perform(self, action):
        self.actions.append(action)
        return type("Result", (), {"ok": True, "message": "done"})()


class DummyMonitor:
    def __init__(self):
        self.controller = DummyController()
        self.status = {
            "account": {"account_id": "test", "server_id": "test"},
            "script": {"running": True, "state": "running", "pid": 123},
            "emulator": {"running": True, "state": "running"},
            "checked_at": "2026-08-07T00:00:00Z",
            "next_check_at": "2026-08-07T00:01:00Z",
            "interval_seconds": 60,
        }

    def snapshot(self):
        return self.status

    def refresh(self):
        return self.status


class AuthenticationTests(unittest.TestCase):
    def test_only_configured_credentials_are_accepted(self):
        self.assertTrue(verify_admin_credentials("admin", "admin12345"))
        self.assertFalse(verify_admin_credentials("admin", "wrong"))
        self.assertFalse(verify_admin_credentials("other", "admin12345"))

    def test_fifth_failure_triggers_lockout(self):
        limiter = LoginLimiter()
        for _ in range(4):
            self.assertEqual(limiter.record_failure("192.0.2.1"), 0)
        self.assertGreater(limiter.record_failure("192.0.2.1"), 0)
        self.assertGreater(limiter.retry_after("192.0.2.1"), 0)

    def test_http_routes_require_login_and_csrf(self):
        monitor = DummyMonitor()
        server = ZhuoruiServer(("127.0.0.1", 0), monitor)
        server.is_tls = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)

            body = json.dumps({"username": "admin", "password": "admin12345"})
            connection.request(
                "POST",
                "/api/login",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            set_cookie = response.getheader("Set-Cookie")
            self.assertIn("Secure", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=Strict", set_cookie)

            connection.request("GET", "/api/status", headers={"Cookie": cookie})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)

            connection.request(
                "POST",
                "/api/script/start",
                body="{}",
                headers={
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                    "X-Zhuorui-Action": "1",
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            connection.request(
                "POST",
                "/api/script/start",
                body="{}",
                headers={
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                    "X-Zhuorui-Action": "1",
                    "X-CSRF-Token": payload["csrf_token"],
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(monitor.controller.actions, ["script/start"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_port_80_server_redirects_to_standard_https(self):
        server = RedirectServer(("127.0.0.1", 0), "209.250.240.250", 443)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/dashboard?view=live")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 308)
            self.assertEqual(
                response.getheader("Location"),
                "https://209.250.240.250/dashboard?view=live",
            )
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
