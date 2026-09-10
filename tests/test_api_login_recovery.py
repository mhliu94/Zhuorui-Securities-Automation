"""Scheduled login recovery with synthetic sessions and no broker/ADB calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zhuorui.api.config import ApiSettings
from zhuorui.api.errors import ApiError, LoggedInElsewhere, LoginBlocked, SessionError, SessionExpired
from zhuorui.api.listener import ClientProvider
from zhuorui.api.listener_config import load_listener_settings
from zhuorui.api.recovery import BEIJING


CONFIG = {"account_id": "synthetic-account", "account_num_id": 7, "server_id": "synthetic-server",
          "kafka": {"bootstrap_servers": "unused.invalid:9092"},
          "api": {"auto_login_enabled": True, "auto_import_session": True}}


def at_beijing(hour, minute=0, second=0):
    return datetime(2026, 9, 11, hour, minute, second, tzinfo=BEIJING).timestamp()


def synthetic_session(token="a" * 32):
    return {"version": 1, "headers": {"userid": "c" * 32, "token": token, "deviceid": "synthetic-device"},
            "generation": hashlib.sha256(token.encode()).hexdigest(), "signing_key": "synthetic-only"}


class ApiLoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.session_file = self.root / "session.dpapi"
        self.api_settings = ApiSettings(self.session_file, self.root / "unused-capture", self.root / "unused-apk")
        self.settings = load_listener_settings(self.root / "synthetic-config.json", CONFIG)
        self.now = at_beijing(10)
        self.state = Mock()
        self.current_session = None
        self.mtime = 1_800_000_000_000_000_000
        self.write_session(synthetic_session())
        self.load_patch = patch("zhuorui.api.listener.load_session", side_effect=lambda *_: deepcopy(self.current_session))
        self.client_patch = patch("zhuorui.api.listener.ApiClient", side_effect=lambda session, _: SimpleNamespace(
            session=deepcopy(session), headers=dict(session["headers"])))
        self.load_patch.start()
        self.client_patch.start()
        self.provider = self.new_provider()
        self.original_client = self.provider()

    def tearDown(self):
        self.client_patch.stop()
        self.load_patch.stop()
        self.directory.cleanup()

    def new_provider(self, *, config=None):
        return ClientProvider(self.root / "synthetic-config.json", CONFIG if config is None else config,
                              self.api_settings, self.settings, self.state, wall=lambda: self.now)

    def write_session(self, session):
        self.current_session = deepcopy(session)
        self.session_file.write_bytes(b"synthetic encrypted-session placeholder")
        self.mtime += 1_000_000
        os.utime(self.session_file, ns=(self.mtime, self.mtime))

    def login_success(self, *args, **kwargs):
        recovered = synthetic_session("b" * 32)
        self.write_session(recovered)
        return recovered

    def observe(self, error=None):
        return self.provider.report_error(error or SessionExpired("Session expired"), client=self.original_client)

    def test_confirmed_logout_records_first_detection_once_and_persists_without_secrets(self):
        self.observe()
        self.assertEqual(self.provider.schedule.detected_at, self.now)
        self.assertEqual(self.provider.schedule.due_at, self.now + 300)
        original_due = self.provider.schedule.due_at
        self.now += 240
        self.observe()
        self.assertEqual(self.provider.schedule.due_at, original_due)
        saved = json.loads(self.provider.recovery_path.read_text())
        self.assertEqual(saved["schedule"]["due_at"], original_due)
        self.assertNotIn("a" * 32, self.provider.recovery_path.read_text())
        self.assertNotIn("signing_key", self.provider.recovery_path.read_text())

    def test_before_deadline_no_login_or_emulator_import_occurs(self):
        self.observe()
        self.now += 299
        with patch("zhuorui.api.auth.password_login") as login, \
             patch("zhuorui.api.emulator.import_emulator_session") as importer:
            self.assertFalse(self.provider.recover())
            self.provider.refresh_from_emulator()
        login.assert_not_called()
        importer.assert_not_called()
        with self.assertRaises(SessionExpired):
            self.provider()

    def test_due_attempt_replaces_cache_clears_schedule_and_requests_immediate_refresh(self):
        self.observe()
        due = self.provider.schedule.due_at
        self.now = due
        reserved_states = []

        def login(*args, **kwargs):
            reserved_states.append(json.loads(self.provider.recovery_path.read_text())["schedule"])
            return self.login_success(*args, **kwargs)

        with patch("zhuorui.api.auth.password_login", side_effect=login) as login, patch("builtins.print"):
            self.assertTrue(self.provider.recover())  # run_listener queues login_recovery on True.
        login.assert_called_once()
        self.assertEqual(reserved_states[0]["attempts"], 1)
        self.assertTrue(reserved_states[0]["in_progress"])
        self.assertEqual(reserved_states[0]["due_at"], due + 300)
        self.assertIsNot(self.provider(), self.original_client)
        self.assertEqual(self.provider.session["headers"]["token"], "b" * 32)
        self.assertIsNone(self.provider.schedule.detected_at)
        self.assertFalse(self.provider.recovery_needed)
        self.assertFalse(self.provider.displaced)
        self.assertEqual(self.state.update.call_args.kwargs["session_status"], "login_succeeded")

    def test_outside_beijing_window_displaced_login_is_immediately_due(self):
        self.now = at_beijing(16)
        self.observe(LoggedInElsewhere("Login displaced"))
        self.assertEqual(self.provider.schedule.due_at, self.now)
        with patch("zhuorui.api.auth.password_login", side_effect=self.login_success) as login, patch("builtins.print"):
            self.assertTrue(self.provider.recover())
        login.assert_called_once()

    def test_generic_transport_and_missing_cache_errors_do_not_invent_confirmed_logout(self):
        for error in (ApiError("Transport timeout"), OSError("Disconnected"), SessionError("Session file unavailable")):
            with self.subTest(error=type(error).__name__):
                self.provider.report_error(error, client=self.original_client)
                self.assertIsNone(self.provider.schedule.detected_at)
                with patch("zhuorui.api.auth.password_login") as login:
                    self.assertFalse(self.provider.recover())
                login.assert_not_called()

    def test_missing_verified_device_identity_prevents_password_submission(self):
        self.observe()
        self.now += 300
        self.session_file.unlink()
        with patch("zhuorui.api.auth.password_login") as login:
            self.assertFalse(self.provider.recover())
        login.assert_not_called()

    def test_disabled_auto_login_preserves_schedule_without_sending_password(self):
        self.provider.settings = replace(self.settings, auto_login_enabled=False)
        self.observe()
        self.now += 301
        with patch("zhuorui.api.auth.password_login") as login:
            self.assertFalse(self.provider.recover())
        login.assert_not_called()
        self.assertIsNotNone(self.provider.schedule.detected_at)

    def test_permanent_login_rejection_blocks_retries_and_survives_restart(self):
        self.observe()
        self.now += 300
        with patch("zhuorui.api.auth.password_login", side_effect=LoginBlocked("Verification is required")) as login:
            self.assertFalse(self.provider.recover())
            self.now += 10000
            self.assertFalse(self.provider.recover())
        login.assert_called_once()
        self.assertEqual(self.provider.schedule.blocked_reason, "login_rejected")
        restored = self.new_provider()
        restored.bootstrap()
        self.assertEqual(restored.schedule.blocked_reason, "login_rejected")
        with patch("zhuorui.api.auth.password_login") as login:
            self.assertFalse(restored.recover())
        login.assert_not_called()

    def test_transient_failure_retries_after_configured_interval_without_resetting_detection(self):
        self.provider.settings = replace(self.settings, login_retry_seconds=60)
        self.provider.schedule.retry_seconds = 60
        detected = self.now
        self.observe()
        self.now += 300
        with patch("zhuorui.api.auth.password_login", side_effect=ApiError("Temporary transport failure")) as login:
            self.assertFalse(self.provider.recover())
            self.now += 59
            self.assertFalse(self.provider.recover())
        login.assert_called_once()
        self.assertEqual(self.provider.schedule.detected_at, detected)
        self.assertEqual(self.provider.schedule.due_at, detected + 360)
        self.assertIsNone(self.provider.schedule.blocked_reason)
        self.now += 1
        with patch("zhuorui.api.auth.password_login", side_effect=self.login_success) as login, patch("builtins.print"):
            self.assertTrue(self.provider.recover())
        login.assert_called_once()

    def test_restart_and_same_token_reimport_preserve_original_five_minute_deadline(self):
        self.observe()
        original_due = self.provider.schedule.due_at
        self.now += 200
        self.write_session(synthetic_session())
        restored = self.new_provider()
        restored.bootstrap()
        self.assertEqual(restored.schedule.due_at, original_due)
        self.assertEqual(restored.schedule.detected_at, original_due - 300)
        with patch("zhuorui.api.auth.password_login") as login:
            self.assertFalse(restored.recover())
        login.assert_not_called()

    def test_changed_session_clears_old_schedule_and_late_old_client_results_are_ignored(self):
        self.observe()
        self.write_session(synthetic_session("b" * 32))
        replacement = self.provider()
        self.state.reset_mock()
        self.assertIsNone(self.provider.schedule.detected_at)
        self.assertFalse(self.provider.report_error(SessionExpired("Late failure"), client=self.original_client))
        self.assertFalse(self.provider.report_success(self.original_client, last_holdings_reason="old"))
        self.state.update.assert_not_called()
        self.state.holdings_recovered.assert_not_called()
        self.assertTrue(self.provider.report_success(replacement, last_holdings_reason="new"))
        self.state.holdings_recovered.assert_called_once()

    def test_success_from_invalidated_current_client_does_not_clear_pending_login(self):
        self.observe()
        self.state.reset_mock()
        self.assertFalse(self.provider.report_success(self.original_client, last_holdings_reason="late-read"))
        self.state.holdings_recovered.assert_not_called()
        self.assertIsNotNone(self.provider.schedule.detected_at)

    def test_bootstrap_prefers_existing_encrypted_cache_over_emulator_token(self):
        with patch("zhuorui.api.emulator.import_emulator_session") as importer:
            self.provider.bootstrap()
        importer.assert_not_called()
        self.assertIs(self.provider(), self.original_client)

    def test_bootstrap_can_import_missing_identity_but_does_not_password_login(self):
        self.session_file.unlink()
        with patch("zhuorui.api.listener.time.monotonic", return_value=100), \
             patch("zhuorui.api.emulator.import_emulator_session") as importer, \
             patch("zhuorui.api.auth.password_login") as login:
            self.provider.bootstrap()
        importer.assert_called_once()
        login.assert_not_called()

    def test_changed_manual_session_during_login_makes_old_failure_obsolete(self):
        self.observe()
        self.now += 300

        def superseded_login(*args, **kwargs):
            self.write_session(synthetic_session("b" * 32))
            # The publishing thread may not replace an in-progress login's cache.
            with self.assertRaises(SessionExpired):
                self.provider()
            raise ApiError("Old attempt failed after a newer session became available")

        with patch("zhuorui.api.auth.password_login", side_effect=superseded_login):
            self.assertFalse(self.provider.recover())
        replacement = self.provider()  # The next call imports the now-stable manual session.
        self.assertIsNone(self.provider.schedule.detected_at)
        self.assertEqual(replacement.headers["token"], "b" * 32)

    def test_invalid_or_other_account_persisted_state_is_a_safe_session_error(self):
        for raw in ("[]", "null", "{}"):
            with self.subTest(raw=raw):
                self.provider.recovery_path.write_text(raw)
                with self.assertRaises(SessionError):
                    self.new_provider()
        self.provider.recovery_path.unlink()
        self.observe()
        with self.assertRaises(SessionError):
            self.new_provider(config={**CONFIG, "account_num_id": 8})


if __name__ == "__main__":
    unittest.main()
