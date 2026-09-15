import base64
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from zhuorui.api.client import ApiClient
from zhuorui.api.config import ApiSettings, load_settings
from zhuorui.api.errors import ApiError, SessionError, SessionExpired, LoggedInElsewhere
from zhuorui.api.orders import plan_order, plan_cancel
from zhuorui.api.session import binding, load_session, protect, save_session, session_from_flows, READ_PATHS
from zhuorui.api.signing import canonical
from zhuorui.common.config import ZhuoruiAutomationError


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CONFIG = {"account_id": "synthetic-account", "server_id": "test-server"}
SETTINGS = ApiSettings(Path("unused-session"), Path("unused-capture"), Path("unused-apk"))


def session():
    return {"version": 1, "binding": binding(CONFIG), "captured_at": 1999,
            "headers": {"token": "synthetic-secret", "userid": "test-user", "deviceid": "test-device"},
            "signing_key": base64.b64encode(KEY.private_bytes(serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode()}


class Reply(io.BytesIO):
    status = 200


class FakeOpener:
    def __init__(self, body):
        self.body, self.calls = body, []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return Reply(json.dumps(self.body).encode())


def flow(stamp=1999, code="000000", *, path=None, token="synthetic-secret"):
    request = SimpleNamespace(host="backendpro.zr.hk", scheme="https", port=443,
        method="POST", path=path or READ_PATHS["holdings"], timestamp_start=stamp,
        headers={"token": token, "userid": "test-user", "deviceid": "test-device"},
        get_text=lambda: json.dumps({"timeStamp": int(stamp * 1000), "sign": "synthetic-signature"}))
    response = SimpleNamespace(status_code=200, get_text=lambda: json.dumps({"code": code, "data": {"holdList": []}}))
    return SimpleNamespace(request=request, response=response)


class ApiConfigurationTests(unittest.TestCase):
    def test_paths_follow_config_directory_and_existing_fields_survive(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / "custom.json"
            p.write_text(json.dumps({**CONFIG, "kafka": {"command_topic": "unchanged"},
                                    "api": {"session_file": "local/session.dpapi"}}))
            cfg, settings = load_settings(p)
            self.assertEqual(cfg["kafka"]["command_topic"], "unchanged")
            self.assertEqual(settings.session_file, (Path(folder) / "local/session.dpapi").resolve())
            self.assertEqual(settings.cancel_after_seconds, 1)

    def test_missing_configuration_does_not_fall_back_to_another_account(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(ZhuoruiAutomationError):
                load_settings(Path(folder) / "missing.json")

    def test_invalid_api_timings_rejected(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / "config.json"
            for value in (0, -1, True, "NaN", "Infinity", "wrong"):
                with self.subTest(value=value):
                    p.write_text(json.dumps({"api": {"request_timeout_seconds": value}}))
                    with self.assertRaises(ZhuoruiAutomationError):
                        load_settings(p)


class OrderPlanTests(unittest.TestCase):
    def test_native_market_has_no_limit_price_or_session_override(self):
        plan = plan_order("bili", "buy", 1, "market")
        self.assertEqual(plan["unsigned_body"]["entrustProp"], "MO")
        self.assertNotIn("entrustPrice", plan["unsigned_body"])
        self.assertNotIn("allowPrePost", plan["unsigned_body"])
        self.assertFalse(plan["request_sent"])
        with self.assertRaises(ApiError):
            plan_order("BILI", "buy", 1, "market", price="25")

    def test_limit_price_is_serialized_in_cents(self):
        plan = plan_order("BILI", "buy", 1, "limit", price="25.1000", allow_pre_post=True)
        self.assertIn(b'"entrustPrice":25.10', canonical(plan))
        self.assertEqual(plan["unsigned_body"]["allowPrePost"], "Y")

    def test_timed_cancel_is_limit_with_dispatch_deadline_and_partial_fills(self):
        plan = plan_order("BILI", "buy", 1, "timed-cancel", price="25")
        self.assertEqual(plan["unsigned_body"]["entrustProp"], "LO")
        self.assertNotIn("timeInForce", plan["unsigned_body"])
        self.assertEqual(plan["cancellation"]["delay_seconds_from_dispatch"], 1)
        self.assertTrue(plan["cancellation"]["partial_fills_possible"])

    def test_cancel_uses_transaction_reference_only(self):
        self.assertEqual(plan_cancel("synthetic-ref")["unsigned_body"], {"orderTxnReference": "synthetic-ref"})

    def test_unsupported_or_invalid_orders_fail_before_any_transport(self):
        for kwargs in ({"side": "short"}, {"quantity": True}, {"quantity": 0}, {"kind": "fok"},
                       {"kind": "limit", "price": "NaN"}, {"kind": "limit", "price": "Infinity"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ApiError):
                plan_order(**{**dict(symbol="BILI", side="buy", quantity=1, kind="market"), **kwargs})


class ApiTransportTests(unittest.TestCase):
    def client(self, body):
        opener = FakeOpener(body)
        return ApiClient(session(), SETTINGS, opener=opener, now=lambda: 2000), opener

    def test_fresh_signed_read_uses_expected_host_and_timeout(self):
        client, opener = self.client({"code": "000000", "data": {"holdList": []}})
        self.assertEqual(client.query("holdings")["data"], {"holdList": []})
        req, options = opener.calls[0]
        self.assertEqual(req.full_url, "https://backendpro.zr.hk" + READ_PATHS["holdings"])
        self.assertEqual(json.loads(req.data)["timeStamp"], 2000000)
        self.assertEqual(options["timeout"], 10)

    def test_write_paths_cannot_reach_transport(self):
        client, opener = self.client({"code": "000000"})
        for path in ("/as_trade/api/order/v1/entrust_enter", "cancel", "refresh_token", "login", "holdings?next=order"):
            with self.subTest(path=path), self.assertRaises(ApiError):
                client.query(path)
        self.assertFalse(opener.calls)

    def test_auth_codes_have_distinct_errors_and_no_retry(self):
        for code, error in (("000102", SessionExpired), ("000112", LoggedInElsewhere)):
            client, opener = self.client({"code": code, "msg": "synthetic-secret-must-not-appear"})
            with self.subTest(code=code), self.assertRaises(error) as caught:
                client.query("account")
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertEqual(len(opener.calls), 1)

    def test_generic_rejection_does_not_claim_logout(self):
        client, opener = self.client({"code": "000001", "msg": "private detail"})
        with self.assertRaises(ApiError) as caught:
            client.query("account")
        self.assertNotIsInstance(caught.exception, SessionError)
        self.assertNotIn("private detail", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_environment_proxy_disabled_and_redirect_handler_installed(self):
        with patch("zhuorui.api.client.urllib.request.ProxyHandler") as proxy, patch("zhuorui.api.client.urllib.request.build_opener"):
            ApiClient(session(), SETTINGS)
        proxy.assert_called_once_with({})


class SessionImportTests(unittest.TestCase):
    def test_recent_valid_capture_imports_existing_identity_without_network(self):
        with patch("zhuorui.api.session.find_signing_key", return_value=KEY):
            value = session_from_flows([flow()], CONFIG, SETTINGS, now=2000)
        self.assertEqual(value["headers"]["token"], "synthetic-secret")
        self.assertEqual(value["binding"], binding(CONFIG))

    def test_stale_capture_is_rejected_before_key_access(self):
        with patch("zhuorui.api.session.find_signing_key") as key:
            with self.assertRaises(SessionError):
                session_from_flows([flow(stamp=1000)], CONFIG, SETTINGS, now=2000)
            key.assert_not_called()

    def test_latest_failed_read_cannot_fall_back_to_old_success(self):
        with self.assertRaises(SessionError):
            session_from_flows([flow(), flow(stamp=1999.5, code="000102")], CONFIG, SETTINGS, now=2000)

    def test_logout_on_other_endpoint_invalidates_prior_account_capture(self):
        with self.assertRaises(SessionError):
            session_from_flows([flow(), flow(stamp=1999.5, code="000112", path="/as_market/api/stock/status")], CONFIG, SETTINGS, now=2000)

    def test_wrong_host_cannot_be_imported(self):
        candidate = flow()
        candidate.request.host = "example.invalid"
        with self.assertRaises(SessionError):
            session_from_flows([candidate], CONFIG, SETTINGS, now=2000)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_storage_is_encrypted_and_bound_to_config(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / "session.dpapi"
            save_session(p, session())
            self.assertNotIn(b"synthetic-secret", p.read_bytes())
            self.assertEqual(load_session(p, CONFIG)["headers"]["token"], "synthetic-secret")
            with self.assertRaises(SessionError):
                load_session(p, {**CONFIG, "account_id": "different"})


if __name__ == "__main__":
    unittest.main()
