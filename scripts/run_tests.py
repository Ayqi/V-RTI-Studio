#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the test suite against one Blender executable.

    python scripts/run_tests.py --blender /path/to/blender
    python scripts/run_tests.py --blender /path/to/blender --only core orientation

The "installed" test builds the extension and installs it into a throw-away user
profile, so the Blender configuration of the person running the tests is not touched.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "core": "tests/test_core_roundtrip.py",
    "orientation": "tests/test_orientation_storage.py",
    "pipeline": "tests/test_blender_pipeline.py",
}


def run_blender(blender: str, script: str, env: dict | None = None) -> bool:
    # without --python-exit-code Blender returns 0 even when the script raises
    cmd = [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(ROOT / script)]
    return run_step(cmd, env)


def run_step(cmd: list[str], env: dict | None = None) -> bool:
    result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:])
    return result.returncode == 0


def run_installed(blender: str) -> bool:
    """Build the zip, install it as a real extension in a temporary profile and run it."""
    with tempfile.TemporaryDirectory(prefix="vrti_profile_") as tmp:
        env = dict(os.environ, BLENDER_USER_RESOURCES=str(Path(tmp) / "user"))
        dist = Path(tmp) / "dist"
        dist.mkdir()
        if not run_step([blender, "--factory-startup", "--command", "extension", "build", "--source-dir", "vrti",
                         "--output-dir", str(dist)]):
            return False
        archive = next(dist.glob("*.zip"))
        if not run_step([blender, "--factory-startup", "--command", "extension", "install-file",
                         "-r", "user_default", "-e", str(archive)], env):
            return False
        return run_blender(blender, "tests/test_installed_extension.py", env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blender", default=os.environ.get("BLENDER", shutil.which("blender")),
                        help="Blender executable (default: $BLENDER or blender on PATH)")
    parser.add_argument("--only", nargs="*", choices=[*SCRIPTS, "installed"], help="run only these tests")
    args = parser.parse_args()
    if not args.blender:
        parser.error("Blender not found: use --blender or set $BLENDER")

    failed = []
    for name in args.only or [*SCRIPTS, "installed"]:
        start = time.time()
        passed = run_installed(args.blender) if name == "installed" else run_blender(args.blender, SCRIPTS[name])
        print(f"{'PASS' if passed else 'FAIL'}  {name}  ({time.time() - start:.0f} s)", flush=True)
        if not passed:
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
