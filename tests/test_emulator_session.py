import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import zlib

from zhuorui.api import emulator, mmkv
from zhuorui.api.config import ApiSettings
from zhuorui.api.errors import SessionError
from zhuorui.api.session import validate_identity, load_session, save_session
from tests.test_api import CONFIG, session, KEY


def varint(value):
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def field(value):
    return varint(len(value)) + value


def storage(records, *, stale=b""):
    body = b"\x00" + b"".join(field(k.encode()) + field(v) for k, v in records)
    meta = bytearray(112)
    struct.pack_into("<III", meta, 0, zlib.crc32(body), 4, 1)
    struct.pack_into("<I", meta, 28, len(body))
    return struct.pack("<I", len(body)) + body + stale, bytes(meta)


class StorageTests(unittest.TestCase):
    def test_last_update_and_tombstone_win_without_scanning_unused_bytes(self):
        data, meta = storage([("a", b"old-token"), ("b", b"deleted"), ("a", b"new-token"), ("b", b"")], stale=b"old-secret")
        self.assertEqual(mmkv.decode(data, meta), {"a": b"new-token"})

    def test_crc_mismatch_fails_without_partial_recovery_or_secret_in_error(self):
        data, meta = storage([("a", b"private-token")])
        for damaged in (data[:-1], data[:5] + b"corruption" + data[15:]):
            with self.subTest(damaged=bool(damaged)), self.assertRaises(SessionError) as caught:
                mmkv.decode(damaged, meta)
            self.assertNotIn("private-token", str(caught.exception))

    def test_unsupported_encryption_flags_and_versions_fail(self):
        data, meta = storage([("a", b"value")])
        for offset, value in ((4, 5), (12, 1), (104, 1), (28, 999999)):
            changed = bytearray(meta)
            struct.pack_into("<I", changed, offset, value)
            with self.subTest(offset=offset), self.assertRaises(SessionError):
                mmkv.decode(data, changed)

    def test_malformed_map_with_correct_crc_is_rejected(self):
        body = b"\0\xff\xff\xff\xff\xff"
        meta = bytearray(112)
        struct.pack_into("<II", meta, 0, zlib.crc32(body), 4)
        struct.pack_into("<I", meta, 28, len(body))
        with self.assertRaises(SessionError):
            mmkv.decode(struct.pack("<I", len(body)) + body, meta)

    def test_embedded_json_requires_exact_string_length_and_object(self):
        self.assertEqual(mmkv.json_value({"k": field(b'{"a":1}')}, "k"), {"a": 1})
        for value in (field(b'{"a":1}') + b"trailing", field(b"[]"), b"\xff"):
            with self.subTest(value=bool(value)), self.assertRaises(SessionError):
                mmkv.json_value({"k": value}, "k")

    def test_concurrent_storage_change_is_rejected(self):
        adb = Mock()
        data, meta = storage([("a", b"value")])
        adb.run.side_effect = [meta, data, meta + b"changed"]
        with self.assertRaises(SessionError):
            emulator.read_store(adb, "LocalAccountConfig")

    def test_logout_never_recovers_an_old_token(self):
        for value in ({"accountInfo": {"userId": "user"}, "token": None}, {"token": "token"}):
            with patch.object(emulator, "read_store", return_value={emulator.ACCOUNT_KEY: field(json.dumps(value).encode())}):
                with self.assertRaises(SessionError):
                    emulator.read_account(Mock())


class SelectionTests(unittest.TestCase):
    def factory(self, devices):
        class FakeAdb:
            def __init__(self, executable, device=None):
                self.device = device
            def text(self, *args):
                if args == ("devices",):
                    return "List of devices attached\n" + "\n".join(k + " device" for k in devices)
                if args == ("emu", "avd", "name"):
                    return devices[self.device] + "\nOK"
                raise AssertionError(args)
        return FakeAdb

    def test_single_emulator_discovers_name_and_serial(self):
        adb, name = emulator.select_emulator({"adb": "unused"}, adb_factory=self.factory({"emulator-5560": "AnotherAccount"}))
        self.assertEqual((adb.device, name), ("emulator-5560", "AnotherAccount"))

    def test_missing_wrong_or_ambiguous_target_cannot_fall_back(self):
        devices = {"emulator-5560": "A", "emulator-5562": "B"}
        for extra in ({}, {"device": "emulator-5554"}, {"device": "emulator-5560", "avd": "B"}):
            with self.subTest(extra=extra), self.assertRaises(SessionError):
                emulator.select_emulator({"adb": "unused", **extra}, adb_factory=self.factory(devices))

    def test_avd_name_can_select_unique_device(self):
        adb, _ = emulator.select_emulator({"adb": "unused", "avd": "B"}, adb_factory=self.factory({"emulator-5560": "A", "emulator-5562": "B"}))
        self.assertEqual(adb.device, "emulator-5562")


class IdentityTests(unittest.TestCase):
    def test_scoped_id_uses_exact_package_uid_and_not_shell_android_id(self):
        adb = Mock()
        adb.text.return_value = "package:com.zhuorui.securities uid:10123"
        adb.run.return_value = b'<?xml version="1.0"?><settings><setting name="10123" package="com.zhuorui.securities" value="0123456789abcdef"/><setting name="2000" value="shell-id"/></settings><namespaceHashes />'
        with patch.object(emulator, "read_store", return_value={"cache_agree_privacy_agreement": b"\x01"}):
            self.assertEqual(emulator.scoped_device_id(adb), "0123456789abcdef")
            adb.run.return_value = b'<settings><setting name="10123" package="other.app" value="0123456789abcdef"/></settings>'
            with self.assertRaises(SessionError):
                emulator.scoped_device_id(adb)

    def test_chinese_language_and_automatic_region_mapping(self):
        properties = {"ro.build.version.sdk": "36", "ro.product.model": "Model Name", "ro.product.manufacturer": "Google",
                      "persist.sys.locale": "", "ro.product.locale": "zh-HK", "ro.build.version.release": "16"}
        adb = Mock()
        adb.text.side_effect = lambda *args: properties[args[-1]] if args[1] == "getprop" else "Model Name"
        for saved, expected in (("auto", "zh_TW"), ("zh_CN", "zh_CN"), ("zh_TW", "zh_TW")):
            with patch.object(emulator, "read_store", return_value={emulator.LANGUAGE_KEY: field(json.dumps({"appLanguage": saved}).encode())}), patch.object(emulator, "scoped_device_id", return_value="0123456789abcdef"):
                headers = emulator.device_headers(adb)
            self.assertEqual(headers["lang"], expected)
            self.assertEqual(headers["devicename"], "Google-Model%20Name")

    def test_wrong_user_or_device_cannot_replace_existing_session(self):
        with TemporaryDirectory() as folder:
            settings = ApiSettings(Path(folder) / "session", Path("capture"), Path("apk"))
            settings.session_file.touch()
            for key in ("userid", "deviceid"):
                candidate = session()
                candidate["headers"][key] = "other"
                with patch("zhuorui.api.session.load_session", return_value=session()):
                    with self.assertRaises(SessionError):
                        validate_identity(candidate, CONFIG, settings)

    def test_expected_user_guards_first_import(self):
        with TemporaryDirectory() as folder:
            settings = ApiSettings(Path(folder) / "session", Path("capture"), Path("apk"))
            with self.assertRaises(SessionError):
                validate_identity(session(), {**CONFIG, "api": {"expected_user_id": "other"}}, settings)

    def test_unknown_apk_fails_before_copy_or_key_access(self):
        adb = Mock()
        adb.text.side_effect = ["package:/data/app/installation/app/base.apk", "0" * 64 + "  /data/app/installation/app/base.apk"]
        with TemporaryDirectory() as folder, self.assertRaises(SessionError):
            emulator.verified_apk(adb, Path(folder) / "app.apk")
        adb.run.assert_not_called()

    def test_nonroot_import_never_roots_or_reads_credentials(self):
        adb = Mock()
        adb.text.return_value = "2000"
        with patch.object(emulator, "load_capture_settings", return_value={}), patch.object(emulator, "select_emulator", return_value=(adb, "A")), patch.object(emulator, "verified_apk") as apk:
            with self.assertRaises(SessionError):
                emulator.import_emulator_session("unused", CONFIG, None)
            apk.assert_not_called()
        adb.text.assert_called_once_with("shell", "id", "-u")
        adb.run.assert_not_called()

    def test_account_changes_during_import_do_not_save_cache(self):
        adb = Mock(device="emulator-5560")
        adb.text.side_effect = ["0", "0"]
        account = {"userid": "test-user", "token": "synthetic-token"}
        with patch.object(emulator, "load_capture_settings", return_value={}), patch.object(emulator, "select_emulator", return_value=(adb, "A")), patch.object(emulator, "verified_apk", return_value="verified"), patch.object(emulator, "device_headers", return_value={"deviceid": "test-device"}), patch.object(emulator, "known_signing_key", return_value=KEY), patch.object(emulator, "read_account", side_effect=[account, {**account, "token": "changed"}]), patch("zhuorui.api.session.save_session") as save:
            with self.assertRaises(SessionError):
                emulator.import_emulator_session("unused", CONFIG, SimpleNamespace(apk_file=Path("unused")))
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
