"""Offline API password-login tests with synthetic identities and HTTP replies."""
import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from tests.test_api import FakeOpener, KEY, SETTINGS, session
from zhuorui.api.auth import password_login
from zhuorui.api.client import ApiClient, LOGIN_PATH
from zhuorui.api.errors import ApiError, LoginBlocked, SessionError
from zhuorui.api.signing import canonical


NOW = 1_800_000_000
USER_ID = "1" * 32
OLD_TOKEN = "a" * 32
NEW_TOKEN = "2" * 32
PHONE = "12345678901"


def login_reply(**changes):
    data = {"userId": USER_ID, "token": NEW_TOKEN, "phone": PHONE,
            "phoneArea": "86", "certification": True}
    data.update(changes)
    return {"code": "000000", "data": data}


class PasswordLoginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = replace(SETTINGS, session_file=Path(self.temporary.name) / "session.dpapi")
        self.config = {"account_id": "synthetic-account", "server_id": "test-server",
                       "api": {"expected_user_id": USER_ID},
                       "login": {"phone": PHONE, "phone_area": "86", "password": "password"}}
        self.previous = session()
        self.previous["headers"].update(userid=USER_ID, token=OLD_TOKEN,
                                         deviceid="synthetic-device-id", devicename="Synthetic emulator",
                                         devicemodel="Synthetic model", ostype="android", osversion="16",
                                         appversion="3.1.5(315001)")
        self.save = self.enterContext(patch("zhuorui.api.auth.save_session"))

    def make_factory(self, response=None, *, opener=None):
        opener = opener or FakeOpener(response if response is not None else login_reply())
        factory = Mock(side_effect=lambda previous, settings: ApiClient(previous, settings, opener=opener, now=lambda: NOW))
        return factory, opener

    def login(self, response=None, *, factory=None):
        factory, opener = self.make_factory(response) if factory is None else (factory, None)
        result = password_login(self.config, self.settings, self.previous, client_factory=factory, now=lambda: NOW)
        return result, opener

    def assert_one_login_only(self, opener):
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0][0]
        self.assertEqual(request.full_url, "https://backendpro.zr.hk" + LOGIN_PATH)
        return request

    def test_captured_phone_login_fields_and_fresh_signature(self):
        _, opener = self.login()
        request = self.assert_one_login_only(opener)
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data)
        signature = base64.b64decode(body.pop("sign"))
        self.assertEqual(body, {"phone": PHONE, "phoneArea": "86", "accountType": 1,
                                "type": 1, "loginPassword": "5f4dcc3b5aa765d61d8327deb882cf99",
                                "timeStamp": NOW * 1000})
        self.assertIs(type(body["accountType"]), int)
        self.assertIs(type(body["type"]), int)
        self.assertNotIn("password", body)
        self.assertNotIn("clientId", body)
        KEY.public_key().verify(signature, canonical(body), padding.PKCS1v15(), hashes.SHA1())

    def test_login_omits_old_auth_headers_and_preserves_same_device_headers(self):
        self.previous["headers"].update(Token="synthetic-extra-token", UserId="synthetic-extra-user",
                                         Authorization="synthetic-auth", Cookie="synthetic-cookie")
        before = deepcopy(self.previous)
        _, opener = self.login()
        request = self.assert_one_login_only(opener)
        headers = {name.lower(): value for name, value in request.header_items()}
        for key in ("token", "userid", "authorization", "cookie"):
            self.assertNotIn(key, headers)
        for key in ("deviceid", "devicename", "devicemodel", "ostype", "osversion", "appversion"):
            self.assertEqual(headers[key], before["headers"][key])
        self.assertEqual(self.previous, before)

    def test_successful_session_is_identity_checked_then_saved_before_any_read(self):
        sequence = []
        with patch("zhuorui.api.auth.validate_identity", side_effect=lambda *args: sequence.append("validate")) as validate:
            self.save.side_effect = lambda *args: sequence.append("save")
            current, opener = self.login()
        self.assertEqual(sequence, ["validate", "save"])
        validate.assert_called_once_with(current, self.config, self.settings)
        self.save.assert_called_once_with(self.settings.session_file, current)
        self.assert_one_login_only(opener)
        self.assertEqual(current["headers"]["userid"], USER_ID)
        self.assertEqual(current["headers"]["token"], NEW_TOKEN)
        self.assertEqual(current["headers"]["deviceid"], self.previous["headers"]["deviceid"])
        self.assertEqual(current["signing_key"], self.previous["signing_key"])
        self.assertEqual(current["binding"], self.previous["binding"])
        self.assertEqual(current["generation"], hashlib.sha256(NEW_TOKEN.encode()).hexdigest())
        self.assertEqual(current["source"], "api_password_login")
        self.assertEqual(current["captured_at"], NOW)
        self.assertEqual(current["imported_at"], NOW)
        self.assertEqual(self.previous["headers"]["token"], OLD_TOKEN)

    def test_wrong_user_or_phone_identity_cannot_be_saved(self):
        for changes in ({"userId": "3" * 32}, {"phone": "12345678902"}, {"phoneArea": "852"}):
            with self.subTest(field=next(iter(changes))):
                factory, opener = self.make_factory(login_reply(**changes))
                with self.assertRaises(LoginBlocked):
                    self.login(factory=factory)
                self.assert_one_login_only(opener)
                self.save.assert_not_called()

    def test_missing_or_malformed_user_and_token_cannot_be_saved(self):
        for field in ("userId", "token"):
            for value in (None, "", "z" * 32, "1" * 31, True, 123):
                with self.subTest(field=field, type=type(value).__name__):
                    factory, opener = self.make_factory(login_reply(**{field: value}))
                    with self.assertRaises(LoginBlocked):
                        self.login(factory=factory)
                    self.assert_one_login_only(opener)
                    self.save.assert_not_called()

    def test_missing_or_non_object_login_data_cannot_be_saved(self):
        for response in ({"code": "000000"}, {"code": "000000", "data": None},
                         {"code": "000000", "data": []}, {"code": "000000", "data": "synthetic-private-data"}):
            with self.subTest(response_type=type(response.get("data")).__name__):
                with self.assertRaises(LoginBlocked):
                    self.login(response)
                self.save.assert_not_called()

    def test_configured_account_guard_blocks_valid_looking_other_identity(self):
        self.config["api"]["expected_user_id"] = "3" * 32
        with self.assertRaisesRegex(LoginBlocked, "identity checks"):
            self.login()
        self.save.assert_not_called()

    def test_identity_validator_failure_blocks_session_save(self):
        with patch("zhuorui.api.auth.validate_identity", side_effect=SessionError("synthetic-private-detail")):
            with self.assertRaises(LoginBlocked) as caught:
                self.login()
        self.assertNotIn("synthetic-private-detail", str(caught.exception))
        self.save.assert_not_called()

    def test_sms_verification_and_wrong_password_block_without_retry_or_save(self):
        for code, pattern in (("010007", "phone verification"), ("010001", "rejected"), ("350117", "rejected")):
            with self.subTest(code=code):
                factory, opener = self.make_factory({"code": code, "msg": "synthetic-private-broker-message"})
                with self.assertRaisesRegex(LoginBlocked, pattern) as caught:
                    self.login(factory=factory)
                self.assertNotIn("synthetic-private-broker-message", str(caught.exception))
                self.assert_one_login_only(opener)
                self.save.assert_not_called()

    def test_session_error_during_login_blocks_without_retry_or_save(self):
        for code in ("000102", "000112"):
            factory, opener = self.make_factory({"code": code})
            with self.assertRaises(LoginBlocked):
                self.login(factory=factory)
            self.assert_one_login_only(opener)
            self.save.assert_not_called()

    def test_network_failure_remains_retryable_api_error_without_internal_retry(self):
        opener = FakeOpener(login_reply())
        opener.open = Mock(side_effect=TimeoutError("synthetic-private-timeout-detail"))
        factory, _ = self.make_factory(opener=opener)
        with self.assertRaises(ApiError) as caught:
            self.login(factory=factory)
        self.assertNotIsInstance(caught.exception, LoginBlocked)
        self.assertNotIn("synthetic-private-timeout-detail", str(caught.exception))
        opener.open.assert_called_once()
        self.save.assert_not_called()

    def test_unconfigured_or_invalid_credentials_never_open_transport(self):
        invalid = ({"phone": ""}, {"phone": "not-a-phone"}, {"password": ""}, {"phone_area": "invalid"})
        for changes in invalid:
            with self.subTest(field=next(iter(changes))):
                self.config["login"] = {"phone": PHONE, "phone_area": "86", "password": "password", **changes}
                factory = Mock(side_effect=AssertionError("Transport must not be opened"))
                with self.assertRaises(LoginBlocked):
                    self.login(factory=factory)
                factory.assert_not_called()
                self.save.assert_not_called()

    def test_success_and_rejection_do_not_print_passwords_tokens_or_responses(self):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.login()
            with self.assertRaises(LoginBlocked) as caught:
                self.login({"code": "010007", "msg": "synthetic-private-broker-response"})
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("synthetic-private-broker-response", str(caught.exception))
        self.assertNotIn(OLD_TOKEN, str(caught.exception))
        self.assertNotIn(NEW_TOKEN, str(caught.exception))
        self.assertNotIn(PHONE, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
