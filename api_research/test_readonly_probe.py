"""Read-only endpoint/auth guards, with all network entry points mocked."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mitmproxy import http

import probe_holdings_api as probe
from probe_public_api import canonical, find_signing_key, signature


NOW = 2000.0
READ_PATH = "/as_trade/api/order/v1/get_hold_list"


def capture(*, path=READ_PATH, token="synthetic-session", stamp=1999000, status=200, code="000000"):
    request = http.Request.make("POST", "https://backendpro.zr.hk" + path,
        json.dumps({"market": 1, "timeStamp": stamp, "sign": "synthetic-signature"}),
        {"token": token, "content-type": "application/json"})
    response = http.Response.make(status, json.dumps({"code": code, "data": {"holdList": []}}),
                                  {"content-type": "application/json"})
    return SimpleNamespace(request=request, response=response)


class ReadOnlyProbeTests(unittest.TestCase):
    def assert_no_network(self, flow):
        with patch.object(probe, "select_capture", return_value=flow), patch.object(probe.time, "time", return_value=NOW), patch.object(probe.urllib.request, "build_opener") as network:
            with self.assertRaises(ValueError):
                probe.run_probe("holdings", send=True)
            network.assert_not_called()

    def test_entry_amend_cancel_and_login_paths_are_blocked_before_network(self):
        for path in ["/as_trade/api/order/v1/entrust_enter", "/as_trade/api/order/v1/entrust_modify",
                     "/as_trade/api/order/v1/entrust_withdraw", "/as_user/api/user_account/v1/user_login_pwd",
                     READ_PATH + "?next=/as_trade/api/order/v1/entrust_enter"]:
            with self.subTest(path=path):
                self.assert_no_network(capture(path=path))

    def test_host_scheme_and_port_are_not_overridable(self):
        for attribute, value in [("host", "example.invalid"), ("scheme", "http"), ("port", 80), ("method", "DELETE")]:
            with self.subTest(attribute=attribute):
                flow = capture()
                setattr(flow.request, attribute, value)
                self.assert_no_network(flow)

    def test_missing_auth_cannot_trigger_login(self):
        self.assert_no_network(capture(token=""))

    def test_expired_or_future_capture_is_blocked(self):
        for stamp in [1000000, 2100000]:
            with self.subTest(stamp=stamp):
                self.assert_no_network(capture(stamp=stamp))

    def test_http_and_application_failures_are_blocked(self):
        self.assert_no_network(capture(status=401))
        self.assert_no_network(capture(code="000102"))

    def test_holdings_mode_cannot_send_cash_query(self):
        self.assert_no_network(capture(path="/as_trade/api/funds/v1/info"))

    def test_valid_recent_read_preserves_observed_payload(self):
        payload = probe.validate_capture(capture(), "holdings", now=NOW)
        self.assertEqual(payload["market"], 1)
        self.assertEqual(payload["timeStamp"], 1999000)

    def test_bad_timestamp_values_are_blocked(self):
        for stamp in [True, 0, "1999000"]:
            with self.subTest(stamp=stamp):
                self.assert_no_network(capture(stamp=stamp))

    def test_no_matching_capture_sends_nothing(self):
        with patch.object(probe, "select_capture", side_effect=ValueError("No capture")), patch.object(probe.urllib.request, "build_opener") as network:
            with self.assertRaises(ValueError):
                probe.run_probe("holdings", send=True)
            network.assert_not_called()

    def test_offline_prepare_uses_no_network(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        flow = capture()
        with patch.object(probe, "select_capture", return_value=flow), patch.object(probe, "find_signing_key", return_value=key), patch.object(probe.urllib.request, "build_opener") as network:
            result = probe.run_probe("holdings", send=False)
        self.assertFalse(result["request_sent"])
        network.assert_not_called()

    def test_shared_signer_matches_observation_and_rejects_wrong_signature(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        payload = {"timeStamp": 123, "market": 1}
        expected = signature(key, payload)
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "fixture.apk"
            with ZipFile(apk, "w") as archive:
                archive.writestr("assets/config.properties", "private_key=" + base64.b64encode(der).decode())
            observed_key = find_signing_key(apk, payload, expected)
            self.assertEqual(signature(observed_key, payload), expected)
            with self.assertRaises(RuntimeError):
                find_signing_key(apk, payload, "wrong")
        self.assertEqual(canonical(payload), b'{"market":1,"timeStamp":123}')


if __name__ == "__main__":
    unittest.main()
