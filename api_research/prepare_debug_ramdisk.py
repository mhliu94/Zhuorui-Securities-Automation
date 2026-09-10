"""Build a private temporary debug ramdisk; never edit the installed SDK image.

Uses Android's force_debuggable mechanism and a matching userdebug platform
policy. A boot test with the original system image is required before use on
the existing AVD. Keeps ADB authentication enabled.
"""
import gzip
import hashlib
import json
import struct
from pathlib import Path

from lz4.block import decompress
from capture_settings import arguments

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"


def unpack_lz4(data):
    offset = 0
    chunks = []
    while offset < len(data):
        value = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if value in (0x184C2102, 0):
            continue
        if value > len(data) - offset:
            raise ValueError("Truncated LZ4 block")
        chunks.append(decompress(data[offset:offset + value], uncompressed_size=8 * 1024 * 1024))
        offset += value
    return b"".join(chunks)


def cpio_entry(name, data, inode):
    filename = name.encode() + b"\0"
    fields = [inode, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(filename), 0]
    result = b"070701" + b"".join(f"{v:08x}".encode() for v in fields) + filename
    result += b"\0" * (-len(result) % 4)
    result += data
    result += b"\0" * (-len(result) % 4)
    return result


def main():
    _, settings = arguments()
    private = Path(settings["private_dir"])
    private.mkdir(parents=True, exist_ok=True)
    if not settings["system_image_dir"]:
        raise RuntimeError("Configure capture.system_image_dir or the original AVD directory first")
    source = Path(settings["system_image_dir"]) / "ramdisk.img"
    original = source.read_bytes()
    policy = Path(settings["debug_policy_file"]).read_bytes()
    if not policy.startswith(b"(role ") or b"(type adbd)" not in policy:
        raise ValueError("Missing platform debugging policy")
    archive = unpack_lz4(original)
    additions = {
        "force_debuggable": b"",
        "adb_debug.prop": b"ro.adb.secure=1\nro.debuggable=1\nro.force.debuggable=1\n",
        "userdebug_plat_sepolicy.cil": policy,
    }
    extra = b"".join(cpio_entry(name, data, 9000 + i) for i, (name, data) in enumerate(additions.items()))
    extra += cpio_entry("TRAILER!!!", b"", 9999)
    extra += b"\0" * (-len(extra) % 512)
    output = Path(settings["debug_ramdisk_file"])
    if output.resolve() == source.resolve() or output.resolve().is_relative_to(Path(settings["system_image_dir"]).resolve()):
        raise RuntimeError("Temporary ramdisk must be outside the installed system image")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(archive + extra, mtime=0))
    assert gzip.decompress(output.read_bytes())[:len(archive)] == archive
    assert source.read_bytes() == original
    evidence = {
        "source": str(source), "source_sha256": hashlib.sha256(original).hexdigest(),
        "policy_sha256": hashlib.sha256(policy).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "added_entries": list(additions), "installed_image_modified": False,
        "adb_authentication": "enabled", "boot_test_passed": False,
    }
    (private / "debug-ramdisk-evidence.json").write_text(json.dumps(evidence, indent=2))
    print("Temporary debug boot archive created; installed SDK image unchanged.")


if __name__ == "__main__":
    main()
