# SPDX-License-Identifier: GPL-3.0-or-later
"""Specimen import / alignment / material / dome operators."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper

from . import align, dome, presets
from .properties import capture_resolution, get_settings
from .rti import imageio


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _specimen_collection(context):
    """The specimen collection of this scene (another scene keeps its own)."""
    scene = context.scene
    for coll in scene.collection.children_recursive:
        if coll.name == dome.COLL_SPECIMEN or coll.name.startswith(f"{dome.COLL_SPECIMEN}."):
            return coll
    coll = bpy.data.collections.new(dome.COLL_SPECIMEN)
    scene.collection.children.link(coll)
    return coll


def move_to_collection(obj, coll):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)


def vertex_colour_name(obj) -> str:
    """Name of the mesh's colour attribute ('' when it has none)."""
    me = getattr(obj, "data", None)
    attrs = getattr(me, "color_attributes", None)
    if not attrs:
        return ""
    act = attrs.active_color if attrs.active_color is not None else attrs[0]
    return act.name


def has_colour(obj) -> bool:
    """True when the model has a colour texture or vertex colours."""
    for slot in obj.material_slots:
        mat = slot.material
        if mat is not None and mat.use_nodes and any(
                n.type == "TEX_IMAGE" and n.image is not None for n in mat.node_tree.nodes):
            return True
    return bool(vertex_colour_name(obj))


def apply_material_settings(obj, settings) -> int:
    """Normalise the photogrammetry material(s). Returns number of materials touched."""
    touched = 0
    if not any(slot.material for slot in obj.material_slots):
        # OBJ without MTL: give it a material so colour and roughness settings apply
        mat = bpy.data.materials.new(f"{obj.name}_VRTI")
        mat.use_nodes = True
        if obj.material_slots:
            obj.material_slots[0].material = mat
        else:
            obj.data.materials.append(mat)
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        mat.use_nodes = True
        nt = mat.node_tree
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        if out is None:
            out = nt.nodes.new("ShaderNodeOutputMaterial")
        principled = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        tex = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image is not None), None)
        if principled is None:
            principled = nt.nodes.new("ShaderNodeBsdfPrincipled")
            if tex is not None:
                nt.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
        vcol = vertex_colour_name(obj)
        if tex is None and vcol and not principled.inputs["Base Color"].is_linked:
            # Metashape models without a texture carry their colour per vertex
            node = next((n for n in nt.nodes if n.type == "VERTEX_COLOR"), None)
            if node is None:
                node = nt.nodes.new("ShaderNodeVertexColor")
                node.location = principled.location + type(principled.location)((-300, 0))
            node.layer_name = vcol
            nt.links.new(node.outputs["Color"], principled.inputs["Base Color"])
        # photogrammetry colour is display-referred sRGB
        if tex is not None:
            try:
                tex.image.colorspace_settings.name = "sRGB"
            except TypeError:
                pass
        principled.inputs["Metallic"].default_value = 0.0
        principled.inputs["Roughness"].default_value = settings.roughness
        for key in ("Specular IOR Level", "Specular"):
            if key in principled.inputs:
                principled.inputs[key].default_value = settings.specular_level
                break
        if "Coat Weight" in principled.inputs:
            principled.inputs["Coat Weight"].default_value = 0.0
        if "Emission Strength" in principled.inputs:
            principled.inputs["Emission Strength"].default_value = 0.0
        # remove existing surface links
        for link in list(nt.links):
            if link.to_node == out and link.to_socket.name == "Surface":
                nt.links.remove(link)
        if settings.material_mode == "LAMBERT":
            diff = next((n for n in nt.nodes if n.type == "BSDF_DIFFUSE"), None)
            if diff is None:
                diff = nt.nodes.new("ShaderNodeBsdfDiffuse")
                diff.location = principled.location + type(principled.location)((0, -300))
            diff.inputs["Roughness"].default_value = 0.0
            src = principled.inputs["Base Color"]
            if src.is_linked:
                nt.links.new(src.links[0].from_socket, diff.inputs["Color"])
            else:
                diff.inputs["Color"].default_value = src.default_value
            nt.links.new(diff.outputs[0], out.inputs["Surface"])
        else:
            nt.links.new(principled.outputs[0], out.inputs["Surface"])
        touched += 1
    return touched


def analyze_and_store(context, settings, obj, reset_resolution: bool = False) -> dict:
    info = align.analyze_object(obj)
    settings.info_has_colour = has_colour(obj)
    settings.info_faces = info["faces"]
    settings.info_texels = info["texels"]
    settings.info_size_x, settings.info_size_y, settings.info_size_z = info["size"]
    w, h = capture_resolution(settings)
    fill = align.footprint_fill_ratio(info["size"][0], info["size"][1], w, h, settings.framing_margin)
    vis = info.get("visible_fraction", 1.0)
    settings.info_visible_fraction = vis
    settings.recommended_mp = presets.recommended_mp(info["texels"] * vis, info["faces"] * vis, fill)
    settings.detail_mp = presets.detail_mp(info["texels"] * vis, info["faces"] * vis, fill)
    if reset_resolution or settings.resolution_mp not in presets.RESOLUTION_MP:
        from .properties import apply_quality_tier
        if settings.quality_tier != "CUSTOM":
            apply_quality_tier(settings, settings.quality_tier)
        else:
            settings.resolution_mp = settings.recommended_mp
    info["fill"] = fill
    return info


def store_alignment(settings, stats: dict) -> None:
    settings.info_tilt = stats.get("tilt_deg", 0.0)
    settings.info_source_tilt = stats.get("source_tilt_deg", 0.0)
    settings.info_orient_used = stats.get("mode", "")
    settings.info_auto_angle = stats.get("auto_angle_deg", 0.0)
    settings.info_auto_method = stats.get("auto_method", "")


def realign_specimen(context, settings) -> dict:
    """Re-align the specimen from its imported pose with the current orientation mode."""
    obj = settings.target
    stats = align.align_object(obj, settings.orient_mode)
    store_alignment(settings, stats)
    analyze_and_store(context, settings, obj)
    dome.build_dome(context, settings)
    return stats


def setup_specimen(context, settings, obj, rebuild_dome: bool = True) -> dict:
    """Full automatic preparation of an imported mesh (current pose = source pose)."""
    settings.target = obj
    align.remember_source(obj)
    for key in (align.SPIN_KEY, align.FINE_KEY, align.AUTO_KEY):
        if key in obj:
            del obj[key]
    settings["fine_rotation"] = 0.0
    stats = align.align_object(obj, settings.orient_mode)
    store_alignment(settings, stats)
    apply_material_settings(obj, settings)
    info = analyze_and_store(context, settings, obj, reset_resolution=True)
    if not settings.output_name or settings.output_name == "specimen":
        settings.output_name = bpy.path.clean_name(obj.name)[:48] or "specimen"
    if rebuild_dome:
        dome.build_dome(context, settings)
    frame_viewports(context)
    return {**stats, **info}


def frame_viewports(context) -> None:
    for area in context.screen.areas if context.screen else []:
        if area.type == "VIEW_3D":
            for region in area.regions:
                if region.type == "WINDOW":
                    try:
                        with context.temp_override(area=area, region=region):
                            bpy.ops.view3d.view_all(center=False)
                    except Exception:  # noqa: BLE001
                        pass


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #
def remove_objects(objects) -> None:
    """Delete objects, then the meshes, materials and images nothing else uses any more."""
    data, materials, images = set(), set(), set()
    for obj in objects:
        if obj.data is not None:
            data.add(obj.data)
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            materials.add(mat)
            if mat.use_nodes:
                images.update(n.image for n in mat.node_tree.nodes
                              if n.type == "TEX_IMAGE" and n.image is not None)
    bpy.data.batch_remove(list(objects))
    # order matters: objects hold the data, the data holds materials, materials hold images
    for ids in (data, materials, images):
        bpy.data.batch_remove([i for i in ids if i.users == 0])


def clear_scene(context, keep) -> int:
    """Remove every object of the scene except ``keep``, and the data only those objects used.

    Whatever something outside this scene still uses survives: a collection another scene
    shows is unlinked from this scene, and an object used elsewhere (in another scene, as its
    camera, as a constraint target...) is only taken out of this scene's collections.
    Returns the number of objects deleted.
    """
    scene = context.scene
    keep = set(keep)
    other_trees = set()
    for other in bpy.data.scenes:
        if other != scene:
            other_trees.update(other.collection.children_recursive)

    tree = [scene.collection, *scene.collection.children_recursive]
    for parent in tree:
        for child in list(parent.children):
            if child in other_trees and not keep.intersection(child.all_objects):
                parent.children.unlink(child)
    tree = [scene.collection, *scene.collection.children_recursive]
    own_collections = {coll for coll in tree if coll not in other_trees}

    candidates = {obj for obj in scene.objects if obj not in keep}
    users = bpy.data.user_map(subset=candidates)
    ignored = (bpy.types.Screen, bpy.types.WindowManager, bpy.types.WorkSpace)   # views are not real uses

    def used_elsewhere(obj) -> bool:
        for user in users[obj]:
            if isinstance(user, ignored) or user in own_collections or user in candidates or user == scene:
                continue
            return True
        return False

    # an object that stays keeps what it depends on, so repeat until nothing changes
    while True:
        spared = {obj for obj in candidates if used_elsewhere(obj)}
        if not spared:
            break
        candidates -= spared
        for obj in spared:
            for coll in own_collections.intersection(obj.users_collection):
                coll.objects.unlink(obj)

    settings = get_settings(context)
    if settings.target in candidates:
        settings.target = None
    remove_objects(candidates)
    return len(candidates)


class VRTI_OT_import_obj(bpy.types.Operator, ImportHelper):
    """Import an OBJ + MTL model (e.g. Metashape export), align it to the dome and frame the camera.
    The rest of the scene is removed unless "Clear scene" is switched off"""
    bl_idname = "vrti.import_obj"
    bl_label = "Import OBJ + MTL"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".obj"
    filter_glob: StringProperty(default="*.obj", options={"HIDDEN"})
    clear_scene: bpy.props.BoolProperty(
        name="Clear scene", default=True,
        description="Remove all other objects from the scene once the model has been read (Ctrl+Z restores them)")

    @classmethod
    def poll(cls, context):
        s = get_settings(context)
        return not s.is_rendering and not s.is_fitting

    def execute(self, context):
        settings = get_settings(context)
        path = self.filepath
        if not os.path.isfile(path):
            self.report({"ERROR"}, f"File not found: {path}")
            return {"CANCELLED"}
        if context.mode != "OBJECT" and context.active_object is not None:
            bpy.ops.object.mode_set(mode="OBJECT")
        before = set(bpy.data.objects)
        t0 = time.time()
        try:
            # Metashape writes its Z-up chunk coordinates: read them 1:1 (forward Y, up Z)
            if settings.import_up_axis == "Z":
                axes = {"forward_axis": "Y", "up_axis": "Z"}
            else:
                axes = {"forward_axis": "NEGATIVE_Z", "up_axis": "Y"}
            bpy.ops.wm.obj_import(filepath=path, use_split_objects=False, use_split_groups=False,
                                  validate_meshes=True, **axes)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"OBJ import failed: {exc}")
            return {"CANCELLED"}
        new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
        if not new:
            self.report({"ERROR"}, "The OBJ contained no mesh data")
            return {"CANCELLED"}
        obj = new[0]
        if len(new) > 1:
            with context.temp_override(active_object=obj, selected_editable_objects=new, selected_objects=new):
                bpy.ops.object.join()
        stem = os.path.splitext(os.path.basename(path))[0]
        obj.name = f"model_{stem}" if stem.startswith(dome.RIG_PREFIX) else stem   # the prefix is the rig's
        saved = {key: getattr(settings, key) for key in ("target", "source_path", "output_name", "output_dir")}
        saved_rotation = settings.fine_rotation
        move_to_collection(obj, _specimen_collection(context))
        settings.source_path = path
        settings.output_name = bpy.path.clean_name(stem)[:48] or "specimen"
        settings.output_dir = os.path.join(os.path.dirname(path), f"{settings.output_name}_VRTI") + os.sep
        try:
            info = setup_specimen(context, settings, obj)
        except Exception as exc:  # noqa: BLE001
            # a cancelled operator gets no undo step: leave everything as it was
            remove_objects([obj])
            for key, value in saved.items():
                setattr(settings, key, value)
            settings["fine_rotation"] = saved_rotation
            self.report({"ERROR"}, f"Alignment failed: {exc}")
            return {"CANCELLED"}
        # only now, so that a model which cannot be used costs nothing
        if self.clear_scene:
            if clear_scene(context, keep={obj, *dome.rig_objects()}):
                frame_viewports(context)
        settings.last_outputs = ""
        settings.last_error = ""
        settings.status_text = ""
        if info.get("mode") == "LEVEL" and settings.orient_mode == "SOURCE":
            self.report({"WARNING"}, f"The surface was tilted {info['source_tilt_deg']:.0f}° from the camera "
                                     "axis, so it was levelled automatically (heading kept)")
        if not info["texels"] and not settings.info_has_colour:
            self.report({"WARNING"}, "No colour texture or vertex colours found - the model will render "
                                     "with a flat grey material")
        elif not info["texels"]:
            self.report({"INFO"}, "No texture: using the model's vertex colours")
        else:
            self.report({"INFO"}, "Note: the photogrammetry texture is used as albedo; lighting baked "
                                  "into it by the original photographs is relit again")
        size = f"{align.nice_length(info['size'][0])} × {align.nice_length(info['size'][1])}"
        self.report({"INFO"}, f"Imported {obj.name}: {info['faces']:,} triangles, {size}. "
                              f"Recommended size: {settings.recommended_mp} MP ({time.time() - t0:.1f}s)")
        return {"FINISHED"}


class VRTI_OT_use_selected(bpy.types.Operator):
    """Use the active mesh object as the specimen (align, analyse, build dome)"""
    bl_idname = "vrti.use_selected"
    bl_label = "Use Selected Object"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH" \
            and not context.active_object.name.startswith("VRTI_")

    def execute(self, context):
        settings = get_settings(context)
        obj = context.active_object
        move_to_collection(obj, _specimen_collection(context))
        settings.output_name = bpy.path.clean_name(obj.name)[:48] or "specimen"
        setup_specimen(context, settings, obj)
        self.report({"INFO"}, f"{obj.name} is now the specimen")
        return {"FINISHED"}


class VRTI_OT_realign(bpy.types.Operator):
    """Re-align from the imported pose with the chosen orientation mode and re-centre under the camera"""
    bl_idname = "vrti.realign"
    bl_label = "Re-align to Dome"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_settings(context).target is not None

    def execute(self, context):
        settings = get_settings(context)
        stats = realign_specimen(context, settings)
        self.report({"INFO"}, f"Aligned ({stats['mode'].lower()}), surface tilt {stats['tilt_deg']:.1f}°")
        return {"FINISHED"}


class VRTI_OT_spin(bpy.types.Operator):
    """Turn the specimen in the image plane (about the camera axis)"""
    bl_idname = "vrti.spin"
    bl_label = "Rotate in Image"
    bl_options = {"REGISTER", "UNDO"}

    angle: bpy.props.FloatProperty(name="Angle", default=90.0)

    @classmethod
    def poll(cls, context):
        return get_settings(context).target is not None

    def execute(self, context):
        settings = get_settings(context)
        align.rotate_about_z(settings.target, self.angle, settings.orient_mode)
        realign_specimen(context, settings)
        return {"FINISHED"}


class VRTI_OT_keep_pose(bpy.types.Operator):
    """Take the specimen's current pose (e.g. after rotating it by hand) as its source orientation"""
    bl_idname = "vrti.keep_pose"
    bl_label = "Use Current Pose"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_settings(context).target is not None

    def execute(self, context):
        settings = get_settings(context)
        obj = settings.target
        align.remember_source(obj)
        for key in (align.SPIN_KEY, align.FINE_KEY, align.AUTO_KEY):
            if key in obj:
                del obj[key]
        settings["fine_rotation"] = 0.0
        settings["orient_mode"] = 0  # SOURCE, without triggering the update callback
        stats = realign_specimen(context, settings)
        self.report({"INFO"}, f"Current pose kept, surface tilt {stats['tilt_deg']:.1f}°")
        return {"FINISHED"}


class VRTI_OT_apply_material(bpy.types.Operator):
    """Apply roughness / specular / Lambertian settings to the specimen materials"""
    bl_idname = "vrti.apply_material"
    bl_label = "Apply Material Settings"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_settings(context).target is not None

    def execute(self, context):
        settings = get_settings(context)
        n = apply_material_settings(settings.target, settings)
        self.report({"INFO"}, f"Updated {n} material(s)")
        return {"FINISHED"}


class VRTI_OT_build_dome(bpy.types.Operator):
    """(Re)build the virtual dome, LED markers, capture light and zenith camera"""
    bl_idname = "vrti.build_dome"
    bl_label = "Rebuild Dome"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_settings(context)
        if settings.target is not None:
            analyze_and_store(context, settings, settings.target)
        info = dome.build_dome(context, settings)
        cam = info["camera"]
        lens = f"{cam['lens']:.0f} mm" if cam.get("lens") else "orthographic"
        self.report({"INFO"}, f"Dome: {info['lights']} lights, radius {align.nice_length(info['radius'])}, "
                              f"camera {lens} at {align.nice_length(cam['distance'])}")
        return {"FINISHED"}


class VRTI_OT_frame_camera(bpy.types.Operator):
    """Re-frame the zenith camera on the specimen using the current size/aspect settings"""
    bl_idname = "vrti.frame_camera"
    bl_label = "Frame Camera"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_settings(context)
        if bpy.data.objects.get(dome.CAM_NAME) is None:
            dome.build_dome(context, settings)
        else:
            if settings.target is not None:
                analyze_and_store(context, settings, settings.target)
            dome.frame_camera(settings)
        return {"FINISHED"}


def brightest_frames(dirs) -> list:
    """Indices of the lights that give the brightest images.

    Measured on real captures the 99.9th percentile rises steadily with the light
    elevation (cosine law + specular lobe towards the zenith camera), so the
    highest ring decides the exposure: two opposite lights of the highest ring
    and one of the ring below it.
    """
    rings = {}
    for i, d in enumerate(dirs):
        rings.setdefault(round(d[2], 4), []).append(i)
    levels = sorted(rings, reverse=True)
    top = rings[levels[0]]
    frames = [top[0], top[len(top) // 2]]
    if len(levels) > 1:
        frames.append(rings[levels[1]][0])
    return sorted(set(frames))


def calibrate_exposure(context, settings) -> dict:
    """Render the brightest lights at capture resolution and set the light strength.

    Returns {"peak", "factor", "strength"}; raises RuntimeError when the test
    renders are black (specimen outside the camera view).
    """
    scene = context.scene
    if bpy.data.objects.get(dome.LIGHT_NAME) is None or settings.dirty_dome:
        dome.build_dome(context, settings)
    dome.apply_light_strength(settings)  # make sure the lamp carries the current strength
    dirs = dome.light_directions(settings)
    test_frames = brightest_frames(dirs)

    r = scene.render
    saved = (r.resolution_percentage, r.filepath, scene.frame_current, scene.cycles.samples,
             r.image_settings.file_format, r.image_settings.color_mode, r.image_settings.color_depth,
             scene.cycles.use_denoising, r.use_border, r.use_crop_to_border, r.border_min_x, r.border_max_x,
             r.border_min_y, r.border_max_y, r.use_persistent_data)
    dome.configure_render(scene, settings)
    r.image_settings.file_format = "TIFF"
    r.image_settings.color_mode = "RGB"
    r.image_settings.color_depth = "16"
    tmp = tempfile.mkdtemp(prefix="vrti_calib_")
    try:
        # Specular highlights are a few pixels wide: a downscaled test frame averages them
        # away and under-estimates the peak, so the test frames are rendered at the real
        # capture resolution (fewer samples; noise only makes the estimate safer).
        r.use_border = False
        r.use_persistent_data = True     # keep the BVH between the test renders
        scene.cycles.samples = min(16, settings.samples)
        scene.cycles.use_denoising = False
        factor = 1.0
        for _attempt in range(3):        # repeat while the brightest light still clips
            peak = 0.0
            for f in test_frames:
                scene.frame_set(f + 1)
                r.filepath = os.path.join(tmp, f"calib_{f + 1:04d}")
                bpy.ops.render.render(write_still=True)
                path = r.filepath + ".tif"
                if not os.path.isfile(path):
                    continue
                v = imageio.read_image(path, 3).max(axis=2).reshape(-1)
                # percentile of the specimen only: the black background must not dilute the peak
                fg = v[v > 0.02]
                peak = max(peak, float(imageio.np.percentile(fg if fg.size >= 100 else v,
                                                             settings.calib_percentile)))
            if peak <= 1e-4:
                raise RuntimeError("Test renders are black - check that the specimen is inside the camera view")
            peak_lin = float(imageio.srgb_to_linear(imageio.np.array([peak]))[0])
            target_lin = float(imageio.srgb_to_linear(imageio.np.array([settings.exposure_target]))[0])
            step = target_lin / max(peak_lin, 1e-6)
            if peak >= 0.999:
                step = min(step, 0.5)    # clipped: only "too bright" is known - halve and measure again
            factor *= step
            settings.light_strength = max(0.001, settings.light_strength * step)
            dome.apply_light_strength(settings)
            if peak < 0.999:
                break
    finally:
        (r.resolution_percentage, r.filepath, frame, scene.cycles.samples, r.image_settings.file_format,
         r.image_settings.color_mode, r.image_settings.color_depth, scene.cycles.use_denoising, r.use_border,
         r.use_crop_to_border, r.border_min_x, r.border_max_x, r.border_min_y, r.border_max_y,
         r.use_persistent_data) = saved
        scene.frame_set(frame)
        for fn in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, fn))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    return {"peak": peak, "factor": factor, "strength": settings.light_strength}


class VRTI_OT_calibrate_exposure(bpy.types.Operator):
    """Render the brightest lights and set the light strength so they peak just below clipping"""
    bl_idname = "vrti.calibrate_exposure"
    bl_label = "Calibrate Exposure"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_settings(context).target is not None

    def execute(self, context):
        settings = get_settings(context)
        try:
            res = calibrate_exposure(context, settings)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        msg = f"Exposure set: peak {res['peak']:.3f} → light strength {res['strength']:.3f}"
        if res["peak"] >= 0.999:
            msg += " (clipped - run again)"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class VRTI_OT_open_output(bpy.types.Operator):
    """Open the output folder in the system file browser"""
    bl_idname = "vrti.open_output"
    bl_label = "Open Output Folder"

    def execute(self, context):
        path = dome.renders_dir(get_settings(context))
        os.makedirs(path, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", path])  # noqa: S603,S607
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES = (
    VRTI_OT_import_obj, VRTI_OT_use_selected, VRTI_OT_realign, VRTI_OT_spin, VRTI_OT_keep_pose,
    VRTI_OT_apply_material,
    VRTI_OT_build_dome, VRTI_OT_frame_camera, VRTI_OT_calibrate_exposure, VRTI_OT_open_output,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
