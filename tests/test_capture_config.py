import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zhuorui.api.config import load_settings
from zhuorui.capture.config import load_capture_settings, verify_boot_evidence
from zhuorui.common.config import ZhuoruiAutomationError, config_screen_size


class CaptureConfigurationTests(unittest.TestCase):
    def setup_config(self, folder, **capture):
        root = Path(folder)
        config = root / "config.json"
        config.write_text(json.dumps({"adb": "sdk/platform-tools/adb.exe", "avd": "Second_Account", "device": "emulator-5560",
            "trade_password": "test-secret-never-in-output", "capture": {"avd_home": "avds", **capture}}))
        return config

    def test_nondefault_machine_and_paths_resolve_without_credentials(self):
        with TemporaryDirectory() as folder:
            path = self.setup_config(folder, private_dir="private storage", proxy_port=9090, test_port=5590, boot_test_port=5592,
                                     test_avd="CaptureSecond", boot_test_avd="BootSecond", python_executable="tools/python.exe")
            result = load_capture_settings(path)
            self.assertEqual(result["port"], 5560)
            self.assertEqual(result["test_device"], "emulator-5590")
            self.assertEqual(result["emulator_proxy"], "10.0.2.2:9090")
            self.assertEqual(result["private_dir"], str((Path(folder) / "private storage").resolve()))
            self.assertNotIn("test-secret", json.dumps(result))
            _, api = load_settings(path)
            self.assertEqual(api.capture_file, (Path(folder) / "private storage/zhuorui-flows.mitm").resolve())
            self.assertEqual(api.apk_file, (Path(folder) / "private storage/zhuorui.apk").resolve())

    def test_system_image_is_inferred_from_the_registered_avd(self):
        with TemporaryDirectory() as folder:
            path = self.setup_config(folder)
            root = Path(folder).resolve()
            avd = root / "relocated/Second_Account.avd"
            avd.mkdir(parents=True)
            (root / "avds").mkdir()
            (root / "avds/Second_Account.ini").write_text("path=" + str(avd))
            (avd / "config.ini").write_text("AvdId=Second_Account\nimage.sysdir.1=system-images/android-99/google_apis_playstore/x86_64/\n")
            result = load_capture_settings(path)
            self.assertEqual(result["original_avd_dir"], str(avd))
            self.assertEqual(result["system_image_dir"], str(root / "sdk/system-images/android-99/google_apis_playstore/x86_64"))
            self.assertEqual(result["test_system_image_dir"], str(root / "sdk/system-images/android-99/google_apis/x86_64"))
            changed = json.loads(path.read_text())
            changed["capture"]["system_image_dir"] = "some-other-image"
            path.write_text(json.dumps(changed))
            with self.assertRaises(ZhuoruiAutomationError):
                load_capture_settings(path)

    def test_original_and_test_collisions_and_remote_listening_are_rejected(self):
        for values in ({"test_port": 5560}, {"test_port": 5591}, {"boot_test_port": 5580},
                       {"test_avd": "Second_Account"}, {"proxy_port": 5561}, {"proxy_port": True},
                       {"proxy_listen_host": "0.0.0.0"}, {"emulator_proxy_host": "host;command"}, {"test_avd": "../Original"}):
            with self.subTest(values=values), TemporaryDirectory() as folder:
                with self.assertRaises(ZhuoruiAutomationError):
                    load_capture_settings(self.setup_config(folder, **values))

    def test_explicit_avd_path_must_match_launcher_registration(self):
        with TemporaryDirectory() as folder:
            path = self.setup_config(folder, original_avd_dir="wrong")
            home = Path(folder) / "avds"
            home.mkdir()
            (home / "Second_Account.ini").write_text("path=" + str(Path(folder) / "correct"))
            with self.assertRaises(ZhuoruiAutomationError):
                load_capture_settings(path)

    def test_boot_evidence_is_bound_to_source_policy_and_output(self):
        with TemporaryDirectory() as folder:
            path = self.setup_config(folder, private_dir="private", system_image_dir="image")
            settings = load_capture_settings(path)
            private, image = Path(folder) / "private", Path(folder) / "image"
            private.mkdir(); image.mkdir()
            files = [(image / "ramdisk.img", "source_sha256"), (Path(settings["debug_policy_file"]), "policy_sha256"),
                     (Path(settings["debug_ramdisk_file"]), "output_sha256")]
            evidence = {"source": str(files[0][0]), "boot_test_passed": True, "certificate_mount_passed": True}
            for file, key in files:
                file.write_bytes(key.encode())
                evidence[key] = hashlib.sha256(file.read_bytes()).hexdigest()
            (private / "debug-ramdisk-evidence.json").write_text(json.dumps(evidence))
            verify_boot_evidence(settings)
            files[0][0].write_bytes(b"different machine image")
            with self.assertRaises(ZhuoruiAutomationError):
                verify_boot_evidence(settings)

    def test_missing_identity_is_only_allowed_for_readonly_discovery(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text("{}")
            with self.assertRaises(ZhuoruiAutomationError):
                load_capture_settings(path)
            self.assertEqual(load_capture_settings(path, require_identity=False)["device"], "")

    def test_shared_string_screen_size_parses_after_config_extraction(self):
        self.assertEqual(config_screen_size({"screen_size": "1080x2424"}), (1080, 2424))


if __name__ == "__main__":
    unittest.main()
