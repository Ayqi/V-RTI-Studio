# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end headless test of the add-on inside Blender.

    blender -b --factory-startup --python-exit-code 1 --python tests/test_blender_pipeline.py

1. Builds a synthetic "photogrammetry" specimen: a displaced, textured relief
   mesh in an arbitrary orientation, exported as OBJ + MTL + PNG texture.
2. Registers the add-on, imports the OBJ with the real operator, checks the
   automatic alignment, dome, camera framing and recommended resolution.
3. Verifies the sun strength convention (white Lambertian plane -> value).
4. Runs exposure calibration, a reduced-size capture (Cycles CPU) and the fit.
5. Independently re-reads the PTM / HSH files, relights them with a light
   direction from the capture and with a held-out direction rendered by
   Blender, and reports PSNR.
"""
import math
import os
import shutil
import sys
import tempfile
import time

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vrti  # noqa: E402
from vrti import dome as vdome  # noqa: E402
from vrti.rti import imageio, lp, writers  # noqa: E402

WORK = tempfile.mkdtemp(prefix="vrti_e2e_")
print("work dir:", WORK)


def psnr(a, b):
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


# --------------------------------------------------------------------------- #
def make_specimen_obj() -> str:
    """Create a relief slab with a colour texture, in a skewed orientation; export OBJ."""
    reset_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=160, y_subdivisions=100, size=1.0)
    obj = bpy.context.active_object
    obj.name = "relief_slab"
    obj.scale = (0.36, 0.22, 1.0)  # 36 x 22 cm slab (grid size 1 -> 0.36 x 0.22 m)
    bpy.ops.object.transform_apply(scale=True)
    # relief: letters-like bumps + fine grain
    me = obj.data
    rng = np.random.default_rng(3)
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    x, y = co[:, 0], co[:, 1]
    relief = 0.004 * np.sin(x * 120) * np.cos(y * 150) + 0.002 * np.sin(x * 400 + y * 300)
    relief += 0.0008 * rng.standard_normal(len(x)).astype(np.float32)
    bowl = -0.01 * ((x / 0.18) ** 2 + (y / 0.11) ** 2)
    co[:, 2] = relief + bowl
    me.vertices.foreach_set("co", co.reshape(-1))
    me.update()
    # texture
    tex_w, tex_h = 1024, 640
    img = bpy.data.images.new("slab_tex", tex_w, tex_h)
    yy, xx = np.mgrid[0:tex_h, 0:tex_w].astype(np.float32)
    r = 0.45 + 0.35 * np.sin(xx / 40.0)
    g = 0.40 + 0.25 * np.cos(yy / 33.0)
    b = 0.35 + 0.30 * np.sin((xx + yy) / 70.0)
    px = np.stack([r, g, b, np.ones_like(r)], axis=-1).reshape(-1)
    img.pixels.foreach_set(px.astype(np.float32))
    tex_path = os.path.join(WORK, "slab_tex.png")
    img.filepath_raw = tex_path
    img.file_format = "PNG"
    img.save()
    mat = bpy.data.materials.new("slab_mat")
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    pr = nt.nodes["Principled BSDF"]
    nt.links.new(tex.outputs["Color"], pr.inputs["Base Color"])
    me.materials.append(mat)
    bpy.ops.object.shade_smooth()
    # arbitrary orientation + offset like a Metashape chunk
    obj.rotation_euler = (math.radians(70), math.radians(-35), math.radians(120))
    obj.location = (2.3, -1.1, 0.7)
    obj_path = os.path.join(WORK, "relief_slab.obj")
    bpy.ops.wm.obj_export(filepath=obj_path, export_materials=True, path_mode="COPY",
                          export_selected_objects=False, forward_axis="NEGATIVE_Z", up_axis="Y")
    assert os.path.isfile(obj_path) and os.path.isfile(obj_path[:-4] + ".mtl")
    return obj_path


def check_sun_convention():
    """Measure the pixel value of a white Lambertian plane under sun strength 1."""
    reset_scene()
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    plane = bpy.context.active_object
    mat = bpy.data.materials.new("white")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    d = nt.nodes.new("ShaderNodeBsdfDiffuse")
    d.inputs["Color"].default_value = (1, 1, 1, 1)
    nt.links.new(d.outputs[0], out.inputs[0])
    plane.data.materials.append(mat)
    sun = bpy.data.lights.new("sun", "SUN")
    sun.energy = 1.0
    sun.angle = 0.0
    so = bpy.data.objects.new("sun", sun)
    scene.collection.objects.link(so)
    cam = bpy.data.cameras.new("cam")
    co = bpy.data.objects.new("cam", cam)
    co.location = (0, 0, 3)
    scene.collection.objects.link(co)
    scene.camera = co
    cam.type = "ORTHO"
    cam.ortho_scale = 1.0
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 16
    scene.cycles.device = "CPU"
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "Standard"
    scene.world = bpy.data.worlds.new("w")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    p = os.path.join(WORK, "sun_check.exr")
    scene.render.filepath = p
    bpy.ops.render.render(write_still=True)
    val = float(imageio.read_image(p).mean())
    print(f"[sun convention] white Lambertian plane under sun strength 1.0 -> linear value {val:.4f}")
    return val


# --------------------------------------------------------------------------- #
def run_pipeline(obj_path: str):
    reset_scene()
    try:
        vrti.register()
    except Exception as exc:  # already registered
        print("register:", exc)
    scene = bpy.context.scene
    s = scene.vrti

    s.dome_preset = "D48"
    s.quality_tier = "CUSTOM"   # formats are chosen below by hand
    s.import_up_axis = "Y"      # the synthetic OBJ is written Y-up in a skewed pose
    s.orient_mode = "PCA"       # fully automatic alignment (long side along X)
    s.engine = "CYCLES"
    s.samples = 24
    s.use_gpu = False
    s.render_scale = 5          # 24 MP ladder step -> ~ 300 x 200 px test frames
    s.fit_ptm_lrgb = s.fit_ptm_rgb = s.fit_hsh2 = s.fit_hsh3 = True
    s.fit_hsh1 = True
    s.export_jpeg = True
    s.auto_fit = True

    # Blender removes bl_info from extension modules once loaded: the add-on must not need it
    if hasattr(vrti, "bl_info"):
        del vrti.bl_info
    t0 = time.time()
    res = bpy.ops.vrti.import_obj(filepath=obj_path)
    assert res == {"FINISHED"}, res
    obj = s.target
    assert obj is not None
    print(f"[import] {obj.name}: faces={s.info_faces} texels={s.info_texels / 1e6:.2f}M "
          f"size=({s.info_size_x:.3f},{s.info_size_y:.3f},{s.info_size_z:.4f}) m  rec={s.recommended_mp} MP "
          f"preset={s.resolution_preset}  ({time.time() - t0:.1f}s)")
    # alignment checks: slab should be flat in XY, long side along X, surface near z=0, centred
    assert abs(s.info_size_x - 0.36) < 0.02 and abs(s.info_size_y - 0.22) < 0.02, (s.info_size_x, s.info_size_y)
    assert s.info_size_z < 0.05
    bb = [obj.matrix_world @ v.co for v in obj.data.vertices]
    zs = np.array([v.z for v in bb]); xs = np.array([v.x for v in bb]); ys = np.array([v.y for v in bb])
    assert abs(xs.min() + xs.max()) < 1e-3 and abs(ys.min() + ys.max()) < 1e-3, "not centred"
    assert zs.max() < 0.01 and zs.max() > -0.01, ("surface not at z=0", zs.min(), zs.max())
    # the textured face must point up: mean normal z > 0
    nrm = np.empty(len(obj.data.vertices) * 3, np.float32)
    obj.data.vertex_normals.foreach_get("vector", nrm)
    nrm = nrm.reshape(-1, 3) @ np.array(obj.matrix_world.to_3x3()).T
    assert nrm[:, 2].mean() > 0.5, nrm[:, 2].mean()

    # dome / camera
    cam = bpy.data.objects[vdome.CAM_NAME]
    light = bpy.data.objects[vdome.LIGHT_NAME]
    leds = [o for o in bpy.data.objects if o.name.startswith(vdome.LED_PREFIX)]
    assert len(leds) == 48 and scene.frame_end == 48
    R = vdome.dome_radius(s)
    print(f"[dome] radius={R:.3f} m  camera z={cam.location.z:.3f}  lens={cam.data.lens:.1f} mm  "
          f"res={scene.render.resolution_x}x{scene.render.resolution_y}")
    assert abs(R - 1.6 * 0.36) < 0.03
    assert s.resolution_mp == s.recommended_mp
    # light orientation at frame 1 must match direction 1
    dirs = vdome.light_directions(s)
    scene.frame_set(1)
    from mathutils import Vector
    local_z = light.matrix_world.to_3x3() @ Vector((0, 0, 1))
    assert (local_z.normalized() - Vector(dirs[0])).length < 1e-4, (local_z, dirs[0])
    scene.frame_set(20)
    local_z = light.matrix_world.to_3x3() @ Vector((0, 0, 1))
    assert (local_z.normalized() - Vector(dirs[19])).length < 1e-4

    # exposure calibration
    t0 = time.time()
    s.light_strength = 1.0
    res = bpy.ops.vrti.calibrate_exposure()
    assert res == {"FINISHED"}
    print(f"[calibration] light strength -> {s.light_strength:.3f}  ({time.time() - t0:.1f}s)")

    # capture (+ auto fit + jpeg in background mode)
    t0 = time.time()
    res = bpy.ops.vrti.capture()
    assert res == {"FINISHED"}, res
    out_dir = vdome.renders_dir(s)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(WORK, "pipeline_debug.blend"))
    cap_dir = os.path.join(out_dir, "jpeg-exports")           # default: JPEG captures, written there
    assert not os.path.isdir(os.path.join(out_dir, "original-captures")), "no TIFF masters for JPEG captures"
    fin_dir = os.path.join(out_dir, "finished-files")
    files = sorted(f for f in os.listdir(cap_dir) if f.endswith(".jpg"))
    print(f"[capture] {len(files)} frames in {time.time() - t0:.1f}s -> {cap_dir}")
    assert len(files) == 48, files
    lp_path = os.path.join(cap_dir, f"{s.output_name}.lp")
    names, ldirs = lp.read_lp(lp_path)
    assert len(names) == 48 and names[0] == f"{s.output_name}_0001.jpg"
    # RTIBuilder / relight layout: JPEGs + .lp (file names) and assembly-files .lp (absolute paths)
    jnames, _jd = lp.read_lp(os.path.join(out_dir, "jpeg-exports", f"{s.output_name}.lp"))
    anames, _ad = lp.read_lp(os.path.join(out_dir, "assembly-files", f"{s.output_name}.lp"))
    assert jnames[0] == f"{s.output_name}_0001.jpg" and os.path.isabs(anames[0]) and os.path.isfile(anames[0])
    assert jnames == sorted(jnames), "relight-cli matches .lp lines to the images in sorted order"
    assert os.path.isfile(os.path.join(out_dir, "README.txt"))
    assert os.path.isfile(os.path.join(out_dir, f"{s.output_name}_capture.json"))
    assert os.path.isfile(os.path.join(out_dir, "jpeg-exports", f"{s.output_name}_0001.jpg"))

    # image sanity: brightest frame should be below clipping but reasonably exposed
    peak = 0.0
    for f in files:
        img = imageio.read_image(os.path.join(cap_dir, f))
        peak = max(peak, float(np.percentile(img.max(axis=2), 99.9)))
    print(f"[exposure] 99.9 pct peak over all frames = {peak:.3f} (target {s.exposure_target})")
    assert 0.80 < peak <= 1.0, peak

    # fit outputs
    outs = s.last_outputs.split(";")
    print("[fit] outputs:", [os.path.basename(p) for p in outs])
    assert any(p.endswith("_lrgb.ptm") for p in outs) and any(p.endswith("_hsh9.rti") for p in outs)
    rep_path = os.path.join(fin_dir, f"{s.output_name}_fit_report.json")
    assert os.path.isfile(rep_path)
    import json
    rep = json.load(open(rep_path))
    for k, v in rep["formats"].items():
        if k.startswith("_"):
            continue
        print(f"      {k:9s} PSNR {v['fit_psnr_db']:.1f} dB   cond {v['design_condition_number']:.1f}")

    # relight check against a training frame (frame 5) and a held-out direction
    ptm = writers.read_ptm(os.path.join(fin_dir, f"{s.output_name}_lrgb.ptm"))
    hsh = writers.read_hsh(os.path.join(fin_dir, f"{s.output_name}_hsh9.rti"))
    hsh16 = writers.read_hsh(os.path.join(fin_dir, f"{s.output_name}_hsh16.rti"))
    truth5 = imageio.read_image(os.path.join(cap_dir, files[4]))
    print(f"[relight frame 5] PTM-LRGB {psnr(writers.relight_ptm(ptm, ldirs[4]), truth5):.1f} dB  "
          f"HSH9 {psnr(writers.relight_hsh(hsh, ldirs[4]), truth5):.1f} dB  "
          f"HSH16 {psnr(writers.relight_hsh(hsh16, ldirs[4]), truth5):.1f} dB")
    assert psnr(writers.relight_hsh(hsh, ldirs[4]), truth5) > 25

    # held-out: render an extra direction with Blender itself
    d_new = np.array([0.35, 0.25, 0.0]); d_new[2] = math.sqrt(1 - d_new[0] ** 2 - d_new[1] ** 2)
    from mathutils import Vector as V
    light = bpy.data.objects[vdome.LIGHT_NAME]      # the capture rebuilt the rig
    light.animation_data_clear()
    light.rotation_quaternion = V(d_new).to_track_quat("Z", "Y")
    scene.render.filepath = os.path.join(WORK, "heldout")
    bpy.ops.render.render(write_still=True)
    held_file = next(os.path.join(WORK, f) for f in os.listdir(WORK) if f.startswith("heldout."))
    truth_new = imageio.read_image(held_file)
    print(f"[held-out] PTM-LRGB {psnr(writers.relight_ptm(ptm, d_new), truth_new):.1f} dB  "
          f"HSH9 {psnr(writers.relight_hsh(hsh, d_new), truth_new):.1f} dB  "
          f"HSH16 {psnr(writers.relight_hsh(hsh16, d_new), truth_new):.1f} dB")
    # orientation: relit image must not be flipped relative to the Blender render
    rel = writers.relight_hsh(hsh, ldirs[4])
    flipped = rel[::-1]
    assert psnr(rel, truth5) > psnr(flipped, truth5) + 3, "vertical orientation mismatch"
    # normals: PTM normals should point mostly up (flat slab)
    nm = writers.ptm_normals(ptm["coeffs"])
    mask = truth5.max(axis=2) > 0.05
    print(f"[normals] mean z of PTM normals on specimen = {nm[..., 2][mask].mean():.3f}")
    print("E2E PIPELINE TEST PASSED ->", out_dir)
    return out_dir


def test_pipeline():
    val = check_sun_convention()
    assert abs(val - 1.0 / math.pi) < 0.01, val
    run_pipeline(make_specimen_obj())


if __name__ == "__main__":
    val = check_sun_convention()
    assert abs(val - 1.0 / math.pi) < 0.01, ("sun strength convention changed", val)
    obj_path = make_specimen_obj()
    out = run_pipeline(obj_path)
    if os.environ.get("VRTI_KEEP"):
        print("kept", WORK)
    else:
        shutil.rmtree(WORK, ignore_errors=True)
