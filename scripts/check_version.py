#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail when the version numbers disagree: manifest, bl_info, vrti/version.py, CHANGELOG, CITATION."""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def bl_info_version() -> str:
    tree = ast.parse((ROOT / "vrti/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "bl_info":
            return ".".join(str(v) for v in ast.literal_eval(node.value)["version"])
    raise SystemExit("bl_info not found")


def module_version() -> str:
    text = (ROOT / "vrti/version.py").read_text(encoding="utf-8")
    return ".".join(re.search(r"VERSION = \((\d+), (\d+), (\d+)\)", text).groups())


def main() -> int:
    versions = {
        "blender_manifest.toml": tomllib.loads((ROOT / "vrti/blender_manifest.toml").read_text("utf-8"))["version"],
        "bl_info": bl_info_version(),
        "vrti/version.py": module_version(),
        "CHANGELOG.md": re.search(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text("utf-8"), re.M)[1],
        "CITATION.cff": re.search(r"^version: (\S+)", (ROOT / "CITATION.cff").read_text("utf-8"), re.M)[1],
    }
    if len(set(versions.values())) != 1:
        print("Version mismatch:", versions)
        return 1
    print("Version", next(iter(versions.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
