# SPDX-License-Identifier: GPL-3.0-or-later
"""Image reading helpers.

Blender bundles the OpenImageIO Python module (``OpenImageIO``) since 4.0,
which gives fast, row-range reads of TIFF/PNG/EXR/JPEG. A minimal pure-numpy
reader for *uncompressed* baseline TIFF is kept as a fallback so the core
still works in a plain Python environment without OIIO.
"""
from __future__ import annotations

import struct

import numpy as np

try:  # pragma: no cover - availability depends on the host
    import OpenImageIO as oiio  # type: ignore
except Exception:  # noqa: BLE001
    oiio = None


def have_oiio() -> bool:
    return oiio is not None


def image_size(path: str) -> tuple[int, int, int]:
    """Return (width, height, channels)."""
    if oiio is not None:
        inp = oiio.ImageInput.open(path)
        if inp is None:
            raise OSError(f"OpenImageIO cannot open {path}: {oiio.geterror()}")
        try:
            spec = inp.spec()
            return int(spec.width), int(spec.height), int(spec.nchannels)
        finally:
            inp.close()
    info = _tiff_info(path)
    return info["width"], info["height"], info["spp"]


def read_rows(path: str, y0: int, y1: int, channels: int = 3) -> np.ndarray:
    """Read rows ``[y0, y1)`` as float32 in 0..1, shape (rows, width, channels).

    Row 0 is the **top** row of the image (standard raster order).
    """
    if oiio is not None:
        inp = oiio.ImageInput.open(path)
        if inp is None:
            raise OSError(f"OpenImageIO cannot open {path}: {oiio.geterror()}")
        try:
            spec = inp.spec()
            nch = min(channels, spec.nchannels)
            data = inp.read_scanlines(0, 0, y0, y1, 0, 0, nch, oiio.FLOAT)
            if data is None:
                raise OSError(f"read_scanlines failed on {path}: {inp.geterror()}")
            arr = np.asarray(data, dtype=np.float32).reshape(y1 - y0, spec.width, nch)
        finally:
            inp.close()
    else:
        arr = _tiff_read_rows(path, y0, y1)[..., :channels]
    if arr.shape[2] < channels:  # grey -> replicate
        arr = np.repeat(arr[..., :1], channels, axis=2)
    return arr


class RowReader:
    """Persistent, sequential row reader (keeps the decoder state between blocks).

    Reading successive row ranges from one open OpenImageIO input decodes each
    compressed TIFF strip once; re-opening the file for every block makes the
    decoder restart from the top (measured 10x slower on 24 MP deflate TIFFs).
    """

    def __init__(self, path: str, channels: int = 3):
        self.path = path
        self.channels = channels
        self._inp = None
        if oiio is not None:
            self._inp = oiio.ImageInput.open(path)
            if self._inp is None:
                raise OSError(f"OpenImageIO cannot open {path}: {oiio.geterror()}")
            spec = self._inp.spec()
            self.width, self.height, self.nchannels = int(spec.width), int(spec.height), int(spec.nchannels)
        else:
            info = _tiff_info(path)
            self.width, self.height, self.nchannels = info["width"], info["height"], info["spp"]

    def read(self, y0: int, y1: int) -> np.ndarray:
        if self._inp is not None:
            nch = min(self.channels, self.nchannels)
            data = self._inp.read_scanlines(0, 0, y0, y1, 0, 0, nch, oiio.FLOAT)
            if data is None:
                raise OSError(f"read_scanlines failed on {self.path}: {self._inp.geterror()}")
            arr = np.asarray(data, dtype=np.float32).reshape(y1 - y0, self.width, nch)
        else:
            arr = _tiff_read_rows(self.path, y0, y1)[..., :self.channels]
        if arr.shape[2] < self.channels:
            arr = np.repeat(arr[..., :1], self.channels, axis=2)
        return arr

    def close(self) -> None:
        if self._inp is not None:
            try:
                self._inp.close()
            except Exception:  # noqa: BLE001
                pass
            self._inp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_image(path: str, channels: int = 3) -> np.ndarray:
    _w, h, _c = image_size(path)
    return read_rows(path, 0, h, channels)


def write_image_8bit(path: str, rgb: np.ndarray, quality: int = 100) -> None:
    """Write an (H, W, 3) array (float 0..1 or uint8) as an 8-bit image via OIIO."""
    if oiio is None:
        raise RuntimeError("OpenImageIO is required to write images")
    if rgb.dtype != np.uint8:
        rgb = np.clip(np.rint(np.asarray(rgb, dtype=np.float32) * 255.0), 0, 255).astype(np.uint8)
    h, w, c = rgb.shape
    spec = oiio.ImageSpec(w, h, c, oiio.UINT8)
    spec.attribute("CompressionQuality", int(quality))
    if path.lower().endswith((".jpg", ".jpeg")):
        spec.attribute("Compression", f"jpeg:{int(quality)}")
        spec.attribute("jpeg:subsampling", "4:4:4")   # full colour resolution for the fitters
    out = oiio.ImageOutput.create(path)
    if out is None:
        raise OSError(f"OpenImageIO cannot create {path}: {oiio.geterror()}")
    try:
        if not out.open(path, spec):
            raise OSError(out.geterror())
        if not out.write_image(np.ascontiguousarray(rgb)):
            raise OSError(out.geterror())
    finally:
        out.close()


def convert_to_jpeg(src: str, dst: str, quality: int = 100) -> None:
    """Convert any readable image to an 8-bit JPEG (RTIBuilder input)."""
    rgb = read_image(src, 3)
    write_image_8bit(dst, rgb, quality)


# --------------------------------------------------------------------------- #
# sRGB helpers (IEC 61966-2-1 piecewise transfer function)
# --------------------------------------------------------------------------- #
def srgb_to_linear(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(v: np.ndarray) -> np.ndarray:
    v = np.clip(np.asarray(v, dtype=np.float32), 0.0, None)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1.0 / 2.4) - 0.055).astype(np.float32)


# --------------------------------------------------------------------------- #
# Fallback: uncompressed baseline TIFF (8/16 bit, chunky RGB/RGBA/grey)
# --------------------------------------------------------------------------- #
_TIFF_TYPES = {1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 16: ("Q", 8)}


def _tiff_info(path: str) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(8)
        if head[:2] == b"II":
            e = "<"
        elif head[:2] == b"MM":
            e = ">"
        else:
            raise OSError("not a TIFF file")
        magic = struct.unpack(e + "H", head[2:4])[0]
        if magic != 42:
            raise OSError("BigTIFF is not supported by the fallback reader")
        ifd = struct.unpack(e + "I", head[4:8])[0]
        fh.seek(ifd)
        n = struct.unpack(e + "H", fh.read(2))[0]
        tags = {}
        for _ in range(n):
            tag, typ, cnt = struct.unpack(e + "HHI", fh.read(8))
            raw = fh.read(4)
            fmt, size = _TIFF_TYPES.get(typ, ("B", 1))
            total = size * cnt
            if typ == 5:
                vals = ()
            elif total <= 4:
                vals = struct.unpack(e + fmt * cnt, raw[:total])
            else:
                off = struct.unpack(e + "I", raw)[0]
                cur = fh.tell()
                fh.seek(off)
                vals = struct.unpack(e + fmt * cnt, fh.read(total))
                fh.seek(cur)
            tags[tag] = vals
    width, height = tags[256][0], tags[257][0]
    bps = tags.get(258, (8,))[0]
    spp = tags.get(277, (1,))[0]
    comp = tags.get(259, (1,))[0]
    planar = tags.get(284, (1,))[0]
    if comp != 1 or planar != 1:
        raise OSError("fallback TIFF reader supports only uncompressed chunky data; "
                      "install/enable OpenImageIO for other files")
    rps = tags.get(278, (height,))[0]
    return {
        "endian": e, "width": width, "height": height, "bps": bps, "spp": spp,
        "rows_per_strip": rps, "strip_offsets": tags[273], "strip_counts": tags.get(279, ()),
    }


def _tiff_read_rows(path: str, y0: int, y1: int) -> np.ndarray:
    info = _tiff_info(path)
    w, spp, bps = info["width"], info["spp"], info["bps"]
    dtype = np.dtype(info["endian"] + ("u2" if bps == 16 else "u1"))
    rps = info["rows_per_strip"]
    row_bytes = w * spp * dtype.itemsize
    out = np.empty((y1 - y0, w, spp), dtype=dtype)
    with open(path, "rb") as fh:
        for y in range(y0, y1):
            strip = y // rps
            fh.seek(info["strip_offsets"][strip] + (y - strip * rps) * row_bytes)
            out[y - y0] = np.frombuffer(fh.read(row_bytes), dtype=dtype).reshape(w, spp)
    scale = 65535.0 if bps == 16 else 255.0
    return out.astype(np.float32) / scale
