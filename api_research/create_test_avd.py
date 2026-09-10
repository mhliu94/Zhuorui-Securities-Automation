"""Create a configured private test AVD without copying the original's app data."""
import argparse
import json
from pathlib import Path
from capture_settings import arguments
from zhuorui.capture.config import read_ini


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", choices=["boot", "capture"], required=True)
    args, settings = arguments(p)
    image_value = settings["system_image_dir" if args.kind == "boot" else "test_system_image_dir"]
    if not image_value:
        raise RuntimeError("Configure the selected system image directory first")
    image = Path(image_value)
    props = read_ini(image / "source.properties")
    if not (image / "system.img").is_file() or not props:
        raise RuntimeError("Configured system image is not installed")
    abi, tag, level = props.get("SystemImage.Abi"), props.get("SystemImage.TagId"), props.get("AndroidVersion.ApiLevel")
    if not abi or not tag or not level:
        raise RuntimeError("System image metadata is incomplete")
    target = image.parent.parent.name
    if not target.startswith("android-"):
        target = "android-" + level
    name = settings["boot_test_avd" if args.kind == "boot" else "test_avd"]
    avd_home = Path(settings["private_dir"]) / "avd"
    folder = (avd_home / (name + ".avd")).resolve()
    original = Path(settings["original_avd_dir"]).resolve()
    if folder == original or folder.is_relative_to(original) or original.is_relative_to(folder):
        raise RuntimeError("Test and original AVD directories overlap")
    old = read_ini(folder / "config.ini")
    if old and (old.get("AvdId") != name or Path(old.get("image.sysdir.1", "")).resolve() != image.resolve()):
        raise RuntimeError("Existing test AVD belongs to a different name/image; use a new test name")
    folder.mkdir(parents=True, exist_ok=True)
    (avd_home / (name + ".ini")).write_text(f"avd.ini.encoding=UTF-8\npath={folder}\ntarget={target}\n", encoding="utf8")
    values = {"AvdId": name, "avd.ini.encoding": "UTF-8", "PlayStore.enabled": str("playstore" in tag).lower(),
        "abi.type": abi, "hw.cpu.arch": "arm64" if abi == "arm64-v8a" else abi, "hw.cpu.ncore": 2, "hw.ramSize": 2048,
        "hw.lcd.width": 1080, "hw.lcd.height": 2424, "hw.lcd.density": 420, "hw.keyboard": "yes",
        "hw.mainKeys": "no", "hw.gpu.enabled": "yes", "hw.gpu.mode": "software", "hw.audioInput": "no",
        "hw.camera.back": "none", "hw.camera.front": "none", "disk.dataPartition.size": "4G",
        "image.sysdir.1": str(image) + "/", "tag.id": tag, "target": target, "showDeviceFrame": "no"}
    (folder / "config.ini").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf8")
    print(json.dumps({"avd_home": str(avd_home), "avd": name}))


if __name__ == "__main__":
    main()
