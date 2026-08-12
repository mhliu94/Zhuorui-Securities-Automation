import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from zhuorui_market_order import (
    AD_CLASSIFICATION_RECHECK_SECONDS,
    AD_DISMISS_SETTLE_SECONDS,
    FAST_AD_CLOSE_BUTTON,
    PACKAGE,
    AdScreenClassification,
    AdScreenProfile,
    Adb,
    EMPTY_POSITIONS_LABEL,
    LOGIN_DELAY_SECONDS,
    ORDER_RESTART_SETTLE_SECONDS,
    Bounds,
    LightRegionStats,
    TradingCommand,
    UiNode,
    ZhuoruiAutomationError,
    ZhuoruiTrader,
    classify_ad_screen,
    login_delay_seconds,
    main,
    submit_trading_command,
)


def node(
    text: str = "",
    resource_id: str = "",
    bounds: Bounds | None = None,
    clickable: bool = False,
) -> UiNode:
    return UiNode(
        text=text,
        hint="",
        content_desc="",
        resource_id=resource_id,
        klass="android.view.View",
        clickable=clickable,
        focusable=False,
        focused=False,
        password=False,
        bounds=bounds or Bounds(0, 0, 100, 100),
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


class AdScreenDetectionTests(unittest.TestCase):
    @staticmethod
    def classification(kind: str) -> AdScreenClassification:
        stats = LightRegionStats(
            mean=50.0,
            dark_fraction=1.0,
            bright_fraction=0.0,
            sample_count=10,
        )
        return AdScreenClassification(
            kind=kind,
            profile=AdScreenProfile(
                width=1080,
                height=2424,
                center=stats,
                outer=stats,
                lower=stats,
                footer=stats,
            ),
        )

    @staticmethod
    def save_screen(path: Path, background: int, center: int, bottom: int | None = None) -> None:
        from PIL import Image, ImageDraw

        width, height = 300, 600
        image = Image.new("RGB", (width, height), (background,) * 3)
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (round(width * 0.22), round(height * 0.22), round(width * 0.78), round(height * 0.70)),
            fill=(center,) * 3,
        )
        if bottom is not None:
            draw.rectangle(
                (round(width * 0.04), round(height * 0.76), round(width * 0.96), round(height * 0.965)),
                fill=(bottom,) * 3,
            )
        image.save(path)

    def test_gigamoney_dark_backdrop_rule_detects_ad(self) -> None:
        with TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "ad.png"
            self.save_screen(path, background=45, center=220)

            self.assertEqual(classify_ad_screen(path).kind, "Ad")

    def test_zhuorui_midgray_backdrop_rule_detects_large_ad(self) -> None:
        with TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "ad.png"
            self.save_screen(path, background=127, center=210)

            self.assertEqual(classify_ad_screen(path).kind, "Ad")

    def test_lit_bottom_keeps_confirmation_screen_safe(self) -> None:
        with TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "confirmation.png"
            self.save_screen(path, background=80, center=220, bottom=210)

            self.assertEqual(classify_ad_screen(path).kind, "ConfirmationLike")

    def test_uniform_screen_is_not_an_ad(self) -> None:
        with TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "normal.png"
            self.save_screen(path, background=235, center=235, bottom=235)

            self.assertEqual(classify_ad_screen(path).kind, "None")

    def test_stable_classifier_requires_two_ad_frames(self) -> None:
        trader = ZhuoruiTrader(FakeAdb([]))
        trader.current_ad_screen_classification = Mock(
            side_effect=[self.classification("Ad"), self.classification("Ad")]
        )

        with patch("zhuorui_market_order.time.sleep") as sleep:
            result = trader.stable_ad_screen_classification()

        self.assertEqual(result.kind, "Ad")
        sleep.assert_called_once_with(AD_CLASSIFICATION_RECHECK_SECONDS)

    def test_inconsistent_ad_frames_are_not_clicked(self) -> None:
        trader = ZhuoruiTrader(FakeAdb([]))
        trader.current_ad_screen_classification = Mock(
            side_effect=[self.classification("Ad"), self.classification("ConfirmationLike")]
        )

        with patch("zhuorui_market_order.time.sleep"):
            result = trader.stable_ad_screen_classification()

        self.assertEqual(result.kind, "ConfirmationLike")

    def test_confirmed_ad_taps_fixed_close_point_and_waits(self) -> None:
        adb = FakeAdb([])
        trader = ZhuoruiTrader(adb)
        trader.stable_ad_screen_classification = Mock(return_value=self.classification("Ad"))

        with patch("zhuorui_market_order.time.sleep") as sleep:
            handled = trader.dismiss_ad_screen_if_present()

        self.assertTrue(handled)
        self.assertEqual(adb.taps, [FAST_AD_CLOSE_BUTTON])
        sleep.assert_called_once_with(AD_DISMISS_SETTLE_SECONDS)

    def test_foreground_check_runs_ad_guard(self) -> None:
        adb = FakeAdb([])
        adb.foreground_package = Mock(return_value=PACKAGE)
        trader = ZhuoruiTrader(adb)
        trader.dismiss_ad_screen_if_present = Mock(return_value=False)

        trader.ensure_app_foreground(launch_if_needed=True)

        trader.dismiss_ad_screen_if_present.assert_called_once_with()

    def test_launch_runs_ad_guard_immediately_after_foreground_wait(self) -> None:
        adb = Mock()
        trader = ZhuoruiTrader(adb)
        events: list[str] = []
        trader.wait_for_app_foreground = Mock(side_effect=lambda **_kwargs: events.append("foreground"))
        trader.dismiss_ad_screen_if_present = Mock(side_effect=lambda: events.append("ad_guard"))

        with patch("zhuorui_market_order.time.sleep"):
            trader.launch()

        self.assertEqual(events, ["foreground", "ad_guard"])


class WatchlistUiLookupTests(unittest.TestCase):
    class RecordingAdb(FakeAdb):
        def __init__(self, nodes: list[UiNode]):
            super().__init__(nodes)
            self.idle_timeouts: list[int | None] = []

        def dump_xml(self, idle_timeout_ms: int | None = None) -> list[UiNode]:
            self.idle_timeouts.append(idle_timeout_ms)
            return self.nodes

    @staticmethod
    def watchlist_nodes() -> list[UiNode]:
        return [
            node(resource_id="com.zhuorui.securities:id/vStock", bounds=Bounds(42, 667, 1038, 831)),
            node("AAPL", "com.zhuorui.securities:id/vCode", Bounds(95, 756, 175, 798)),
            node("308.260", "com.zhuorui.securities:id/vLast", Bounds(536, 689, 766, 763)),
            node("308.920", "com.zhuorui.securities:id/vBALast", Bounds(536, 763, 766, 818)),
            node(resource_id="com.zhuorui.securities:id/vStock", bounds=Bounds(42, 831, 1038, 995)),
            node("GOOG", "com.zhuorui.securities:id/vCode", Bounds(95, 920, 185, 962)),
            node("355.840", "com.zhuorui.securities:id/vLast", Bounds(536, 853, 766, 927)),
        ]

    def test_exact_symbol_uses_regular_last_price_from_same_row(self) -> None:
        trader = ZhuoruiTrader(FakeAdb([]))

        match = trader.watchlist_symbol_match(self.watchlist_nodes(), "aapl")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.tap_point, (135, 777))
        self.assertEqual(match.last_price, Decimal("308.260"))
        self.assertIsNone(trader.watchlist_symbol_match(self.watchlist_nodes(), "AAP"))

    def test_open_symbol_uses_zero_idle_dump_without_screenshot_ocr(self) -> None:
        adb = self.RecordingAdb(self.watchlist_nodes())
        trader = ZhuoruiTrader(adb)
        trader.return_to_watchlist_landing = Mock()
        trader.tap_quotes_tab_fast = Mock()

        with patch("zhuorui_market_order.time.sleep"):
            price = trader.open_symbol_from_watchlist("AAPL")

        self.assertEqual(price, Decimal("308.260"))
        self.assertEqual(adb.idle_timeouts, [0])
        self.assertEqual(adb.taps, [(135, 777)])

        trader.current_nodes()
        self.assertEqual(adb.idle_timeouts, [0, None])

    def test_missing_watchlist_price_still_opens_symbol(self) -> None:
        nodes = [
            node(resource_id="com.zhuorui.securities:id/vStock", bounds=Bounds(42, 667, 1038, 831)),
            node("AAPL", "com.zhuorui.securities:id/vCode", Bounds(95, 756, 175, 798)),
        ]
        adb = self.RecordingAdb(nodes)
        trader = ZhuoruiTrader(adb)
        trader.return_to_watchlist_landing = Mock()
        trader.tap_quotes_tab_fast = Mock()

        with patch("zhuorui_market_order.time.sleep"):
            price = trader.open_symbol_from_watchlist("AAPL")

        self.assertIsNone(price)
        self.assertEqual(adb.taps, [(135, 777)])

    def test_not_found_uses_two_zero_idle_dumps(self) -> None:
        adb = self.RecordingAdb(self.watchlist_nodes())
        trader = ZhuoruiTrader(adb)
        trader.return_to_watchlist_landing = Mock()
        trader.tap_quotes_tab_fast = Mock()

        with (
            patch("zhuorui_market_order.time.sleep"),
            self.assertRaisesRegex(ZhuoruiAutomationError, "MSFT was not found"),
        ):
            trader.open_symbol_from_watchlist("MSFT")

        self.assertEqual(adb.idle_timeouts, [0, 0])
        self.assertEqual(adb.taps, [])

    def test_duplicate_symbol_rows_fail_safely_after_recheck(self) -> None:
        nodes = self.watchlist_nodes() + [
            node(resource_id="com.zhuorui.securities:id/vStock", bounds=Bounds(42, 995, 1038, 1159)),
            node("AAPL", "com.zhuorui.securities:id/vCode", Bounds(95, 1084, 175, 1126)),
            node("309.000", "com.zhuorui.securities:id/vLast", Bounds(536, 1017, 766, 1091)),
        ]
        adb = self.RecordingAdb(nodes)
        trader = ZhuoruiTrader(adb)
        trader.return_to_watchlist_landing = Mock()
        trader.tap_quotes_tab_fast = Mock()

        with (
            patch("zhuorui_market_order.time.sleep"),
            self.assertRaisesRegex(ZhuoruiAutomationError, "Multiple visible watchlist rows"),
        ):
            trader.open_symbol_from_watchlist("AAPL")

        self.assertEqual(adb.idle_timeouts, [0, 0])
        self.assertEqual(adb.taps, [])

    def test_clean_second_lookup_does_not_report_stale_ambiguity(self) -> None:
        duplicate_nodes = self.watchlist_nodes() + [
            node(resource_id="com.zhuorui.securities:id/vStock", bounds=Bounds(42, 995, 1038, 1159)),
            node("AAPL", "com.zhuorui.securities:id/vCode", Bounds(95, 1084, 175, 1126)),
        ]

        class SequencedAdb(self.RecordingAdb):
            def __init__(self) -> None:
                super().__init__([])
                self.responses = [duplicate_nodes, [node("GOOG", "com.zhuorui.securities:id/vCode")]]

            def dump_xml(self, idle_timeout_ms: int | None = None) -> list[UiNode]:
                self.idle_timeouts.append(idle_timeout_ms)
                return self.responses.pop(0)

        adb = SequencedAdb()
        trader = ZhuoruiTrader(adb)
        trader.return_to_watchlist_landing = Mock()
        trader.tap_quotes_tab_fast = Mock()

        with (
            patch("zhuorui_market_order.time.sleep"),
            self.assertRaisesRegex(ZhuoruiAutomationError, "AAPL was not found"),
        ):
            trader.open_symbol_from_watchlist("AAPL")


class OrderRetryTests(unittest.TestCase):
    @staticmethod
    def command(order_type: str = "market") -> TradingCommand:
        return TradingCommand(
            command_id=f"retry-{order_type}",
            symbol="AAPL",
            side="buy",
            quantity=1,
            order_type=order_type,
            limit_price=Decimal("300") if order_type in {"limit", "fok"} else None,
        )

    def test_first_failure_restarts_then_retries_once(self) -> None:
        trader = Mock()
        expected = {"prepared_order_type": "limit", "resolved_quantity": 1}

        with patch(
            "zhuorui_market_order.submit_trading_command_once",
            side_effect=[ZhuoruiAutomationError("first failure"), expected],
        ) as attempt:
            result = submit_trading_command(
                trader,
                self.command(),
                "secret",
                assume_current_symbol=True,
                launch_if_needed=False,
            )

        self.assertEqual(result, expected)
        self.assertEqual(attempt.call_count, 2)
        self.assertTrue(attempt.call_args_list[0].kwargs["assume_current_symbol"])
        self.assertFalse(attempt.call_args_list[0].kwargs["launch_if_needed"])
        self.assertFalse(attempt.call_args_list[1].kwargs["assume_current_symbol"])
        self.assertTrue(attempt.call_args_list[1].kwargs["launch_if_needed"])
        trader.restart_app_for_order_retry.assert_called_once_with()

    def test_success_does_not_restart(self) -> None:
        trader = Mock()
        expected = {"prepared_order_type": "limit", "resolved_quantity": 1}

        with patch("zhuorui_market_order.submit_trading_command_once", return_value=expected) as attempt:
            result = submit_trading_command(trader, self.command("limit"), "secret")

        self.assertEqual(result, expected)
        attempt.assert_called_once()
        trader.restart_app_for_order_retry.assert_not_called()

    def test_second_failure_is_final_and_does_not_restart_again(self) -> None:
        trader = Mock()

        with (
            patch(
                "zhuorui_market_order.submit_trading_command_once",
                side_effect=[ZhuoruiAutomationError("first failure"), ZhuoruiAutomationError("retry failure")],
            ) as attempt,
            self.assertRaisesRegex(ZhuoruiAutomationError, "initial attempt.*retry failure"),
        ):
            submit_trading_command(trader, self.command("fok"), "secret")

        self.assertEqual(attempt.call_count, 2)
        trader.restart_app_for_order_retry.assert_called_once_with()

    def test_restart_failure_prevents_second_order_attempt(self) -> None:
        trader = Mock()
        trader.restart_app_for_order_retry.side_effect = ZhuoruiAutomationError("restart failed")

        with (
            patch(
                "zhuorui_market_order.submit_trading_command_once",
                side_effect=ZhuoruiAutomationError("first failure"),
            ) as attempt,
            self.assertRaisesRegex(ZhuoruiAutomationError, "could not be restarted"),
        ):
            submit_trading_command(trader, self.command(), "secret")

        attempt.assert_called_once()

    def test_notional_market_recomputes_quantity_on_full_retry(self) -> None:
        trader = Mock()
        trader.open_symbol_from_watchlist.side_effect = [Decimal("100"), Decimal("200")]
        trader.read_quote_last_price.return_value = None
        trader.market_reference_price = None
        quantities: list[int] = []

        def prepare_order(**kwargs) -> None:
            quantities.append(kwargs["quantity"])
            if len(quantities) == 1:
                raise ZhuoruiAutomationError("ticket failed")
            trader.prepared_order_type_name = "limit"
            trader.prepared_limit_price = Decimal("210")

        trader.prepare_order.side_effect = prepare_order
        command = TradingCommand(
            command_id="retry-notional",
            symbol="AAPL",
            side="buy",
            quantity=None,
            order_type="market",
            limit_price=None,
            notional_usd=Decimal("1000"),
        )

        result = submit_trading_command(trader, command, "secret")

        self.assertEqual(quantities, [10, 5])
        self.assertEqual(result["resolved_quantity"], 5)
        self.assertEqual(result["notional_reference_price"], Decimal("200"))
        trader.restart_app_for_order_retry.assert_called_once_with()
        trader.submit_prepared_order.assert_called_once_with(password="secret")

    def test_fill_or_kill_retry_wrapper_preserves_revoke_delay(self) -> None:
        trader = Mock()
        trader.prepared_order_type_name = "limit"
        trader.prepared_limit_price = Decimal("300")
        trader.market_reference_price = None

        result = submit_trading_command(
            trader,
            self.command("fok"),
            "secret",
            revoke_delay=1.25,
        )

        self.assertEqual(result["resolved_quantity"], 1)
        trader.submit_fill_or_kill_order.assert_called_once_with(
            password="secret",
            revoke_delay=1.25,
        )

    def test_restart_force_stops_launches_and_waits_for_landing(self) -> None:
        adb = Mock()
        trader = ZhuoruiTrader(adb)
        trader.prepared_submit = Mock()
        trader.prepared_order_type_name = "limit"
        trader.prepared_limit_price = Decimal("300")
        trader.market_reference_price = Decimal("295")
        events: list[object] = []
        adb.shell.side_effect = lambda *args, **kwargs: events.append(("shell", args, kwargs))
        trader.launch = Mock(side_effect=lambda: events.append("launch"))
        trader.wait_for_order_retry_landing = Mock(side_effect=lambda: events.append("stable_landing"))

        with patch(
            "zhuorui_market_order.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ):
            trader.restart_app_for_order_retry()

        self.assertEqual(events[0][0:2], ("shell", ("am", "force-stop", PACKAGE)))
        self.assertEqual(events[1:], [("sleep", ORDER_RESTART_SETTLE_SECONDS), "launch", "stable_landing"])
        self.assertIsNone(trader.prepared_submit)
        self.assertIsNone(trader.prepared_order_type_name)
        self.assertIsNone(trader.prepared_limit_price)
        self.assertIsNone(trader.market_reference_price)

    def test_restart_landing_requires_two_consecutive_zero_idle_reads(self) -> None:
        adb = Mock()
        trader = ZhuoruiTrader(adb)
        splash = [node("Zhuorui")]
        landing = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Assets"),
            node("News"),
        ]
        trader.current_nodes = Mock(side_effect=[splash, landing, landing])

        with patch("zhuorui_market_order.time.sleep"):
            trader.wait_for_order_retry_landing()

        self.assertEqual(trader.current_nodes.call_count, 3)
        for read in trader.current_nodes.call_args_list:
            self.assertEqual(read.kwargs, {"idle_timeout_ms": 0})

    def test_restart_landing_rechecks_for_a_delayed_startup_ad(self) -> None:
        trader = ZhuoruiTrader(Mock())
        splash = [node("Zhuorui")]
        landing = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Assets"),
            node("News"),
        ]
        trader.current_nodes = Mock(side_effect=[splash, splash, splash, splash, landing, landing])
        trader.dismiss_ad_screen_if_present = Mock(return_value=True)

        with patch("zhuorui_market_order.time.sleep"):
            trader.wait_for_order_retry_landing()

        trader.dismiss_ad_screen_if_present.assert_called_once_with()

    def test_restart_landing_gets_a_fresh_deadline_after_login(self) -> None:
        trader = ZhuoruiTrader(Mock())
        logged_out = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Open A/C"),
            node("News"),
        ]
        logged_in = [
            node(resource_id="com.zhuorui.securities:id/bottomBar"),
            node("Quotes"),
            node("Assets"),
            node("News"),
        ]
        trader.current_nodes = Mock(side_effect=[logged_out, logged_in, logged_in])
        clock = [0.0]

        def finish_slow_login(_nodes: list[UiNode]) -> None:
            clock[0] = 181.0

        trader.ensure_logged_in = Mock(side_effect=finish_slow_login)
        with (
            patch("zhuorui_market_order.time.monotonic", side_effect=lambda: clock[0]),
            patch("zhuorui_market_order.time.sleep"),
        ):
            trader.wait_for_order_retry_landing()

        trader.ensure_logged_in.assert_called_once_with(logged_out)
        self.assertEqual(trader.current_nodes.call_count, 3)

    def test_recognized_order_success_is_not_retried_for_cleanup_failure(self) -> None:
        trader = ZhuoruiTrader(Mock())
        trader.current_nodes = Mock(return_value=[node("Order submitted")])
        trader.maybe_enter_trading_password_from_screenshot = Mock(return_value=False)
        trader.dismiss_order_success_dialog = Mock(
            side_effect=ZhuoruiAutomationError("cleanup failed")
        )

        with patch("zhuorui_market_order.time.sleep"):
            trader.handle_confirmation_flow(password=None)

        trader.dismiss_order_success_dialog.assert_called_once()


class CliOrderRetryRoutingTests(unittest.TestCase):
    def test_all_live_cli_order_types_use_shared_retry_wrapper(self) -> None:
        cases = [
            (
                ["AAPL", "buy", "1", "--confirm-live-order"],
                "market",
                3.0,
            ),
            (
                [
                    "AAPL",
                    "sell",
                    "2",
                    "--order-type",
                    "limit",
                    "--limit-price",
                    "300",
                    "--confirm-live-order",
                ],
                "limit",
                3.0,
            ),
            (
                [
                    "AAPL",
                    "buy",
                    "1",
                    "--order-type",
                    "limit",
                    "--limit-price",
                    "300",
                    "--fill-or-kill",
                    "--revoke-delay",
                    "1.25",
                    "--confirm-live-order",
                ],
                "fok",
                1.25,
            ),
        ]
        for argv, expected_type, expected_delay in cases:
            with self.subTest(order_type=expected_type):
                trader = Mock()
                trader.prepared_limit_price = Decimal("300")
                trader.market_reference_price = Decimal("295") if expected_type == "market" else None
                runtime = Mock(
                    trader=trader,
                    trade_password="secret",
                    launch_app=False,
                )
                with (
                    patch("zhuorui_market_order.build_automation_runtime", return_value=runtime),
                    patch("zhuorui_market_order.submit_trading_command", return_value={}) as submit,
                ):
                    exit_code = main(argv)

                self.assertEqual(exit_code, 0)
                command = submit.call_args.args[1]
                self.assertEqual(command.order_type, expected_type)
                self.assertEqual(submit.call_args.kwargs["revoke_delay"], expected_delay)

    def test_dry_run_still_prepares_without_using_live_retry_wrapper(self) -> None:
        trader = Mock()
        trader.prepared_limit_price = Decimal("315")
        trader.market_reference_price = Decimal("300")
        runtime = Mock(trader=trader, trade_password="secret", launch_app=False)
        with (
            patch("zhuorui_market_order.build_automation_runtime", return_value=runtime),
            patch("zhuorui_market_order.submit_trading_command") as submit,
        ):
            exit_code = main(["AAPL", "buy", "1"])

        self.assertEqual(exit_code, 0)
        trader.prepare_order.assert_called_once()
        submit.assert_not_called()


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
