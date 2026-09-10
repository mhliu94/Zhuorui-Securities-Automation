"""Extract local static evidence from the user's installed Zhuorui APK.

Output is private/ and excluded from Git. No server requests are made.
"""
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile

from loguru import logger

logger.remove()
from androguard.core.dex import DEX

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"


def main():
    apk = PRIVATE / "zhuorui.apk"
    endpoints = set()
    classes = []
    with ZipFile(apk) as archive, (PRIVATE / "network-bytecode.txt").open("w", encoding="utf-8") as output:
        for name in archive.namelist():
            if not re.fullmatch(r"classes\d*\.dex", name):
                continue
            dex = DEX(archive.read(name))
            for string in dex.get_strings():
                if re.match(r"/?as_\w+/api/", string):
                    endpoints.add(string)
            for cls in dex.get_classes():
                cname = cls.get_name()
                if not cname.startswith("Lcom/zhuorui/"):
                    continue
                if any(part in cname.lower() for part in ["/net/", "interceptor", "encrypt", "signutil", "http", "ssl", "base64", "md5", "aes", "rsa", "urlconfig", "domain", "hostconfig"]):
                    classes.append({"dex": name, "name": cname,
                                    "fields": [{"name": f.get_name(), "type": f.get_descriptor()} for f in cls.get_fields()],
                                    "methods": [m.get_name() for m in cls.get_methods()]})
                    output.write(f"\nCLASS {cname}\n")
                    for field in cls.get_fields():
                        output.write(f"FIELD {field.get_name()} {field.get_descriptor()}\n")
                    for method in cls.get_methods():
                        output.write(f"METHOD {method.get_name()} {method.get_descriptor()}\n")
                        for ins in method.get_instructions():
                            output.write(f"  {ins.get_name()} {ins.get_output()}\n")
            print(f"Inspected {name}", flush=True)
    (PRIVATE / "network-classes.json").write_text(json.dumps(classes, indent=2), encoding="utf-8")
    (PRIVATE / "endpoints.txt").write_text("\n".join(sorted(endpoints)), encoding="utf-8")
    print(f"Found {len(endpoints)} endpoint strings and {len(classes)} network-related classes.")
    print(f"APK SHA256: {hashlib.sha256(apk.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
