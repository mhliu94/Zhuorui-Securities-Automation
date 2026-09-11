"""Offline unlock tests: synthetic passwords, identities, transport and journal."""
import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from gmalg import SM2

from tests.test_api import FakeOpener, KEY, SETTINGS, session
from tests import test_api_execution as execution_fixtures
from tests.test_api_execution import trade
from zhuorui.api.client import ApiClient, TRADE_AUTH_PATH
from zhuorui.api.commands import CancelCommand
from zhuorui.api.errors import ApiError, BrokerRejected, SessionExpired
from zhuorui.api.passwords import TRADE_PUBLIC_KEY, trade_password_ciphertext
from zhuorui.api.signing import canonical
from zhuorui.api.trade_auth import TradeAuthorizer

PUBLIC = bytes.fromhex(
    "0409F9DF311E5421A150DD7D161E4BC5C672179FAD1833FC076BB08FF356F35020"
    "CCEA490CE26775A52DC6EA718CC1AA600AED05FBF35E084A6632F6072DA9AD13")
PRIVATE = bytes.fromhex("3945208F7B2144B13F36E38AC6D39F95889393692860B51A42FB81EF4DF7C5B8")
SCALAR = int("59276E27D506861A16680F3AD9C02DCCEF3CC1FA3CDBE4CE6D54B80DEAC1BC21", 16)
ACCOUNT = {"code": "000000", "data": {"clientId": "client-1"}}
AUTH = {"code": "000000", "data": {"accountId": "client-1", "userId": "user-1", "accountType": "CASH"}}
LOCKED = {"code": "000000", "data": None}
UNLOCKED = {"code": "000000", "data": {"userId": "user-1", "forceChangePwd": 1}}


class TradePasswordTransformTests(unittest.TestCase):
    def test_published_sm2_encryption_vector_and_app_prefix_removal(self):
        # gmalg upstream test_encrypt3 / SM2 recommended-curve encryption vector.
        # https://github.com/ww-rm/gmalg/blob/main/tests.py
        expected = (
            "04ebfc718e8d1798620432268e77feb6415e2ede0e073c0f4f640ecd2e149a73"
            "e858f9d81e5430a57b36daab8f950a3c64e6ee6a63094d99283aff767e124df0"
            "59983c18f809e262923c53aec295d30383b54e39d609d160afcb1908d0bd8766"
            "21886ca989ca9c7d58087307ca93092d651efa")
        with patch("zhuorui.api.passwords.TRADE_PUBLIC_KEY", PUBLIC), \
             patch("zhuorui.api.passwords.secrets.randbits", return_value=SCALAR):
            self.assertEqual(trade_password_ciphertext("encryption standard"), expected)

    def test_utf8_randomized_ciphertext_round_trips_without_normalizing(self):
        with patch("zhuorui.api.passwords.TRADE_PUBLIC_KEY", PUBLIC):
            for password in ("012345", " Password ", "你好"):
                first, second = (trade_password_ciphertext(password) for _ in range(2))
                self.assertNotEqual(first, second)
                self.assertRegex(first, r"^[0-9a-f]+$")
                self.assertEqual(len(first), 192 + 2 * len(password.encode("utf8")))
                self.assertEqual(SM2(PRIVATE, pk=PUBLIC).decrypt(bytes.fromhex("04" + first)),
                                 password.encode("utf8"))

    def test_broker_public_key_and_six_digit_wire_length(self):
        self.assertEqual(len(TRADE_PUBLIC_KEY), 65)
        self.assertTrue(SM2().verify_pk(TRADE_PUBLIC_KEY))
        self.assertEqual(len(trade_password_ciphertext("012345")), 204)

    def test_invalid_passwords_and_crypto_errors_do_not_leak(self):
        for password in ("", None, 123456, True, "private-marker" + chr(0xd800), "x" * 4097):
            with self.assertRaises(ApiError):
                trade_password_ciphertext(password)
        with patch("gmalg.SM2.encrypt", side_effect=ValueError("private-marker")):
            with self.assertRaises(ApiError) as caught:
                trade_password_ciphertext("private-marker")
            self.assertNotIn("private-marker", str(caught.exception))


class TradeAuthWireTests(unittest.TestCase):
    def test_signed_auth_request_preserves_session_and_encrypts_password(self):
        opener = FakeOpener(UNLOCKED)
        current = session()
        current["headers"]["userid"] = "user-1"
        client = ApiClient(current, SETTINGS, opener=opener, now=lambda: 1800000000)
        with patch("zhuorui.api.passwords.TRADE_PUBLIC_KEY", PUBLIC):
            client.unlock_trading("client-1", "012345")
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0][0]
        self.assertEqual(request.full_url, "https://backendpro.zr.hk" + TRADE_AUTH_PATH)
        body = json.loads(request.data)
        signature = base64.b64decode(body.pop("sign"))
        self.assertEqual(set(body), {"clientId", "password", "timeStamp"})
        self.assertEqual(body["clientId"], "client-1")
        self.assertEqual(body["timeStamp"], 1800000000000)
        self.assertEqual(SM2(PRIVATE, pk=PUBLIC).decrypt(bytes.fromhex("04" + body["password"])), b"012345")
        KEY.public_key().verify(signature, canonical(body), padding.PKCS1v15(), hashes.SHA1())
        headers = {k.lower(): v for k, v in request.header_items()}
        for key in ("token", "userid", "deviceid"):
            self.assertEqual(headers[key], current["headers"][key])
        self.assertNotIn(b"012345", request.data)

    def test_auth_endpoint_requires_its_own_operation_and_rejects_mixed_flags(self):
        opener = FakeOpener(UNLOCKED)
        client = ApiClient(session(), SETTINGS, opener=opener)
        for kwargs in ({}, {"write": True}, {"login": True},
                       {"trade_auth": True, "write": True}, {"trade_auth": True, "login": True}):
            with self.assertRaises(ApiError):
                client._request(TRADE_AUTH_PATH, {}, **kwargs)
        self.assertEqual(opener.calls, [])

    def test_transport_failure_and_password_rejection_are_never_retried(self):
        opener = Mock()
        opener.open.side_effect = URLError("private-marker")
        client = ApiClient(session(), SETTINGS, opener=opener)
        with self.assertRaises(ApiError) as caught:
            client.unlock_trading("client-1", "012345")
        self.assertNotIn("private-marker", str(caught.exception))
        opener.open.assert_called_once()
        opener = FakeOpener({"code": "350117", "msg": "private-marker"})
        client = ApiClient(session(), SETTINGS, opener=opener)
        with self.assertRaises(BrokerRejected) as caught:
            client.unlock_trading("client-1", "012345")
        self.assertEqual(caught.exception.code, "350117")
        self.assertNotIn("private-marker", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)


class TradeAuthorizerTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(headers={"userid": "user-1"})
        self.client.unlock_trading.return_value = UNLOCKED
        self.client.query.return_value = AUTH
        self.authorizer = TradeAuthorizer({"trade_password": "012345"})

    def test_already_unlocked_sends_no_password(self):
        self.authorizer.ensure(self.client, ACCOUNT, AUTH)
        self.client.unlock_trading.assert_not_called()
        self.client.query.assert_not_called()

    def test_locked_and_relocked_sessions_use_existing_config_password(self):
        for locked in (LOCKED, {"code": "000000", "data": {}}):
            self.authorizer.ensure(self.client, ACCOUNT, locked)
        self.assertEqual(self.client.unlock_trading.call_count, 2)
        self.client.unlock_trading.assert_called_with("client-1", "012345")

    def test_existing_config_aliases_and_precedence(self):
        for config, expected in (({"trade_password": "first", "trade": {"password": "second"}}, "first"),
                                 ({"trade": {"password": "second"}}, "second"),
                                 ({"password": "third"}, "third")):
            TradeAuthorizer(config).ensure(self.client, ACCOUNT, LOCKED)
            self.client.unlock_trading.assert_called_with("client-1", expected)

    def test_missing_password_is_actionable_and_sends_nothing(self):
        with self.assertRaisesRegex(ApiError, "Configure trade_password"):
            TradeAuthorizer({}).ensure(self.client, ACCOUNT, LOCKED)
        self.client.unlock_trading.assert_not_called()

    def test_malformed_or_wrong_identity_never_sends_password(self):
        for account, auth in (({"code": "000000", "data": {}}, LOCKED),
                              (ACCOUNT, {"code": "000000", "data": []}),
                              (ACCOUNT, {"code": "000000", "data": {"accountId": "other", "userId": "user-1"}}),
                              (ACCOUNT, {"code": "000000", "data": {"accountId": "client-1", "userId": "other"}})):
            with self.assertRaises(ApiError):
                self.authorizer.ensure(self.client, account, auth)
        self.client.unlock_trading.assert_not_called()

    def test_bad_password_pauses_further_attempts_without_logging_secrets(self):
        self.client.unlock_trading.side_effect = BrokerRejected("private-marker", code="350117")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaisesRegex(ApiError, "350117") as caught:
                self.authorizer.ensure(self.client, ACCOUNT, LOCKED)
            self.assertNotIn("private-marker", str(caught.exception))
            with self.assertRaisesRegex(ApiError, "paused"):
                self.authorizer.ensure(self.client, ACCOUNT, LOCKED)
        self.client.unlock_trading.assert_called_once()
        self.assertEqual(output.getvalue(), "")
        self.authorizer.ensure(self.client, ACCOUNT, AUTH)
        self.assertFalse(self.authorizer.blocked)

    def test_uncertain_or_unverified_unlock_blocks_later_attempts(self):
        for failure in (TimeoutError("private-marker"), BrokerRejected("private-marker", code="999999")):
            authorizer = TradeAuthorizer({"trade_password": "private-marker"})
            self.client.unlock_trading.side_effect = failure
            with self.assertRaises(ApiError) as caught:
                authorizer.ensure(self.client, ACCOUNT, LOCKED)
            self.assertNotIn("private-marker", str(caught.exception))
            self.assertTrue(authorizer.blocked)
        self.client.unlock_trading.side_effect = None
        for reply, recheck in (({"code": "000000", "data": {"userId": "other"}}, AUTH),
                               (UNLOCKED, LOCKED),
                               (UNLOCKED, {"code": "000000", "data": {"accountId": "other", "userId": "user-1"}})):
            authorizer = TradeAuthorizer({"trade_password": "012345"})
            self.client.unlock_trading.return_value = reply
            self.client.query.return_value = recheck
            with self.assertRaises(ApiError):
                authorizer.ensure(self.client, ACCOUNT, LOCKED)
            self.assertTrue(authorizer.blocked)

    def test_logout_is_reportable_and_new_login_does_not_repeat_failed_password(self):
        error = SessionExpired("Synthetic logout")
        self.client.unlock_trading.side_effect = error
        with self.assertRaises(SessionExpired):
            self.authorizer.ensure(self.client, ACCOUNT, LOCKED)
        replacement = Mock(headers={"userid": "user-1"})
        with self.assertRaisesRegex(ApiError, "paused"):
            self.authorizer.ensure(replacement, ACCOUNT, LOCKED)
        replacement.unlock_trading.assert_not_called()


class UnlockExecutionTests(unittest.TestCase):
    setUp = execution_fixtures.ExecutionTests.setUp
    tearDown = execution_fixtures.ExecutionTests.tearDown
    emit = execution_fixtures.ExecutionTests.emit

    def lock(self):
        self.executor.trade_authorizer = TradeAuthorizer({"trade_password": "012345"})
        self.client.trade_auth = {"code": "000000", "data": None}
        def unlock(client_id, password):
            self.clock.value += 2  # Unlock latency is before order dispatch.
            self.client.trade_auth = {"code": "000000", "data":
                {"userId": "synthetic-user", "accountId": "synthetic-client"}}
            return {"code": "000000", "data": {"userId": "synthetic-user", "forceChangePwd": 1}}
        self.client.unlock_trading = Mock(side_effect=unlock)

    def test_market_limit_and_timed_cancel_unlock_before_single_submission(self):
        for kind in ("market", "limit", "fok"):
            self.lock()
            command = trade(kind, kind=kind)
            self.executor.execute(command)
            self.executor.execute(command)
            self.client.unlock_trading.assert_called_once_with("synthetic-client", "012345")
            self.assertEqual(self.journal.get(kind)["state"], "submitted")
        submissions = [entry for entry in self.client.calls if entry[0] == "submit"]
        self.assertEqual(len(submissions), 3)
        cancellation = next(entry for entry in self.client.calls if entry[0] == "cancel")
        self.assertAlmostEqual(cancellation[1] - submissions[-1][1], 1)
        self.assertNotIn("012345", "\n".join(row[0] for row in self.journal.db.execute("select payload from commands")))

    def test_unlock_failure_never_dispatches_order_or_cancellation(self):
        self.lock()
        self.client.unlock_trading.side_effect = BrokerRejected("private-marker", code="350117")
        self.executor.execute(trade())
        self.executor.execute(CancelCommand("cancel", cancel_all=True))
        self.client.unlock_trading.assert_called_once()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.holdings.requests, [])
        for command_id in ("command-1", "cancel"):
            self.assertEqual(self.journal.get(command_id)["state"], "rejected")
            self.assertNotIn("private-marker", self.journal.get(command_id)["message"])

    def test_disabled_or_unresolved_commands_never_unlock(self):
        self.lock()
        self.settings.live_orders_enabled = False
        self.executor.execute(trade("disabled"))
        self.settings.live_orders_enabled = True
        self.journal.claim("old", {"synthetic": True})
        self.journal.update("old", "unknown")
        self.executor.execute(trade("blocked"))
        self.client.unlock_trading.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_pending_timed_cancellation_can_unlock_without_resubmitting(self):
        self.lock()
        command = trade(kind="fok")
        self.journal.claim(command.command_id, asdict(command))
        self.journal.update(command.command_id, "submitted", reference="existing-ref",
                            cancel_due=self.clock.wall() - 1, cancel_state="pending")
        self.executor.recover_pending_cancellations()
        self.client.unlock_trading.assert_called_once()
        self.assertEqual([entry[0] for entry in self.client.calls], ["cancel"])

    def test_read_only_holdings_do_not_invoke_unlock(self):
        self.lock()
        self.client.query("account")
        self.client.query("cash")
        self.client.query("holdings")
        self.client.unlock_trading.assert_not_called()


if __name__ == "__main__":
    unittest.main()
