# SPDX-License-Identifier: GPL-3.0-or-later
"""PTM / HSH fitting operator (modal, keeps the UI responsive)."""
from __future__ import annotations

import json
import os
import time

import bpy

from . import dome
from .properties import get_settings, selected_formats
from .rti import lp
from .rti.fit import FitJob, FitRequest


def build_fit_request(settings) -> FitRequest:
    out_dir = dome.renders_dir(settings)
    lp_path = dome.capture_lp_path(settings)
    if not os.path.isfile(lp_path):
        raise FileNotFoundError(f"Light position file not found: {lp_path}")
    names, dirs = lp.read_lp(lp_path)
    paths = lp.resolve_lp_paths(lp_path, names)
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(f"{len(missing)} capture image(s) missing, e.g. {os.path.basename(missing[0])}")
    formats = selected_formats(settings)
    if not formats:
        raise ValueError("Select at least one output format (PTM / HSH)")
    meta = {}
    meta_path = os.path.join(out_dir, f"{settings.output_name}_capture.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = {"capture": json.load(fh)}
        except Exception:  # noqa: BLE001
            meta = {}
    finished = os.path.join(out_dir, dome.DIR_FINISHED)
    if os.path.dirname(os.path.abspath(lp_path)) == os.path.abspath(out_dir):
        finished = out_dir                         # project made with V-RTI <= 1.2
    return FitRequest(image_paths=paths, directions=dirs, formats=formats, out_dir=finished,
                      base_name=settings.output_name, colorspace=settings.fit_colorspace,
                      block_mem_mb=settings.block_mem_mb, extra_metadata=meta)


def run_fit_blocking(settings, callback=None) -> FitJob:
    job = FitJob(build_fit_request(settings))
    settings.is_fitting = True
    try:
        job.run(callback)
    finally:
        settings.is_fitting = False
    from .ops_render import write_openrti_suggestions
    extra = write_openrti_suggestions(settings, job.outputs)
    settings.last_outputs = ";".join(job.outputs + ([extra] if extra else []))
    settings.fit_progress = 1.0
    names = ", ".join(os.path.basename(p) for p in job.outputs if not p.endswith(".json"))
    settings.status_text = f"RTI ready: {names}"
    return job


def _redraw_all():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001
        pass


class VRTI_OT_fit(bpy.types.Operator):
    """Fit PTM / HSH models to the rendered capture set and write .ptm / .rti files"""
    bl_idname = "vrti.fit"
    bl_label = "Fit RTI from Renders"
    bl_options = {"REGISTER"}

    _timer = None
    _job: FitJob = None

    @classmethod
    def poll(cls, context):
        s = get_settings(context)
        return not s.is_rendering and not s.is_fitting

    def invoke(self, context, event):
        settings = get_settings(context)
        try:
            self._job = FitJob(build_fit_request(settings))
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.is_fitting = True
        settings.fit_progress = 0.0
        settings.last_error = ""
        settings.status_text = "Preparing fit…"
        self._t0 = time.time()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        # non-interactive path (scripts / background)
        settings = get_settings(context)
        try:
            job = run_fit_blocking(settings)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Wrote {len(job.outputs)} file(s)")
        return {"FINISHED"}

    def modal(self, context, event):
        settings = get_settings(context)
        if event.type == "ESC":
            self._job.cancel()
            return self._finish(context, "Fit cancelled", cancelled=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        budget_end = time.time() + 0.15
        try:
            done = False
            while time.time() < budget_end and not done:
                done = self._job.step()
        except Exception as exc:  # noqa: BLE001
            settings.last_error = str(exc)
            self.report({"ERROR"}, f"Fit failed: {exc}")
            return self._finish(context, f"Fit failed: {exc}", cancelled=True)
        settings.fit_progress = self._job.progress
        elapsed = time.time() - self._t0
        eta = elapsed / max(1e-3, self._job.progress) - elapsed if self._job.progress > 0.02 else 0.0
        settings.status_text = f"{self._job.message}  ·  {int(self._job.progress * 100)} %" + \
            (f"  ·  ~{int(eta // 60)}m {int(eta % 60):02d}s remaining" if eta > 0 else "")
        _redraw_all()
        if done:
            from .ops_render import write_openrti_suggestions
            extra = write_openrti_suggestions(settings, self._job.outputs)
            settings.last_outputs = ";".join(self._job.outputs + ([extra] if extra else []))
            names = ", ".join(os.path.basename(p) for p in self._job.outputs if not p.endswith(".json"))
            rep = self._job.report
            quality = "  ".join(f"{k}: {v['fit_psnr_db']:.1f} dB" for k, v in rep.items()
                                if not k.startswith("_"))
            self.report({"INFO"}, f"RTI written: {names}   [{quality}]")
            return self._finish(context, f"RTI ready: {names}", cancelled=False)
        return {"RUNNING_MODAL"}

    def _finish(self, context, text, cancelled):
        settings = get_settings(context)
        context.window_manager.event_timer_remove(self._timer)
        settings.is_fitting = False
        settings.fit_progress = 0.0 if cancelled else 1.0
        settings.status_text = text
        _redraw_all()
        from .ops_render import invoke_operator_later, run_pending_cleanup
        if cancelled:
            settings.pending_cleanup = False
        if settings.pending_jpeg:
            settings.pending_jpeg = False
            if not cancelled:
                invoke_operator_later("vrti.export_jpeg", settings)  # cleans up when done
                return {"FINISHED"}
        if not cancelled:
            run_pending_cleanup(settings)
        return {"CANCELLED"} if cancelled else {"FINISHED"}


CLASSES = (VRTI_OT_fit,)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
