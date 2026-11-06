# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene-level settings (``scene.vrti``) and runtime status."""
from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import presets

_RES_ITEMS_CACHE = []  # keep strings alive for dynamic enum items


def _aspect_value(settings) -> float:
    if settings.aspect == "FIT":
        return presets.fit_aspect(settings.info_size_x, settings.info_size_y)
    return presets.ASPECTS.get(settings.aspect, (1.5, ""))[0]


def resolution_items(self, context):
    global _RES_ITEMS_CACHE
    aspect = _aspect_value(self)
    rec = int(self.recommended_mp)
    items = []
    for mp in presets.RESOLUTION_MP:
        w, h = presets.resolution_for(mp, aspect)
        if self.orientation == "PORTRAIT":
            w, h = h, w
        star = "  ★" if mp == rec else ""
        name = f"{mp} MP  ·  {w} × {h}{star}"
        desc = (f"{w} x {h} px ({presets.megapixels(w, h):.1f} MP). "
                + ("Recommended: keeps the visible detail of the model within the full-frame "
                   "H-RTI range." if mp == rec else "Full-frame style capture size."))
        items.append((f"MP{mp}", name, desc, "NONE", mp))
    _RES_ITEMS_CACHE = items
    return items


def _mesh_poll(self, obj):
    return obj is not None and obj.type == "MESH"


def _on_geometry_change(self, context):
    """Dome / camera depend on these; rebuild lazily (operators handle it)."""
    self.dirty_dome = True


def _on_show_dome(self, context):
    visual = bpy.data.collections.get("V-RTI Dome (visual)")
    if visual is not None:
        visual.hide_viewport = not self.show_dome


def _on_orient_change(self, context):
    """Re-run the alignment from the imported pose with the new orientation mode."""
    if self.target is None:
        return
    from . import ops_import  # local import: ops_import depends on this module
    try:
        ops_import.realign_specimen(context, self)
    except Exception as exc:  # noqa: BLE001
        self.last_error = str(exc)
        self.dirty_dome = True


def _on_fine_rotation(self, context):
    """Small in-plane turn (e.g. to make text lines horizontal), kept on the object."""
    if self.target is None:
        return
    self.target["vrti_spin_fine"] = float(self.fine_rotation)
    _on_orient_change(self, context)


_APPLYING_TIER = [False]


def apply_quality_tier(settings, tier: str) -> None:
    """Set size, RTI types and bit depth for a quality tier."""
    t = presets.QUALITY_TIERS[tier]
    _APPLYING_TIER[0] = True
    try:
        settings.resolution_mp = presets.tier_mp(tier, settings.recommended_mp)
        n_lights = len(presets.ring_directions(_rings(settings), settings.stagger, settings.include_zenith))
        n_rings = len(_rings(settings))
        for fmt, prop in (("PTM_LRGB", "fit_ptm_lrgb"), ("PTM_RGB", "fit_ptm_rgb"), ("HSH1", "fit_hsh1"),
                          ("HSH2", "fit_hsh2"), ("HSH3", "fit_hsh3")):
            on = fmt in t["formats"]
            if fmt == "HSH3" and (n_lights < 48 or n_rings < 4):
                on = False                       # HSH 16 needs a dense dome
            setattr(settings, prop, on)
        settings.capture_depth = t["depth"]
    finally:
        _APPLYING_TIER[0] = False


def _rings(settings):
    if settings.dome_preset == "CUSTOM":
        return presets.custom_rings(settings.custom_rings, settings.custom_first_ring,
                                    settings.custom_el_min, settings.custom_el_max)
    return presets.DOME_PRESETS[settings.dome_preset]["rings"]


def _on_tier(self, context):
    if self.quality_tier != "CUSTOM":
        apply_quality_tier(self, self.quality_tier)


def _mark_custom(self, context=None):
    """A manual change of size / RTI types / depth leaves the chosen tier."""
    if not _APPLYING_TIER[0] and self.quality_tier != "CUSTOM":
        self["quality_tier"] = 3


def _on_depth(self, context):
    _on_geometry_change(self, context)
    _mark_custom(self, context)


def _set_resolution(self, value):
    self.resolution_mp = int(value)
    _mark_custom(self)


def _on_light_change(self, context):
    """Light strength / angle can be applied to the existing light immediately."""
    from . import dome  # local import: dome depends on this module
    dome.apply_light_strength(self)


class VRTI_Settings(bpy.types.PropertyGroup):
    # ---- specimen ---------------------------------------------------------
    target: PointerProperty(
        name="Specimen", type=bpy.types.Object, poll=_mesh_poll,
        description="Mesh that will be captured (imported OBJ)")
    source_path: StringProperty(name="Source OBJ", subtype="FILE_PATH")
    import_up_axis: EnumProperty(
        name="OBJ axes", items=[
            ("Z", "Z up (Metashape, RealityCapture)",
             "Read the OBJ coordinates 1:1. Metashape writes its own Z-up chunk coordinates, so "
             "the orientation you set there is kept"),
            ("Y", "Y up (Blender, Maya, Meshroom ...)",
             "Classic OBJ convention: Y is up and -Z is forward"),
        ], default="Z")
    orient_mode: EnumProperty(
        name="Model orientation", update=_on_orient_change, items=[
            ("SOURCE", "Keep model orientation",
             "Use the orientation set in Metashape: model +Z faces the camera, model +Y is the top "
             "of the image (Metashape Top view). The model is only centred. If its surface is "
             "tilted more than 30° from the camera axis it is levelled automatically"),
            ("LEVEL", "Level surface, keep heading",
             "Tilt the model so the surface faces the camera squarely, without turning it in the "
             "image plane (text lines stay where they were)"),
            ("PCA", "Automatic (long side along X)",
             "Fully automatic PCA alignment. The in-plane angle follows the outline of the "
             "stone, not the text"),
            ("AUTO", "Level and straighten",
             "Recommended. The inscribed face looks squarely at the camera and the text lines (or, "
             "without legible text, the edges of the block) run horizontally. Fine-tune with the "
             "rotation buttons"),
        ], default="AUTO")
    fine_rotation: FloatProperty(
        name="Fine rotation", default=0.0, min=-45.0, max=45.0, step=10, precision=1,
        update=_on_fine_rotation,
        description="Turn the specimen in the image plane by a few degrees, e.g. to make the text lines "
                    "horizontal (degrees, counter-clockwise)")
    info_tilt: FloatProperty(default=0.0, description="Angle between the surface normal and the "
                                                      "camera axis after alignment (degrees)")
    info_source_tilt: FloatProperty(default=0.0)
    info_orient_used: StringProperty(default="")
    info_auto_angle: FloatProperty(default=0.0)
    info_auto_method: StringProperty(default="")
    info_has_colour: BoolProperty(default=True)
    material_mode: EnumProperty(
        name="Material", items=[
            ("KEEP", "Photogrammetry texture (Principled)",
             "Keep the imported colour texture as base colour and use a physically based "
             "Principled BSDF with the roughness / specular below. Reproduces the glossy "
             "behaviour that RTI is meant to capture."),
            ("LAMBERT", "Lambertian (diffuse only)",
             "Pure diffuse surface: the fit becomes a clean shape-from-shading experiment "
             "(ideal for normal extraction, no specular highlights)."),
        ], default="KEEP")
    roughness: FloatProperty(name="Roughness", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
                             description="Micro-facet roughness of the specimen surface "
                                         "(0.3 glossy ceramic · 0.5 stone/bone · 0.8 matte clay)")
    specular_level: FloatProperty(name="Specular", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
                                  description="Specular reflectance level (0.5 = dielectric with IOR 1.5)")

    # ---- dome ---------------------------------------------------------------
    dome_preset: EnumProperty(
        name="Dome preset", update=_on_geometry_change,
        items=[(k, v["label"], v["note"]) for k, v in presets.DOME_PRESETS.items()]
                          + [("CUSTOM", "Custom rings", "Define rings, counts and elevation range yourself")],
        default="D64")
    custom_rings: IntProperty(name="Rings", default=4, min=1, max=12, update=_on_geometry_change)
    custom_first_ring: IntProperty(name="Lights on first ring", default=16, min=3, max=64,
                                   update=_on_geometry_change)
    custom_el_min: FloatProperty(name="Min elevation", default=15.0, min=2.0, max=60.0, subtype="NONE",
                                 update=_on_geometry_change, description="Degrees above the specimen plane")
    custom_el_max: FloatProperty(name="Max elevation", default=75.0, min=20.0, max=89.0,
                                 update=_on_geometry_change)
    include_zenith: BoolProperty(name="Include zenith light", default=False, update=_on_geometry_change,
                                 description="Add one light exactly above the specimen (impossible with a "
                                             "real dome because the camera sits there)")
    stagger: BoolProperty(name="Stagger rings", default=True, update=_on_geometry_change,
                          description="Rotate every second ring by half a step for more even azimuth coverage")
    light_model: EnumProperty(
        name="Light model", update=_on_geometry_change, items=[
            ("SUN", "Directional (ideal, distant light)",
             "Perfectly collimated light. Matches the mathematical assumption of PTM/HSH exactly "
             "(uniform light direction over the image) - zero geometric loss"),
            ("POINT", "Physical LED on dome",
             "Point light on the dome surface at the dome radius: simulates a real dome including "
             "distance fall-off and direction variation across the specimen"),
        ], default="SUN")
    light_angle: FloatProperty(name="Light angular size", default=1.0, min=0.0, max=10.0,
                               update=_on_light_change,
                               description="Angular diameter of the light in degrees (0 = razor-sharp shadows; "
                                           "~1° matches an LED or flash head seen from the specimen)")
    dome_radius_auto: BoolProperty(name="Auto radius", default=True, update=_on_geometry_change,
                                   description="Dome radius = 1.6 x the largest specimen extent "
                                               "(object fills at most 1/3 of the dome diameter, like real domes)")
    dome_radius: FloatProperty(name="Dome radius", default=1.0, min=0.01, soft_max=10.0, unit="LENGTH",
                               update=_on_geometry_change)
    show_dome: BoolProperty(name="Show dome", default=True, update=_on_show_dome,
                            description="Show the dome, the LEDs and the light cone in the viewport (never rendered)")
    led_size: FloatProperty(name="LED marker size", default=1.0, min=0.2, max=4.0)

    # ---- camera ---------------------------------------------------------------
    camera_projection: EnumProperty(
        name="Projection", update=_on_geometry_change, items=[
            ("PERSP", "Perspective (full-frame 36 mm)",
             "Virtual full-frame camera at the zenith; focal length chosen automatically (50-200 mm) "
             "like a macro/portrait lens on a copy stand"),
            ("ORTHO", "Orthographic", "Parallel projection: no perspective displacement at all"),
        ], default="PERSP")
    framing_margin: FloatProperty(name="Margin", default=0.05, min=0.0, max=0.3, subtype="FACTOR",
                                  update=_on_geometry_change,
                                  description="Empty border around the specimen as a fraction of the frame")
    aspect: EnumProperty(name="Aspect", update=_on_geometry_change,
                         items=[(k, v[1], "Frame shaped like the specimen: the most pixels on "
                                                       "the object for the same megapixels" if k == "FIT" else "")
                                            for k, v in presets.ASPECTS.items()],
                         default="FIT")
    orientation: EnumProperty(
        name="Orientation", update=_on_geometry_change, items=[
            ("AUTO", "Auto", "Landscape or portrait, whichever fits the specimen better"),
            ("LANDSCAPE", "Landscape", ""), ("PORTRAIT", "Portrait", "")], default="AUTO")
    quality_tier: EnumProperty(
        name="Quality", update=_on_tier, items=[
            ("QUICK", "Quick", "≤ 8 MP, PTM, JPEG: check framing and material fast"),
            ("WEB", "Web", "≤ 16 MP, PTM + HSH 9, JPEG images: compact and ready for openrti.org "
                           "(PTM-LRGB and HSH are its formats; 1 GB per member)"),
            ("ARCHIVE", "Archive", "≤ 36 MP (full-frame H-RTI range), PTM + HSH 9 + HSH 16, 16-bit masters"),
            ("CUSTOM", "Custom", "Size and RTI types chosen by hand"),
        ], default="WEB")
    resolution_mp: IntProperty(name="Megapixels", default=24, min=1)
    resolution_preset: EnumProperty(name="Resolution", items=resolution_items,
                                    get=lambda self: int(self.resolution_mp),
                                    set=_set_resolution,
                                    description="Capture size (full-frame style megapixel steps)")
    recommended_mp: IntProperty(name="Recommended MP", default=0)
    detail_mp: IntProperty(name="Full detail MP", default=0,
                           description="Size that would hold every visible texel and face (1 pixel each)")
    info_visible_fraction: FloatProperty(default=1.0)

    # ---- analysis of the imported model (read-only info) -----------------------
    info_faces: IntProperty(default=0)
    info_texels: FloatProperty(default=0.0)
    info_size_x: FloatProperty(default=0.0)
    info_size_y: FloatProperty(default=0.0)
    info_size_z: FloatProperty(default=0.0)
    dirty_dome: BoolProperty(default=False)

    # ---- render --------------------------------------------------------------
    engine: EnumProperty(name="Engine", items=[
        ("CYCLES", "Cycles (path tracing)", "Physically based; recommended for scientific output"),
        ("EEVEE", "EEVEE (real-time)", "Much faster preview quality; shadows and speculars are approximate"),
    ], default="CYCLES")
    samples: IntProperty(name="Samples", default=64, min=1, max=4096,
                         description="Cycles samples per pixel. Direct lighting with hard shadows converges "
                                     "quickly; 64 is noise-free for diffuse/glossy surfaces")
    lighting_mode: EnumProperty(name="Lighting", items=[
        ("DIRECT", "Direct light only", "No inter-reflections: pure light-surface interaction, matching the "
                                        "single-bounce reflectance model behind PTM/HSH"),
        ("GI", "Global illumination", "Allow light bouncing between parts of the specimen "
                                      "(as in a real capture of a concave object)"),
    ], default="DIRECT")
    use_denoise: BoolProperty(name="Denoise", default=False,
                              description="Denoising smooths micro-detail; keep off and raise samples instead")
    use_gpu: BoolProperty(name="Use GPU", default=True)
    light_strength: FloatProperty(name="Light strength", default=3.0, min=0.001, soft_max=100.0,
                                  update=_on_light_change,
                                  description="Sun strength (W/m²). Use Calibrate Exposure to set it so the "
                                              "brightest frame peaks just below clipping")
    auto_exposure: BoolProperty(name="Automatic exposure", default=True,
                                description="Measure the brightest light before every capture and set the "
                                            "light strength so nothing clips")
    test_scale: IntProperty(name="Quick test size", default=10, min=2, max=50, subtype="PERCENTAGE",
                            description="Image size of 'Quick test' as a percentage of the chosen size")
    exposure_target: FloatProperty(name="Target peak", default=0.95, min=0.5, max=1.0, subtype="FACTOR",
                                   description="Display value the brightest pixels should reach after "
                                               "calibration (0.95 = 1/3 stop headroom, no clipping)")
    calib_percentile: FloatProperty(name="Percentile", default=99.9, min=90.0, max=100.0,
                                    description="Percentile treated as 'peak' so single fireflies are ignored")

    render_scale: IntProperty(name="Scale %", default=100, min=1, max=100, subtype="PERCENTAGE",
                              description="Render at a fraction of the chosen size for quick test captures "
                                          "(100 % for the final data set)")

    # ---- output --------------------------------------------------------------
    output_dir: StringProperty(name="Output folder", subtype="DIR_PATH", default="//vrti_output/")
    output_name: StringProperty(name="Name", default="specimen")
    export_jpeg: BoolProperty(name="JPEG set (RTIBuilder, relight)", default=True,
                              description="With TIFF captures: also write JPEG copies to jpeg-exports with .lp "
                                          "files, so the images can be fitted with CHI RTIBuilder, relight-cli or "
                                          "RelightLab. JPEG captures are written there directly")
    jpeg_quality: IntProperty(name="JPEG quality", default=95, min=70, max=100)
    write_metadata: BoolProperty(name="Write metadata JSON", default=True)
    capture_depth: EnumProperty(
        name="Image format", update=_on_depth, items=[
            ("8", "TIFF 8-bit",
             "256 levels, deflate-compressed, no JPEG artefacts. The rounding error (0.1 %) is far "
             "below the PTM/HSH model error: relighting differs by 0.01 dB from 16-bit captures "
             "(measured), and the RTI files store 8-bit coefficients anyway"),
            ("16", "TIFF 16-bit",
             "65 536 levels per channel. Same RTI result, about 3-4x larger files; choose it when "
             "the capture images themselves are archived or fitted in linear colour space"),
            ("JPEG", "JPEG",
             "Compact: written straight to jpeg-exports, ready for RTIBuilder and relight. At quality "
             "95 the RTI differs from one fitted on lossless TIFF by 44.6 dB (normals 0.4°) - far "
             "below the PTM/HSH model error - at a third of the size of 8-bit TIFF (measured)"),
        ], default="JPEG")
    capture_colour: EnumProperty(
        name="Colour", update=_on_geometry_change, items=[
            ("AUTO", "Auto",
             "Greyscale when the model has no colour (no texture, no vertex colours), RGB otherwise"),
            ("RGB", "RGB", "Full colour captures"),
            ("BW", "Greyscale",
             "One channel; ideal for inscriptions where relief, not colour, is studied. Measured: "
             "OpenRTI upload of HSH 9 46 % smaller, PTM 11 % smaller, JPEG images about 3x smaller. "
             "The .ptm/.rti files on disk keep their size (the formats store RGB)"),
        ], default="AUTO")
    delete_captures_after_fit: BoolProperty(
        name="Delete images after fit", default=False,
        description="Delete the capture images once every RTI file was written successfully: the JPEG images "
                    "with JPEG captures, the TIFF masters with TIFF captures (their JPEG set stays). "
                    "The .lp files, metadata and RTI files are kept")

    # ---- fit -------------------------------------------------------------------
    fit_ptm_lrgb: BoolProperty(update=_mark_custom, name="PTM · LRGB (.ptm)", default=True,
                               description="Luminance polynomial + RGB chroma, 9 bytes/pixel. Most compatible")
    fit_ptm_rgb: BoolProperty(update=_mark_custom, name="PTM · RGB (.ptm)", default=False,
                              description="Independent biquadratic per channel, 18 bytes/pixel. "
                                          "Not accepted by OpenRTI (use PTM-LRGB or HSH there)")
    fit_hsh1: BoolProperty(update=_mark_custom, name="HSH · order 1, 4 terms (.rti)", default=False)
    fit_hsh2: BoolProperty(update=_mark_custom, name="HSH · order 2, 9 terms (.rti)", default=True,
                           description="Standard RTIBuilder/HSHfitter output; captures specular behaviour")
    fit_hsh3: BoolProperty(update=_mark_custom, name="HSH · order 3, 16 terms (.rti)", default=False,
                           description="Highest fidelity; needs >= 48 lights")
    fit_colorspace: EnumProperty(name="Fit colour space", items=[
        ("SRGB", "Display (sRGB) - RTIBuilder compatible",
         "Fit the 16-bit display-referred values exactly as RTIBuilder fits camera JPEG/TIFF files"),
        ("LINEAR", "Linear radiance", "Linearise before fitting (radiometrically faithful coefficients)"),
    ], default="SRGB")
    auto_fit: BoolProperty(name="Fit after capture", default=True)
    block_mem_mb: IntProperty(name="Block memory (MB)", default=1024, min=64, max=65536,
                              description="RAM budget for one row block during fitting (including "
                                          "temporaries). Larger blocks = fewer passes over the TIFFs")

    # ---- runtime status ----------------------------------------------------------
    is_rendering: BoolProperty(default=False)
    render_done: IntProperty(default=0)
    render_total: IntProperty(default=0)
    render_started: FloatProperty(default=0.0)
    is_fitting: BoolProperty(default=False)
    pending_jpeg: BoolProperty(default=False)
    pending_cleanup: BoolProperty(default=False)
    fit_progress: FloatProperty(default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    status_text: StringProperty(default="")
    last_error: StringProperty(default="")
    last_outputs: StringProperty(default="")


def get_settings(context=None) -> VRTI_Settings:
    scene = (context or bpy.context).scene
    return scene.vrti


def selected_formats(settings) -> list:
    out = []
    if settings.fit_ptm_lrgb:
        out.append("PTM_LRGB")
    if settings.fit_ptm_rgb:
        out.append("PTM_RGB")
    if settings.fit_hsh1:
        out.append("HSH1")
    if settings.fit_hsh2:
        out.append("HSH2")
    if settings.fit_hsh3:
        out.append("HSH3")
    return out


def capture_channels(settings) -> str:
    """'RGB' or 'BW' for the capture images."""
    if settings.capture_colour == "AUTO":
        return "RGB" if settings.info_has_colour else "BW"
    return settings.capture_colour


def capture_is_jpeg(settings) -> bool:
    return settings.capture_depth == "JPEG"


def capture_format_label(settings) -> str:
    ch = "RGB" if capture_channels(settings) == "RGB" else "grey"
    if capture_is_jpeg(settings):
        return f"JPEG q{settings.jpeg_quality} {ch}"
    return f"{settings.capture_depth}-bit {ch} TIFF"


def capture_resolution(settings):
    """(width, height) in pixels from preset, aspect and orientation."""
    mp = int(settings.resolution_mp) if settings.resolution_mp in presets.RESOLUTION_MP else 24
    long_side, short_side = presets.resolution_for(mp, _aspect_value(settings))
    landscape = _is_landscape(settings)
    return (long_side, short_side) if landscape else (short_side, long_side)


def _is_landscape(settings) -> bool:
    if settings.orientation == "LANDSCAPE":
        return True
    if settings.orientation == "PORTRAIT":
        return False
    return settings.info_size_x >= settings.info_size_y


def register():
    bpy.utils.register_class(VRTI_Settings)
    bpy.types.Scene.vrti = PointerProperty(type=VRTI_Settings)


def unregister():
    del bpy.types.Scene.vrti
    bpy.utils.unregister_class(VRTI_Settings)
