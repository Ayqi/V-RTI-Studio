# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-on version. Blender removes ``bl_info`` from extension modules at load time,
so code that needs the version reads it from here (keep in sync with
blender_manifest.toml and bl_info)."""
VERSION = (1, 6, 0)
VERSION_STRING = ".".join(str(v) for v in VERSION)
