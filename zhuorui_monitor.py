"""Compatibility entry point; implementation lives in zhuorui.monitor.server."""
import sys
from zhuorui.monitor import server as implementation

if __name__ == "__main__":
    raise SystemExit(implementation.main())
else:
    sys.modules[__name__] = implementation
