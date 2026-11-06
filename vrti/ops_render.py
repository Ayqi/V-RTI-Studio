# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture sequence: animation render with progress tracking, LP + metadata output."""
from __future__ import annotations

import datetime
import json
import math
import os
import platform
import time

import bpy
from bpy.app.handlers import persistent

from . import dome, presets
from .properties import capture_channels, capture_format_label, capture_resolution, get_settings, selected_formats
from .rti import imageio, lp

_STATE = {"active": False, "scene": "", "written": 0, "t0": 0.0, "last": 0.0}


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
def write_capture_metadata(settings, dirs, filenames, framing, path: str, pose: dict = None) -> None:
    rings = dome.rings_for(settings)
    lights = []
    for i, (fn, d) in enumerate(zip(filenames, dirs), start=1):
        el, az = presets.direction_to_angles(d)
        lights.append({"index": i, "file": fn, "x": round(d[0], 6), "y": round(d[1], 6), "z": round(d[2], 6),
                       "elevation_deg": round(el, 2), "azimuth_deg": round(az, 2)})
    meta = {
        "vrti_capture": 1,
        "software": {"name": "V-RTI Studio", "version": bl_version(), "blender": bpy.app.version_string,
                     "engine": settings.engine, "platform": platform.platform()},
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_model": os.path.basename(settings.source_path) if settings.source_path else "",
        "orientation": {"mode": settings.orient_mode, "used": settings.info_orient_used,
                        "auto_straighten_deg": round(settings.info_auto_angle, 2),
                        "auto_straighten_by": settings.info_auto_method,
                        "obj_axes": settings.import_up_axis,
                        "source_tilt_deg": round(settings.info_source_tilt, 3),
                        "spin_deg": (float(settings.target.get("vrti_spin_deg", 0.0))
                                     + float(settings.target.get("vrti_spin_fine", 0.0))) if settings.target else 0.0,
                        # measured on the specimen as it is rendered (includes manual edits)
                        "at_capture": pose or {}},
        "specimen": {"object": settings.target.name if settings.target else "",
                     "faces": settings.info_faces, "texture_texels": settings.info_texels,
                     "size_m": [settings.info_size_x, settings.info_size_y, settings.info_size_z],
                     "material_mode": settings.material_mode, "roughness": settings.roughness,
                     "specular_level": settings.specular_level},
        "preview": {"is_preview": bool(framing.get("is_preview", False)),
                    "scale_percent": framing.get("preview_scale_percent", 100),
                    "nominal_width": framing.get("nominal_width", framing["width"]),
                    "nominal_height": framing.get("nominal_height", framing["height"])},
        "texture_assumption": "photogrammetry colour texture used as albedo (baked lighting not removed)",
        "camera": {"projection": framing["projection"], "focal_length_mm": framing.get("lens"),
                   "ortho_scale": framing.get("ortho_scale"), "sensor_mm": [presets.SENSOR_WIDTH_MM,
                                                                             presets.SENSOR_HEIGHT_MM],
                   "distance_m": framing["distance"], "width": framing["width"], "height": framing["height"],
                   "megapixels": round(presets.megapixels(framing["width"], framing["height"]), 2),
                   "image_x_axis": "+X world (right)", "image_y_axis": "+Y world (up)"},
        "dome": {"preset": settings.dome_preset, "radius_m": dome.dome_radius(settings),
                 "light_model": settings.light_model, "light_strength": settings.light_strength,
                 "light_angle_deg": settings.light_angle,
                 "rings": [{"elevation_deg": el, "count": cnt} for el, cnt in rings],
                 "stagger": settings.stagger, "zenith": settings.include_zenith, "count": len(dirs)},
        "render": {"samples": settings.samples, "lighting": settings.lighting_mode,
                   "denoise": settings.use_denoise, "view_transform": "Standard",
                   "output": f"{capture_format_label(settings)} (display-referred sRGB"
                             f"{'' if settings.capture_depth == 'JPEG' else ', deflate'})",
                   "quality_tier": settings.quality_tier,
                   "bit_depth": 8 if settings.capture_depth == "JPEG" else int(settings.capture_depth),
                   "format": "JPEG" if settings.capture_depth == "JPEG" else "TIFF",
                   "jpeg_quality": settings.jpeg_quality if settings.capture_depth == "JPEG" else None,
                   "channels": capture_channels(settings),
                   "pixel_filter": "default"},
        "lp_convention": "unit vector surface->light; x right, y up (image), z towards camera",
        "lights": lights,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def _redraw_all():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001
        pass


def _fmt_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 60}m {seconds % 60:02d}s"


@persistent
def _on_render_init(scene, _depsgraph=None):
    if not _STATE["active"]:
        return
    _STATE["t0"] = time.time()
    _STATE["last"] = _STATE["t0"]


@persistent
def _on_render_write(scene, _depsgraph=None):
    if not _STATE["active"]:
        return
    settings = scene.vrti
    _STATE["written"] += 1
    now = time.time()
    settings.render_done = _STATE["written"]
    per = (now - _STATE["t0"]) / max(1, _STATE["written"])
    remaining = per * max(0, settings.render_total - _STATE["written"])
    settings.status_text = (f"Rendering {_STATE['written']} / {settings.render_total}  ·  "
                            f"{per:.1f}s per image  ·  {_fmt_eta(remaining)} left")
    _STATE["last"] = now
    _redraw_all()


@persistent
def _on_render_complete(scene, _depsgraph=None):
    if not _STATE["active"]:
        return
    settings = scene.vrti
    _finish(settings, cancelled=False)


@persistent
def _on_render_cancel(scene, _depsgraph=None):
    if not _STATE["active"]:
        return
    _finish(scene.vrti, cancelled=True)


def _install_handlers():
    pairs = ((bpy.app.handlers.render_init, _on_render_init),
             (bpy.app.handlers.render_write, _on_render_write),
             (bpy.app.handlers.render_complete, _on_render_complete),
             (bpy.app.handlers.render_cancel, _on_render_cancel))
    for lst, fn in pairs:
        if fn not in lst:
            lst.append(fn)


def _remove_handlers():
    pairs = ((bpy.app.handlers.render_init, _on_render_init),
             (bpy.app.handlers.render_write, _on_render_write),
             (bpy.app.handlers.render_complete, _on_render_complete),
             (bpy.app.handlers.render_cancel, _on_render_cancel))
    for lst, fn in pairs:
        while fn in lst:
            lst.remove(fn)


def write_capture_records(settings) -> None:
    """Write the .lp file(s) and the capture metadata for a finished capture."""
    p = _STATE.pop("pending_write", None)
    if not p:
        return
    if settings.capture_depth == "JPEG":
        write_jpeg_lps(settings, p["cap_dir"], p["filenames"], p["dirs"])
    else:
        lp.write_lp(p["lp"], p["filenames"], p["dirs"])   # file names only: relight / RelightLab style
    if settings.write_metadata:
        write_capture_metadata(settings, p["dirs"], p["filenames"], p["framing"],
                               os.path.join(p["out_dir"], f"{settings.output_name}_capture.json"), p["pose"])


def _finish(settings, cancelled: bool):
    _STATE["active"] = False
    _remove_handlers()
    settings.is_rendering = False
    elapsed = time.time() - _STATE["t0"] if _STATE["t0"] else 0.0
    if cancelled:
        _STATE.pop("pending_write", None)
        settings.status_text = f"Capture cancelled after {settings.render_done} images"
        _redraw_all()
        return
    write_capture_records(settings)
    settings.status_text = f"Capture finished: {settings.render_done} images in {_fmt_eta(elapsed)}"
    _redraw_all()
    follow = []
    if settings.export_jpeg and settings.capture_depth != "JPEG":   # JPEG captures already are the set
        follow.append("jpeg")
    if settings.auto_fit and selected_formats(settings):
        follow.append("fit")
    if not follow:
        return
    # captures are only removed after an automatic fit that succeeded
    settings.pending_cleanup = "fit" in follow and settings.delete_captures_after_fit
    if bpy.app.background:
        from . import ops_fit
        if "fit" in follow:
            ops_fit.run_fit_blocking(settings)
        if "jpeg" in follow:
            export_jpeg_set(settings)
        run_pending_cleanup(settings)
        return

    # interactive: both follow-ups are modal operators, so run them one after the
    # other - the fit operator starts the JPEG export when ``pending_jpeg`` is set.
    settings.pending_jpeg = "jpeg" in follow
    first = "vrti.fit" if "fit" in follow else "vrti.export_jpeg"
    if first == "vrti.export_jpeg":
        settings.pending_jpeg = False
    invoke_operator_later(first, settings)


def capture_files(settings) -> list:
    """Existing capture images of the current output (only files listed in its .lp)."""
    lp_path = dome.capture_lp_path(settings)
    if not os.path.isfile(lp_path):
        return []
    names, _dirs = lp.read_lp(lp_path)
    # only image files inside the capture folder are ever deleted (JPEG copies are kept)
    cap_dir = os.path.normcase(os.path.dirname(os.path.abspath(lp.resolve_lp_paths(lp_path, names[:1])[0]))
                               if names else "")
    allowed = {os.path.normcase(os.path.abspath(dome.project_path(settings, d)))
               for d in (dome.DIR_CAPTURES, dome.LEGACY_CAPTURES, dome.DIR_JPEG)}
    if cap_dir not in allowed:
        return []                                  # never delete outside the project's capture folder
    out = []
    for p in lp.resolve_lp_paths(lp_path, names):
        p = os.path.abspath(p)
        if os.path.normcase(os.path.dirname(p)) == cap_dir and os.path.isfile(p):
            out.append(p)
    return out


def delete_captures(settings) -> tuple:
    """Delete the capture images of the current output. Returns (count, bytes)."""
    n, size = 0, 0
    for p in capture_files(settings):
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            n += 1
            size += sz
        except OSError:
            pass
    return n, size


def run_pending_cleanup(settings) -> None:
    """Delete captures when requested and the fit wrote every selected format."""
    if not settings.pending_cleanup:
        return
    settings.pending_cleanup = False
    outs = [p for p in settings.last_outputs.split(";") if p and not p.endswith(".json")]
    if len(outs) < len(selected_formats(settings)) or not all(os.path.isfile(p) for p in outs):
        settings.status_text = "Images kept: not every RTI file was written"
        return
    n, size = delete_captures(settings)
    settings.status_text = f"RTI ready · {n} images deleted ({size / 1e9:.2f} GB freed)"
    _redraw_all()


def invoke_operator_later(op_idname: str, settings, delay: float = 0.5):
    """Invoke a modal operator from a handler/timer context with a valid window."""

    def _later():
        try:
            wm = bpy.context.window_manager
            win = wm.windows[0] if wm.windows else None
            if win is None:
                return None
            mod, name = op_idname.split(".")
            op = getattr(getattr(bpy.ops, mod), name)
            with bpy.context.temp_override(window=win, screen=win.screen, scene=bpy.context.scene):
                op("INVOKE_DEFAULT")
        except Exception as exc:  # noqa: BLE001
            settings.last_error = str(exc)
            settings.status_text = f"Could not start {op_idname}: {exc}"
        return None

    bpy.app.timers.register(_later, first_interval=delay)


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #
def prepare_capture(context, settings) -> dict:
    """Configure scene + write .lp and metadata. Returns dict with paths."""
    scene = context.scene
    if settings.target is None:
        raise RuntimeError("No specimen - import an OBJ first")
    if settings.dirty_dome:
        from .ops_import import analyze_and_store
        analyze_and_store(context, settings, settings.target)
    # Always rebuild the light animation from the current layout: a scene saved with an
    # older layout (e.g. before a preset changed) would otherwise render other light
    # directions than the ones written to the .lp file.
    dome.build_dome(context, settings)
    tier = presets.QUALITY_TIERS.get(settings.quality_tier)
    if tier and settings.capture_depth != tier["depth"]:
        # scene saved by V-RTI <= 1.4 (its Web/Quick tiers stored TIFF): follow the tier.
        # A TIFF chosen by hand switches the tier to Custom, so it is never overridden.
        settings["capture_depth"] = {"8": 0, "16": 1, "JPEG": 2}[tier["depth"]]
    w, h = capture_resolution(settings)
    framing = dome.frame_camera(settings)
    dome.apply_light_strength(settings)
    prefix = dome.configure_render(scene, settings, w, h)
    scale = int(settings.render_scale)
    if scale != 100:
        # record what is really rendered, not the nominal ladder size
        framing = dict(framing, width=w * scale // 100, height=h * scale // 100,
                       nominal_width=w, nominal_height=h, preview_scale_percent=scale, is_preview=True)
    dirs = dome.light_directions(settings)
    keyed = dome.keyed_light_directions(len(dirs))
    worst = max((math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(k, d))))))
                 for k, d in zip(keyed, dirs)), default=180.0)
    if len(keyed) != len(dirs) or worst > 0.1:       # keyframe float rounding stays ~0.02°
        raise RuntimeError(f"Light animation does not match the light positions ({worst:.2f}°) - "
                           "rebuild the dome and try again")
    filenames = dome.capture_filenames(settings)
    out_dir = dome.renders_dir(settings)
    subs = [dome.DIR_JPEG, dome.DIR_ASSEMBLY, dome.DIR_FINISHED]
    if settings.capture_depth != "JPEG":
        subs.append(dome.DIR_CAPTURES)
    for sub in subs:
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
    write_project_readme(out_dir, settings)
    try:
        import shutil
        free = shutil.disk_usage(out_dir).free
        bpp = presets.capture_bytes_per_pixel(settings.capture_depth, capture_channels(settings))
        need = int(framing["width"] * framing["height"] * bpp) * len(dirs)
        disk_warning = (f"Only {free / 1e9:.1f} GB free on the output drive, captures need about "
                        f"{need / 1e9:.1f} GB") if free < need * 1.2 else ""
    except Exception:  # noqa: BLE001
        disk_warning = ""
    cap_dir = os.path.join(out_dir, dome.capture_dir_name(settings))
    lp_path = os.path.join(cap_dir, f"{settings.output_name}.lp")
    pose = None
    if settings.write_metadata:
        from .align import pose_report
        pose = pose_report(settings.target)
    # .lp and metadata describe the images: they are written when the capture has
    # finished, so a cancelled capture or preview never relabels an existing set
    _STATE["pending_write"] = {"cap_dir": cap_dir, "lp": lp_path, "filenames": filenames, "dirs": dirs,
                               "framing": framing, "pose": pose, "out_dir": out_dir}
    scene.frame_start = 1
    scene.frame_end = len(dirs)
    return {"prefix": prefix, "lp": lp_path, "count": len(dirs), "width": framing["width"],
            "height": framing["height"], "disk_warning": disk_warning, "preview": scale != 100}


class VRTI_OT_capture(bpy.types.Operator):
    """Render the whole light sequence (one frame per light) and write the .lp file"""
    bl_idname = "vrti.capture"
    bl_label = "Start Capture"
    bl_options = {"REGISTER"}

    scale: bpy.props.IntProperty(
        name="Scale %", default=0, min=0, max=100,
        description="Image size in percent of the chosen size (0 = use the scene setting)")

    @classmethod
    def poll(cls, context):
        s = get_settings(context)
        return s.target is not None and not s.is_rendering and not s.is_fitting

    @classmethod
    def description(cls, context, properties):
        if properties.scale and properties.scale < 100:
            return "A small, fast capture to check framing and light before the real run"
        return "Exposure, capture and RTI computation in one step"

    def execute(self, context):
        settings = get_settings(context)
        if self.scale:
            settings.render_scale = self.scale
        settings.last_error = ""
        settings.last_outputs = ""
        if settings.auto_exposure:
            from .ops_import import calibrate_exposure
            try:
                cal = calibrate_exposure(context, settings)
                self.report({"INFO"}, f"Exposure set: peak {cal['peak']:.3f} → "
                                      f"light strength {cal['strength']:.3f}")
            except (RuntimeError, OSError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
        try:
            info = prepare_capture(context, settings)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if info["disk_warning"]:
            self.report({"WARNING"}, info["disk_warning"])
        if info["preview"]:
            self.report({"WARNING"}, f"Preview at {settings.render_scale} % - use 'Create RTI' "
                                     "for the final data set")
        _STATE.update(active=True, scene=context.scene.name, written=0, t0=time.time(), last=time.time())
        settings.is_rendering = True
        settings.render_done = 0
        settings.render_total = info["count"]
        settings.render_started = time.time()
        settings.last_error = ""
        settings.status_text = f"Rendering 0 / {info['count']}  ·  {info['width']} x {info['height']}"
        _install_handlers()
        if bpy.app.background:
            bpy.ops.render.render(animation=True)
        else:
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
        return {"FINISHED"}


def write_jpeg_lps(settings, jdir: str, jnames, dirs) -> str:
    """.lp files for the JPEG set: next to the images (relight-cli, RelightLab: file names,
    matched in sorted order) and in assembly-files (RTIBuilder: absolute paths)."""
    jlp = os.path.join(jdir, f"{settings.output_name}.lp")
    lp.write_lp(jlp, jnames, dirs)
    adir = dome.project_path(settings, dome.DIR_ASSEMBLY)
    os.makedirs(adir, exist_ok=True)
    lp.write_lp(os.path.join(adir, f"{settings.output_name}.lp"),
                [os.path.abspath(os.path.join(jdir, n)) for n in jnames], dirs)
    return jlp


def write_openrti_suggestions(settings, outputs) -> str:
    """Pre-filled OpenRTI record fields (openrti.org/contribute) for the finished RTI files."""
    import json
    finished = dome.project_path(settings, dome.DIR_FINISHED)
    if not os.path.isdir(finished):
        return ""
    files = [os.path.basename(p) for p in outputs if p.endswith((".ptm", ".rti"))]
    w, h = capture_resolution(settings)
    scale = int(settings.render_scale) / 100.0
    px_w = max(1, int(max(w, h) * scale))     # the 36 mm sensor side is the long image side
    mm_per_px = None
    try:
        fr = dome.compute_framing(settings, dome.dome_radius(settings))
        if fr.get("projection") == "ORTHO":
            field = fr["ortho_scale"]
        else:
            field = presets.SENSOR_WIDTH_MM * fr["distance"] / fr["lens"]
        mm_per_px = round(field * 1000.0 / px_w, 5)
    except Exception:  # noqa: BLE001
        pass
    n = len(dome.light_directions(settings))
    hsh = [f for f in ("HSH1", "HSH2", "HSH3") if f in selected_formats(settings)]
    data = {
        "openrti_record_suggestions": 1,
        "upload_these_files": [f for f in files if not f.endswith("_rgb.ptm")],
        "not_accepted_by_openrti": [f for f in files if f.endswith("_rgb.ptm")],
        "capture_method": "virtual-rti",
        "equipment": (f"Virtual full-frame camera 36 x 24 mm, {int(w * scale)} x {int(h * scale)} px, "
                      f"virtual dome {n} lights ({settings.light_model.lower()} light) - V-RTI Studio"),
        "number_of_photographs": n,
        "software_and_settings": (f"V-RTI Studio {bl_version()}, Blender {bpy.app.version_string}, "
                                  f"{settings.engine.title()} {settings.samples} samples, "
                                  f"{'direct light' if settings.lighting_mode == 'DIRECT' else 'global illumination'}, "
                                  f"{capture_format_label(settings)} captures, fit "
                                  + ", ".join(["PTM-LRGB"] * settings.fit_ptm_lrgb + [
                                      {"HSH1": "HSH order 1", "HSH2": "HSH order 2", "HSH3": "HSH order 3"}[f]
                                      for f in hsh])),
        "source_model": os.path.basename(settings.source_path) if settings.source_path else "",
        "changes_before_publication": "none (rendered at full size, no crop)",
        "scale_mm_per_px_if_model_in_metres": mm_per_px,
        "note": "Virtual RTI of a photogrammetric model: no detail finer than the mesh and texture can appear. "
                "Fill in object, creator, rights and licence yourself.",
    }
    path = os.path.join(finished, f"{settings.output_name}_openrti.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return path


def bl_version() -> str:
    from .version import VERSION_STRING  # not bl_info: Blender strips it from extensions
    return VERSION_STRING


def write_project_readme(out_dir: str, settings) -> None:
    text = f"""V-RTI Studio project: {settings.output_name}
Folder layout follows CHI RTIBuilder, so the images can also be fitted with other tools.

  original-captures/  lossless TIFF renders when "TIFF" was chosen (+ .lp, file names)
                      -> RelightLab reads TIFF directly
  jpeg-exports/       JPEG images (captures or copies) + {settings.output_name}.lp (file names, sorted order)
                      -> relight-cli:  relight-cli -b hsh -p 27 jpeg-exports out.rti
                      -> RTIBuilder:   project folder = this folder ("Dome LP File" / LP operation)
  assembly-files/     {settings.output_name}.lp with absolute paths to jpeg-exports (RTIBuilder style)
  finished-files/     PTM (.ptm) and HSH (.rti) files, fit report, OpenRTI record suggestions
  {settings.output_name}_capture.json   full provenance (camera, dome, lights, orientation, software)

Light vectors: unit vector from the surface to the light; x right, y up (image), z towards the camera.
RTIBuilder needs a path without spaces.
"""
    try:
        with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def export_jpeg_set(settings, progress_cb=None) -> str:
    """Blocking JPEG export (background mode). Returns the jpeg .lp path."""
    cap_lp = dome.capture_lp_path(settings)
    names, dirs = lp.read_lp(cap_lp)
    src = lp.resolve_lp_paths(cap_lp, names)
    jdir = dome.project_path(settings, dome.DIR_JPEG)
    os.makedirs(jdir, exist_ok=True)
    jnames = []
    for i, s in enumerate(src):
        jn = os.path.splitext(os.path.basename(s))[0] + ".jpg"
        imageio.convert_to_jpeg(s, os.path.join(jdir, jn), settings.jpeg_quality)
        jnames.append(jn)
        if progress_cb:
            progress_cb((i + 1) / len(src))
    return write_jpeg_lps(settings, jdir, jnames, dirs)


class VRTI_OT_export_jpeg(bpy.types.Operator):
    """Write 8-bit JPEG copies of the captures plus an .lp file for RTIBuilder / relight"""
    bl_idname = "vrti.export_jpeg"
    bl_label = "Export JPEG Set (RTIBuilder)"
    bl_options = {"REGISTER"}

    _timer = None
    _items = None
    _index = 0

    @classmethod
    def poll(cls, context):
        s = get_settings(context)
        return not s.is_rendering and not s.is_fitting

    def invoke(self, context, event):
        settings = get_settings(context)
        lp_path = dome.capture_lp_path(settings)
        if not os.path.isfile(lp_path):
            self.report({"ERROR"}, f"No capture found ({lp_path})")
            return {"CANCELLED"}
        names, dirs = lp.read_lp(lp_path)
        src = lp.resolve_lp_paths(lp_path, names)
        missing = [p for p in src if not os.path.isfile(p)]
        if missing:
            self.report({"ERROR"}, f"{len(missing)} capture files are missing (first: {missing[0]})")
            return {"CANCELLED"}
        self._jdir = dome.project_path(settings, dome.DIR_JPEG)
        os.makedirs(self._jdir, exist_ok=True)
        self._items = list(zip(src, dirs))
        self._jnames = []
        self._index = 0
        settings.is_fitting = True
        settings.fit_progress = 0.0
        settings.status_text = "Exporting JPEG set…"
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.02, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        settings = get_settings(context)
        if event.type == "ESC":
            return self._done(context, cancelled=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._index < len(self._items):
            s, _d = self._items[self._index]
            jn = os.path.splitext(os.path.basename(s))[0] + ".jpg"
            try:
                imageio.convert_to_jpeg(s, os.path.join(self._jdir, jn), settings.jpeg_quality)
            except Exception as exc:  # noqa: BLE001
                self.report({"ERROR"}, f"JPEG export failed: {exc}")
                return self._done(context, cancelled=True)
            self._jnames.append(jn)
            self._index += 1
            settings.fit_progress = self._index / len(self._items)
            settings.status_text = f"JPEG {self._index} / {len(self._items)}"
            _redraw_all()
            return {"RUNNING_MODAL"}
        write_jpeg_lps(settings, self._jdir, self._jnames, [d for _s, d in self._items])
        self.report({"INFO"}, f"JPEG set written to {self._jdir}")
        return self._done(context, cancelled=False)

    def _done(self, context, cancelled):
        settings = get_settings(context)
        context.window_manager.event_timer_remove(self._timer)
        settings.is_fitting = False
        settings.status_text = "JPEG export cancelled" if cancelled else "JPEG set ready"
        if cancelled:
            settings.pending_cleanup = False
        else:
            run_pending_cleanup(settings)
        _redraw_all()
        return {"CANCELLED"} if cancelled else {"FINISHED"}


class VRTI_OT_delete_captures(bpy.types.Operator):
    """Delete the capture images of this output to free disk space (RTI, .lp and metadata are kept)"""
    bl_idname = "vrti.delete_captures"
    bl_label = "Delete Capture Images"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        s = get_settings(context)
        return not s.is_rendering and not s.is_fitting

    def invoke(self, context, event):
        files = capture_files(get_settings(context))
        if not files:
            self.report({"INFO"}, "No capture images to delete")
            return {"CANCELLED"}
        self._size = sum(os.path.getsize(p) for p in files)
        return context.window_manager.invoke_confirm(
            self, event, title="Delete capture images?",
            message=f"{len(files)} files, {self._size / 1e9:.2f} GB. Re-fitting will need a new capture.",
            confirm_text="Delete", icon="WARNING")

    def execute(self, context):
        n, size = delete_captures(get_settings(context))
        self.report({"INFO"}, f"Deleted {n} capture images ({size / 1e9:.2f} GB)")
        return {"FINISHED"}


CLASSES = (VRTI_OT_capture, VRTI_OT_export_jpeg, VRTI_OT_delete_captures)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    _remove_handlers()
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
