# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end run of the add-on as an INSTALLED extension (bl_ext.user_default.vrti_studio).

Blender loads extensions differently from a plain package (e.g. it removes bl_info), so
this test needs the built zip installed in an isolated user profile. The runner does that:

    python scripts/run_tests.py --blender <path to blender> --only installed
"""
import json
import os
import shutil
import sys
import tempfile

import addon_utils
import bpy

mod = "bl_ext.user_default.vrti_studio"
addon_utils.enable(mod, default_set=True)
pkg = sys.modules[mod]
print("INSTALLED has bl_info:", hasattr(pkg, "bl_info"))
work = tempfile.mkdtemp(prefix="vrti_inst_")
# small textured test object exported as Metashape-like OBJ
for _o in list(bpy.data.objects): bpy.data.objects.remove(_o)
bpy.ops.mesh.primitive_grid_add(x_subdivisions=120, y_subdivisions=80, size=1.0)
o = bpy.context.active_object; o.scale = (0.3, 0.2, 1); bpy.ops.object.transform_apply(scale=True)
import numpy as np

me = o.data; co = np.empty(len(me.vertices) * 3, np.float32); me.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
co[:, 2] = 0.003 * np.sin(co[:, 0] * 150) * (np.abs(co[:, 1]) < 0.06); me.vertices.foreach_set("co", co.ravel()); me.update()
o.rotation_euler = (0, 0, 0.1); bpy.context.view_layer.update()
obj_path = os.path.join(work, "inst_test.obj")
bpy.ops.wm.obj_export(filepath=obj_path, export_materials=False, forward_axis="Y", up_axis="Z")
bpy.ops.object.delete()
s = bpy.context.scene.vrti
s.dome_preset = "D36"; s.samples = 8; s.use_gpu = False; s.export_jpeg = True
assert not hasattr(pkg, "bl_info"), "expected Blender to strip bl_info from the extension"
assert bpy.ops.vrti.import_obj(filepath=obj_path) == {"FINISHED"}
print("straightened", s.info_auto_method, round(s.info_auto_angle, 2))
assert abs(s.info_auto_angle + 5.73) < 0.3
s.output_dir = os.path.join(work, "out") + os.sep
assert bpy.ops.vrti.capture(scale=4) == {"FINISHED"}
out = os.path.join(work, "out")
for root, dirs, files in os.walk(out):
    rel = os.path.relpath(root, out)
    print("TREE", rel, len(files), sorted(files)[:3])
cap = json.load(open(os.path.join(out, "inst_test_capture.json"), encoding="utf-8"))
print("META version", cap["software"]["version"], "| status:", s.status_text, "| error:", s.last_error)
print("OUTPUTS", [os.path.basename(p) for p in s.last_outputs.split(";") if p])
assert not s.last_error, s.last_error
for need in ("finished-files/inst_test_lrgb.ptm", "finished-files/inst_test_hsh9.rti", "jpeg-exports/inst_test.lp",
             "assembly-files/inst_test.lp", "jpeg-exports/inst_test_0001.jpg", "finished-files/inst_test_openrti.json"):
    assert os.path.isfile(os.path.join(out, need)), need
assert not os.path.isdir(os.path.join(out, "original-captures")), "JPEG captures need no TIFF folder"
assert cap["software"]["version"] == pkg.version.VERSION_STRING
shutil.rmtree(work, ignore_errors=True)
print("INSTALLED EXTENSION TEST PASSED")
