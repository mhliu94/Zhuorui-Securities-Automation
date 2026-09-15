"""Status-file fault injection and real Windows sharing-conflict regression tests."""
from contextlib import redirect_stdout
import ctypes
import errno
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_api_listener as listener_fixtures
from zhuorui.api.errors import ApiError
from zhuorui.api.listener import ClientProvider
from zhuorui.api.publishing import HoldingsPublisher, ListenerState


class ListenerStatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "listener-state.json"
        self.state = ListenerState(self.path, running=True, last_error=None)
        self.state.update(sequence=0)
        self.before = self.path.read_bytes()

    def test_replace_failure_preserves_snapshot_and_recovers_latest_memory(self):
        output = io.StringIO()
        with redirect_stdout(output):
            with patch.object(Path, "replace", side_effect=PermissionError(5, "private-marker")):
                self.assertFalse(self.state.update(sequence=1))
                self.assertFalse(self.state.update(sequence=2))
            self.assertEqual(self.path.read_bytes(), self.before)
            self.assertEqual(self.state.values["sequence"], 2)
            self.assertIsNone(self.state.values["last_error"])
            self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
            self.assertTrue(self.state.update(kafka_connected=True))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["sequence"], 2)
        self.assertTrue(saved["kafka_connected"])
        self.assertEqual(output.getvalue().count("WARNING:"), 1)
        self.assertEqual(output.getvalue().count("writes recovered"), 1)
        self.assertNotIn("private-marker", output.getvalue())

    def test_directory_or_temporary_creation_failure_is_nonfatal(self):
        for operation in ("pathlib.Path.mkdir", "zhuorui.api.publishing.tempfile.NamedTemporaryFile"):
            with self.subTest(operation=operation), patch(operation, side_effect=PermissionError()), \
                 patch("builtins.print"):
                self.assertFalse(self.state.update(sequence=1))
            self.assertEqual(self.path.read_bytes(), self.before)

    def test_full_disk_during_write_preserves_old_file_and_cleans_owned_temp(self):
        import tempfile
        original = tempfile.NamedTemporaryFile
        def full_disk(*args, **kwargs):
            handle = original(*args, **kwargs)
            handle.write = Mock(side_effect=OSError(errno.ENOSPC, "private-marker"))
            return handle
        with patch("zhuorui.api.publishing.tempfile.NamedTemporaryFile", side_effect=full_disk), \
             patch("builtins.print"):
            self.assertFalse(self.state.update(sequence=1))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_existing_other_temp_file_is_not_overwritten_or_deleted(self):
        other = self.path.with_suffix(".json.tmp")
        other.write_bytes(b"other writer")
        self.assertTrue(self.state.update(sequence=1))
        self.assertEqual(other.read_bytes(), b"other writer")

    def test_warning_output_failure_cannot_recreate_the_crash(self):
        with patch.object(Path, "replace", side_effect=PermissionError()), \
             patch("builtins.print", side_effect=OSError("log unavailable")):
            self.assertFalse(self.state.update(sequence=1))
        with patch("builtins.print", side_effect=OSError("log unavailable")):
            self.assertTrue(self.state.update(sequence=2))
        self.assertEqual(json.loads(self.path.read_text())["sequence"], 2)

    def test_concurrent_updates_keep_a_complete_snapshot_and_all_fields(self):
        failures = []
        def worker(number):
            try:
                for value in range(15):
                    self.state.update(**{"worker_" + str(number): value})
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        saved = json.loads(self.path.read_text())
        for i in range(3):
            self.assertEqual(saved["worker_" + str(i)], 14)
        self.assertEqual(saved, self.state.values)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing semantics")
    def test_windows_open_reader_blocks_replace_then_recovers_after_close(self):
        # Hold a genuine Windows handle without FILE_SHARE_DELETE.
        # https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = create(str(self.path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            with patch("builtins.print"):
                self.assertFalse(self.state.update(sequence=1))
                self.assertFalse(self.state.update(sequence=2))
            self.assertEqual(self.path.read_bytes(), self.before)
            self.assertEqual(self.state.values["sequence"], 2)
        finally:
            close(handle)
        with patch("builtins.print"):
            self.assertTrue(self.state.update(running=False))
        self.assertFalse(json.loads(self.path.read_text())["running"])
        self.assertEqual(json.loads(self.path.read_text())["sequence"], 2)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class HoldingsStateFailureTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state = ListenerState(root / "state.json", last_error=None)
        self.state.update(running=True)
        self.client = Mock()
        self.settings = SimpleNamespace(holdings_interval_seconds=3600, holdings_topic="synthetic",
                                        live_orders_enabled=False, login_retry_seconds=300)
        api = SimpleNamespace(session_file=root / "session.dpapi")
        self.provider = ClientProvider(root / "config.json", {}, api, self.settings, self.state)
        self.provider.cached = self.client
        self.provider._load_client = Mock(return_value=self.client)
        self.producer = Mock()
        self.publisher = HoldingsPublisher({}, self.settings, self.producer, self.provider, self.state)
        self.enterContext(patch("zhuorui.api.snapshot.account_snapshot",
                                return_value={"account_id": "synthetic-account"}))
        self.enterContext(patch("builtins.print"))

    def test_publisher_thread_survives_repeated_status_write_failures(self):
        completed = threading.Event()
        original_publish = self.publisher.publish
        def publish(reason):
            original_publish(reason)
            if self.publisher.published >= 3:
                completed.set()
        self.publisher.publish = publish
        # Startup plus two queued refreshes exercise three failed state writes.
        self.publisher.request("order_cancellation")
        self.publisher.request("order_cancellation")
        with patch.object(Path, "replace", side_effect=PermissionError()):
            self.publisher.start()
            try:
                self.assertTrue(completed.wait(timeout=5), "Publisher died or stopped processing")
                self.assertTrue(self.publisher.thread.is_alive())
                self.assertEqual(self.publisher.published, 3)
                self.assertEqual(self.state.values["holdings_publish_count"], 3)
                self.assertIsNone(self.state.values["last_error"])
                self.assertEqual(self.producer.send.call_count, 3)
            finally:
                self.publisher.close()
        self.assertTrue(self.state.update(running=False))
        saved = json.loads(self.state.path.read_text())
        self.assertEqual(saved["holdings_publish_count"], 3)
        self.assertFalse(saved["running"])

    def test_failed_query_can_report_error_while_status_file_is_blocked(self):
        self.client.query.side_effect = ApiError("Synthetic holdings failure")
        with patch.object(Path, "replace", side_effect=PermissionError()):
            self.publisher.publish("periodic")
            self.publisher.publish("periodic")
        self.assertEqual(self.state.values["last_error"], "Synthetic holdings failure")
        self.producer.send.assert_not_called()
        self.client.query.side_effect = None
        self.publisher.publish("periodic")
        self.assertEqual(self.publisher.published, 1)
        self.assertIsNone(self.state.values["last_error"])
        self.assertIsNone(self.state.values["last_holdings_error"])
        self.assertIsNone(json.loads(self.state.path.read_text())["last_error"])


class ListenerStateOutageLifecycleTests(unittest.TestCase):
    setUp = listener_fixtures.ApiListenerLifecycleTests.setUp
    tearDown = listener_fixtures.ApiListenerLifecycleTests.tearDown
    run_offline = listener_fixtures.ApiListenerLifecycleTests.run_offline

    def test_status_write_outage_does_not_skip_commands_or_cleanup(self):
        with patch.object(Path, "replace", side_effect=PermissionError()):
            self.assertEqual(self.run_offline(), 0)
        self.assertEqual(self.at_commit, ["submitted"])
        self.client.submit_order.assert_called_once()
        self.consumer.close.assert_called_once_with(autocommit=False, timeout_ms=10000)
        self.publisher.close.assert_called_once()
        self.producer.flush.assert_called_once()
        self.producer.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
