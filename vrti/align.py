# SPDX-License-Identifier: GPL-3.0-or-later
"""Specimen analysis and automatic alignment to the dome axes.

The dome looks down -Z with the camera at the zenith; image right = +X,
image up = +Y. Four ways to bring a model into that frame:

* ``AUTO`` (default) levels the facing surface and straightens it by its text lines
  or, failing that, by the minimum-area rectangle of its outline.
* ``SOURCE`` keeps the orientation the user set in Metashape: the
  model's +Z faces the camera and +Y is the top of the image (Metashape
  "Top" view). The model is only centred. If its surface is tilted more than
  ``TILT_LIMIT_DEG`` from the camera axis (e.g. a georeferenced standing
  stele) it is levelled as in ``LEVEL``.
* ``LEVEL`` tilts the model with the smallest rotation that makes the
  surface normal face the camera, keeping the source "up" direction
  (source +Y for models lying face-up, source +Z for standing ones). The
  in-plane heading - text lines of an inscription - is not touched.
* ``PCA`` is fully automatic: smallest-variance axis -> Z, largest -> X.
  The in-plane angle then depends on the outline of the stone.

In every mode the surface normal is the smallest-variance PCA axis, signed
so that the mean vertex normal points to the camera; the imaged surface
(90th height percentile) is moved to Z = 0 and the footprint is centred.
"""
from __future__ import annotations

import math

import bpy
import numpy as np
from mathutils import Matrix, Vector

MAX_SAMPLES = 400_000
TILT_LIMIT_DEG = 30.0
VISIBLE_NZ = 0.2          # surface normals steeper than ~78° from the camera axis are not imaged
SOURCE_MATRIX_KEY = "vrti_source_matrix"
SPIN_KEY = "vrti_spin_deg"
FINE_KEY = "vrti_spin_fine"


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        n = len(me.vertices)
        if n == 0:
            return np.zeros((0, 3), np.float64), np.zeros((0, 3), np.float64)
        co = np.empty(n * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3).astype(np.float64)
        normals = np.empty(n * 3, dtype=np.float32)
        try:
            me.vertex_normals.foreach_get("vector", normals)
            normals = normals.reshape(-1, 3).astype(np.float64)
        except Exception:  # noqa: BLE001
            normals = np.zeros_like(co)
    finally:
        ev.to_mesh_clear()
    mw = np.array(obj.matrix_world, dtype=np.float64)
    co_w = co @ mw[:3, :3].T + mw[:3, 3]
    nrm_w = normals @ np.linalg.inv(mw[:3, :3])
    return co_w, nrm_w


def _subsample(a: np.ndarray, b: np.ndarray):
    n = a.shape[0]
    if n <= MAX_SAMPLES:
        return a, b
    idx = np.random.default_rng(0).choice(n, MAX_SAMPLES, replace=False)
    return a[idx], b[idx]


def surface_normal(co: np.ndarray, normals: np.ndarray) -> np.ndarray:
    """Unit normal of the best-fit plane, pointing to the textured side."""
    x = co - co.mean(axis=0)
    _evals, evecs = np.linalg.eigh(x.T @ x / max(1, len(x)))  # ascending
    n = evecs[:, 0]
    mean_n = normals.mean(axis=0)
    if np.linalg.norm(mean_n) > 1e-9:
        if float(mean_n @ n) < 0.0:
            n = -n
    elif n[2] < 0:
        n = -n
    return n / np.linalg.norm(n)


def pca_frame(co: np.ndarray, normals: np.ndarray) -> np.ndarray:
    """Rotation matrix R (3x3, rows = new X, Y, Z axes in world coordinates)."""
    c = co.mean(axis=0)
    x = co - c
    cov = x.T @ x / max(1, len(x))
    _evals, evecs = np.linalg.eigh(cov)  # ascending
    z_axis = surface_normal(co, normals)
    x_axis = evecs[:, 2]
    # keep the long side roughly aligned with the original dominant direction sign
    if x_axis[np.argmax(np.abs(x_axis))] < 0:
        x_axis = -x_axis
    y_axis = np.cross(z_axis, x_axis)
    y_axis /= np.linalg.norm(y_axis)
    x_axis = np.cross(y_axis, z_axis)
    x_axis /= np.linalg.norm(x_axis)
    return np.stack([x_axis, y_axis, z_axis], axis=0)


def up_hint(n: np.ndarray) -> np.ndarray:
    """Source direction that should become the top of the image.

    A model lying face-up (Metashape top view) keeps its +Y; a standing one
    (surface normal closer to horizontal) keeps its +Z, like a frontal photo.
    """
    return np.array([0.0, 1.0, 0.0]) if abs(float(n[2])) >= 0.5 else np.array([0.0, 0.0, 1.0])


def level_frame(n: np.ndarray, up: np.ndarray) -> np.ndarray:
    """Rotation (rows X, Y, Z) with Z = n and Y = ``up`` projected on the surface plane."""
    z = n / np.linalg.norm(n)
    y = up - float(up @ z) * z
    if np.linalg.norm(y) < 1e-6:
        alt = np.array([0.0, 0.0, 1.0]) if abs(up[2]) < 0.5 else np.array([0.0, 1.0, 0.0])
        y = alt - float(alt @ z) * z
    y /= np.linalg.norm(y)
    x = np.cross(y, z)
    return np.stack([x, y, z], axis=0)


# --------------------------------------------------------------------------- #
# automatic straightening (AUTO mode)
# --------------------------------------------------------------------------- #
AUTO_KEY = "vrti_auto_angle"
TEXT_SEARCH_DEG = 20.0      # inscriptions are rarely turned more than this in a chunk
TEXT_MIN_CONTRAST = 2.0     # row-profile sharpness gain of real text fields: 4-6, weathered/blank: < 0.7
TEXT_WINDOW_DEG = 15.0      # largest turn trusted to the text cue
RECT_MIN = 0.85             # outline area / minimum rectangle area of a cut block


def facing_normal(nrm: np.ndarray, n0: np.ndarray) -> np.ndarray:
    """Mean direction of the imaged surface (vertex normals within 60° of ``n0``).

    Unlike the PCA plane this ignores broken sides and the thickness of the stone, so
    the camera ends up square to the inscribed face itself.
    """
    length = np.linalg.norm(nrm, axis=1)
    ok = length > 1e-9
    u = nrm[ok] / length[ok, None]
    face = u[u @ n0 > 0.5]
    if len(face) < 100:
        return n0
    m = face.mean(axis=0)
    return m / np.linalg.norm(m)


def _box_blur(img: np.ndarray, r: int) -> np.ndarray:
    """Box filter of radius ``r`` via an integral image (no SciPy in Blender)."""
    h, w = img.shape
    ii = np.zeros((h + 1, w + 1), np.float64)
    ii[1:, 1:] = img.cumsum(0).cumsum(1)
    y0 = np.clip(np.arange(h) - r, 0, h)
    y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w)
    x1 = np.clip(np.arange(w) + r + 1, 0, w)
    return (ii[y1][:, x1] - ii[y0][:, x1] - ii[y1][:, x0] + ii[y0][:, x0])


def _relief_energy(P: np.ndarray, z: np.ndarray, cells_long: int = 400):
    """Per-point high-pass relief energy and an interior mask (outline excluded).

    The grid is coarse enough to hold ~6 points per cell (a sparse mesh would leave
    holes that break the interior mask and alias with the profile bins).
    """
    lo = P.min(axis=0)
    span_x, span_y = float(max(np.ptp(P[:, 0]), 1e-9)), float(max(np.ptp(P[:, 1]), 1e-9))
    span = max(span_x, span_y)
    cell_density = math.sqrt(span_x * span_y * 6.0 / max(len(P), 1))
    cells_long = int(max(80, min(cells_long, span / cell_density)))
    cell = span / cells_long
    ix = np.minimum(((P[:, 0] - lo[0]) / cell).astype(np.int64), cells_long)
    iy = np.minimum(((P[:, 1] - lo[1]) / cell).astype(np.int64), cells_long)
    w, h = int(ix.max()) + 1, int(iy.max()) + 1
    idx = iy * w + ix
    cnt = np.bincount(idx, minlength=w * h).reshape(h, w).astype(np.float64)
    tot = np.bincount(idx, weights=z, minlength=w * h).reshape(h, w)
    r = max(2, cells_long // 40)                     # ~2.5 % of the long side: letters pass
    bc = _box_blur((cnt > 0).astype(np.float64), r)
    bz = _box_blur(np.where(cnt > 0, tot / np.maximum(cnt, 1), 0.0), r)
    smooth = bz / np.maximum(bc, 1e-9)
    full = (2 * r + 1) ** 2
    interior = (bc >= 0.97 * full)[iy, ix]          # far enough from the broken outline
    hp = z - smooth[iy, ix]
    return hp * hp, interior, cell


def _profile_score(P: np.ndarray, e: np.ndarray, angle_deg: float, bin_size: float) -> float:
    """Sharpness of the row profile of relief energy for lines at ``angle_deg``.

    Bins are anchored at a fixed origin and averaged over three phase offsets, so the
    score does not jitter as the bin grid slides across the text lines with the angle.
    """
    a = math.radians(angle_deg)
    y = math.sin(a) * P[:, 0] + math.cos(a) * P[:, 1]
    out = 0.0
    for phase in (0.0, 1.0 / 3.0, 2.0 / 3.0):
        b = np.floor(y / bin_size + phase).astype(np.int64)
        b -= b.min()
        s = np.bincount(b, weights=e)
        c = np.bincount(b)
        nz = c > 0
        ok = c >= max(5, int(np.median(c[nz]) * 0.3)) if np.any(nz) else nz
        rows = s / np.maximum(c, 1)
        idx = np.nonzero(ok)[0]
        pair = idx[:-1][np.diff(idx) == 1]           # neighbouring well-filled rows
        if len(pair) < 3:
            continue
        diff = rows[pair + 1] - rows[pair]
        # Postl's criterion: squared differences between neighbouring rows peak sharply
        # when the edges of the text bands are horizontal (variance alone has a broad top)
        out += float(np.mean(diff * diff) / max(np.mean(rows[ok]) ** 2, 1e-30))
    return out / 3.0


def text_line_angle(P: np.ndarray, z: np.ndarray, centre: float = 0.0, half: float = TEXT_SEARCH_DEG):
    """In-plane turn (degrees, counter-clockwise) that makes relief lines horizontal.

    Projection-profile deskew, as used for scanned documents: incised letters raise the
    high-pass relief energy row by row, and the rows separate best when the text lines
    are horizontal. The best angle is searched within ``centre ± half``; the contrast is
    measured against the median over ±TEXT_SEARCH_DEG, so a peak that is no peak (an
    edge of the window, cracks, weathering) scores low. Returns (angle, contrast).
    """
    e, interior, cell = _relief_energy(P, z)
    if interior.sum() < 2000:
        return 0.0, 0.0
    Pi, ei = P[interior], e[interior]
    bin_size = cell * 2.0
    wide = np.arange(-TEXT_SEARCH_DEG, TEXT_SEARCH_DEG + 1e-6, 1.0)
    base = float(np.median([_profile_score(Pi, ei, a, bin_size) for a in wide]))
    window = np.arange(centre - half, centre + half + 1e-6, 0.25)
    scores = np.array([_profile_score(Pi, ei, a, bin_size) for a in window])
    i = int(np.argmax(scores))
    if i in (0, len(window) - 1):          # rising towards the window edge: not a real peak
        return float(window[i]), 0.0
    fine = np.arange(window[i] - 0.25, window[i] + 0.2501, 0.05)
    fscores = np.array([_profile_score(Pi, ei, a, bin_size) for a in fine])
    angle = float(fine[int(np.argmax(fscores))])
    contrast = (float(fscores.max()) - base) / max(base, 1e-30)
    return angle, contrast


def _convex_hull(P: np.ndarray) -> np.ndarray:
    pts = np.unique(np.round(P, 9), axis=0)
    if len(pts) < 3:
        return pts
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

    def half(points):
        out = []
        for p in points:
            while len(out) >= 2 and np.cross(out[-1] - out[-2], p - out[-2]) <= 0:
                out.pop()
            out.append(p)
        return out
    lower, upper = half(pts), half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def outline_angle(P: np.ndarray):
    """Turn (degrees, within ±45) that aligns the minimum-area rectangle of the outline
    with the axes, and how rectangular the outline is (hull area / rectangle area)."""
    if len(P) > 60000:
        P = P[np.random.default_rng(1).choice(len(P), 60000, replace=False)]
    hull = _convex_hull(P)
    if len(hull) < 3:
        return 0.0, 0.0
    edges = np.diff(np.vstack([hull, hull[:1]]), axis=0)
    angles = np.unique(np.mod(np.arctan2(edges[:, 1], edges[:, 0]), math.pi / 2))
    best, best_area = 0.0, float("inf")
    for a in angles:
        c, s = math.cos(-a), math.sin(-a)
        x = c * hull[:, 0] - s * hull[:, 1]
        y = s * hull[:, 0] + c * hull[:, 1]
        area = float(np.ptp(x) * np.ptp(y))
        if area < best_area:
            best, best_area = float(a), area
    turn = -math.degrees(best)
    turn = (turn + 45.0) % 90.0 - 45.0
    x, y = hull[:, 0], hull[:, 1]
    hull_area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    return turn, hull_area / max(best_area, 1e-30)


def straighten_angle(co_levelled: np.ndarray):
    """(angle_deg, method, score) that straightens a levelled specimen in the image plane.

    Calibrated on ten real stone inscriptions:
    1. text lines / panel mouldings (sharp row profile of the relief, contrast >= 2):
       the most faithful cue - it follows the inscribed field even when the block edges
       are broken;
    2. otherwise the minimum-area rectangle of the outline of a cut block (outline area
       >= 85 % of the rectangle): weathered texts with straight edges;
    3. otherwise nothing is turned.
    """
    top = co_levelled[:, 2] >= np.percentile(co_levelled[:, 2], 5.0)   # drop the underside
    P, z = co_levelled[top, :2], co_levelled[top, 2]
    angle, contrast = text_line_angle(P, z, centre=0.0, half=TEXT_WINDOW_DEG)
    if contrast >= TEXT_MIN_CONTRAST:
        return angle, "text", contrast
    turn, rect = outline_angle(P)
    if rect >= RECT_MIN:
        return turn, "outline", rect
    return 0.0, "none", max(contrast, 0.0)


def tilt_deg(n: np.ndarray) -> float:
    """Angle between a surface normal and the camera axis (+Z), degrees."""
    return math.degrees(math.acos(max(-1.0, min(1.0, float(n[2]) / float(np.linalg.norm(n))))))


def remember_source(obj: bpy.types.Object) -> None:
    """Store the imported pose so every re-alignment starts from the source."""
    obj[SOURCE_MATRIX_KEY] = [float(v) for row in obj.matrix_world for v in row]


def restore_source(obj: bpy.types.Object) -> None:
    vals = obj.get(SOURCE_MATRIX_KEY)
    if vals is not None and len(vals) == 16:
        obj.matrix_world = Matrix([list(vals[i:i + 4]) for i in range(0, 16, 4)])


def _apply(obj: bpy.types.Object, R: np.ndarray, co: np.ndarray) -> dict:
    rot = Matrix(((R[0, 0], R[0, 1], R[0, 2], 0.0),
                  (R[1, 0], R[1, 1], R[1, 2], 0.0),
                  (R[2, 0], R[2, 1], R[2, 2], 0.0),
                  (0.0, 0.0, 0.0, 1.0)))
    # new world coords of all vertices
    co_new = co @ R.T
    bb_min, bb_max = co_new.min(axis=0), co_new.max(axis=0)
    centre_xy = (bb_min[:2] + bb_max[:2]) * 0.5
    # the imaged surface (upper part of the point cloud) should sit at z = 0
    z_surface = float(np.percentile(co_new[:, 2], 90.0))
    offset = Vector((-centre_xy[0], -centre_xy[1], -z_surface))
    obj.matrix_world = Matrix.Translation(offset) @ rot @ obj.matrix_world
    size = bb_max - bb_min
    return {"size": (float(size[0]), float(size[1]), float(size[2])),
            "surface_z_span": float(bb_max[2] - z_surface), "vertices": int(co.shape[0])}


def align_object(obj: bpy.types.Object, mode: str = "SOURCE") -> dict:
    """Rotate / translate ``obj`` so it lies centred in the dome. Returns stats.

    ``mode`` is ``AUTO``, ``SOURCE``, ``LEVEL`` or ``PCA`` (see the module docstring).
    Starts from the pose stored by :func:`remember_source` when available.
    """
    if obj.get(SOURCE_MATRIX_KEY) is None:
        remember_source(obj)
    restore_source(obj)
    co, nrm = _world_vertices(obj)
    if co.shape[0] < 3:
        raise ValueError("object has no vertices")
    co_s, nrm_s = _subsample(co, nrm)
    n = surface_normal(co_s, nrm_s)
    source_tilt = tilt_deg(n)
    used = mode
    auto = {}
    if mode == "AUTO":
        n = facing_normal(nrm_s, n)
        R = level_frame(n, up_hint(n))
        cached = obj.get(AUTO_KEY)
        if cached is not None and len(cached) == 3:
            angle, method_code, score = float(cached[0]), int(cached[1]), float(cached[2])
            method = ("none", "text", "outline")[method_code]
        else:
            angle, method, score = straighten_angle(co_s @ R.T)
            obj[AUTO_KEY] = [angle, ("none", "text", "outline").index(method), score]
        c, s_ = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        R = np.array([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]]) @ R
        auto = {"auto_angle_deg": angle, "auto_method": method, "auto_score": score}
    elif mode == "PCA":
        R = pca_frame(co_s, nrm_s)
    elif mode == "LEVEL" or source_tilt > TILT_LIMIT_DEG:
        R = level_frame(n, up_hint(n))
        used = "LEVEL"
    else:
        R = np.eye(3)
    spin = math.radians(float(obj.get(SPIN_KEY, 0.0)) + float(obj.get(FINE_KEY, 0.0)))
    if spin:
        c, s_ = math.cos(spin), math.sin(spin)
        R = np.array([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]]) @ R
    stats = _apply(obj, R, co)
    stats.update(mode=used, requested_mode=mode, source_tilt_deg=source_tilt,
                 tilt_deg=tilt_deg(R @ n),
                 spin_deg=float(obj.get(SPIN_KEY, 0.0)) + float(obj.get(FINE_KEY, 0.0)), **auto)
    return stats


def pose_report(obj: bpy.types.Object) -> dict:
    """The specimen's pose as it is now (also after manual edits), for the capture metadata."""
    co, nrm = _world_vertices(obj)
    co_s, nrm_s = _subsample(co, nrm)
    n = surface_normal(co_s, nrm_s)
    mw = obj.matrix_world
    out = {"surface_tilt_deg": round(tilt_deg(n), 3),
           "matrix_world": [[round(float(v), 9) for v in row] for row in mw]}
    vals = obj.get(SOURCE_MATRIX_KEY)
    if vals is not None and len(vals) == 16:
        src = Matrix([list(vals[i:i + 4]) for i in range(0, 16, 4)])
        rel = mw.to_3x3() @ src.to_3x3().inverted()
        up = rel @ Vector((0.0, 1.0, 0.0))
        out["rotation_from_source_deg"] = round(math.degrees(rel.to_quaternion().angle), 3)
        out["image_up_turn_deg"] = round(math.degrees(math.atan2(-up.x, up.y)), 3)
    return out


def rotate_about_z(obj: bpy.types.Object, degrees: float, mode: str) -> dict:
    """Spin the specimen in the image plane (about the dome axis), then re-align.

    The spin is stored on the object and re-applied on every alignment, so it
    survives switching between orientation modes.
    """
    obj[SPIN_KEY] = (float(obj.get(SPIN_KEY, 0.0)) + degrees) % 360.0
    return align_object(obj, mode)


def _uv_coverage(me) -> float:
    """Summed UV area of all triangles (1.0 = one full texture tile); None if unknown."""
    uv_layer = me.uv_layers.active
    n_poly, n_loop = len(me.polygons), len(me.loops)
    if uv_layer is None or n_poly == 0 or n_loop != 3 * n_poly or n_poly > 40_000_000:
        return None   # only for triangle meshes (photogrammetry) of a sane size
    uv = np.empty(n_loop * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv)
    t = uv.reshape(-1, 3, 2).astype(np.float64)
    a = t[:, 1] - t[:, 0]
    b = t[:, 2] - t[:, 0]
    return float(0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum())


def analyze_object(obj: bpy.types.Object) -> dict:
    """Face count, texture texel count and world bounding box."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        faces = len(me.polygons)
        uv_used = _uv_coverage(me)
    finally:
        ev.to_mesh_clear()
    images = set()
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                images.add(node.image)
    texels = 0.0
    for img in images:
        w, h = img.size[0], img.size[1]
        texels += float(w * h)
    # only the part of the texture atlas covered by UV islands carries detail
    atlas_texels = texels
    if images and uv_used is not None:
        texels *= min(1.0, uv_used / len(images))
    co, nrm = _world_vertices(obj)
    if co.shape[0]:
        mins = Vector(tuple(co.min(axis=0)))
        maxs = Vector(tuple(co.max(axis=0)))
        # share of the surface the zenith camera sees (vertex density of a photogrammetric
        # mesh is close to uniform, so the vertex share approximates the area share)
        length = np.linalg.norm(nrm, axis=1)
        nz = np.divide(nrm[:, 2], length, out=np.zeros_like(length), where=length > 1e-12)
        visible = float(np.mean(nz > VISIBLE_NZ)) if len(nz) else 1.0
    else:
        mins = maxs = Vector((0.0, 0.0, 0.0))
        visible = 1.0
    return {"faces": faces, "texels": texels, "atlas_texels": atlas_texels, "images": len(images),
            "uv_coverage": uv_used, "visible_fraction": visible,
            "size": tuple(maxs - mins), "min": tuple(mins), "max": tuple(maxs)}


def footprint_fill_ratio(size_x: float, size_y: float, frame_w: int, frame_h: int, margin: float) -> float:
    """Fraction of the frame covered by the specimen footprint after framing."""
    if size_x <= 0 or size_y <= 0 or frame_w <= 0 or frame_h <= 0:
        return 1.0
    usable = max(1e-6, 1.0 - 2.0 * margin)
    frame_aspect = frame_w / frame_h
    obj_aspect = size_x / size_y
    if obj_aspect >= frame_aspect:   # width limited
        fill_w = usable
        fill_h = usable * frame_aspect / obj_aspect
    else:
        fill_h = usable
        fill_w = usable * obj_aspect / frame_aspect
    return float(min(1.0, fill_w * fill_h))


def nice_length(v: float) -> str:
    if v >= 1.0:
        return f"{v:.2f} m"
    if v >= 0.01:
        return f"{v * 100:.1f} cm"
    return f"{v * 1000:.1f} mm"


__all__ = ["align_object", "analyze_object", "footprint_fill_ratio", "nice_length",
           "remember_source", "restore_source", "rotate_about_z", "surface_normal", "tilt_deg"]
