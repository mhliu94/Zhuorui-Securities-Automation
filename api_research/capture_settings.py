"""Shared configuration bootstrap for directly invoked research helpers."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zhuorui.common.config import default_config_path
from zhuorui.capture.config import load_capture_settings, verify_boot_evidence


def arguments(parser=None):
    parser = parser or argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=default_config_path())
    args = parser.parse_args()
    return args, load_capture_settings(args.config)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--verify-boot", action="store_true")
    try:
        args, settings = arguments(p)
        if args.verify_boot:
            verify_boot_evidence(settings)
        print(json.dumps(settings))
    except (OSError, ValueError, RuntimeError):
        print("Capture settings or boot evidence are invalid; check the configured paths, identities and validation records.", file=sys.stderr)
        sys.exit(2)
