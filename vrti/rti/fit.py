# SPDX-License-Identifier: GPL-3.0-or-later
"""Streaming least-squares RTI fitting.

The fitter never holds the whole image stack in memory. It walks the images in
row blocks, reads the same rows from every light image (through persistent,
sequential readers - 10x faster than re-opening compressed TIFFs), solves the
per-pixel least-squares problem with one matrix product
(``pinv(A) @ observations``) and stores the float coefficients in disk-backed
``numpy.memmap`` scratch files. A second pass quantises them to the 8-bit
PTM / HSH layouts.

This mirrors the reference tools:

* PTM: Malzbender, Gelb, Wolters (2001) – SVD based LSQ fit of a biquadratic
  per pixel; LRGB variant separates luminance polynomial and chroma.
* HSH: CHI ``hshfitter`` – LSQ fit of hemispherical harmonics per channel.

Values are fitted in the **display-referred** (sRGB-encoded) domain by
default, exactly like RTIBuilder fits camera JPEG/TIFF files. A ``LINEAR``
option linearises the data first for radiometric work.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import numpy as np

from . import imageio
from .basis import HSH_TERMS, condition_number, design_rank, hsh_basis, least_squares_operator, ptm_basis
from .writers import (
    file_size_estimate,
    hsh_scale_bias,
    iter_write_hsh,
    iter_write_ptm_lrgb,
    iter_write_ptm_rgb,
    ptm_scale_bias,
)

FORMAT_INFO = {
    "PTM_LRGB": {"label": "PTM (LRGB)", "ext": ".ptm", "suffix": "_lrgb", "terms": 6,
                 "needs": "6 light directions that are not all on one ring (2+ elevation rings)"},
    "PTM_RGB": {"label": "PTM (RGB)", "ext": ".ptm", "suffix": "_rgb", "terms": 6,
                "needs": "6 light directions that are not all on one ring (2+ elevation rings)"},
    "HSH1": {"label": "HSH order 1 (4 terms)", "ext": ".rti", "suffix": "_hsh4", "terms": 4,
             "needs": "4+ lights on 2+ elevation rings"},
    "HSH2": {"label": "HSH order 2 (9 terms)", "ext": ".rti", "suffix": "_hsh9", "terms": 9,
             "needs": "9+ lights on 3+ elevation rings (5+ azimuths)"},
    "HSH3": {"label": "HSH order 3 (16 terms)", "ext": ".rti", "suffix": "_hsh16", "terms": 16,
             "needs": "16+ lights on 4+ elevation rings (7+ azimuths); 48+ lights recommended"},
}

# peak temporaries per block relative to the raw input stack (stack + obs copy +
# reconstruction + LRGB working copies)
_PEAK_FACTOR = 4.0


@dataclass
class FitRequest:
    image_paths: list[str]
    directions: Sequence[Sequence[float]]
    formats: list[str]
    out_dir: str
    base_name: str
    colorspace: str = "SRGB"            # "SRGB" (RTIBuilder-like) or "LINEAR"
    block_mem_mb: int = 1024
    temp_dir: str | None = None
    write_report: bool = True
    extra_metadata: dict = field(default_factory=dict)


class DesignError(ValueError):
    """The light layout cannot determine all basis terms of a requested format."""


def check_design(A: np.ndarray, fmt: str) -> dict:
    terms = FORMAT_INFO[fmt]["terms"]
    n = A.shape[0]
    rank = design_rank(A)
    cond = condition_number(A) if rank == terms else float("inf")
    if n < terms or rank < terms:
        raise DesignError(
            f"{FORMAT_INFO[fmt]['label']} cannot be fitted with this light layout "
            f"({n} lights, rank {rank} of {terms} basis terms). Needs {FORMAT_INFO[fmt]['needs']}.")
    return {"rank": rank, "cond": cond, "lights": n}


class FitJob:
    """Resumable, step-wise fitting job (drive it from a modal operator or run())."""

    def __init__(self, req: FitRequest):
        self.req = req
        self.progress = 0.0
        self.message = ""
        self.outputs: list[str] = []
        self.report: dict = {}
        self.error: str | None = None
        self._gen: Iterator[None] | None = None
        self._memmaps: dict[str, np.memmap] = {}
        self._store: dict[str, np.memmap] = {}
        self._tmp_files: list[str] = []
        self._readers: list[imageio.RowReader] = []
        self.width = self.height = 0
        self.started = time.time()
        self.finished = False

    # ------------------------------------------------------------------ #
    def step(self) -> bool:
        """Advance one block. Returns True when the job is finished."""
        if self.finished:
            return True
        if self._gen is None:
            self._gen = self._run()
        try:
            next(self._gen)
            return False
        except StopIteration:
            self.progress = 1.0
            self.finished = True
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.cleanup()
            raise

    def run(self, callback=None) -> None:
        while not self.step():
            if callback is not None:
                callback(self.progress, self.message)

    def cancel(self) -> None:
        if self._gen is not None:
            try:
                self._gen.close()  # runs the generator's finally: block
            except Exception:  # noqa: BLE001
                pass
        self.cleanup()

    def cleanup(self) -> None:
        for r in self._readers:
            r.close()
        self._readers = []
        self._store.clear()
        for key in list(self._memmaps):
            mm = self._memmaps.pop(key)
            try:
                mm.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                mm._mmap.close()  # release the Windows file mapping before unlinking
            except Exception:  # noqa: BLE001
                pass
            del mm
        for p in self._tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass
        self._tmp_files.clear()
        try:
            tmp_dir = self.req.temp_dir or os.path.join(self.req.out_dir, ".vrti_tmp")
            if os.path.isdir(tmp_dir) and not os.listdir(tmp_dir):
                os.rmdir(tmp_dir)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def _memmap(self, key: str, shape, dtype=np.float32) -> np.memmap:
        tmp_dir = self.req.temp_dir or os.path.join(self.req.out_dir, ".vrti_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, f"{self.req.base_name}_{key}.bin")
        mm = np.memmap(path, dtype=dtype, mode="w+", shape=shape)
        self._memmaps[key] = mm
        self._tmp_files.append(path)
        return mm

    def _run(self) -> Iterator[None]:
        req = self.req
        n = len(req.image_paths)
        if n != len(req.directions):
            raise ValueError("number of images and light directions differ")
        if not req.formats:
            raise ValueError("no output format selected")
        for f in req.formats:
            if f not in FORMAT_INFO:
                raise ValueError(f"unknown format {f}")
        os.makedirs(req.out_dir, exist_ok=True)

        w, h, _c = imageio.image_size(req.image_paths[0])
        self.width, self.height = w, h
        dirs = np.asarray(req.directions, dtype=np.float64)

        # design matrices, rank checks and pseudo-inverses ----------------
        ops: dict[str, dict] = {}
        if any(f.startswith("PTM") for f in req.formats):
            A = ptm_basis(dirs)
            info = check_design(A, "PTM_LRGB")
            ops["PTM"] = {"A": A.astype(np.float32), "P": least_squares_operator(A).astype(np.float32), **info}
        for order in HSH_TERMS:
            key = f"HSH{order}"
            if key in req.formats:
                A = hsh_basis(dirs, order)
                info = check_design(A, key)
                ops[key] = {"A": A.astype(np.float32), "P": least_squares_operator(A).astype(np.float32), **info}

        # scratch storage ---------------------------------------------------
        store = self._store
        if "PTM_LRGB" in req.formats:
            store["PTM_LRGB"] = self._memmap("lrgb_coef", (h, w, 6))
            store["PTM_LRGB_RGB"] = self._memmap("lrgb_rgb", (h, w, 3), np.uint8)
        if "PTM_RGB" in req.formats:
            store["PTM_RGB"] = self._memmap("rgb_coef", (h, w, 3, 6))
        for order, terms in HSH_TERMS.items():
            key = f"HSH{order}"
            if key in req.formats:
                store[key] = self._memmap(f"hsh{terms}_coef", (h, w, 3, terms))

        cmin = {k: np.full(FORMAT_INFO[k]["terms"], np.inf, np.float64) for k in req.formats}
        cmax = {k: np.full(FORMAT_INFO[k]["terms"], -np.inf, np.float64) for k in req.formats}
        # residuals are reported for the specimen only: the black background of a
        # virtual capture is fitted perfectly and would inflate the PSNR
        sse = {k: 0.0 for k in req.formats}
        count = {k: 0 for k in req.formats}
        sse_all = {k: 0.0 for k in req.formats}
        count_all = {k: 0 for k in req.formats}
        clipped_px = 0
        specimen_px = 0

        bytes_per_row = n * w * 3 * 4 * _PEAK_FACTOR
        block_rows = int(max(1, min(h, (req.block_mem_mb * 1024 * 1024) // max(1, bytes_per_row))))
        n_blocks = int(math.ceil(h / block_rows))
        fit_weight = 0.8
        done_blocks = 0

        self._readers = [imageio.RowReader(p) for p in req.image_paths]
        for r in self._readers:
            if (r.width, r.height) != (w, h):
                raise ValueError(f"image size mismatch: {os.path.basename(r.path)} is "
                                 f"{r.width}x{r.height}, expected {w}x{h}")

        try:
            # pass 1: fit ---------------------------------------------------
            stack = np.empty((n, block_rows, w, 3), dtype=np.float32)
            for y0 in range(0, h, block_rows):
                y1 = min(h, y0 + block_rows)
                rows = y1 - y0
                self.message = f"Fitting rows {y0}-{y1} of {h}"
                blk = stack[:, :rows]
                for i, r in enumerate(self._readers):
                    blk[i] = r.read(y0, y1)
                fg = (blk.max(axis=(0, 3)) > FOREGROUND_LEVEL).reshape(-1)   # (P,) specimen pixels
                specimen_px += int(np.count_nonzero(fg))
                fg3 = np.repeat(fg, 3)
                if req.colorspace == "LINEAR":
                    blk = imageio.srgb_to_linear(blk)
                clipped_px += int(np.count_nonzero(np.any(blk >= 0.999, axis=(0, 3))))
                p = rows * w
                obs = blk.reshape(n, p * 3)          # (N, P*3) values 0..1

                if "PTM_RGB" in req.formats or "PTM_LRGB" in req.formats:
                    P = ops["PTM"]["P"]
                    A = ops["PTM"]["A"]
                if "PTM_RGB" in req.formats:
                    coef = (P @ obs) * 255.0                       # (6, P*3)
                    recon = A @ coef                               # (N, P*3)
                    err = (recon - obs * 255.0) ** 2
                    sse_all["PTM_RGB"] += float(np.sum(err))
                    count_all["PTM_RGB"] += err.size
                    sse["PTM_RGB"] += float(np.sum(err[:, fg3]))
                    count["PTM_RGB"] += n * int(np.count_nonzero(fg3))
                    del err
                    coef = coef.reshape(6, rows, w, 3).transpose(1, 2, 3, 0)
                    store["PTM_RGB"][y0:y1] = coef
                    flat = coef.reshape(-1, 6)
                    cmin["PTM_RGB"] = np.minimum(cmin["PTM_RGB"], flat.min(axis=0))
                    cmax["PTM_RGB"] = np.maximum(cmax["PTM_RGB"], flat.max(axis=0))
                    del coef, recon
                if "PTM_LRGB" in req.formats:
                    st = blk.reshape(n, p, 3)                      # (N, P, 3) 0..1
                    maxc = st.max(axis=2)                          # (N, P)
                    mean_rgb = st.mean(axis=0)                     # (P, 3)
                    mean_max = np.maximum(maxc.mean(axis=0), 1e-6)  # (P,)
                    chroma = np.clip(mean_rgb / mean_max[:, None] * 255.0, 0, 255)  # (P, 3) <= 255
                    cc = np.maximum(np.sum(chroma * chroma, axis=1), 1e-6)          # (P,)
                    lum = np.einsum("npc,pc->np", st, chroma) * (255.0 * 255.0) / cc  # (N, P) projection
                    coef = P @ lum                                                  # (6, P)
                    recon = A @ coef
                    err = (recon - lum) ** 2
                    sse_all["PTM_LRGB"] += float(np.sum(err))
                    count_all["PTM_LRGB"] += err.size
                    sse["PTM_LRGB"] += float(np.sum(err[:, fg]))
                    count["PTM_LRGB"] += n * int(np.count_nonzero(fg))
                    del err
                    coef = coef.T.reshape(rows, w, 6)
                    store["PTM_LRGB"][y0:y1] = coef
                    store["PTM_LRGB_RGB"][y0:y1] = np.rint(chroma).astype(np.uint8).reshape(rows, w, 3)
                    flat = coef.reshape(-1, 6)
                    cmin["PTM_LRGB"] = np.minimum(cmin["PTM_LRGB"], flat.min(axis=0))
                    cmax["PTM_LRGB"] = np.maximum(cmax["PTM_LRGB"], flat.max(axis=0))
                    del st, maxc, mean_rgb, chroma, lum, coef, recon
                for order, terms in HSH_TERMS.items():
                    key = f"HSH{order}"
                    if key not in req.formats:
                        continue
                    P = ops[key]["P"]
                    A = ops[key]["A"]
                    coef = P @ obs                                 # (T, P*3)
                    recon = A @ coef
                    err = (recon - obs) ** 2
                    sse_all[key] += float(np.sum(err)) * 255.0 ** 2
                    count_all[key] += err.size
                    sse[key] += float(np.sum(err[:, fg3])) * 255.0 ** 2
                    count[key] += n * int(np.count_nonzero(fg3))
                    del err
                    coef = coef.reshape(terms, rows, w, 3).transpose(1, 2, 3, 0)
                    store[key][y0:y1] = coef
                    flat = coef.reshape(-1, terms)
                    cmin[key] = np.minimum(cmin[key], flat.min(axis=0))
                    cmax[key] = np.maximum(cmax[key], flat.max(axis=0))
                    del coef, recon

                done_blocks += 1
                self.progress = fit_weight * done_blocks / n_blocks
                yield
            del stack
            for r in self._readers:
                r.close()
            self._readers = []
            for mm in store.values():
                mm.flush()

            # pass 2: quantise + write ------------------------------------
            n_out = len(req.formats)
            for idx, fmt in enumerate(req.formats):
                info = FORMAT_INFO[fmt]
                out_path = os.path.join(req.out_dir, f"{req.base_name}{info['suffix']}{info['ext']}")
                self.message = f"Writing {info['label']}"
                if fmt == "PTM_LRGB":
                    scale, bias = ptm_scale_bias(cmin[fmt], cmax[fmt])
                    gen = iter_write_ptm_lrgb(out_path, w, h,
                                              lambda a, b: store["PTM_LRGB"][a:b],
                                              lambda a, b: store["PTM_LRGB_RGB"][a:b],
                                              scale, bias, block_rows=max(16, block_rows))
                elif fmt == "PTM_RGB":
                    scale, bias = ptm_scale_bias(cmin[fmt], cmax[fmt])
                    gen = iter_write_ptm_rgb(out_path, w, h, lambda a, b: store["PTM_RGB"][a:b],
                                             scale, bias, block_rows=max(16, block_rows))
                else:
                    scale, bias = hsh_scale_bias(cmin[fmt], cmax[fmt])
                    terms = info["terms"]
                    gen = iter_write_hsh(out_path, w, h, terms, lambda a, b, k=fmt: store[k][a:b],
                                         scale, bias, block_rows=max(16, block_rows))
                for frac in gen:
                    self.progress = fit_weight + (1.0 - fit_weight) * (idx + frac) / n_out
                    yield
                self.outputs.append(out_path)
                rmse = math.sqrt(sse[fmt] / max(1, count[fmt]))
                rmse_all = math.sqrt(sse_all[fmt] / max(1, count_all[fmt]))
                op = ops["PTM" if fmt.startswith("PTM") else fmt]
                self.report[fmt] = {
                    "file": os.path.basename(out_path),
                    "bytes": file_size_estimate(w, h, fmt),
                    "scale": [float(v) for v in scale],
                    "bias": [float(v) for v in bias],
                    "coefficient_min": [float(v) for v in cmin[fmt]],
                    "coefficient_max": [float(v) for v in cmax[fmt]],
                    "fit_rmse_8bit": rmse,
                    "fit_psnr_db": 20.0 * math.log10(255.0 / rmse) if rmse > 0 else float("inf"),
                    "fit_psnr_db_all_pixels": (20.0 * math.log10(255.0 / rmse_all) if rmse_all > 0
                                               else float("inf")),
                    "statistics_region": "specimen pixels (brighter than background in any light)",
                    "design_rank": op["rank"],
                    "design_condition_number": op["cond"],
                    "well_conditioned": op["cond"] < 1e4,
                }

            self.report["_summary"] = {
                "width": w, "height": h, "lights": n, "colorspace": req.colorspace,
                "specimen_pixels": specimen_px, "specimen_fraction": specimen_px / float(w * h),
                "clipped_pixels": clipped_px, "clipped_fraction": clipped_px / float(w * h),
                "clipped_fraction_of_specimen": clipped_px / float(max(1, specimen_px)),
                "block_rows": block_rows, "seconds": time.time() - self.started,
            }
            if req.write_report:
                rep_path = os.path.join(req.out_dir, f"{req.base_name}_fit_report.json")
                with open(rep_path, "w", encoding="utf-8") as fh:
                    json.dump({"vrti_fit_report": 1, **req.extra_metadata, "formats": self.report},
                              fh, indent=2)
                self.outputs.append(rep_path)
            self.message = "Done"
        finally:
            self.cleanup()


FOREGROUND_LEVEL = 0.02  # display value above which a pixel belongs to the specimen


def estimate_disk_bytes(width: int, height: int, formats: Sequence[str], lights: int,
                        capture_bytes_per_pixel: float = 2.3) -> dict[str, int]:
    """Rough disk budget: capture TIFFs (default: 16-bit RGB deflate), scratch and outputs."""
    tiff = int(width * height * capture_bytes_per_pixel) * lights
    scratch = 0
    outputs = 0
    for f in formats:
        terms = FORMAT_INFO[f]["terms"]
        if f == "PTM_LRGB":
            scratch += width * height * (6 * 4 + 3)
        else:
            scratch += width * height * 3 * terms * 4
        outputs += file_size_estimate(width, height, f)
    return {"captures": tiff, "scratch": scratch, "outputs": outputs, "total": tiff + scratch + outputs}
