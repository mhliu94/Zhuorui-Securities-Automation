"""Compatibility entry point; implementation lives in zhuorui.ui.automation."""
import sys
from zhuorui.ui import automation as implementation

if __name__ == "__main__":
    raise SystemExit(implementation.main(sys.argv[1:]))
else:
    sys.modules[__name__] = implementation
