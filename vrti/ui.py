# SPDX-License-Identifier: GPL-3.0-or-later
"""Sidebar UI (3D Viewport > N panel > V-RTI).

The main panel is a three-step guide (model, dome, quality) with one button
that does the rest (exposure, capture, fit). Every other option lives in the
closed "Advanced settings" panels.
"""
from __future__ import annotations

import os
import textwrap

import bpy

from . import align, dome, presets
from .properties import capture_channels, capture_format_label, capture_resolution, get_settings, selected_formats
from .rti.fit import FORMAT_INFO, estimate_disk_bytes
from .rti.writers import file_size_estimate

CATEGORY = "V-RTI"


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _progress(layout, factor: float, text: str):
    layout.progress(factor=max(0.0, min(1.0, factor)), type="BAR", text=text)


def _step(layout, number: int, title: str, done: bool):
    box = layout.box()
    row = box.row()
    row.label(text=f"{number}  ·  {title}", icon="CHECKMARK" if done else "RADIOBUT_OFF")
    return box


def _checks(layout, s, props):
    """Checkboxes in a full-width column (property split would truncate their labels)."""
    col = layout.column(align=True)
    col.use_property_split = False
    for prop, text in props:
        col.prop(s, prop, text=text)
    return col


def _wrap(layout, context, text: str, icon: str = "NONE"):
    """Label text wrapped to the sidebar width."""
    width = max(20, int((context.region.width if context.region else 300) / (8.0 * context.preferences.view.ui_scale)))
    col = layout.column(align=True)
    for i, line in enumerate(textwrap.wrap(text, width)):
        col.label(text=line, icon=icon if i == 0 else "BLANK1" if icon != "NONE" else "NONE")


def _busy(s) -> bool:
    return s.is_rendering or s.is_fitting


# --------------------------------------------------------------------------- #
# main panel
# --------------------------------------------------------------------------- #
class VRTI_PT_main(bpy.types.Panel):
    bl_label = "V-RTI Studio"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY

    def draw(self, context):
        s = get_settings(context)
        layout = self.layout
        if _busy(s):
            self._draw_busy(layout, s)
        col = layout.column()
        col.enabled = not _busy(s)
        self._draw_model(col, s)
        self._draw_dome(col, s)
        self._draw_quality(col, s)
        self._draw_run(col, s)
        self._draw_results(col, s)

    # ---- progress -------------------------------------------------------------
    @staticmethod
    def _draw_busy(layout, s):
        box = layout.box()
        col = box.column(align=True)
        if s.is_rendering:
            col.label(text="Capturing", icon="RENDER_ANIMATION")
            _progress(col, s.render_done / max(1, s.render_total),
                      f"{s.render_done} / {s.render_total} lights")
            col.label(text=s.status_text or "…", icon="TIME")
            col.label(text="ESC in the render window cancels", icon="INFO")
        else:
            col.label(text="Computing RTI", icon="LIGHT_HEMI")
            _progress(col, s.fit_progress, f"{int(s.fit_progress * 100)} %")
            col.label(text=s.status_text or "…", icon="TIME")
            col.label(text="ESC cancels", icon="INFO")

    # ---- step 1 ---------------------------------------------------------------
    @staticmethod
    def _draw_model(layout, s):
        box = _step(layout, 1, "Model", s.target is not None)
        if s.target is None:
            row = box.row()
            row.scale_y = 1.5
            row.operator("vrti.import_obj", icon="IMPORT", text="Import OBJ / MTL")
            box.label(text="Metashape: Export Model ▸ OBJ (+ texture)", icon="INFO")
            box.operator("vrti.use_selected", icon="RESTRICT_SELECT_OFF", text="Use selected object")
            return
        row = box.row(align=True)
        row.label(text=s.target.name, icon="MESH_DATA")
        row.operator("vrti.import_obj", icon="IMPORT", text="")
        colour = (f"{s.info_texels / 1e6:.1f} M texture pixels" if s.info_texels
                  else "vertex colours" if s.info_has_colour else "no colour")
        box.label(text=f"{s.info_faces:,} triangles · {colour}")
        box.label(text=f"{align.nice_length(s.info_size_x)} × {align.nice_length(s.info_size_y)} × "
                       f"{align.nice_length(s.info_size_z)}", icon="FIXED_SIZE")
        box.prop(s, "orient_mode", text="")
        row = box.row(align=True)
        op = row.operator("vrti.spin", text="90°", icon="LOOP_BACK")
        op.angle = 90.0
        op = row.operator("vrti.spin", text="180°", icon="FILE_REFRESH")
        op.angle = 180.0
        op = row.operator("vrti.spin", text="90°", icon="LOOP_FORWARDS")
        op.angle = -90.0
        box.prop(s, "fine_rotation", text="Fine rotation (°)", slider=True)
        if s.orient_mode == "AUTO" and s.info_auto_method in ("text", "outline"):
            what = {"text": "text lines", "outline": "block edges"}[s.info_auto_method]
            box.label(text=f"Straightened {s.info_auto_angle:.1f}° by {what}", icon="CHECKMARK")
        elif s.orient_mode == "AUTO" and s.info_auto_method == "none":
            box.label(text="No clear lines found - use the rotation buttons", icon="INFO")
        elif s.orient_mode == "SOURCE" and s.info_orient_used == "LEVEL":
            box.label(text=f"Tilted {s.info_source_tilt:.0f}° in the file - levelled", icon="ERROR")
        elif s.info_tilt > 2.0:
            box.label(text=f"Surface tilt: {s.info_tilt:.1f}°", icon="INFO")

    # ---- step 2 ---------------------------------------------------------------
    @staticmethod
    def _draw_dome(layout, s):
        box = _step(layout, 2, "Virtual dome", bpy.data.objects.get(dome.CAM_NAME) is not None)
        box.prop(s, "dome_preset", text="")
        rings = dome.rings_for(s)
        n = len(dome.light_directions(s))
        low, high = int(min(el for el, _c in rings)), int(max(el for el, _c in rings))
        box.label(text=f"{n} lights in {len(rings)} rings, elevation {low}°–{high}°", icon="LIGHT_SUN")
        row = box.row(align=True)
        row.prop(s, "show_dome", text="Dome", toggle=True, icon="HIDE_OFF" if s.show_dome else "HIDE_ON")
        row.operator("view3d.view_camera", text="Camera", icon="VIEW_CAMERA")
        if s.dirty_dome and s.target is not None:
            box.operator("vrti.build_dome", icon="FILE_REFRESH", text="Apply changes to the dome")

    # ---- step 3 ---------------------------------------------------------------
    @staticmethod
    def _draw_quality(layout, s):
        box = _step(layout, 3, "Quality and output", bool(selected_formats(s)))
        box.row(align=True).prop(s, "quality_tier", expand=True)
        box.prop(s, "resolution_preset", text="")
        if s.recommended_mp:
            if s.detail_mp > s.recommended_mp:
                box.label(text=f"★ recommended: {s.recommended_mp} MP · full detail ≈ {s.detail_mp} MP")
            else:
                box.label(text=f"★ recommended: {s.recommended_mp} MP")
        split = box.split(factor=0.3)
        split.label(text="RTI files")
        row = split.row(align=True)
        row.prop(s, "fit_ptm_lrgb", text="PTM", toggle=True)
        row.prop(s, "fit_hsh2", text="HSH 9", toggle=True)
        row.prop(s, "fit_hsh3", text="HSH 16", toggle=True)
        n = len(dome.light_directions(s))
        if s.fit_hsh3 and (n < 48 or len(dome.rings_for(s)) < 4):
            box.label(text="HSH 16 needs a dome with 48+ lights and 4+ rings", icon="ERROR")
        if not selected_formats(s):
            box.label(text="Choose at least one RTI file type", icon="ERROR")
        split = box.split(factor=0.3)
        split.label(text="Images")
        row = split.row(align=True)
        row.prop(s, "capture_depth", text="")
        row.prop(s, "capture_colour", text="")
        w, h = capture_resolution(s)
        bpp = presets.capture_bytes_per_pixel(s.capture_depth, capture_channels(s))
        est = estimate_disk_bytes(w, h, selected_formats(s) or ["PTM_LRGB"], n, bpp)
        captures = 0 if s.delete_captures_after_fit else est["captures"]
        box.label(text=f"Disk ≈ {_human_bytes(captures + est['outputs'])}", icon="DISK_DRIVE")
        fill = align.footprint_fill_ratio(s.info_size_x, s.info_size_y, w, h, s.framing_margin)
        grey = capture_channels(s) == "BW"
        web = [(f, presets.openrti_upload_bytes(w, h, f, fill, grey)) for f in selected_formats(s)
               if f in presets.OPENRTI_PLANES]
        if web:
            total = sum(b for _f, b in web)
            share = round(100.0 * total / presets.OPENRTI_QUOTA_BYTES)
            box.label(text=f"OpenRTI ≈ {_human_bytes(total)} · {share} % of quota", icon="URL")
            if w * h > presets.OPENRTI_DEVICE_MAX_MP * 1e6:
                box.label(text="Over 60 MP: OpenRTI converts it only on a computer", icon="ERROR")
        if s.export_jpeg and min(w, h) > 4000:
            box.label(text="RTIBuilder's PTM fitter handles about 4000 px height", icon="INFO")
        col = box.column(align=True)
        if s.capture_depth != "JPEG":
            col.prop(s, "export_jpeg", text="JPEG set (RTIBuilder, relight)")
        col.prop(s, "delete_captures_after_fit",
                 text="Then delete the images" if s.capture_depth == "JPEG" else "Then delete the TIFFs")
        row = box.row(align=True)
        row.prop(s, "output_dir", text="")
        row.operator("vrti.open_output", icon="FOLDER_REDIRECT", text="")
        if " " in dome.renders_dir(s):
            box.label(text="RTIBuilder: the folder path must not contain spaces", icon="ERROR")

    # ---- run --------------------------------------------------------------------
    @staticmethod
    def _draw_run(layout, s):
        col = layout.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.8
        row.enabled = s.target is not None and bool(selected_formats(s) or not s.auto_fit)
        op = row.operator("vrti.capture", icon="RENDER_ANIMATION", text="Create RTI", depress=True)
        op.scale = 100
        row = col.row(align=True)
        row.enabled = s.target is not None
        op = row.operator("vrti.capture", icon="PLAY", text=f"Quick test ({s.test_scale} %)")
        op.scale = s.test_scale
        n = len(dome.light_directions(s))
        w, h = capture_resolution(s)
        layout.label(text=f"{n} images · {w} × {h}", icon="RENDER_STILL")

    # ---- results ---------------------------------------------------------------
    @staticmethod
    def _draw_results(layout, s):
        if not s.last_outputs and not s.status_text:
            return
        box = layout.box()
        if s.status_text and not _busy(s):
            box.label(text=s.status_text, icon="ERROR" if s.last_error else "CHECKMARK")
        outs = [p for p in s.last_outputs.split(";") if p and not p.endswith(".json")]
        for p in outs:
            box.label(text=os.path.basename(p), icon="FILE_IMAGE")
        if outs:
            box.operator("vrti.open_output", icon="FILEBROWSER", text="Open folder")


# --------------------------------------------------------------------------- #
# advanced settings (closed by default)
# --------------------------------------------------------------------------- #
class _Sub:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_options = {"DEFAULT_CLOSED"}


class VRTI_PT_advanced(_Sub, bpy.types.Panel):
    bl_label = "Advanced settings"
    bl_parent_id = "VRTI_PT_main"

    def draw_header(self, context):
        self.layout.label(text="", icon="PREFERENCES")

    def draw(self, context):
        self.layout.label(text="The defaults suit most objects.", icon="INFO")


class _Adv(_Sub):
    bl_parent_id = "VRTI_PT_advanced"
    _icon = "NONE"

    def draw_header(self, context):
        self.layout.label(text="", icon=self._icon)


class VRTI_PT_adv_model(_Adv, bpy.types.Panel):
    bl_label = "Model and material"
    _icon = "MESH_DATA"

    def draw(self, context):
        s = get_settings(context)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "import_up_axis", text="OBJ axes")
        layout.prop(s, "target", text="Model")
        row = layout.row(align=True)
        row.operator("vrti.use_selected", text="Use selected", icon="RESTRICT_SELECT_OFF")
        row.operator("vrti.keep_pose", text="Keep current pose", icon="OBJECT_ORIGIN")
        layout.operator("vrti.realign", text="Re-align", icon="ORIENTATION_GLOBAL")
        layout.separator()
        layout.prop(s, "material_mode", text="Surface")
        sub = layout.column(align=True)
        sub.enabled = s.material_mode == "KEEP"
        sub.prop(s, "roughness", text="Roughness")
        sub.prop(s, "specular_level", text="Specular")
        layout.label(text="Glossy ceramic 0.3 · stone 0.5 · matte clay 0.8")
        layout.operator("vrti.apply_material", text="Apply material", icon="CHECKMARK")


class VRTI_PT_adv_light(_Adv, bpy.types.Panel):
    bl_label = "Light and dome"
    _icon = "LIGHT_HEMI"

    def draw(self, context):
        s = get_settings(context)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "light_model", text="Light")
        layout.prop(s, "light_angle", text="Light size (°)")
        if s.dome_preset == "CUSTOM":
            col = layout.column(align=True)
            col.prop(s, "custom_rings", text="Rings")
            col.prop(s, "custom_first_ring", text="Lights on lowest ring")
            col.prop(s, "custom_el_min", text="Lowest elevation")
            col.prop(s, "custom_el_max", text="Highest elevation")
        _checks(layout, s, (("stagger", "Stagger rings"), ("include_zenith", "Light at the zenith"),
                            ("dome_radius_auto", "Automatic radius")))
        sub = layout.row()
        sub.enabled = not s.dome_radius_auto
        sub.prop(s, "dome_radius", text="Radius")
        layout.prop(s, "led_size", text="LED marker size")
        layout.operator("vrti.build_dome", text="Rebuild dome", icon="FILE_REFRESH")


class VRTI_PT_adv_camera(_Adv, bpy.types.Panel):
    bl_label = "Camera"
    _icon = "CAMERA_DATA"

    def draw(self, context):
        s = get_settings(context)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "camera_projection", text="Projection")
        layout.prop(s, "aspect", text="Aspect")
        layout.prop(s, "orientation", text="Frame")
        layout.prop(s, "framing_margin", text="Margin")
        try:
            fr = dome.compute_framing(s, dome.dome_radius(s))
            if fr["projection"] == "PERSP":
                layout.label(text=f"{fr['lens']:.0f} mm full-frame lens at {align.nice_length(fr['distance'])}",
                             icon="OUTLINER_OB_CAMERA")
        except Exception:  # noqa: BLE001
            pass
        layout.operator("vrti.frame_camera", text="Frame camera", icon="VIEW_CAMERA")


class VRTI_PT_adv_render(_Adv, bpy.types.Panel):
    bl_label = "Render and exposure"
    _icon = "RENDER_STILL"

    def draw(self, context):
        s = get_settings(context)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "engine", text="Engine")
        layout.prop(s, "samples", text="Samples")
        layout.prop(s, "lighting_mode", text="Lighting")
        _checks(layout, s, (("use_gpu", "Use GPU"), ("use_denoise", "Denoise")))
        layout.separator()
        _checks(layout, s, (("auto_exposure", "Automatic exposure"),))
        layout.prop(s, "light_strength", text="Light strength")
        layout.prop(s, "exposure_target", text="Peak")
        layout.prop(s, "calib_percentile", text="Percentile")
        layout.operator("vrti.calibrate_exposure", text="Measure exposure now", icon="SHADING_RENDERED")
        layout.prop(s, "test_scale", text="Test size")


class VRTI_PT_adv_files(_Adv, bpy.types.Panel):
    bl_label = "Files and RTI"
    _icon = "FILE_FOLDER"

    def draw(self, context):
        s = get_settings(context)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "output_name", text="Name")
        layout.label(text="More RTI types")
        row = layout.row(align=True)
        row.prop(s, "fit_ptm_rgb", text="PTM RGB", toggle=True)
        row.prop(s, "fit_hsh1", text="HSH 4", toggle=True)
        layout.prop(s, "fit_colorspace", text="Colour space")
        _checks(layout, s, (("auto_fit", "Compute RTI after capture"), ("write_metadata", "Metadata (JSON)")))
        if s.export_jpeg:
            layout.prop(s, "jpeg_quality", text="JPEG quality")
        layout.prop(s, "block_mem_mb", text="Memory (MB)")
        col = layout.column(align=True)
        col.operator("vrti.fit", icon="LIGHT_HEMI", text="Compute RTI from existing images")
        col.operator("vrti.export_jpeg", icon="EXPORT", text="Export JPEG set")
        col.operator("vrti.delete_captures", icon="TRASH", text="Delete capture images")
        w, h = capture_resolution(s)
        for f in selected_formats(s):
            size = _human_bytes(file_size_estimate(w, h, f))
            layout.label(text=f"{FORMAT_INFO[f]['label']}: ≈ {size}", icon="DOT")
        layout.label(text=capture_format_label(s), icon="IMAGE_DATA")


class VRTI_PT_adv_science(_Adv, bpy.types.Panel):
    bl_label = "Science notes"
    _icon = "HELP"

    def draw(self, context):
        for para in (
            "Each image is one light of a virtual LED dome; the camera sits at the zenith like on a copy stand.",
            "Directional light keeps the light direction constant over the image - the exact assumption of "
            "PTM and HSH.",
            "The .lp file follows RTIBuilder (x right, y up, z towards the camera).",
            "PTM: Malzbender et al. 2001, 6-term biquadratic. HSH: hemispherical harmonics in the CHI hshfitter "
            "layout. Fit: per-pixel SVD least squares, as in RTIBuilder.",
            "8-bit images round by 0.1 %, the same as the 8-bit coefficients inside every PTM/HSH file.",
        ):
            _wrap(self.layout, context, para)


CLASSES = (VRTI_PT_main, VRTI_PT_advanced, VRTI_PT_adv_model, VRTI_PT_adv_light,
           VRTI_PT_adv_camera, VRTI_PT_adv_render, VRTI_PT_adv_files, VRTI_PT_adv_science)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
