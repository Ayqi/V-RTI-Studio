# SPDX-License-Identifier: GPL-3.0-or-later
"""
V-RTI Studio — Virtual Reflectance Transformation Imaging for Blender.

Turns a photogrammetric model (OBJ + MTL, e.g. exported from Agisoft
Metashape) into scientifically faithful RTI data sets:

* a virtual LED dome (directional "sun" lights or physical LEDs on a sphere)
* a full-frame style zenith camera that frames the specimen automatically
* a multi-light capture sequence (JPEG or lossless TIFF, Standard sRGB
  transform, like the JPEG/TIFF output of a real camera)
* a Light Position (.lp) file compatible with RTIBuilder / relight
* PTM (LRGB / RGB, Malzbender 2001) and HSH (.rti, orders 1-3) fitting with
  bit-exact file formats readable by RTIViewer, relight and web viewers.
"""

bl_info = {
    "name": "V-RTI Studio",
    "author": "A. Akçay",
    "version": (1, 6, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > V-RTI",
    "description": "Virtual RTI (PTM / HSH) capture from photogrammetry models",
    "category": "Render",
    "doc_url": "https://github.com/Ayqi/V-RTI-Studio",
}

try:
    import bpy  # noqa: F401
except ImportError:  # vrti.rti and vrti.presets also work in plain Python (tools, tests)
    bpy = None

if bpy is not None:
    from . import ops_fit, ops_import, ops_render, properties, ui

    _MODULES = (properties, ops_import, ops_render, ops_fit, ui)

    def register():
        for module in _MODULES:
            module.register()

    def unregister():
        for module in reversed(_MODULES):
            module.unregister()
