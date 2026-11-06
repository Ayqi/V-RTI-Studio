# SPDX-License-Identifier: GPL-3.0-or-later
"""Light Position (.lp) files as used by RTIBuilder, hshfitter and relight.

Format::

    <number of images>
    <image file name> <x> <y> <z>
    ...

``x y z`` is the unit vector pointing from the object towards the light,
``x`` right, ``y`` up (image space), ``z`` towards the camera.
"""
from __future__ import annotations

import os
from collections.abc import Sequence


def write_lp(path: str, filenames: Sequence[str], directions, precision: int = 6) -> None:
    dirs = [tuple(float(c) for c in d) for d in directions]
    if len(dirs) != len(filenames):
        raise ValueError("filenames and directions must have the same length")
    lines = [str(len(dirs))]
    fmt = "{:." + str(precision) + "f}"
    for name, (x, y, z) in zip(filenames, dirs):
        lines.append(f"{name} {fmt.format(x)} {fmt.format(y)} {fmt.format(z)}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def read_lp(path: str) -> tuple[list[str], list[tuple[float, float, float]]]:
    names: list[str] = []
    dirs: list[tuple[float, float, float]] = []
    with open(path, encoding="utf-8") as fh:
        raw = [ln.strip() for ln in fh if ln.strip()]
    if not raw:
        raise ValueError("empty .lp file")
    try:
        count = int(raw[0].split()[0])
        body = raw[1:]
    except ValueError:
        count = len(raw)
        body = raw
    for ln in body[:count]:
        parts = ln.split()
        if len(parts) < 4:
            continue
        names.append(parts[0])
        dirs.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return names, dirs


def resolve_lp_paths(lp_path: str, names: Sequence[str]) -> list[str]:
    base = os.path.dirname(os.path.abspath(lp_path))
    return [n if os.path.isabs(n) else os.path.join(base, n) for n in names]
