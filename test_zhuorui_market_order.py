import unittest
from datetime import datetime, timezone

from zhuorui_market_order import (
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


if __name__ == "__main__":
    unittest.main()
