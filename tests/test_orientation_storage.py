# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless Blender test: scene handling, orientation modes, vertex colours and compact captures.

    blender -b --factory-startup --python-exit-code 1 --python tests/test_orientation_storage.py

* A 30 x 20 cm plate with an asymmetric corner marker is exported the way
  Metashape writes OBJ (Z up, coordinates 1:1, vertex colours, no texture).
* SOURCE keeps the heading and the small tilt; LEVEL removes the tilt but
  keeps the heading; a standing plate (normal horizontal) is levelled with
  its top edge at the top of the image; the spin buttons turn it in-plane;
  no mode ever mirrors the image.
* A small capture in 8-bit greyscale is fitted and the captures are deleted
  afterwards when requested.
"""
import math
import os
import shutil
import sys
import tempfile

import bpy
import numpy as np
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vrti  # noqa: E402
from vrti import dome as vdome  # noqa: E402
from vrti.rti import imageio, writers  # noqa: E402

WORK = tempfile.mkdtemp(prefix="vrti_orient_")


def ensure_registered():
    if not hasattr(bpy.types.Scene, "vrti"):
        vrti.register()
MARK = (0.12, 0.07)       # marker vertex near the top-left corner (x < 0 is left, y > 0 is top)


def make_plate(path: str, pose: Matrix, colours: bool = True) -> None:
    """Plate 0.30 x 0.20 m in its own frame: face +Z, text up +Y, a bump at the top-left."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=60, y_subdivisions=40, size=1.0)
    obj = bpy.context.active_object
    obj.scale = (0.30, 0.20, 1.0)
    bpy.ops.object.transform_apply(scale=True)
    me = obj.data
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    d = np.hypot(co[:, 0] + MARK[0], co[:, 1] - MARK[1])
    co[:, 2] = 0.01 * np.exp(-(d / 0.02) ** 2) + 0.002 * np.sin(co[:, 0] * 90)
    me.vertices.foreach_set("co", co.reshape(-1))
    me.update()
    if colours:
        attr = me.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
        rgb = np.zeros((len(me.vertices), 4), np.float32)
        rgb[:, 0] = 0.2 + 0.6 * (co[:, 0] > 0)      # red on the right half
        rgb[:, 1] = 0.3
        rgb[:, 2] = 0.3
        rgb[:, 3] = 1.0
        attr.data.foreach_set("color", rgb.reshape(-1))
    obj.matrix_world = pose
    bpy.ops.wm.obj_export(filepath=path, export_materials=True, export_colors=colours,
                          forward_axis="Y", up_axis="Z")  # Metashape-like: coordinates 1:1, Z up


def import_plate(path: str, mode: str = "SOURCE"):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ensure_registered()
    s = bpy.context.scene.vrti
    s.orient_mode = mode
    s.dome_preset = "D36"
    assert bpy.ops.vrti.import_obj(filepath=path) == {"FINISHED"}
    return s, s.target


def marker_world(obj) -> Vector:
    """World position of the highest point of the bump (the marker)."""
    me = obj.data
    mw = obj.matrix_world
    best = None
    for v in me.vertices:
        p = mw @ v.co
        if best is None or p.z > best.z:
            best = p
    return best


def frame_axes(obj, pose: Matrix):
    """Directions of the plate's own +X / +Y / +Z after alignment.

    The OBJ stores world coordinates, so the plate frame in the file is ``pose``.
    """
    mw = obj.matrix_world.to_3x3() @ pose.to_3x3()
    return mw @ Vector((1, 0, 0)), mw @ Vector((0, 1, 0)), mw @ Vector((0, 0, 1))


def deg(a: Vector, b: Vector) -> float:
    return math.degrees(a.angle(b))


def test_orientation():
    tilt = Matrix.Rotation(math.radians(8.0), 4, "X")
    heading = Matrix.Rotation(math.radians(12.0), 4, "Z")
    pose_flat = Matrix.Translation((3.0, -2.0, 1.5)) @ heading @ tilt
    p_flat = os.path.join(WORK, "plate_flat.obj")
    make_plate(p_flat, pose_flat)

    # SOURCE: file coordinates untouched (12° heading, 8° tilt), only centred
    s, obj = import_plate(p_flat, "SOURCE")
    R = obj.matrix_world.to_3x3()
    err = max(abs(a - b) for ra, rb in zip(R, Matrix.Identity(3)) for a, b in zip(ra, rb))
    print(f"[SOURCE] rotation error vs Metashape pose {err:.2e}, tilt {s.info_tilt:.2f}°, used {s.info_orient_used}")
    assert err < 1e-4 and s.info_orient_used == "SOURCE"
    assert abs(s.info_tilt - 8.0) < 1.0
    m = marker_world(obj)
    assert m.x < 0 and m.y > 0, ("marker must stay top-left", m)
    assert R.determinant() > 0.999, "mirrored"
    # vertex colours are rendered
    assert s.info_has_colour
    nodes = obj.material_slots[0].material.node_tree.nodes
    vc = [n for n in nodes if n.type == "VERTEX_COLOR"]
    assert vc and vc[0].outputs["Color"].is_linked, "vertex colours not wired to the material"
    attr = obj.data.color_attributes[vc[0].layer_name]
    print(f"[colour] attribute '{attr.name}' {attr.data_type}/{attr.domain}, "
          f"first value {tuple(round(c, 3) for c in attr.data[0].color)}")

    # LEVEL: tilt removed, heading (12°) kept
    s.orient_mode = "LEVEL"                       # update callback re-aligns
    x_ax, y_ax, z_ax = frame_axes(obj, pose_flat)
    print(f"[LEVEL] tilt {s.info_tilt:.2f}°, plate +Y heading {math.degrees(math.atan2(-y_ax.x, y_ax.y)):.2f}°")
    assert s.info_tilt < 0.5
    assert abs(math.degrees(math.atan2(-y_ax.x, y_ax.y)) - 12.0) < 1.0
    assert obj.matrix_world.to_3x3().determinant() > 0.999
    m = marker_world(obj)
    assert m.x < 0 and m.y > 0

    # spin: turn left 90° -> the top-left marker goes to the bottom-left
    assert bpy.ops.vrti.spin(angle=90.0) == {"FINISHED"}
    m = marker_world(obj)
    print(f"[spin +90] marker at ({m.x:+.3f}, {m.y:+.3f})")
    assert m.x < 0 and m.y < 0
    s.orient_mode = "SOURCE"                       # spin survives a mode change
    m = marker_world(obj)
    assert m.x < 0 and m.y < 0 and abs(obj["vrti_spin_deg"] - 90.0) < 1e-6
    assert bpy.ops.vrti.spin(angle=-90.0) == {"FINISHED"}
    m = marker_world(obj)
    assert m.x < 0 and m.y > 0
    # fine rotation (levelling text lines): +4° turns the plate's +Y axis by 4°
    head0 = math.degrees(math.atan2(-frame_axes(obj, pose_flat)[1].x, frame_axes(obj, pose_flat)[1].y))
    s.fine_rotation = 4.0
    head1 = math.degrees(math.atan2(-frame_axes(obj, pose_flat)[1].x, frame_axes(obj, pose_flat)[1].y))
    print(f"[fine rotation] heading {head0:.2f}° -> {head1:.2f}°")
    assert abs((head1 - head0) - 4.0) < 0.05
    from vrti import align as _al
    rep = _al.pose_report(obj)
    assert abs(rep["image_up_turn_deg"] - 4.0) < 0.05   # SOURCE mode: only the fine turn
    print(f"[pose report] tilt {rep['surface_tilt_deg']}°, turn {rep['image_up_turn_deg']}°")
    s.fine_rotation = 0.0

    # standing stele (normal horizontal, like a georeferenced chunk): auto level, top stays top
    stand = Matrix.Translation((0.5, 0.2, 0.9)) @ Matrix.Rotation(math.radians(90), 4, "X")
    p_stand = os.path.join(WORK, "plate_standing.obj")
    make_plate(p_stand, stand)
    s, obj = import_plate(p_stand, "SOURCE")
    m = marker_world(obj)
    print(f"[standing] used {s.info_orient_used}, source tilt {s.info_source_tilt:.1f}°, "
          f"tilt {s.info_tilt:.2f}°, marker ({m.x:+.3f}, {m.y:+.3f})")
    assert s.info_orient_used == "LEVEL" and s.info_tilt < 0.5
    assert m.x < 0 and m.y > 0, "standing stele must read upright, not mirrored"
    assert obj.matrix_world.to_3x3().determinant() > 0.999

    # classic Y-up OBJ option still works
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ensure_registered()
    s = bpy.context.scene.vrti
    s.import_up_axis = "Y"
    p_y = os.path.join(WORK, "plate_yup.obj")
    make_plate(p_y, Matrix.Identity(4))
    bpy.ops.wm.obj_export(filepath=p_y, export_materials=True, export_colors=True,
                          forward_axis="NEGATIVE_Z", up_axis="Y")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ensure_registered()
    s = bpy.context.scene.vrti
    s.import_up_axis = "Y"
    assert bpy.ops.vrti.import_obj(filepath=p_y) == {"FINISHED"}
    m = marker_world(s.target)
    print(f"[Y-up OBJ] tilt {s.info_tilt:.2f}°, marker ({m.x:+.3f}, {m.y:+.3f})")
    assert s.info_tilt < 0.5 and m.x < 0 and m.y > 0
    return p_flat


def make_inscription(path: str, turn_deg: float, tilt_deg: float) -> None:
    """A 40 x 25 cm block with six incised text lines, turned and tilted like a raw chunk."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=400, y_subdivisions=250, size=1.0)
    obj = bpy.context.active_object
    obj.scale = (0.40, 0.25, 1.0)
    bpy.ops.object.transform_apply(scale=True)
    me = obj.data
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    rng = np.random.default_rng(5)
    z = 0.0006 * rng.standard_normal(len(co))                     # weathered surface
    for k in range(6):                                            # text lines: rows of letters
        yc = 0.08 - k * 0.032
        row = np.abs(co[:, 1] - yc) < 0.008
        letters = (np.sin(co[:, 0] * 260.0) > 0.2) & (np.abs(co[:, 0]) < 0.17)
        z[row & letters] -= 0.0025
    co[:, 2] = z
    me.vertices.foreach_set("co", co.reshape(-1))
    me.update()
    obj.matrix_world = (Matrix.Rotation(math.radians(turn_deg), 4, "Z")
                        @ Matrix.Rotation(math.radians(tilt_deg), 4, "Y"))
    bpy.ops.wm.obj_export(filepath=path, export_materials=False, forward_axis="Y", up_axis="Z")


def test_auto_straighten():
    p = os.path.join(WORK, "inscription.obj")
    make_inscription(p, turn_deg=7.0, tilt_deg=6.0)
    pose = Matrix.Rotation(math.radians(7.0), 4, "Z") @ Matrix.Rotation(math.radians(6.0), 4, "Y")

    def line_angle():
        """Angle of the plate's text-line direction (its own +X) in the image, degrees."""
        d = obj.matrix_world.to_3x3() @ pose.to_3x3() @ Vector((1.0, 0.0, 0.0))
        return math.degrees(math.atan2(d.y, d.x))

    s, obj = import_plate(p, "AUTO")
    print(f"[auto] method {s.info_auto_method}, turn {s.info_auto_angle:+.2f}°, tilt {s.info_tilt:.2f}°, "
          f"text lines at {line_angle():+.2f}°")
    assert s.info_auto_method in ("text", "outline")
    assert abs(line_angle()) < 0.3, "text lines must end up horizontal"
    assert s.info_tilt < 0.5
    # stable when switching modes back and forth
    s.orient_mode = "SOURCE"
    assert abs(line_angle()) > 3.0
    s.orient_mode = "AUTO"
    assert abs(line_angle()) < 0.3


def test_import_clears_scene(p_flat: str):
    """Importing replaces the scene; a file that cannot be used leaves it untouched."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ensure_registered()
    s = bpy.context.scene.vrti
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube_mesh = cube.data.name
    cube.data.materials.append(bpy.data.materials.new("cube_material"))
    bpy.ops.object.light_add(type="POINT")

    empty = os.path.join(WORK, "no_mesh.obj")
    with open(empty, "w") as fh:
        fh.write("# nothing here\n")
    try:   # in background mode an operator's error report is raised as an exception
        bpy.ops.vrti.import_obj(filepath=empty)
        raise AssertionError("an OBJ without a mesh must be refused")
    except RuntimeError:
        pass
    assert "Cube" in bpy.data.objects, "a failed import must keep the scene"

    assert bpy.ops.vrti.import_obj(filepath=p_flat, clear_scene=False) == {"FINISHED"}
    assert "Cube" in bpy.data.objects, "Clear scene off must keep the other objects"
    first = s.target.name

    assert bpy.ops.vrti.import_obj(filepath=p_flat) == {"FINISHED"}
    names = {o.name for o in bpy.context.scene.objects}
    assert "Cube" not in names and "Point" not in names and first not in names, names
    assert cube_mesh not in bpy.data.meshes and "cube_material" not in bpy.data.materials, "orphans left behind"
    assert s.target is not None and s.target.name in names
    assert all(o == s.target or o.name.startswith("VRTI_") for o in bpy.context.scene.objects), names
    assert bpy.data.objects[vdome.BEAM_NAME].active_material.diffuse_color[3] < 0.1, "beam must stay see-through"
    print(f"[scene] after import: specimen + {len(names) - 1} rig objects")

    # the model cannot be aligned: nothing may change, because a cancelled operator has no undo step
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object.name
    target, out_dir = s.target.name, s.output_dir
    count = len(bpy.data.objects)
    real_align = vrti.align.align_object

    def broken_align(*_args, **_kwargs):
        raise RuntimeError("cannot align")

    vrti.align.align_object = broken_align
    try:
        try:
            bpy.ops.vrti.import_obj(filepath=p_flat)
            raise AssertionError("a failed alignment must cancel the import")
        except RuntimeError as exc:
            assert "cannot align" in str(exc), exc
    finally:
        vrti.align.align_object = real_align
    assert cube in bpy.data.objects and len(bpy.data.objects) == count, "a failed import must leave no trace"
    assert s.target.name == target and s.output_dir == out_dir, "a failed import must keep the settings"

    # a file named like the rig is an ordinary specimen, and rig-like names of others survive a rebuild
    reserved = os.path.join(WORK, "VRTI_model.obj")
    shutil.copyfile(p_flat, reserved)
    assert bpy.ops.vrti.import_obj(filepath=reserved) == {"FINISHED"}
    assert s.target is not None and s.target.name == "model_VRTI_model" and s.output_name == "VRTI_model", s.target
    assert s.target.name in bpy.context.scene.objects
    assert bpy.ops.vrti.build_dome() == {"FINISHED"} and s.target is not None

    # the Dome button shows and hides the rig
    s.show_dome = False
    assert bpy.data.collections[vdome.COLL_VISUAL].hide_viewport
    s.show_dome = True
    assert not bpy.data.collections[vdome.COLL_VISUAL].hide_viewport

    # import from Edit Mode
    bpy.ops.mesh.primitive_cube_add()
    bpy.ops.object.mode_set(mode="EDIT")
    assert bpy.ops.vrti.import_obj(filepath=p_flat) == {"FINISHED"}
    assert bpy.context.mode == "OBJECT"

    # no import while a capture is running
    s.is_rendering = True
    assert not bpy.ops.vrti.import_obj.poll()
    s.is_rendering = False


def test_clear_scene_keeps_other_scenes(p_flat: str):
    """Whatever another scene uses must survive the import in this one."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ensure_registered()
    this = bpy.context.scene
    other = bpy.data.scenes.new("other")

    def add(name, coll):
        ob = bpy.data.objects.new(name, bpy.data.meshes.new(name))
        coll.objects.link(ob)
        return ob

    add("in_both", this.collection)
    other.collection.objects.link(bpy.data.objects["in_both"])
    shared = bpy.data.collections.new("shared")
    this.collection.children.link(shared)
    other.collection.children.link(shared)
    add("in_shared", shared)
    own = bpy.data.collections.new("own")
    this.collection.children.link(own)
    add("only_here", own)
    camera = bpy.data.objects.new("other_camera", bpy.data.cameras.new("other_camera"))
    own.objects.link(camera)
    other.camera = camera
    target_of = add("target_of_other", own)
    follower = add("follower", other.collection)
    follower.constraints.new("COPY_LOCATION").target = target_of
    parent_only = add("parent_of_doomed", own)
    add("child_of_parent", own).parent = parent_only

    assert bpy.ops.vrti.import_obj(filepath=p_flat) == {"FINISHED"}
    names = {o.name for o in this.objects}
    other_names = {o.name for o in other.objects}
    for gone in ("only_here", "parent_of_doomed", "child_of_parent"):
        assert gone not in bpy.data.objects, f"{gone} should be deleted"
    for name in ("in_both", "in_shared", "follower"):
        assert name in other_names, f"{name} must stay in the other scene"
    for name in ("in_both", "in_shared", "other_camera", "target_of_other"):
        assert name not in names, f"{name} must leave this scene"
        assert name in bpy.data.objects, f"{name} is still used by the other scene"
    assert other.camera == camera and follower.constraints[0].target == target_of

    # a second scene gets its own specimen collection, and keeps the model it imported
    first = this.vrti.target.name
    with bpy.context.temp_override(scene=other, view_layer=other.view_layers[0]):
        assert bpy.ops.vrti.import_obj(filepath=p_flat) == {"FINISHED"}
    assert other.vrti.target.name in other.objects, "the model must land in the scene it was imported into"
    assert other.vrti.target.name not in this.objects and first in this.objects and first not in other.objects
    print("[scene] other scenes keep what they use")


def test_layout_change_and_cancel(p_flat: str):
    """A preset changed after the dome was built (e.g. an add-on update) must not render
    other lights than the .lp says; a cancelled capture must not touch an existing .lp."""
    from vrti import ops_render as R
    from vrti import presets as P
    from vrti.rti import lp as LP
    s, obj = import_plate(p_flat, "LEVEL")
    s.output_dir = os.path.join(WORK, "layout") + os.sep
    s.dome_preset = "D36"
    assert bpy.ops.vrti.build_dome() == {"FINISHED"}
    saved = P.DOME_PRESETS["D36"]["rings"]
    P.DOME_PRESETS["D36"]["rings"] = [(18, 14), (40, 12), (66, 10)]      # "new version" of the preset
    try:
        R.prepare_capture(bpy.context, s)
        want = vdome.light_directions(s)
        got = vdome.keyed_light_directions(len(want))
        err = max(math.degrees(math.acos(min(1.0, sum(a * b for a, b in zip(g, w))))) for g, w in zip(got, want))
        print(f"[layout] rendered vs .lp directions after a preset change: max {err:.4f}°, {len(got)} lights")
        assert len(got) == 36 and err < 0.1
        # cancelled: nothing written
        lp_file = vdome.capture_lp_path(s)
        assert not os.path.isfile(lp_file), "the .lp must not be written before the capture finishes"
        R._finish(s, cancelled=True)
        assert not os.path.isfile(lp_file)
        # finished: written, and it matches the rendered lights
        R.prepare_capture(bpy.context, s)
        R.write_capture_records(s)
        _n, dirs = LP.read_lp(lp_file)
        assert max(abs(a - b) for d, g in zip(dirs, got) for a, b in zip(d, g)) < 1e-5
    finally:
        P.DOME_PRESETS["D36"]["rings"] = saved


def test_compact_capture(p_flat: str):
    s, obj = import_plate(p_flat, "LEVEL")
    # quality tiers
    from vrti import presets as P
    assert s.quality_tier == "WEB" and s.capture_depth == "JPEG" and s.fit_ptm_lrgb and s.fit_hsh2
    s.quality_tier = "ARCHIVE"
    assert s.capture_depth == "16" and not s.fit_hsh3, "HSH 16 needs 48+ lights; D36 has 36"
    assert P.tier_mp("WEB", 36) == 16 and P.tier_mp("ARCHIVE", 100) == 36 and P.tier_mp("QUICK", 12) == 8
    # a scene saved by 1.4 stored TIFF for the Web tier: the capture must follow the tier (JPEG)
    s.quality_tier = "WEB"
    s["capture_depth"] = 0
    from vrti.ops_render import prepare_capture
    prepare_capture(bpy.context, s)
    assert s.capture_depth == "JPEG" and s.quality_tier == "WEB", "old TIFF setting must not survive in Web"
    s.capture_depth = "8"
    assert s.quality_tier == "CUSTOM", "a TIFF chosen by hand leaves the tier"
    s.quality_tier = "WEB"
    s.fit_ptm_rgb = True
    assert s.quality_tier == "CUSTOM", "a manual change leaves the tier"
    s.fit_ptm_rgb = False
    s.capture_depth = "8"
    s.capture_colour = "BW"
    s.samples = 8
    s.use_gpu = False
    s.render_scale = 3
    s.fit_ptm_lrgb = s.fit_hsh2 = True
    s.fit_ptm_rgb = s.fit_hsh1 = s.fit_hsh3 = False
    s.export_jpeg = False
    s.auto_fit = True
    s.delete_captures_after_fit = True
    s.output_dir = os.path.join(WORK, "out") + os.sep
    assert bpy.ops.vrti.calibrate_exposure() == {"FINISHED"}
    # keep one frame for checks before the auto-fit deletes them: render frame 1 directly
    assert bpy.ops.vrti.capture() == {"FINISHED"}
    out_dir = vdome.renders_dir(s)
    cap_dir = os.path.join(out_dir, "original-captures")
    left = [f for f in os.listdir(cap_dir) if f.endswith(".tif")] if os.path.isdir(cap_dir) else []
    print(f"[capture 8-bit grey] status: {s.status_text}; files left in captures: {len(left)}")
    assert not left, left
    outs = [p for p in s.last_outputs.split(";") if p]
    assert any(p.endswith("_lrgb.ptm") for p in outs) and any(p.endswith("_hsh9.rti") for p in outs)
    ptm = writers.read_ptm(os.path.join(out_dir, "finished-files", f"{s.output_name}_lrgb.ptm"))
    print(f"[fit] PTM {ptm['width']}x{ptm['height']}")
    import json
    rep = json.load(open(os.path.join(out_dir, "finished-files", f"{s.output_name}_fit_report.json")))
    cap = rep["capture"]
    assert cap["render"]["bit_depth"] == 8 and cap["render"]["channels"] == "BW"
    assert cap["orientation"]["mode"] == "LEVEL"
    sug = json.load(open(os.path.join(out_dir, "finished-files", f"{s.output_name}_openrti.json"), encoding="utf-8"))
    assert sug["capture_method"] == "virtual-rti" and sug["number_of_photographs"] == 36
    print(f"[openrti] upload: {sug['upload_these_files']}, {sug['scale_mm_per_px_if_model_in_metres']} mm/px")
    assert "surface_tilt_deg" in cap["orientation"]["at_capture"]
    assert cap["orientation"]["at_capture"]["surface_tilt_deg"] < 0.5
    # fit statistics are measured on the specimen, not on the black background
    summ = rep["formats"]["_summary"]
    print(f"[report] specimen covers {summ['specimen_fraction'] * 100:.0f} % of the frame")
    assert 0.05 < summ["specimen_fraction"] < 0.95
    for k, v in rep["formats"].items():
        if not k.startswith("_"):
            print(f"      {k:9s} PSNR {v['fit_psnr_db']:.1f} dB")
            assert v["fit_psnr_db"] > 25

    # a kept 8-bit grey capture really is 1 channel / 8 bit
    s.delete_captures_after_fit = False
    s.auto_fit = False
    assert bpy.ops.vrti.capture() == {"FINISHED"}
    first = os.path.join(cap_dir, sorted(f for f in os.listdir(cap_dir) if f.endswith(".tif"))[0])
    import OpenImageIO as oiio
    spec = oiio.ImageInput.open(first).spec()
    print(f"[file] {os.path.basename(first)}: {spec.nchannels} channel(s), {spec.format}, "
          f"{os.path.getsize(first)} bytes")
    assert spec.nchannels == 1 and str(spec.format) == "uint8"
    img = imageio.read_image(first, 3)
    assert img.shape[2] == 3 and np.allclose(img[..., 0], img[..., 2])


if __name__ == "__main__":
    try:
        path = test_orientation()
        test_auto_straighten()
        test_import_clears_scene(path)
        test_clear_scene_keeps_other_scenes(path)
        test_layout_change_and_cancel(path)
        test_compact_capture(path)
        print("ORIENTATION + STORAGE TEST PASSED")
    finally:
        if not os.environ.get("VRTI_KEEP"):
            shutil.rmtree(WORK, ignore_errors=True)
