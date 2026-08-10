import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from zhuorui_market_order import (
    Adb,
    EMPTY_POSITIONS_LABEL,
    LOGIN_DELAY_SECONDS,
    Bounds,
    UiNode,
    ZhuoruiAutomationError,
    ZhuoruiTrader,
    login_delay_seconds,
)


def node(text: str = "", resource_id: str = "") -> UiNode:
    return UiNode(
        text=text,
        hint="",
        content_desc="",
        resource_id=resource_id,
        klass="android.view.View",
        clickable=False,
        focusable=False,
        focused=False,
        password=False,
        bounds=Bounds(0, 0, 100, 100),
    )


class FakeAdb:
    def __init__(self, nodes: list[UiNode]):
        self.nodes = nodes
        self.taps: list[tuple[int, int]] = []

    def dump_xml(self) -> list[UiNode]:
        return self.nodes

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def wm_size(self) -> tuple[int, int]:
        return 1080, 2424


class LoggedInDetectionTests(unittest.TestCase):
    def test_open_account_bottom_tab_means_logged_out(self) -> None:
        nodes = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Open A/C"),
            node("S-Invest"),
        ]
        trader = ZhuoruiTrader(FakeAdb(nodes))

        self.assertTrue(trader.is_logged_out_landing_page(nodes))
        self.assertFalse(trader.is_main_landing_page(nodes))
        with self.assertRaisesRegex(ZhuoruiAutomationError, "not logged in"):
            trader.ensure_logged_in(nodes)

    def test_assets_bottom_tab_means_logged_in(self) -> None:
        nodes = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Assets"),
            node("S-Invest"),
        ]
        trader = ZhuoruiTrader(FakeAdb(nodes))

        self.assertFalse(trader.is_logged_out_landing_page(nodes))
        trader.ensure_logged_in(nodes)

    def test_logged_out_landing_page_without_credentials_aborts_before_navigation(self) -> None:
        nodes = [node("Quotes"), node("Open A/C"), node("S-Invest"), node("Wealth")]
        adb = FakeAdb(nodes)
        trader = ZhuoruiTrader(adb, fast_path=False)

        with self.assertRaisesRegex(ZhuoruiAutomationError, "credentials are not configured"):
            trader.return_to_landing_page()

        self.assertEqual(adb.taps, [])

    def test_fast_path_checks_logged_out_state_before_screenshot_classification(self) -> None:
        class ScreenshotMustNotRunTrader(ZhuoruiTrader):
            def screenshot_shows_main_landing_page(self) -> bool:
                raise AssertionError("screenshot classifier ran before logged-out detection")

        nodes = [node("Quotes"), node("Open A/C"), node("S-Invest"), node("Wealth")]
        trader = ScreenshotMustNotRunTrader(FakeAdb(nodes))

        with self.assertRaisesRegex(ZhuoruiAutomationError, "credentials are not configured"):
            trader.return_to_landing_page_fast()

    def test_watchlist_navigation_checks_login_before_ocr_or_back(self) -> None:
        nodes = [node("Quotes"), node("Open A/C"), node("S-Invest"), node("Wealth")]
        adb = FakeAdb(nodes)
        trader = ZhuoruiTrader(adb)

        with self.assertRaisesRegex(ZhuoruiAutomationError, "credentials are not configured"):
            trader.return_to_watchlist_landing()

        self.assertEqual(adb.taps, [])

    def test_navigation_checks_login_again_after_each_back_press(self) -> None:
        logged_out = [node("Quotes"), node("Open A/C"), node("S-Invest"), node("Wealth")]

        class SequenceAdb(FakeAdb):
            def __init__(self) -> None:
                super().__init__([node("Account details")])
                self.node_states = [self.nodes, self.nodes, logged_out]
                self.keyevents: list[int] = []
                self.screenshot_count = 0

            def dump_xml(self) -> list[UiNode]:
                if len(self.node_states) > 1:
                    return self.node_states.pop(0)
                return self.node_states[0]

            def screenshot(self, _path) -> None:
                self.screenshot_count += 1

            def keyevent(self, keycode: int) -> None:
                self.keyevents.append(keycode)

        adb = SequenceAdb()
        trader = ZhuoruiTrader(adb)
        trader.home_screen_ocr_text = Mock(return_value="")

        with (
            patch("zhuorui_market_order.HomeScreenTextOcr.from_adb", return_value=Mock()),
            self.assertRaisesRegex(ZhuoruiAutomationError, "credentials are not configured"),
        ):
            trader.return_to_landing_page_fast()

        self.assertEqual(adb.keyevents, [4])
        self.assertEqual(adb.screenshot_count, 1)

    def test_logged_out_account_logs_in_immediately_outside_delay_window(self) -> None:
        nodes = [node("Quotes"), node("Open A/C"), node("S-Invest"), node("Wealth")]
        trader = ZhuoruiTrader(FakeAdb(nodes), login_phone="123", login_password="secret")
        trader.login_from_landing_page = Mock()

        with patch("zhuorui_market_order.login_delay_seconds", return_value=0.0):
            trader.ensure_logged_in(nodes)

        trader.login_from_landing_page.assert_called_once_with(nodes)

    def test_waiting_login_returns_if_account_is_logged_in_during_delay(self) -> None:
        logged_out = [node("Quotes"), node("Open A/C"), node("S-Invest"), node("Wealth")]
        logged_in = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Assets"),
            node("S-Invest"),
        ]
        trader = ZhuoruiTrader(FakeAdb(logged_out), login_phone="123", login_password="secret")
        trader.current_nodes = Mock(return_value=logged_in)
        trader.login_from_landing_page = Mock()

        with (
            patch("zhuorui_market_order.login_delay_seconds", return_value=LOGIN_DELAY_SECONDS),
            patch("zhuorui_market_order.time.monotonic", side_effect=[100.0, 280.0]),
        ):
            trader.ensure_logged_in(logged_out)

        trader.login_from_landing_page.assert_not_called()

    def test_login_delay_uses_beijing_business_hours(self) -> None:
        self.assertEqual(login_delay_seconds(datetime(2026, 7, 31, 0, 59, tzinfo=timezone.utc)), 0.0)
        self.assertEqual(
            login_delay_seconds(datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)),
            LOGIN_DELAY_SECONDS,
        )
        self.assertEqual(
            login_delay_seconds(datetime(2026, 7, 31, 7, 59, tzinfo=timezone.utc)),
            LOGIN_DELAY_SECONDS,
        )
        self.assertEqual(login_delay_seconds(datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)), 0.0)


class EmptyPositionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.empty_nodes = [node(EMPTY_POSITIONS_LABEL)]

    def test_fast_collector_returns_empty_list_for_empty_state(self) -> None:
        trader = ZhuoruiTrader(FakeAdb(self.empty_nodes))

        self.assertEqual(trader.collect_visible_security_positions_once(), [])

    def test_slow_collector_returns_empty_list_for_empty_state(self) -> None:
        trader = ZhuoruiTrader(FakeAdb(self.empty_nodes))

        self.assertEqual(trader.collect_security_positions(self.empty_nodes), [])

    def test_unrelated_screen_is_not_treated_as_empty_positions(self) -> None:
        trader = ZhuoruiTrader(FakeAdb([node("No orders yet")]))

        self.assertFalse(trader.empty_positions_visible([node("No orders yet")]))


class HoldingsDumpTimeoutTests(unittest.TestCase):
    class RecordingAdb(FakeAdb):
        def __init__(self, nodes: list[UiNode]):
            super().__init__(nodes)
            self.idle_timeouts: list[int | None] = []

        def dump_xml(self, idle_timeout_ms: int | None = None) -> list[UiNode]:
            self.idle_timeouts.append(idle_timeout_ms)
            return self.nodes

    def test_holdings_uses_zero_idle_timeout_and_restores_default(self) -> None:
        adb = self.RecordingAdb([node("Assets")])
        trader = ZhuoruiTrader(adb)

        def collect_fast() -> dict[str, list[dict[str, str]]]:
            trader.current_nodes()
            return {"cash": [], "securities": []}

        trader.collect_positions_fast = Mock(side_effect=collect_fast)

        self.assertEqual(trader.collect_positions(), {"cash": [], "securities": []})
        self.assertEqual(adb.idle_timeouts, [0])

        trader.current_nodes()
        self.assertEqual(adb.idle_timeouts, [0, None])

    def test_holdings_restores_default_after_failure(self) -> None:
        adb = self.RecordingAdb([node("Assets")])
        trader = ZhuoruiTrader(adb)

        def fail_fast() -> dict[str, list[dict[str, str]]]:
            trader.current_nodes()
            raise ZhuoruiAutomationError("simulated holdings failure")

        trader.collect_positions_fast = Mock(side_effect=fail_fast)

        with self.assertRaisesRegex(ZhuoruiAutomationError, "simulated holdings failure"):
            trader.collect_positions()
        trader.current_nodes()

        self.assertEqual(adb.idle_timeouts, [0, None])

    def test_zero_idle_dump_uses_helper_with_positive_host_timeout(self) -> None:
        adb = Adb.__new__(Adb)
        adb._zero_idle_dump_helper_ready = True
        shell_calls: list[tuple[tuple[str, ...], float]] = []
        cmd_calls: list[tuple[tuple[str, ...], float]] = []
        xml = '<hierarchy rotation="0"><node text="Assets" bounds="[0,0][1,1]" /></hierarchy>'

        def shell(*args: str, timeout: float, check: bool) -> Mock:
            shell_calls.append((args, timeout))
            return Mock(returncode=0, stdout="OK (1 test)", stderr="")

        def cmd(*args: str, timeout: float, check: bool) -> Mock:
            cmd_calls.append((args, timeout))
            if args[0] == "pull":
                Path(args[2]).write_text(xml, encoding="utf-8")
            return Mock(returncode=0, stdout="", stderr="")

        adb.shell = shell
        adb.cmd = cmd

        nodes = adb.dump_xml(idle_timeout_ms=0)

        self.assertEqual([item.text for item in nodes], ["Assets"])
        runner_calls = [
            call
            for call in shell_calls
            if "app_process" in call[0] and "runtest" in call[0]
        ]
        self.assertEqual(len(runner_calls), 1)
        self.assertGreater(runner_calls[0][1], 0)
        self.assertTrue(
            any(
                "/zhuorui-zero-idle-dump-" in arg and arg.endswith(".jar")
                for arg in runner_calls[0][0]
            )
        )
        self.assertIn("ANDROID_DATA=/data", runner_calls[0][0])
        self.assertFalse(
            any(call[0][:2] == ("uiautomator", "dump") for call in shell_calls)
        )
        self.assertFalse(
            any(call[0][:3] == ("exec-out", "uiautomator", "dump") for call in cmd_calls)
        )
        self.assertTrue(any(call[0][0] == "pull" for call in cmd_calls))

    def test_default_dump_keeps_legacy_transport(self) -> None:
        adb = Adb.__new__(Adb)
        adb._dump_transport = None
        cmd_calls: list[tuple[tuple[str, ...], float]] = []
        xml = '<hierarchy rotation="0"><node text="Quotes" bounds="[0,0][1,1]" /></hierarchy>'

        def cmd(*args: str, timeout: float, check: bool) -> Mock:
            cmd_calls.append((args, timeout))
            return Mock(returncode=0, stdout=xml, stderr="")

        adb.cmd = cmd

        nodes = adb.dump_xml()

        self.assertEqual([item.text for item in nodes], ["Quotes"])
        self.assertEqual(cmd_calls[0][0][:3], ("exec-out", "uiautomator", "dump"))
        self.assertFalse(any("app_process" in call[0] for call in cmd_calls))

    def test_zero_idle_helper_install_is_atomic(self) -> None:
        adb = Adb.__new__(Adb)
        adb._zero_idle_dump_helper_ready = False
        shell_calls: list[tuple[str, ...]] = []
        cmd_calls: list[tuple[str, ...]] = []

        def shell(*args: str, timeout: float, check: bool) -> Mock:
            shell_calls.append(args)
            return Mock(returncode=1 if args[0] == "test" else 0, stdout="", stderr="")

        def cmd(*args: str, timeout: float, check: bool) -> Mock:
            cmd_calls.append(args)
            return Mock(returncode=0, stdout="", stderr="")

        adb.shell = shell
        adb.cmd = cmd

        adb._ensure_zero_idle_dump_helper()

        push = next(call for call in cmd_calls if call[0] == "push")
        move = next(call for call in shell_calls if call[0] == "mv")
        self.assertTrue(push[2].endswith(".tmp"))
        self.assertEqual(move[2], push[2])
        self.assertNotEqual(move[3], push[2])
        self.assertTrue(adb._zero_idle_dump_helper_ready)


if __name__ == "__main__":
    unittest.main()
