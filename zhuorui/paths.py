"""Paths are anchored to the checkout, independent of the launch directory."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
