"""Build a project-owned Windows Python runtime from an existing CPython install.

Copies the interpreter and standard library, creates a new virtual environment,
and installs the pinned API dependencies. Existing environments are not moved.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def prepare(root, source_python):
    root = Path(root).resolve()
    base = root / "runtime" / "python-base"
    environment = root / "runtime" / "python-env"
    source = json.loads(subprocess.check_output([
        str(source_python), "-I", "-c",
        "import json,sys; print(json.dumps({'base':sys.base_prefix,'version':sys.version}))",
    ], text=True))
    source_base = Path(source["base"]).resolve()
    manifest = base / "zhuorui-runtime.json"
    if base.exists() and not manifest.is_file():
        raise RuntimeError("Runtime directory already exists without an installation manifest; inspect it first.")
    if not base.exists():
        base.mkdir(parents=True)
        # A manifest also makes an interrupted copy explicitly recoverable.
        manifest.write_text(json.dumps({"source": str(source_base), "version": source["version"], "complete": False}))
    recorded = json.loads(manifest.read_text())
    if not recorded["complete"]:
        if recorded["source"] != str(source_base) or recorded["version"] != source["version"]:
            raise RuntimeError("An interrupted runtime copy came from a different interpreter.")
        for item in source_base.iterdir():
            if item.is_file() and (item.suffix.lower() in (".exe", ".dll") or item.name == "LICENSE.txt"):
                shutil.copy2(item, base / item.name)
        for name in ("Lib", "DLLs", "tcl"):
            if (source_base / name).is_dir():
                shutil.copytree(source_base / name, base / name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("site-packages", "__pycache__"))
        recorded["complete"] = True
        manifest.write_text(json.dumps(recorded, indent=2))
    python = base / "python.exe"
    actual = Path(subprocess.check_output([str(python), "-I", "-c", "import sys; print(sys.base_prefix)"], text=True).strip()).resolve()
    if actual != base:
        raise RuntimeError("Copied interpreter still depends on another installation.")
    if environment.exists() and not (environment / "pyvenv.cfg").is_file():
        raise RuntimeError("Environment directory exists but is not a virtual environment.")
    subprocess.run([str(python), "-I", "-m", "venv", str(environment)], check=True)
    executable = environment / "Scripts" / "python.exe"
    subprocess.run([str(executable), "-I", "-m", "pip", "install", "-r", str(root / "requirements-api.txt")], check=True)
    subprocess.run([str(executable), "-I", "-m", "pip", "check"], check=True)
    subprocess.run([str(executable), "-I", "-c",
                    "import cryptography,kafka,gmalg,tzdata,sys; print('Independent runtime ready:', sys.executable, sys.base_prefix)"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-python", required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This installer is for Windows CPython.")
    prepare(Path(__file__).resolve().parents[2], args.source_python)
