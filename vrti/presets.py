# SPDX-License-Identifier: GPL-3.0-or-later
"""Static presets: sensor formats, resolution ladder and dome light layouts.

Everything here is plain Python (no bpy) so it can be unit-tested.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

# --------------------------------------------------------------------------- #
# Sensor / resolution ladder
# --------------------------------------------------------------------------- #
# Megapixel steps modelled on real full-frame and medium-format bodies that are
# commonly used for H-RTI (24 MP being the de-facto standard; 36-61 MP for
# high-end capture; 100 MP medium format).
RESOLUTION_MP: list[int] = [8, 12, 16, 20, 24, 30, 36, 45, 50, 61, 100]

ASPECTS: dict[str, tuple[float, str]] = {
    "3:2": (3.0 / 2.0, "Full frame / APS-C (3:2)"),
    "4:3": (4.0 / 3.0, "Medium format / 4:3"),
    "1:1": (1.0, "Square (1:1)"),
    "FIT": (0.0, "Fit to object"),   # aspect taken from the specimen footprint (1:1 … 4:1)
}
MAX_FIT_ASPECT = 4.0


def fit_aspect(size_x: float, size_y: float) -> float:
    """Frame aspect (long / short side) that matches the specimen footprint."""
    long_side, short_side = max(size_x, size_y), min(size_x, size_y)
    if long_side <= 0 or short_side <= 0:
        return 1.5
    return max(1.0, min(MAX_FIT_ASPECT, long_side / short_side))

# physical sensor used for the virtual camera (full-frame 35 mm)
SENSOR_WIDTH_MM = 36.0
SENSOR_HEIGHT_MM = 24.0


# Measured size of real Cycles captures of stone inscriptions, bytes per pixel.
# TIFF: deflate, 6 MP tests. JPEG: q95, 16 MP set. Content dependent - estimates only.
CAPTURE_BYTES_PER_PIXEL = {("16", "RGB"): 2.3, ("16", "BW"): 0.8, ("8", "RGB"): 0.66, ("8", "BW"): 0.28,
                           ("JPEG", "RGB"): 0.31, ("JPEG", "BW"): 0.12}


def capture_bytes_per_pixel(depth: str, channels: str) -> float:
    return CAPTURE_BYTES_PER_PIXEL.get((str(depth), channels), 2.3)


def resolution_for(mp: int, aspect: float) -> tuple[int, int]:
    """Return (long side, short side) in pixels, multiples of 8, ~``mp`` megapixels."""
    px = mp * 1_000_000
    long_side = int(round(math.sqrt(px * aspect) / 8.0) * 8)
    short_side = int(round(long_side / aspect / 8.0) * 8)
    return long_side, short_side


def megapixels(w: int, h: int) -> float:
    return w * h / 1_000_000.0


# Upper end of the recommendation: the resolution of the full-frame bodies used for
# H-RTI and dome RTI. Above it the PTM/HSH files grow past what desktop and web
# viewers handle comfortably (HSH order 2 = 27 bytes per pixel: ~1 GB at 36 MP),
# while the photogrammetric texture rarely carries more real detail than the
# photographs it was made from. Larger steps stay available in the list.
PRACTICAL_MAX_MP = 36


# Quality tiers. The size is the ★ recommendation limited to ``max_mp``.
#   QUICK    framing / material check
#   WEB      OpenRTI and web viewers: OpenRTI accepts PTM-LRGB and HSH .rti, converts them
#            in the browser (phones/tablets stop at 60 MP) and stores lossless WebP tiles in
#            a 1 GB quota per member
#   ARCHIVE  full-frame H-RTI range (≤ 36 MP), 16-bit masters, HSH 16 when the dome allows it
#
# JPEG q95 captures (measured on a 64-light inscription): RTI fitted from them differs from the
# RTI fitted from lossless TIFF by 44.6 dB (normals 0.37° median, 1.1° p95) - 10 dB below
# the PTM/HSH model's own error - at 1/3 of the size of 8-bit TIFF.
QUALITY_TIERS = {
    "QUICK": {"max_mp": 8, "formats": ("PTM_LRGB",), "depth": "JPEG"},
    "WEB": {"max_mp": 16, "formats": ("PTM_LRGB", "HSH2"), "depth": "JPEG"},
    "ARCHIVE": {"max_mp": 36, "formats": ("PTM_LRGB", "HSH2", "HSH3"), "depth": "16"},
}

# OpenRTI (openrti.org) facts used for the upload estimate
OPENRTI_PLANES = {"PTM_LRGB": 3, "HSH1": 4, "HSH2": 9, "HSH3": 16}   # PTM_RGB is not accepted
# Upload bytes per SPECIMEN pixel (lossless WebP tile pyramid; the black background costs
# almost nothing). Measured with OpenRTI's own converter on an inscription at 16 MP:
#   3:2 frame, 25 % stone:    PTM-LRGB 144 MB -> 23 MB, HSH 9 431 MB -> 47 MB
#   4:1 fitted frame, 66 %:   PTM-LRGB 144 MB -> 56 MB, HSH 9 432 MB -> 124 MB
#   same, greyscale:          PTM-LRGB -> 50 MB, HSH 9 -> 67 MB (R = G = B costs ~nothing)
OPENRTI_BYTES_PER_SPECIMEN_PIXEL = {"PTM_LRGB": 5.7, "HSH1": 5.2, "HSH2": 11.7, "HSH3": 20.8}
OPENRTI_BYTES_PER_SPECIMEN_PIXEL_GREY = {"PTM_LRGB": 5.1, "HSH1": 2.8, "HSH2": 6.3, "HSH3": 11.2}
SPECIMEN_OUTLINE_SHARE = 0.85            # irregular outline inside the footprint rectangle
OPENRTI_DEVICE_MAX_MP = 60               # browser conversion limit on phones / tablets
OPENRTI_QUOTA_BYTES = 1024 ** 3          # storage per member


def openrti_upload_bytes(width: int, height: int, fmt: str, fill: float = 1.0, grey: bool = False) -> int:
    """Estimated OpenRTI upload for one RTI file; ``fill`` = footprint share of the frame."""
    rate = (OPENRTI_BYTES_PER_SPECIMEN_PIXEL_GREY if grey else OPENRTI_BYTES_PER_SPECIMEN_PIXEL).get(fmt)
    if not rate:
        return 0
    return int(width * height * max(0.05, min(1.0, fill)) * SPECIMEN_OUTLINE_SHARE * rate)


def tier_mp(tier: str, recommended: int) -> int:
    """Ladder step for a quality tier: the recommendation, at most the tier's maximum."""
    cap = QUALITY_TIERS[tier]["max_mp"]
    rec = recommended or cap
    return rec if rec <= cap else max(mp for mp in RESOLUTION_MP if mp <= cap)


def detail_mp(texel_count: float, face_count: int, fill_ratio: float) -> int:
    """Smallest ladder step with one pixel per visible texel and per visible face.

    * ``texel_count`` – texture pixels on the camera-facing surface
    * ``face_count`` – mesh faces on the camera-facing surface
    * ``fill_ratio`` – fraction of the frame covered by the specimen (0..1)
    """
    fill = max(0.05, min(1.0, fill_ratio))
    need = max(texel_count, float(face_count)) / fill
    for mp in RESOLUTION_MP:
        if mp * 1_000_000 >= need:
            return mp
    return RESOLUTION_MP[-1]


def recommended_mp(texel_count: float, face_count: int, fill_ratio: float,
                   cap: int = PRACTICAL_MAX_MP) -> int:
    """Smallest step that keeps the visible detail, at most ``cap`` (the H-RTI range)."""
    full = detail_mp(texel_count, face_count, fill_ratio)
    if full <= cap:
        return full
    return max(mp for mp in RESOLUTION_MP if mp <= cap)


# --------------------------------------------------------------------------- #
# Dome layouts
# --------------------------------------------------------------------------- #
# Each ring: (elevation in degrees above the specimen plane, number of LEDs).
# Real domes concentrate LEDs at low/medium elevations because grazing light
# is what reveals relief; nothing sits at the zenith because the camera is
# there. Counts scale roughly with cos(elevation) for near-uniform coverage.
DOME_PRESETS: dict[str, dict] = {
    "D36": {
        "label": "Compact dome · 36 lights (3 × 12)",
        "rings": [(20, 12), (45, 12), (70, 12)],
        "note": "Fast preview quality. Comparable to a small 3-ring hobby dome.",
    },
    "D48": {
        "label": "Classic dome · 48 lights (4 rings)",
        # counts follow cos(elevation): every ring has ~22° between neighbours, and the
        # grazing ring that reveals relief gets the most lights (16 / 14 / 11 / 7)
        "rings": [(15, 16), (30, 14), (45, 11), (65, 7)],
        "note": "4 rings at 15°, 30°, 45°, 65°; most lights at grazing angles, ~22° apart everywhere.",
    },
    "D64": {
        "label": "Dense dome · 64 lights (5 rings) ★",
        "rings": [(12, 20), (28, 16), (44, 12), (60, 10), (76, 6)],
        "note": "64-LED domes are the most common research configuration.",
    },
    "D80": {
        "label": "Research dome · 80 lights (5 rings)",
        "rings": [(10, 24), (26, 20), (42, 16), (58, 12), (74, 8)],
        "note": "Near-uniform hemisphere coverage; recommended for HSH order 3.",
    },
    "D120": {
        "label": "Hemisphere sampling · 120 lights (6 rings)",
        "rings": [(8, 32), (22, 28), (36, 22), (50, 18), (64, 12), (78, 8)],
        "note": "Over-determined fit for the lowest possible residual.",
    },
}


def custom_rings(n_rings: int, first_ring: int, el_min: float, el_max: float) -> list[tuple[float, int]]:
    """Evenly spaced rings; LED count per ring scales with cos(elevation)."""
    n_rings = max(1, n_rings)
    rings = []
    for k in range(n_rings):
        el = el_min if n_rings == 1 else el_min + (el_max - el_min) * k / (n_rings - 1)
        cnt = max(3, int(round(first_ring * math.cos(math.radians(el)) / math.cos(math.radians(el_min)))))
        rings.append((el, cnt))
    return rings


def ring_directions(rings: Sequence[tuple[float, int]], stagger: bool = True,
                    include_zenith: bool = False) -> list[tuple[float, float, float]]:
    """Unit vectors (x right, y up, z towards camera) ring by ring, low to high."""
    dirs: list[tuple[float, float, float]] = []
    for k, (el, cnt) in enumerate(rings):
        e = math.radians(el)
        offset = 0.5 if (stagger and k % 2 == 1) else 0.0
        for i in range(cnt):
            az = 2.0 * math.pi * (i + offset) / cnt
            dirs.append((math.cos(e) * math.cos(az), math.cos(e) * math.sin(az), math.sin(e)))
    if include_zenith:
        dirs.append((0.0, 0.0, 1.0))
    return dirs


def direction_to_angles(d: Sequence[float]) -> tuple[float, float]:
    """(elevation deg, azimuth deg) of a unit direction."""
    x, y, z = d
    return math.degrees(math.asin(max(-1.0, min(1.0, z)))), math.degrees(math.atan2(y, x)) % 360.0
