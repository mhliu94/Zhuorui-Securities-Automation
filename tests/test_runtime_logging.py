"""Runtime diagnostics preserve useful evidence without leaking private values."""
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zhuorui.common.runtime_logging import log_event
from zhuorui.monitor.server import RedirectRequestHandler, StatusMonitor, ZhuoruiRequestHandler


class RuntimeLoggingTests(unittest.TestCase):
    def test_timestamp_process_and_safe_exception_chain_without_source_or_values(self):
        output = io.StringIO()
        try:
            try:
                raise OSError(5, "private-token-and-password", "private-account-file")
            except OSError:
                raise RuntimeError("private-broker-response") from None
        except RuntimeError as error:
            with redirect_stdout(output):
                log_event("test", "Operation failed.", level="ERROR", error=error, stage="kafka_ack")
        line = output.getvalue()
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ERROR pid=\d+ thread=")
        self.assertNotIn("private-", line)
        self.assertEqual(len(line.splitlines()), 1)
        fields = json.loads(line[line.index("{"):])
        self.assertEqual(fields["stage"], "kafka_ack")
        self.assertEqual([entry["type"] for entry in fields["errors"]], ["RuntimeError", "OSError"])
        self.assertEqual(fields["errors"][1]["errno"], 5)
        self.assertTrue(all(entry["frames"] for entry in fields["errors"]))

    def test_closed_or_full_output_stream_does_not_propagate(self):
        for error in (OSError("disk full"), ValueError("closed stream")):
            with self.subTest(error=type(error).__name__), patch("builtins.print", side_effect=error):
                log_event("test", "Heartbeat.")

    def test_http_logs_escape_unicode_and_newlines_on_windows_output_stream(self):
        for handler in (RedirectRequestHandler, ZhuoruiRequestHandler):
            with self.subTest(handler=handler.__name__):
                data = io.BytesIO()
                output = io.TextIOWrapper(data, encoding="cp1252", errors="strict")
                request = SimpleNamespace(client_address=("127.0.0.1", 1234))
                with redirect_stdout(output):
                    handler.log_message(request, "%s", "bad-request-\u4e2d\u6587\nFORGED LINE")
                output.flush()
                log = data.getvalue().decode("cp1252")
                self.assertEqual(len(log.splitlines()), 1)
                self.assertIn(r"\u4e2d\u6587\nFORGED LINE", log)
                output.close()
                with patch("builtins.print", side_effect=OSError("log unavailable")):
                    handler.log_message(request, "%s", "bad request")

    def test_monitor_records_disappearance_and_throttles_unchanged_status(self):
        controller = Mock()
        controller.collect_status.return_value = {"script": {"state": "running", "running": True, "pid": 42}}
        monitor = StatusMonitor(controller)
        with patch("zhuorui.monitor.server.log_event") as log, \
             patch("zhuorui.monitor.server.time.monotonic", return_value=100):
            monitor.refresh()
            monitor.refresh()
            self.assertEqual(log.call_count, 1)
            controller.collect_status.return_value = {"script": {"state": "stopped", "running": False}}
            monitor.refresh()
            self.assertEqual(log.call_count, 2)
            self.assertEqual(log.call_args.args[1], "Listener status changed.")
            self.assertFalse(log.call_args.kwargs["running"])
            self.assertEqual(log.call_args.kwargs["level"], "WARNING")
        with patch("zhuorui.monitor.server.log_event") as log, \
             patch("zhuorui.monitor.server.time.monotonic", return_value=161):
            monitor.refresh()
            self.assertEqual(log.call_args.args[1], "Control Room heartbeat.")


if __name__ == "__main__":
    unittest.main()
