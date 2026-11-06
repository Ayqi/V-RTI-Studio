# SPDX-License-Identifier: GPL-3.0-or-later
"""Virtual dome, zenith camera and the animated capture light.

Scene layout created by :func:`build_dome`::

    V-RTI Rig            (rendered)   VRTI_Camera, VRTI_Light
    V-RTI Dome (visual)  (viewport)   VRTI_DomeRoot, VRTI_DomeFrame,
                                      VRTI_Platform, VRTI_Scale, VRTI_Label_*,
                                      VRTI_LED_###, VRTI_ActiveLight, VRTI_Beam (light
                                      cone), VRTI_Gantry, VRTI_Camera* (camera model)
    V-RTI Specimen                    the imported model

Each animation frame ``f`` (1..N) corresponds to light position ``f``: the
light's orientation (SUN) or location (POINT) is keyframed with constant
interpolation, so a normal animation render produces the capture sequence and
the .blend stays self-contained (render farms, command line renders).
"""
from __future__ import annotations

import math
import os

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector

from . import presets
from .properties import capture_resolution

COLL_RIG = "V-RTI Rig"
COLL_VISUAL = "V-RTI Dome (visual)"
COLL_SPECIMEN = "V-RTI Specimen"

RIG_PREFIX = "VRTI_"     # object names the rig reserves; a specimen never uses it
CAM_NAME = "VRTI_Camera"
LIGHT_NAME = "VRTI_Light"
ROOT_NAME = "VRTI_DomeRoot"
BASE_NAME = "VRTI_BaseRing"
FRAME_NAME = "VRTI_DomeFrame"
PLATFORM_NAME = "VRTI_Platform"
SCALE_NAME = "VRTI_Scale"
LABEL_PREFIX = "VRTI_Label_"
GANTRY_NAME = "VRTI_Gantry"
LED_PREFIX = "VRTI_LED_"
ACTIVE_NAME = "VRTI_ActiveLight"
BEAM_NAME = "VRTI_Beam"

FOCAL_MIN_MM = 50.0
FOCAL_MAX_MM = 200.0
CAMERA_APERTURE_ELEVATION = 80.0  # the frame stays open above this elevation (camera hole)
CAMBODY_NAME = "VRTI_CameraBody"
LENS_NAME = "VRTI_CameraLens"

# viewport palette (Solid mode uses these colours; Material Preview the shaders)
COL_FRAME = (0.20, 0.22, 0.25)      # anodised graphite
COL_ACCENT = (0.10, 0.65, 1.00)     # cyan accent: base and aperture rings
COL_PLATFORM = (0.030, 0.034, 0.040)
COL_MARK = (0.78, 0.82, 0.88)
COL_ACTIVE = (1.00, 0.86, 0.35)
LED_LOW = (1.00, 0.50, 0.10)        # grazing rings: warm amber
LED_MID = (1.00, 0.92, 0.75)
LED_HIGH = (0.35, 0.80, 1.00)       # high rings: ice blue


# --------------------------------------------------------------------------- #
# light layout
# --------------------------------------------------------------------------- #
def rings_for(settings) -> list[tuple[float, int]]:
    if settings.dome_preset == "CUSTOM":
        return presets.custom_rings(settings.custom_rings, settings.custom_first_ring,
                                    settings.custom_el_min, settings.custom_el_max)
    return list(presets.DOME_PRESETS[settings.dome_preset]["rings"])


def light_directions(settings) -> list[tuple[float, float, float]]:
    return presets.ring_directions(rings_for(settings), settings.stagger, settings.include_zenith)


def dome_radius(settings) -> float:
    if settings.dome_radius_auto:
        ext = max(settings.info_size_x, settings.info_size_y, 1e-3)
        return max(0.05, 1.6 * ext)
    return float(settings.dome_radius)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _collection(name: str, parent: bpy.types.Collection = None) -> bpy.types.Collection:
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    parent = parent or bpy.context.scene.collection
    if coll.name not in parent.children and coll.name not in [c.name for c in parent.children_recursive]:
        parent.children.link(coll)
    return coll


def _remove_object(obj: bpy.types.Object) -> None:
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        if isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)
        elif isinstance(data, bpy.types.Camera):
            bpy.data.cameras.remove(data)
        elif isinstance(data, bpy.types.Light):
            bpy.data.lights.remove(data)
        elif isinstance(data, bpy.types.Curve):
            bpy.data.curves.remove(data)


def rig_objects() -> list[bpy.types.Object]:
    colls = (bpy.data.collections.get(COLL_RIG), bpy.data.collections.get(COLL_VISUAL))
    return [obj for coll in colls if coll is not None for obj in coll.all_objects]


def clear_rig() -> None:
    for obj in rig_objects():
        _remove_object(obj)


def _emission_material(name: str, color, strength: float, alpha: float = 1.0) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (*color, 1.0)
    em.inputs["Strength"].default_value = strength
    if alpha < 1.0:
        mix = nt.nodes.new("ShaderNodeMixShader")
        tr = nt.nodes.new("ShaderNodeBsdfTransparent")
        mix.inputs["Fac"].default_value = alpha
        nt.links.new(tr.outputs[0], mix.inputs[1])
        nt.links.new(em.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], out.inputs[0])
        mat.surface_render_method = "BLENDED"
        mat.show_transparent_back = False
    else:
        nt.links.new(em.outputs[0], out.inputs[0])
    # Solid-mode viewport colour: keep it opaque so the dome stays readable
    mat.diffuse_color = (*color, 1.0)
    mat.roughness = 0.6
    return mat


def _surface_material(name: str, color, metallic: float = 0.0, roughness: float = 0.5,
                      emission: float = 0.0) -> bpy.types.Material:
    """Principled material for the viewport model (Solid colour = ``color``)."""
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    if emission > 0.0:
        for key in ("Emission Color", "Emission"):
            if key in bsdf.inputs:
                bsdf.inputs[key].default_value = (*color, 1.0)
                break
        bsdf.inputs["Emission Strength"].default_value = emission
    nt.links.new(bsdf.outputs[0], out.inputs[0])
    mat.diffuse_color = (*color, 1.0)
    mat.metallic = metallic
    mat.roughness = roughness
    return mat


def _lerp(a, b, t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def ring_colour(k: int, n: int):
    """LED colour of ring ``k`` of ``n``: amber (grazing) -> warm white -> ice blue (high)."""
    t = 0.0 if n <= 1 else k / (n - 1)
    return _lerp(LED_LOW, LED_MID, t * 2.0) if t < 0.5 else _lerp(LED_MID, LED_HIGH, (t - 0.5) * 2.0)


def _tube(bm: bmesh.types.BMesh, pts, r: float, seg: int = 8, closed: bool = False) -> None:
    """Add a round tube along a polyline (parallel-transport frames, capped ends)."""
    pts = [Vector(p) for p in pts]
    n = len(pts)
    tangents = []
    for i in range(n):
        if closed:
            t = pts[(i + 1) % n] - pts[i - 1]
        else:
            t = pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]
        tangents.append(t.normalized())
    ref = Vector((0.0, 0.0, 1.0)) if abs(tangents[0].z) < 0.9 else Vector((1.0, 0.0, 0.0))
    nrm = tangents[0].cross(ref).normalized()
    rings = []
    for i in range(n):
        if i:
            axis = tangents[i - 1].cross(tangents[i])
            if axis.length > 1e-9:
                nrm = Matrix.Rotation(tangents[i - 1].angle(tangents[i]), 3, axis.normalized()) @ nrm
        bin_ = tangents[i].cross(nrm)
        rings.append([bm.verts.new(pts[i] + r * (math.cos(a) * nrm + math.sin(a) * bin_))
                      for a in (2.0 * math.pi * j / seg for j in range(seg))])
    for i in range(n if closed else n - 1):
        a, b = rings[i], rings[(i + 1) % n]
        for j in range(seg):
            bm.faces.new((a[j], a[(j + 1) % seg], b[(j + 1) % seg], b[j]))
    if not closed:
        bm.faces.new(list(reversed(rings[0])))
        bm.faces.new(rings[-1])


def _circle(radius: float, z: float, n: int = 96):
    return [(radius * math.cos(2 * math.pi * i / n), radius * math.sin(2 * math.pi * i / n), z) for i in range(n)]


def _add_box(bm: bmesh.types.BMesh, centre, size, rot_z: float = 0.0) -> None:
    geom = bmesh.ops.create_cube(bm, size=1.0)
    verts = geom["verts"]
    bmesh.ops.scale(bm, vec=size, verts=verts)
    if rot_z:
        bmesh.ops.rotate(bm, verts=verts, cent=(0, 0, 0), matrix=Matrix.Rotation(rot_z, 3, "Z"))
    bmesh.ops.translate(bm, vec=centre, verts=verts)


def _add_disc(bm: bmesh.types.BMesh, radius: float, z0: float, z1: float, seg: int = 64) -> None:
    """Closed cylinder (disc) between heights z0 and z1."""
    bottom = [bm.verts.new((radius * math.cos(2 * math.pi * i / seg),
                            radius * math.sin(2 * math.pi * i / seg), z0)) for i in range(seg)]
    top = [bm.verts.new((v.co.x, v.co.y, z1)) for v in bottom]
    for i in range(seg):
        bm.faces.new((bottom[i], bottom[(i + 1) % seg], top[(i + 1) % seg], top[i]))
    bm.faces.new(list(reversed(bottom)))
    bm.faces.new(top)


def _label(name: str, text: str, loc, size: float, coll, mat, rot_z: float = 0.0) -> bpy.types.Object:
    cu = bpy.data.curves.new(name, "FONT")
    cu.body = text
    cu.size = size
    cu.align_x = "CENTER"
    cu.align_y = "CENTER"
    cu.materials.append(mat)
    ob = bpy.data.objects.new(name, cu)
    ob.location = loc
    ob.rotation_euler = (0.0, 0.0, rot_z)
    ob.color = mat.diffuse_color
    coll.objects.link(ob)
    ob.hide_render = True
    ob.hide_select = True
    return ob


def aperture_elevation(settings) -> float:
    """Elevation of the camera opening: just above the highest LED ring."""
    top = max((el for el, _c in rings_for(settings)), default=70.0)
    return min(86.0, max(CAMERA_APERTURE_ELEVATION, top + 5.0))


def _build_gantry(settings, R: float, cam_z: float, coll) -> None:
    """Bridge over the camera opening that carries the camera at height ``cam_z``."""
    old = bpy.data.objects.get(GANTRY_NAME)
    if old is not None:
        _remove_object(old)
    if coll is None:
        return
    el = math.radians(aperture_elevation(settings))
    ra, za = R * math.cos(el), R * math.sin(el)
    top = max(za, cam_z + R * 0.12)
    r = R * 0.0075
    bm = bmesh.new()
    for sx in (-1.0, 1.0):  # two posts standing on the aperture ring
        _tube(bm, [(sx * ra, 0.0, za), (sx * ra, 0.0, top)], r, seg=10)
    _tube(bm, [(-ra, 0.0, top), (ra, 0.0, top)], r * 1.2, seg=10)
    gantry = _mesh_object(GANTRY_NAME, bm, coll,
                          _surface_material("VRTI_FrameMat", COL_FRAME, metallic=0.85, roughness=0.32))
    gantry.hide_select = True


def _mesh_object(name: str, bm: bmesh.types.BMesh, coll: bpy.types.Collection,
                 mat: bpy.types.Material = None) -> bpy.types.Object:
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(name, me)
    coll.objects.link(obj)
    obj.hide_render = True
    obj.hide_select = False
    if mat is not None:
        me.materials.append(mat)
        obj.color = mat.diffuse_color
    return obj


# --------------------------------------------------------------------------- #
# camera framing
# --------------------------------------------------------------------------- #
def compute_framing(settings, R: float) -> dict:
    ex = max(settings.info_size_x, 1e-4)
    ey = max(settings.info_size_y, 1e-4)
    w, h = capture_resolution(settings)
    long_side, short_side = max(w, h), min(w, h)
    a = long_side / short_side
    usable = max(0.05, 1.0 - 2.0 * settings.framing_margin)
    landscape = w >= h
    need = max(ex, ey * a) if landscape else max(ey, ex * a)  # extent along the long side (equiv.)
    out = {"width": w, "height": h, "landscape": landscape}
    if settings.camera_projection == "ORTHO":
        out.update(projection="ORTHO", ortho_scale=need / usable, distance=R, lens=None)
        return out
    distance = R
    lens = presets.SENSOR_WIDTH_MM * distance * usable / need
    if lens < FOCAL_MIN_MM:
        lens = FOCAL_MIN_MM
        distance = lens * need / (presets.SENSOR_WIDTH_MM * usable)
    elif lens > FOCAL_MAX_MM:
        lens = FOCAL_MAX_MM
        distance = lens * need / (presets.SENSOR_WIDTH_MM * usable)
    out.update(projection="PERSP", lens=lens, distance=distance, ortho_scale=None)
    return out


def frame_camera(settings) -> dict:
    cam_obj = bpy.data.objects.get(CAM_NAME)
    if cam_obj is None:
        raise RuntimeError("dome not built yet")
    R = dome_radius(settings)
    fr = compute_framing(settings, R)
    cam = cam_obj.data
    cam.sensor_fit = "AUTO"
    cam.sensor_width = presets.SENSOR_WIDTH_MM
    cam.sensor_height = presets.SENSOR_HEIGHT_MM
    cam.dof.use_dof = False
    if fr["projection"] == "ORTHO":
        cam.type = "ORTHO"
        cam.ortho_scale = fr["ortho_scale"]
    else:
        cam.type = "PERSP"
        cam.lens = fr["lens"]
    cam_obj.location = (0.0, 0.0, fr["distance"])
    cam_obj.rotation_euler = (0.0, 0.0, 0.0)  # looks down -Z, +Y is image up
    _build_gantry(settings, R, fr["distance"] + R * 0.11 * (0.9 + 0.35),
                  bpy.data.collections.get(COLL_VISUAL))
    cam.clip_start = max(1e-4, fr["distance"] * 0.01)
    cam.clip_end = fr["distance"] * 20.0 + 10.0
    cam.display_size = max(0.05, R * 0.25)
    cam.show_name = True
    scene = bpy.context.scene
    scene.camera = cam_obj
    scene.render.resolution_x = fr["width"]
    scene.render.resolution_y = fr["height"]
    scene.render.resolution_percentage = 100
    return fr


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build_dome(context, settings) -> dict:
    scene = context.scene
    clear_rig()
    rig = _collection(COLL_RIG)
    visual = _collection(COLL_VISUAL)
    rig.hide_render = False
    visual.hide_render = True
    visual.hide_viewport = not settings.show_dome

    R = dome_radius(settings)
    dirs = light_directions(settings)
    n = len(dirs)

    # ---- visual dome ---------------------------------------------------------
    root = bpy.data.objects.new(ROOT_NAME, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = R * 0.03
    visual.objects.link(root)
    root.hide_render = True
    root.hide_select = True
    rings = rings_for(settings)
    el_ap = math.radians(aperture_elevation(settings))
    frame_mat = _surface_material("VRTI_FrameMat", COL_FRAME, metallic=0.85, roughness=0.32)
    accent_mat = _surface_material("VRTI_AccentMat", COL_ACCENT, roughness=0.3, emission=3.0)

    # frame: one ring per LED row, meridian ribs, legs down to the platform
    z_floor = -max(settings.info_size_z, R * 0.01) - R * 0.01
    frame_bm = bmesh.new()
    rt = R * 0.0055
    for el, _cnt in rings:
        e = math.radians(el)
        _tube(frame_bm, _circle(R * math.cos(e), R * math.sin(e)), rt, closed=True)
    n_ribs = 12 if len(dirs) >= 48 else 8
    for i in range(n_ribs):
        az = 2.0 * math.pi * (i + 0.5) / n_ribs
        arc = [(R * math.cos(e) * math.cos(az), R * math.cos(e) * math.sin(az),
                R * math.sin(e)) for e in (el_ap * j / 40 for j in range(41))]
        _tube(frame_bm, arc, rt * 0.9)
        _tube(frame_bm, [(R * math.cos(az), R * math.sin(az), 0.0),
                         (R * math.cos(az), R * math.sin(az), z_floor)], rt * 1.3)
    frame = _mesh_object(FRAME_NAME, frame_bm, visual, frame_mat)
    frame.hide_select = True

    # glowing base and camera-aperture rings
    accent_bm = bmesh.new()
    _tube(accent_bm, _circle(R, 0.0, 128), R * 0.011, seg=12, closed=True)
    _tube(accent_bm, _circle(R * math.cos(el_ap), R * math.sin(el_ap), 96), R * 0.008, seg=10, closed=True)
    base = _mesh_object(BASE_NAME, accent_bm, visual, accent_mat)
    base.hide_select = True

    # platform with azimuth scale and an "image top" marker
    platform_mat = _surface_material("VRTI_PlatformMat", COL_PLATFORM, roughness=0.6)
    pb = bmesh.new()
    _add_disc(pb, R * 1.12, z_floor - R * 0.025, z_floor, seg=128)
    platform = _mesh_object(PLATFORM_NAME, pb, visual, platform_mat)
    platform.hide_select = True
    mark_mat = _surface_material("VRTI_MarkMat", COL_MARK, roughness=0.4, emission=0.6)
    sb = bmesh.new()
    zt = z_floor + R * 0.002
    for deg in range(0, 360, 5):
        a = math.radians(deg)
        length = R * (0.07 if deg % 90 == 0 else 0.045 if deg % 30 == 0 else 0.022)
        rc = R * 1.10 - length / 2.0
        _add_box(sb, (rc * math.cos(a), rc * math.sin(a), zt), (length, R * 0.004, R * 0.003), rot_z=a)
    # arrow pointing to the top of the captured image (+Y)
    tip, half, back = R * 0.86, R * 0.045, R * 0.78
    v = [sb.verts.new(c) for c in ((0.0, tip, zt), (-half, back, zt), (half, back, zt))]
    sb.faces.new(v)
    scale = _mesh_object(SCALE_NAME, sb, visual, mark_mat)
    scale.hide_select = True
    size = R * 0.05
    for deg in (0, 90, 180, 270):
        a = math.radians(deg)
        _label(f"{LABEL_PREFIX}{deg:03d}", f"{deg}°", (R * 0.97 * math.cos(a), R * 0.97 * math.sin(a), zt),
               size, visual, mark_mat, rot_z=a - math.pi / 2.0)
    _label(f"{LABEL_PREFIX}top", "IMAGE TOP", (0.0, R * 0.72, zt), size * 0.8, visual, accent_mat)

    # LED mounts: dark housing + coloured emitter facing the specimen, one mesh per ring
    housing_mat = _surface_material("VRTI_LedHousingMat", (0.10, 0.11, 0.12), metallic=0.7, roughness=0.35)
    hr = R * 0.02 * settings.led_size
    k_dir = 0
    for k, (_el, cnt) in enumerate(rings):
        col = ring_colour(k, len(rings))
        led_mat = _emission_material(f"VRTI_LedMat_{k + 1}", col, 1.6)
        bm = bmesh.new()
        _add_disc(bm, hr, -hr * 0.1, hr * 0.7, seg=20)            # housing ring, outward of the frame
        emitter = len(bm.faces)
        _add_disc(bm, hr * 0.8, -hr * 0.35, -hr * 0.05, seg=20)   # emitter, towards the centre
        _add_disc(bm, hr * 0.55, hr * 0.7, hr * 0.85, seg=20)     # back cap glows too
        for i, f in enumerate(bm.faces):
            f.material_index = 1 if i >= emitter else 0
        mesh = bpy.data.meshes.new(f"VRTI_LED_ring{k + 1}")
        bm.to_mesh(mesh)
        bm.free()
        mesh.materials.append(housing_mat)
        mesh.materials.append(led_mat)
        for _i in range(cnt):
            d = Vector(dirs[k_dir])
            o = bpy.data.objects.new(f"{LED_PREFIX}{k_dir + 1:03d}", mesh)
            o.location = d * R
            o.rotation_mode = "QUATERNION"
            o.rotation_quaternion = d.to_track_quat("Z", "Y")  # local -Z (emitter) faces the centre
            o.hide_render = True
            o.hide_select = True
            o.color = (*col, 1.0)
            visual.objects.link(o)
            k_dir += 1
    for i in range(k_dir, n):  # zenith light (not part of a ring)
        d = Vector(dirs[i])
        o = bpy.data.objects.new(f"{LED_PREFIX}{i + 1:03d}", bpy.data.meshes.get(f"VRTI_LED_ring{len(rings)}"))
        o.location = d * R
        o.hide_render = True
        o.hide_select = True
        visual.objects.link(o)

    # active light: bright emitter with a halo, plus the cone of light it casts
    active_mat = _emission_material("VRTI_ActiveMat", COL_ACTIVE, 15.0)
    act_bm = bmesh.new()
    bmesh.ops.create_icosphere(act_bm, subdivisions=3, radius=hr * 1.5)
    _tube(act_bm, _circle(hr * 2.6, 0.0, 32), hr * 0.25, seg=8, closed=True)
    active = _mesh_object(ACTIVE_NAME, act_bm, visual, active_mat)
    active.rotation_mode = "QUATERNION"
    active.show_in_front = True
    footprint = 0.5 * math.hypot(settings.info_size_x, settings.info_size_y) or R * 0.3
    # the cone hints at the light without veiling the specimen seen through it
    beam_mat = _emission_material("VRTI_BeamMat", COL_ACTIVE, 0.6, alpha=0.025)
    beam_mat.diffuse_color = (*COL_ACTIVE, 0.03)
    beam_mat.show_transparent_back = True
    beam_mat.use_backface_culling = False
    cone = bmesh.new()
    apex = cone.verts.new((0.0, 0.0, 0.0))
    seg = 48
    rim = [cone.verts.new((footprint * math.cos(2 * math.pi * i / seg),
                           footprint * math.sin(2 * math.pi * i / seg), -R)) for i in range(seg)]
    for i in range(seg):
        cone.faces.new((apex, rim[(i + 1) % seg], rim[i]))
    beam = _mesh_object(BEAM_NAME, cone, visual, beam_mat)
    beam.rotation_mode = "QUATERNION"
    beam.hide_select = True

    # ---- camera ----------------------------------------------------------------
    cam_data = bpy.data.cameras.new(CAM_NAME)
    cam_obj = bpy.data.objects.new(CAM_NAME, cam_data)
    rig.objects.link(cam_obj)
    cam_obj.hide_render = False

    # camera model (full-frame body, lens with focus ring) - viewport only
    body_mat = _surface_material("VRTI_CamBodyMat", (0.045, 0.045, 0.05), metallic=0.2, roughness=0.45)
    lens_mat = _surface_material("VRTI_CamLensMat", (0.02, 0.02, 0.022), metallic=0.4, roughness=0.3)
    glass_mat = _emission_material("VRTI_CamGlassMat", (0.15, 0.35, 0.9), 2.0)
    bs = R * 0.11
    lens_len = bs * 0.9
    bb = bmesh.new()
    zc = lens_len + bs * 0.38
    _add_box(bb, (0.0, 0.0, zc), (bs * 1.5, bs * 0.9, bs * 0.75))                  # body
    _add_box(bb, (0.0, -bs * 0.1, zc + bs * 0.45), (bs * 0.5, bs * 0.45, bs * 0.18))  # prism hump
    _add_box(bb, (bs * 0.45, bs * 0.25, zc + bs * 0.42), (bs * 0.3, bs * 0.25, bs * 0.1))  # top dial
    body = _mesh_object(CAMBODY_NAME, bb, visual, body_mat)
    body.parent = cam_obj
    lb = bmesh.new()
    _add_disc(lb, bs * 0.36, 0.0, lens_len, seg=32)                               # barrel
    _add_disc(lb, bs * 0.40, lens_len * 0.45, lens_len * 0.7, seg=32)             # focus ring
    lens = _mesh_object(LENS_NAME, lb, visual, lens_mat)
    lens.parent = cam_obj
    gb = bmesh.new()
    _add_disc(gb, bs * 0.27, -bs * 0.01, bs * 0.002, seg=32)                       # front element
    _add_disc(gb, bs * 0.365, lens_len * 0.12, lens_len * 0.16, seg=32)           # accent ring
    glass = _mesh_object(f"{LENS_NAME}Glass", gb, visual, glass_mat)
    glass.parent = cam_obj
    for o in (body, lens, glass):
        o.hide_select = True

    # ---- light -----------------------------------------------------------------
    ltype = "SUN" if settings.light_model == "SUN" else "POINT"
    light_data = bpy.data.lights.new(LIGHT_NAME, ltype)
    light_obj = bpy.data.objects.new(LIGHT_NAME, light_data)
    rig.objects.link(light_obj)
    light_obj.rotation_mode = "QUATERNION"
    apply_light_strength(settings, light_data, R)
    light_data.use_shadow = True

    # ---- keyframes: frame f -> light f ------------------------------------------
    for f, d in enumerate(dirs, start=1):
        dv = Vector(d)
        q = dv.to_track_quat("Z", "Y")  # local +Z points at the light, so light shines along -Z = towards specimen
        if ltype == "SUN":
            light_obj.rotation_quaternion = q
            light_obj.keyframe_insert("rotation_quaternion", frame=f)
        else:
            light_obj.location = dv * R
            light_obj.keyframe_insert("location", frame=f)
        active.location = dv * R
        active.rotation_quaternion = q
        active.keyframe_insert("location", frame=f)
        active.keyframe_insert("rotation_quaternion", frame=f)
        beam.location = dv * R
        beam.rotation_quaternion = q
        beam.keyframe_insert("location", frame=f)
        beam.keyframe_insert("rotation_quaternion", frame=f)
    for ob in (light_obj, active, beam):
        _constant_interpolation(ob)

    scene.frame_start = 1
    scene.frame_end = n
    scene.frame_step = 1
    scene.frame_set(1)

    _black_world(scene)
    fr = frame_camera(settings)
    settings.dirty_dome = False
    return {"radius": R, "lights": n, "camera": fr, "rings": rings_for(settings)}


def _action_fcurves(action):
    """F-curves of an action for both legacy (<= 4.5) and slotted (5.x) actions."""
    if hasattr(action, "fcurves"):
        try:
            return list(action.fcurves)
        except Exception:  # noqa: BLE001
            pass
    out = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for bag in getattr(strip, "channelbags", []):
                out.extend(bag.fcurves)
    return out


def keyed_light_directions(count: int) -> list[tuple[float, float, float]]:
    """Light direction of frames 1..count as keyframed on VRTI_Light (what is rendered)."""
    light = bpy.data.objects.get(LIGHT_NAME)
    if light is None or light.animation_data is None or light.animation_data.action is None:
        return []
    curves = {(fc.data_path, fc.array_index): fc for fc in _action_fcurves(light.animation_data.action)}
    out = []
    for f in range(1, count + 1):
        if light.data.type == "SUN":
            q = Quaternion([curves[("rotation_quaternion", i)].evaluate(f) for i in range(4)])
            v = q @ Vector((0.0, 0.0, 1.0))
        else:
            v = Vector([curves[("location", i)].evaluate(f) for i in range(3)])
        v.normalize()
        out.append((v.x, v.y, v.z))
    return out


def _constant_interpolation(obj: bpy.types.Object) -> None:
    ad = obj.animation_data
    if ad is None or ad.action is None:
        return
    for fc in _action_fcurves(ad.action):
        for kp in fc.keyframe_points:
            kp.interpolation = "CONSTANT"


def apply_light_strength(settings, light_data: bpy.types.Light = None, R: float = None) -> None:
    if light_data is None:
        lo = bpy.data.objects.get(LIGHT_NAME)
        if lo is None:
            return
        light_data = lo.data
    R = R or dome_radius(settings)
    angle = math.radians(settings.light_angle)
    if light_data.type == "SUN":
        light_data.energy = settings.light_strength
        light_data.angle = angle
    else:
        # equal irradiance at the specimen: E = P / (4 pi R^2) ; Blender sun strength S == E
        light_data.energy = settings.light_strength * 4.0 * math.pi * R * R
        light_data.shadow_soft_size = R * math.tan(angle / 2.0)


def _black_world(scene: bpy.types.Scene) -> None:
    world = bpy.data.worlds.get("VRTI_World")
    if world is None:
        world = bpy.data.worlds.new("VRTI_World")
    world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    if bg is None:
        nt.nodes.clear()
        bg = nt.nodes.new("ShaderNodeBackground")
        out = nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(bg.outputs[0], out.inputs[0])
    bg.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    bg.inputs["Strength"].default_value = 0.0
    world.color = (0.0, 0.0, 0.0)
    scene.world = world


# --------------------------------------------------------------------------- #
# render configuration
# --------------------------------------------------------------------------- #
def renders_dir(settings) -> str:
    """Project folder of the current output (the RTIBuilder "project")."""
    base = bpy.path.abspath(settings.output_dir) if settings.output_dir else bpy.path.abspath("//vrti_output/")
    return os.path.normpath(base)


# RTIBuilder project layout (CHI): the same folder names let users fit the images
# themselves in RTIBuilder, relight-cli or RelightLab.
DIR_CAPTURES = "original-captures"   # lossless TIFF renders (+ .lp, file names only)
DIR_JPEG = "jpeg-exports"            # JPEG copies + .lp: RTIBuilder, relight-cli, RelightLab
DIR_ASSEMBLY = "assembly-files"      # RTIBuilder-style .lp with absolute paths to jpeg-exports
DIR_FINISHED = "finished-files"      # .ptm / .rti, fit report, OpenRTI metadata
LEGACY_CAPTURES = "captures"         # layout of V-RTI <= 1.2


def project_path(settings, *parts) -> str:
    return os.path.join(renders_dir(settings), *parts)


def capture_dir_name(settings) -> str:
    """JPEG captures go straight to jpeg-exports; TIFF masters to original-captures."""
    return DIR_JPEG if getattr(settings, "capture_depth", "8") == "JPEG" else DIR_CAPTURES


def capture_lp_path(settings) -> str:
    """The .lp of the current captures (falls back to older layouts for old projects)."""
    new = project_path(settings, capture_dir_name(settings), f"{settings.output_name}.lp")
    if os.path.isfile(new):
        return new
    for alt in (project_path(settings, DIR_CAPTURES, f"{settings.output_name}.lp"),
                project_path(settings, f"{settings.output_name}.lp")):
        if os.path.isfile(alt):
            return alt
    return new


def configure_render(scene: bpy.types.Scene, settings, width: int = None, height: int = None) -> str:
    """Set engine, sampling, colour management and output. Returns the frame path prefix."""
    r = scene.render
    if width and height:
        r.resolution_x, r.resolution_y = width, height
    r.resolution_percentage = int(getattr(settings, "render_scale", 100))
    r.film_transparent = False
    r.use_motion_blur = False
    r.dither_intensity = 0.0
    r.use_compositing = False
    r.use_sequencer = False

    if settings.engine == "CYCLES":
        r.engine = "CYCLES"
        cy = scene.cycles
        cy.samples = settings.samples
        cy.use_denoising = settings.use_denoise
        cy.use_adaptive_sampling = False
        cy.film_exposure = 1.0
        cy.sample_clamp_direct = 0.0
        cy.sample_clamp_indirect = 0.0
        cy.caustics_reflective = True
        cy.caustics_refractive = True
        if settings.lighting_mode == "DIRECT":
            cy.max_bounces = 0
            cy.diffuse_bounces = 0
            cy.glossy_bounces = 0
            cy.transmission_bounces = 0
            cy.transparent_max_bounces = 8
        else:
            cy.max_bounces = 6
            cy.diffuse_bounces = 4
            cy.glossy_bounces = 4
            cy.transmission_bounces = 4
        cy.device = "GPU" if settings.use_gpu and _gpu_available() else "CPU"
    else:
        try:
            r.engine = "BLENDER_EEVEE_NEXT"
        except TypeError:
            r.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = max(16, settings.samples)

    vs = scene.view_settings
    try:
        scene.display_settings.display_device = "sRGB"
    except TypeError:
        pass
    vs.view_transform = "Standard"
    try:
        vs.look = "None"
    except TypeError:
        pass
    vs.exposure = 0.0
    vs.gamma = 1.0
    vs.use_curve_mapping = False

    from .properties import capture_channels  # local import: properties imports presets only
    ims = r.image_settings
    if getattr(settings, "capture_depth", "8") == "JPEG":
        ims.file_format = "JPEG"
        ims.color_mode = capture_channels(settings)
        ims.quality = int(getattr(settings, "jpeg_quality", 95))
    else:
        ims.file_format = "TIFF"
        ims.color_mode = capture_channels(settings)
        ims.color_depth = getattr(settings, "capture_depth", "8")
        ims.tiff_codec = "DEFLATE"
    ims.color_management = "FOLLOW_SCENE"

    out_dir = project_path(settings, capture_dir_name(settings))
    os.makedirs(out_dir, exist_ok=True)
    prefix = os.path.join(out_dir, f"{settings.output_name}_")
    r.filepath = prefix
    r.use_file_extension = True
    r.use_overwrite = True
    r.use_placeholder = False
    return prefix


def _gpu_available() -> bool:
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.get_devices()
        return any(d.use and d.type != "CPU" for d in prefs.devices)
    except Exception:  # noqa: BLE001
        return False


def capture_filenames(settings) -> list[str]:
    n = len(light_directions(settings))
    ext = ".jpg" if getattr(settings, "capture_depth", "8") == "JPEG" else ".tif"
    return [f"{settings.output_name}_{f:04d}{ext}" for f in range(1, n + 1)]


__all__ = [
    "build_dome", "frame_camera", "configure_render", "light_directions", "dome_radius",
    "capture_filenames", "renders_dir", "apply_light_strength", "clear_rig", "rings_for",
    "compute_framing", "Matrix", "Quaternion",
]
