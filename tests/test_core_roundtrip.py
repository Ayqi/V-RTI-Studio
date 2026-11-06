# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip test of the RTI core; needs only Python and NumPy.

    python tests/test_core_roundtrip.py
    blender -b --factory-startup --python-exit-code 1 --python tests/test_core_roundtrip.py

Synthesises a small multi-light image stack from an analytic Lambertian +
Blinn-Phong surface, writes 16-bit TIFFs through OIIO (if available) or raw
uncompressed TIFFs, fits PTM-LRGB / PTM-RGB / HSH 4-9-16, and verifies:

* the files re-read with an independent reader exactly match the header spec
* relighting the fitted model reproduces the training images (PSNR)
* a held-out light direction is predicted sensibly
"""
import math
import os
import shutil
import struct
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from vrti.rti import basis, fit, imageio, lp, writers  # noqa: E402


def dome_dirs(rings=((15, 12), (35, 12), (55, 12), (75, 12))):
    out = []
    for k, (el, cnt) in enumerate(rings):
        for i in range(cnt):
            az = 2 * math.pi * (i + 0.5 * (k % 2)) / cnt
            e = math.radians(el)
            out.append((math.cos(e) * math.cos(az), math.cos(e) * math.sin(az), math.sin(e)))
    return np.array(out)


def synth_surface(w, h, seed=1):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    height = 0.4 * np.sin(xx / 7.0) * np.cos(yy / 9.0) + 0.1 * rng.standard_normal((h, w)).astype(np.float32)
    gy, gx = np.gradient(height)
    n = np.stack([-gx, -gy, np.ones_like(gx)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    albedo = np.stack([0.3 + 0.5 * (xx / w), 0.6 * np.ones_like(xx), 0.2 + 0.6 * (yy / h)], axis=-1)
    return n, albedo.astype(np.float32)


def shade(n, albedo, d, rough=0.25):
    ndl = np.clip(n @ d, 0, 1)
    hv = np.array(d) + np.array([0, 0, 1.0]); hv /= np.linalg.norm(hv)
    spec = np.clip(n @ hv, 0, 1) ** (2.0 / (rough ** 2))
    lin = albedo * ndl[..., None] * 0.8 + 0.25 * spec[..., None]
    return np.clip(imageio.linear_to_srgb(lin), 0, 1)


def write_tiff16(path, rgb):
    """Minimal uncompressed little-endian 16-bit RGB TIFF writer (for the fallback reader)."""
    h, w, _ = rgb.shape
    data = np.clip(np.rint(rgb * 65535.0), 0, 65535).astype("<u2").tobytes()
    tags = []

    def tag(code, typ, count, value):
        tags.append((code, typ, count, value))

    n_tags = 11
    ifd_off = 8
    ifd_size = 2 + n_tags * 12 + 4
    bps_off = ifd_off + ifd_size
    data_off = bps_off + 6
    tag(256, 4, 1, w); tag(257, 4, 1, h); tag(258, 3, 3, bps_off); tag(259, 3, 1, 1)
    tag(262, 3, 1, 2); tag(273, 4, 1, data_off); tag(277, 3, 1, 3); tag(278, 4, 1, h)
    tag(279, 4, 1, len(data)); tag(284, 3, 1, 1); tag(339, 3, 1, 1)
    with open(path, "wb") as fh:
        fh.write(b"II" + struct.pack("<HI", 42, ifd_off))
        fh.write(struct.pack("<H", n_tags))
        for code, typ, count, value in tags:
            if typ == 3 and count == 1:
                fh.write(struct.pack("<HHIHH", code, typ, count, value, 0))
            else:
                fh.write(struct.pack("<HHII", code, typ, count, value))
        fh.write(struct.pack("<I", 0))
        fh.write(struct.pack("<HHH", 16, 16, 16))
        fh.write(data)


def psnr(a, b):
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)


def main():
    w, h = 96, 64
    dirs = dome_dirs()
    n, albedo = synth_surface(w, h)
    tmp = tempfile.mkdtemp(prefix="vrti_core_")
    paths = []
    for i, d in enumerate(dirs):
        img = shade(n, albedo, d)
        p = os.path.join(tmp, f"cap_{i + 1:04d}.tif")
        if imageio.have_oiio():
            imageio.oiio  # noqa: B018
            spec = imageio.oiio.ImageSpec(w, h, 3, imageio.oiio.UINT16)
            out = imageio.oiio.ImageOutput.create(p)
            out.open(p, spec)
            out.write_image(np.clip(np.rint(img * 65535), 0, 65535).astype(np.uint16))
            out.close()
        else:
            write_tiff16(p, img)
        paths.append(p)
    lp.write_lp(os.path.join(tmp, "test.lp"), [os.path.basename(p) for p in paths], dirs)
    names, dirs_back = lp.read_lp(os.path.join(tmp, "test.lp"))
    assert names[0] == "cap_0001.tif" and np.allclose(dirs_back, dirs, atol=1e-5)

    # reader sanity: row order of the capture files
    img0 = imageio.read_image(paths[0])
    assert img0.shape == (h, w, 3)
    assert np.allclose(img0, shade(n, albedo, dirs[0]), atol=1.5 / 255)

    formats = ["PTM_LRGB", "PTM_RGB", "HSH1", "HSH2", "HSH3"]
    job = fit.FitJob(fit.FitRequest(paths, dirs, formats, tmp, "test", block_mem_mb=1))
    job.run()
    print("fit outputs:", [os.path.basename(p) for p in job.outputs])
    for f in formats:
        print(f"  {f:9s} rmse8={job.report[f]['fit_rmse_8bit']:.3f}  cond={job.report[f]['design_condition_number']:.1f}")

    # ---- verify PTM LRGB
    ptm = writers.read_ptm(os.path.join(tmp, "test_lrgb.ptm"))
    assert ptm["width"] == w and ptm["height"] == h
    with open(os.path.join(tmp, "test_lrgb.ptm"), "rb") as fh:
        head = fh.read(40)
    assert head.startswith(b"PTM_1.2\nPTM_FORMAT_LRGB\n96\n64\n"), head
    size = os.path.getsize(os.path.join(tmp, "test_lrgb.ptm"))
    ok_size = size - w * h * 9
    assert 40 < ok_size < 400, ("header size", ok_size)

    train_psnr = np.mean([psnr(writers.relight_ptm(ptm, d), shade(n, albedo, d)) for d in dirs[::7]])
    print(f"PTM LRGB  training PSNR {train_psnr:.1f} dB")
    assert train_psnr > 26, train_psnr

    # ---- verify PTM RGB
    ptm_rgb = writers.read_ptm(os.path.join(tmp, "test_rgb.ptm"))
    assert ptm_rgb["coeffs"].shape == (h, w, 3, 6)
    assert os.path.getsize(os.path.join(tmp, "test_rgb.ptm")) - w * h * 18 < 400
    p2 = np.mean([psnr(writers.relight_ptm(ptm_rgb, d), shade(n, albedo, d)) for d in dirs[::7]])
    print(f"PTM RGB   training PSNR {p2:.1f} dB")
    assert p2 > 26

    # ---- verify HSH
    for order, terms in ((1, 4), (2, 9), (3, 16)):
        path = os.path.join(tmp, f"test_hsh{terms}.rti")
        with open(path, "rb") as fh:
            header = fh.read(64)
        assert header.startswith(f"#HSH1.2\n3\n{w} {h} 3\n{terms} 2 1\n".encode()), header
        hsh = writers.read_hsh(path)
        assert hsh["terms"] == terms and hsh["coeffs"].shape == (h, w, 3, terms)
        assert os.path.getsize(path) == len(f"#HSH1.2\n3\n{w} {h} 3\n{terms} 2 1\n") + 8 * terms + w * h * 3 * terms
        p3 = np.mean([psnr(writers.relight_hsh(hsh, d), shade(n, albedo, d)) for d in dirs[::7]])
        print(f"HSH{terms:<3d}    training PSNR {p3:.1f} dB")
        assert p3 > (24 if order == 1 else 27), p3

    # held-out direction
    d_new = np.array([0.3, -0.2, math.sqrt(1 - 0.13)])
    truth = shade(n, albedo, d_new)
    print(f"held-out PSNR  LRGB {psnr(writers.relight_ptm(ptm, d_new), truth):.1f}  "
          f"HSH9 {psnr(writers.relight_hsh(writers.read_hsh(os.path.join(tmp, 'test_hsh9.rti')), d_new), truth):.1f}")

    # raw byte order, independent of our readers: HSH stores the TOP row first,
    # PTM the BOTTOM row first (checked against real RTIBuilder/relight files)
    coef = np.zeros((2, 3, 3, 4), np.float32)
    coef[0, :, :, 0] = 1.0                       # top row bright, bottom row dark
    raw_path = os.path.join(tmp, "order.rti")
    writers.drain(writers.iter_write_hsh(raw_path, 3, 2, 4, lambda a, b: coef[a:b],
                                         np.array([1, 1, 1, 1], np.float32), np.zeros(4, np.float32)))
    with open(raw_path, "rb") as fh:
        body = fh.read()[-2 * 3 * 3 * 4:]
    assert body[0] == 255 and body[-4] == 0, "HSH must store the top row first"
    lum = np.zeros((2, 3, 6), np.float32)
    lum[0, :, 5] = 200.0                         # top row bright (constant term)
    rgb = np.full((2, 3, 3), 255, np.uint8)
    sc, bi = writers.ptm_scale_bias(lum.reshape(-1, 6).min(0), lum.reshape(-1, 6).max(0))
    ptm_path = os.path.join(tmp, "order.ptm")
    writers.drain(writers.iter_write_ptm_lrgb(ptm_path, 3, 2, lambda a, b: lum[a:b], lambda a, b: rgb[a:b], sc, bi))
    with open(ptm_path, "rb") as fh:
        data = fh.read()
    coeffs = data[len(data) - 2 * 3 * 9:len(data) - 2 * 3 * 3]
    first_a5 = (coeffs[5] - bi[5]) * sc[5]
    assert first_a5 < 50, "PTM must store the bottom row first"

    # orientation check: top-left of relit image equals top-left of the source
    rel = writers.relight_hsh(writers.read_hsh(os.path.join(tmp, "test_hsh9.rti")), dirs[0])
    assert abs(float(rel[0, 0, 1]) - float(shade(n, albedo, dirs[0])[0, 0, 1])) < 0.05
    assert abs(float(rel[-1, -1, 1]) - float(shade(n, albedo, dirs[0])[-1, -1, 1])) < 0.05

    # 8-bit captures: the rounding error must be negligible next to the model error
    if imageio.have_oiio():
        paths8 = []
        for i, d in enumerate(dirs):
            p = os.path.join(tmp, f"cap8_{i + 1:04d}.tif")
            out = imageio.oiio.ImageOutput.create(p)
            out.open(p, imageio.oiio.ImageSpec(w, h, 3, imageio.oiio.UINT8))
            out.write_image(np.clip(np.rint(shade(n, albedo, d) * 255), 0, 255).astype(np.uint8))
            out.close()
            paths8.append(p)
        fit.FitJob(fit.FitRequest(paths8, dirs, ["PTM_LRGB", "HSH2"], tmp, "test8", block_mem_mb=1)).run()
        for name, f16, f8, rel in (("PTM-LRGB", "test_lrgb.ptm", "test8_lrgb.ptm", writers.relight_ptm),
                                   ("HSH9", "test_hsh9.rti", "test8_hsh9.rti", writers.relight_hsh)):
            read = writers.read_ptm if f16.endswith(".ptm") else writers.read_hsh
            a = psnr(rel(read(os.path.join(tmp, f16)), d_new), truth)
            b = psnr(rel(read(os.path.join(tmp, f8)), d_new), truth)
            print(f"held-out {name}: 16-bit captures {a:.2f} dB, 8-bit captures {b:.2f} dB")
            assert abs(a - b) < 0.5, (name, a, b)

    # normals from PTM
    nm = writers.ptm_normals(ptm["coeffs"])
    ang = np.degrees(np.arccos(np.clip(np.sum(nm * n, axis=-1), -1, 1)))
    print(f"PTM normal error: median {np.median(ang):.1f} deg")
    # a 3-ring layout cannot determine HSH order 3 -> clear error instead of garbage
    three = dome_dirs(((20, 12), (45, 12), (70, 12)))
    try:
        job = fit.FitJob(fit.FitRequest(paths[:36], three, ["HSH3"], tmp, "bad", block_mem_mb=1))
        job.run()
        raise AssertionError("expected DesignError")
    except fit.DesignError as exc:
        print("design check OK:", str(exc)[:80], "...")
    # below-horizon light directions are rejected
    try:
        basis.hsh_basis([(0.5, 0.5, -0.5)], 2)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    print("ALL CORE TESTS PASSED")


def test_core_roundtrip():
    main()


if __name__ == "__main__":
    main()
