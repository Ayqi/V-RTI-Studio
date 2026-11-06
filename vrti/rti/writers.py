# SPDX-License-Identifier: GPL-3.0-or-later
"""Bit-exact writers (and verification readers) for the classic RTI files.

PTM 1.2 (HP Labs, Malzbender et al. 2001)
-----------------------------------------
::

    PTM_1.2\n
    PTM_FORMAT_LRGB | PTM_FORMAT_RGB\n
    <width>\n<height>\n
    <6 float scale>\n<6 int bias>\n
    <binary data>

* coefficient ``a_k = (byte - bias_k) * scale_k``; ``L(lu, lv)`` is in 0..255
* LRGB: for every pixel 6 coefficient bytes, then (after all pixels) 3 RGB bytes
  per pixel. Colour = ``RGB * L / 255``.
* RGB: three consecutive blocks (R, G, B), each 6 bytes per pixel. Channel value
  = ``L_c``.
* Pixels are stored in **reversed scanline order** (bottom row first).

HSH ``.rti`` (CHI hshfitter / RTIViewer "universal RTI")
---------------------------------------------------------
::

    #HSH1.2\n
    3\n                      (rti type 3 = HSH)
    <width> <height> 3\n     (colours per pixel)
    <terms> 2 1\n            (basis terms 4/9/16, basis type, element size)
    float32[terms] scale, float32[terms] bias   (little endian)
    <binary data>

* coefficient ``c_k = byte / 255 * scale_k + bias_k``; value in 0..1
* per pixel: all R terms, then all G terms, then all B terms
* rows TOP to bottom - unlike PTM. Verified on two real RTIBuilder/relight .rti files
  (correlation with their normal maps 0.95 / 0.89 top-down vs 0.37 / 0.78 bottom-up)
  and matching OpenRTI's reader, which was checked against hshfitter output.
"""
from __future__ import annotations

import os
import struct
from collections.abc import Callable, Iterator, Sequence

import numpy as np

from .basis import hsh_basis, ptm_basis

RowProvider = Callable[[int, int], np.ndarray]  # (y0, y1) -> rows y0..y1 (top-down indexing)


# --------------------------------------------------------------------------- #
# Quantisation helpers
# --------------------------------------------------------------------------- #
def ptm_scale_bias(cmin: np.ndarray, cmax: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-term float ``scale`` and integer ``bias`` for PTM quantisation.

    ``byte = round(c / scale + bias)`` with ``bias`` an unsigned byte, so the
    representable interval is ``[-bias*scale, (255-bias)*scale]`` and must
    contain zero. The interval is widened to include zero and 254 steps are
    used to leave rounding slack.
    """
    lo = np.minimum(np.asarray(cmin, dtype=np.float64), 0.0)
    hi = np.maximum(np.asarray(cmax, dtype=np.float64), 0.0)
    span = np.maximum(hi - lo, 1e-6)
    scale = span / 254.0
    bias = np.clip(np.rint(-lo / scale), 0, 255).astype(np.int64)
    return scale, bias


def ptm_quantize(coeffs: np.ndarray, scale: np.ndarray, bias: np.ndarray) -> np.ndarray:
    q = np.rint(coeffs / scale.astype(np.float32) + bias.astype(np.float32))
    return np.clip(q, 0, 255).astype(np.uint8)


def hsh_scale_bias(cmin: np.ndarray, cmax: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cmin = np.asarray(cmin, dtype=np.float64)
    cmax = np.asarray(cmax, dtype=np.float64)
    scale = np.maximum(cmax - cmin, 1e-6)
    return scale.astype(np.float32), cmin.astype(np.float32)


def hsh_quantize(coeffs: np.ndarray, scale: np.ndarray, bias: np.ndarray) -> np.ndarray:
    q = np.rint((coeffs - bias) / scale * 255.0)
    return np.clip(q, 0, 255).astype(np.uint8)


def _blocks_bottom_up(height: int, block_rows: int) -> Iterator[tuple[int, int]]:
    y1 = height
    while y1 > 0:
        y0 = max(0, y1 - block_rows)
        yield y0, y1
        y1 = y0


# --------------------------------------------------------------------------- #
# PTM writers
# --------------------------------------------------------------------------- #
def _ptm_header(fmt: str, width: int, height: int, scale, bias) -> bytes:
    s = " ".join(f"{float(v):.9g}" for v in scale)
    b = " ".join(str(int(v)) for v in bias)
    return f"PTM_1.2\n{fmt}\n{width}\n{height}\n{s}\n{b}\n".encode("ascii")


def iter_write_ptm_lrgb(path: str, width: int, height: int,
                        coeff_rows: RowProvider, rgb_rows: RowProvider,
                        scale: np.ndarray, bias: np.ndarray,
                        block_rows: int = 256) -> Iterator[float]:
    """Generator writing an LRGB PTM; yields progress 0..1 after each block.

    ``coeff_rows(y0, y1)`` -> float32 (rows, W, 6) luminance coefficients in the
    0..255 domain, top-down indexing. ``rgb_rows(y0, y1)`` -> uint8 (rows, W, 3).
    """
    total = 2 * height
    done = 0
    with open(path, "wb") as fh:
        fh.write(_ptm_header("PTM_FORMAT_LRGB", width, height, scale, bias))
        for y0, y1 in _blocks_bottom_up(height, block_rows):
            blk = np.asarray(coeff_rows(y0, y1), dtype=np.float32)[::-1]  # bottom row first
            fh.write(ptm_quantize(blk, scale, bias).tobytes())
            done += y1 - y0
            yield done / total
        for y0, y1 in _blocks_bottom_up(height, block_rows):
            blk = np.asarray(rgb_rows(y0, y1), dtype=np.uint8)[::-1]
            fh.write(np.ascontiguousarray(blk).tobytes())
            done += y1 - y0
            yield done / total


def iter_write_ptm_rgb(path: str, width: int, height: int,
                       coeff_rows: RowProvider, scale: np.ndarray, bias: np.ndarray,
                       block_rows: int = 256) -> Iterator[float]:
    """Generator writing an RGB PTM. ``coeff_rows`` -> float32 (rows, W, 3, 6)."""
    total = 3 * height
    done = 0
    with open(path, "wb") as fh:
        fh.write(_ptm_header("PTM_FORMAT_RGB", width, height, scale, bias))
        for ch in range(3):
            for y0, y1 in _blocks_bottom_up(height, block_rows):
                blk = np.asarray(coeff_rows(y0, y1), dtype=np.float32)[::-1, :, ch, :]
                fh.write(ptm_quantize(np.ascontiguousarray(blk), scale, bias).tobytes())
                done += y1 - y0
                yield done / total


# --------------------------------------------------------------------------- #
# HSH writer
# --------------------------------------------------------------------------- #
def iter_write_hsh(path: str, width: int, height: int, terms: int,
                   coeff_rows: RowProvider, scale: np.ndarray, bias: np.ndarray,
                   block_rows: int = 256) -> Iterator[float]:
    """Generator writing an HSH ``.rti``. ``coeff_rows`` -> float32 (rows, W, 3, T)."""
    if terms not in (4, 9, 16):
        raise ValueError("HSH terms must be 4, 9 or 16")
    scale = np.asarray(scale, dtype="<f4")
    bias = np.asarray(bias, dtype="<f4")
    done = 0
    with open(path, "wb") as fh:
        fh.write(f"#HSH1.2\n3\n{width} {height} 3\n{terms} 2 1\n".encode("ascii"))
        fh.write(scale.tobytes())
        fh.write(bias.tobytes())
        # HSH rows are stored TOP to bottom (unlike PTM): checked against real
        # RTIBuilder/relight .rti files and their normal maps, and OpenRTI's reader
        for y0 in range(0, height, block_rows):
            y1 = min(height, y0 + block_rows)
            blk = np.asarray(coeff_rows(y0, y1), dtype=np.float32)
            fh.write(hsh_quantize(np.ascontiguousarray(blk), scale, bias).tobytes())
            done += y1 - y0
            yield done / height


def drain(gen: Iterator[float]) -> None:
    for _ in gen:
        pass


# --------------------------------------------------------------------------- #
# Verification readers (independent re-implementation of RTIViewer semantics)
# --------------------------------------------------------------------------- #
def _read_line(fh) -> str:
    out = bytearray()
    while True:
        c = fh.read(1)
        if not c or c == b"\n":
            break
        out += c
    return out.decode("ascii").strip()


def read_ptm(path: str) -> dict:
    """Read an uncompressed PTM 1.2 (LRGB or RGB) into float coefficients.

    Returns ``{"format", "width", "height", "coeffs", "rgb"}`` where ``coeffs``
    is (H, W, 6) for LRGB or (H, W, 3, 6) for RGB, **top row first**, and in
    the 0..255 luminance domain.
    """
    with open(path, "rb") as fh:
        version = _read_line(fh)
        if version != "PTM_1.2":
            raise ValueError(f"unsupported PTM version {version!r}")
        fmt = _read_line(fh)
        dims = _read_line(fh).split()
        if len(dims) == 1:
            w = int(dims[0])
            h = int(_read_line(fh))
        else:
            w, h = int(dims[0]), int(dims[1])
        scale = np.array([float(v) for v in _read_line(fh).split()], dtype=np.float64)
        bias = np.array([float(v) for v in _read_line(fh).split()], dtype=np.float64)
        n = w * h
        if fmt == "PTM_FORMAT_LRGB":
            raw = np.frombuffer(fh.read(n * 6), dtype=np.uint8).reshape(h, w, 6)[::-1]
            rgb = np.frombuffer(fh.read(n * 3), dtype=np.uint8).reshape(h, w, 3)[::-1]
            coeffs = (raw.astype(np.float32) - bias.astype(np.float32)) * scale.astype(np.float32)
            return {"format": fmt, "width": w, "height": h, "coeffs": coeffs, "rgb": rgb.copy(),
                    "scale": scale, "bias": bias}
        if fmt == "PTM_FORMAT_RGB":
            chans = []
            for _ in range(3):
                raw = np.frombuffer(fh.read(n * 6), dtype=np.uint8).reshape(h, w, 6)[::-1]
                chans.append((raw.astype(np.float32) - bias.astype(np.float32)) * scale.astype(np.float32))
            coeffs = np.stack(chans, axis=2)
            return {"format": fmt, "width": w, "height": h, "coeffs": coeffs, "rgb": None,
                    "scale": scale, "bias": bias}
    raise ValueError(f"unsupported PTM format {fmt!r}")


def read_hsh(path: str) -> dict:
    """Read an HSH ``.rti`` into float coefficients (H, W, 3, T), top row first."""
    with open(path, "rb") as fh:
        line = _read_line(fh)
        if not line.startswith("#HSH1.2"):
            raise ValueError(f"not an HSH1.2 rti file (first line {line!r})")
        while line.startswith("#"):
            line = _read_line(fh)
        rti_type = int(line)
        if rti_type != 3:
            raise ValueError("not an HSH rti file")
        w, h, ncol = (int(v) for v in _read_line(fh).split())
        terms, basis_type, _elem = (int(v) for v in _read_line(fh).split())
        if terms == 1:
            terms = 4
        elif terms == 2:
            terms = 9
        scale = np.frombuffer(fh.read(4 * terms), dtype="<f4").astype(np.float32)
        bias = np.frombuffer(fh.read(4 * terms), dtype="<f4").astype(np.float32)
        raw = np.frombuffer(fh.read(w * h * ncol * terms), dtype=np.uint8)
        raw = raw.reshape(h, w, ncol, terms)          # rows top to bottom
        coeffs = raw.astype(np.float32) / 255.0 * scale + bias
        return {"width": w, "height": h, "terms": terms, "basis_type": basis_type,
                "coeffs": coeffs, "scale": scale, "bias": bias}


def relight_ptm(ptm: dict, direction: Sequence[float]) -> np.ndarray:
    """Evaluate a PTM for one light direction -> float32 RGB (H, W, 3) in 0..1."""
    b = ptm_basis([direction])[0].astype(np.float32)  # (6,)
    if ptm["format"] == "PTM_FORMAT_LRGB":
        lum = np.clip(ptm["coeffs"] @ b, 0, 255) / 255.0  # (H, W)
        return np.clip(ptm["rgb"].astype(np.float32) / 255.0 * lum[..., None], 0, 1)
    val = np.clip(ptm["coeffs"] @ b, 0, 255) / 255.0  # (H, W, 3)
    return val.astype(np.float32)


def relight_hsh(hsh: dict, direction: Sequence[float]) -> np.ndarray:
    order = {4: 1, 9: 2, 16: 3}[hsh["terms"]]
    b = hsh_basis([direction], order)[0].astype(np.float32)
    return np.clip(hsh["coeffs"] @ b, 0, 1).astype(np.float32)


def ptm_normals(coeffs6: np.ndarray) -> np.ndarray:
    """Surface normal estimate from PTM coefficients (Malzbender 2001, eq. 4-5)."""
    a0, a1, a2, a3, a4 = (coeffs6[..., k].astype(np.float64) for k in range(5))
    det = 4.0 * a0 * a1 - a2 * a2
    safe = np.abs(det) > 1e-9
    lu = np.where(safe, (a2 * a4 - 2.0 * a1 * a3) / np.where(safe, det, 1.0), 0.0)
    lv = np.where(safe, (a2 * a3 - 2.0 * a0 * a4) / np.where(safe, det, 1.0), 0.0)
    r2 = lu * lu + lv * lv
    too_big = r2 > 1.0
    norm = np.sqrt(np.where(too_big, r2, 1.0))
    lu, lv = lu / norm, lv / norm
    lz = np.sqrt(np.clip(1.0 - lu * lu - lv * lv, 0.0, 1.0))
    return np.stack([lu, lv, lz], axis=-1).astype(np.float32)


def file_size_estimate(width: int, height: int, fmt: str) -> int:
    per_pixel = {"PTM_LRGB": 9, "PTM_RGB": 18, "HSH1": 12, "HSH2": 27, "HSH3": 48}[fmt]
    return width * height * per_pixel + 256


__all__ = [
    "ptm_scale_bias", "ptm_quantize", "hsh_scale_bias", "hsh_quantize",
    "iter_write_ptm_lrgb", "iter_write_ptm_rgb", "iter_write_hsh", "drain",
    "read_ptm", "read_hsh", "relight_ptm", "relight_hsh", "ptm_normals",
    "file_size_estimate",
]

# silence unused import warnings for optional helpers
_ = (os, struct)
