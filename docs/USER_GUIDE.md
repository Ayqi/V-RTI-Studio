# User guide

The panel is in the 3D Viewport sidebar (**N** key), under the **V-RTI** tab. It has three
steps. Everything else is in the closed *Advanced settings* panels and has sensible defaults.

## 1. Model

*Import OBJ / MTL* reads the model and its texture. If the model has no texture, its vertex
colours become the base colour.

**Importing replaces the scene.** Once the file has been read, all other objects in the
scene are removed, along with the meshes, materials and images only they used. To keep them,
turn off *Clear scene* in the file dialog. **Ctrl+Z** restores the scene, and a file that
cannot be read or aligned leaves it untouched. Anything another scene still uses is only taken
out of this scene, not deleted.

Keep one V-RTI setup per `.blend` file: the dome and camera are shared by all scenes in a file, and
building them in one scene replaces them in the others.

OBJ axes default to Z up, as Metashape and RealityCapture write them.

### Orientation

| Mode | What it does |
|---|---|
| **Level and straighten** (default) | Turns the inscribed face square to the camera and straightens it in the image plane |
| Keep model orientation | Uses the pose from the file: +Z faces the camera, +Y is the top of the image |
| Level surface, keep heading | Levels the face, with no turn in the image plane |
| Automatic (long side along X) | Lays the block out by its principal axes |

*Level and straighten* uses the mean normal of the facing surface, so the back and sides of a
block do not bias it. To straighten, it looks for text lines first (a projection-profile
deskew of the relief, ±15°). If the text is too weathered, it uses the minimum-area rectangle
of a cut block's outline. An irregular fragment with neither cue is not turned.

The panel tells you what was done, for example *Straightened -4.1° by text lines*. The 90° and
180° buttons and the *Fine rotation* slider apply on top.

## 2. Virtual dome

| Layout | Rings (elevation × lights) |
|---|---|
| Compact, 36 | 20°×12, 45°×12, 70°×12 |
| Classic, 48 | 15°×16, 30°×14, 45°×11, 65°×7 |
| **Dense, 64 ★** | 12°×20, 28°×16, 44°×12, 60°×10, 76°×6 |
| Research, 80 | 10°×24, 26°×20, 42°×16, 58°×12, 74°×8 |
| Hemisphere, 120 | 8°×32, 22°×28, 36°×22, 50°×18, 64°×12, 78°×8 |

*Custom rings*, at the end of the same list, opens fields for your own layout. The viewport shows the frame, the LED mounts, the
camera, a platform with an azimuth scale and an *image top* arrow, and the active light with a
faint cone. None of this is rendered. Scrub the timeline to step through the lights. The *Dome*
button shows or hides the rig, and *Camera* switches to the camera's view.

## 3. Quality and output

| Tier | Size | RTI files | Images | For |
|---|---|---|---|---|
| Quick | ≤ 8 MP | PTM | JPEG | framing and material checks |
| Web (default) | ≤ 16 MP | PTM-LRGB + HSH 9 | JPEG | OpenRTI, web viewers, everyday use |
| Archive | ≤ 36 MP | PTM + HSH 9 + HSH 16 (dense domes) | 16-bit TIFF + JPEG set | archives |
| Custom | by hand | by hand | by hand | selected automatically after a manual change |

**Size.** ★ marks the smallest step with one pixel per visible texel and per visible face, up to
36 MP. The tier limits it further. The frame aspect defaults to *Fit to object*, so an elongated
stone gets more pixels on the stone than it would in a 3:2 frame.

**Disk and OpenRTI.** The panel estimates the disk use, the OpenRTI upload size and its share of
the 1 GB quota. Above 60 MP it warns that OpenRTI converts the file only on a computer. *Then delete the images* removes the captures once the
RTI files are ready.

**Create RTI** measures the exposure, renders one image per light, and fits the selected files.
Progress and the time left appear at the top of the panel, and you can cancel at any time.
*Quick test* does the same at 10 % size.

### JPEG or TIFF?

JPEG q95 is the default because the loss is well below the error of the RTI model itself.
Measured on a 64-light capture of a stone inscription (3:2 frame, about a quarter of it stone), compared
with an RTI fitted from lossless TIFF of the same frames:

| Captures | Relighting difference | Normals (median / p95) | Size per 16 MP frame |
|---|---|---|---|
| TIFF 8-bit, lossless | reference | reference | 7.5 MB |
| JPEG q98 | 49.5 dB | not measured | 3.6 MB |
| **JPEG q95 (default)** | 44.6 dB | 0.37° / 1.1° | 2.6 MB |
| JPEG q90 | 40.3 dB | not measured | 1.9 MB |

The residual of the PTM/HSH model on the same data is about 34 dB. Files grow with the share of the
frame the stone fills, so the disk estimate in the panel allows for more than this table. Choose TIFF for an archival
master, or for *Linear radiance* fitting.

## Output folder

```text
<name>_VRTI/
  README.txt
  <name>_capture.json     camera, dome, lights, pose at capture time, software
  original-captures/      TIFF renders + <name>.lp (only with TIFF captures)
  jpeg-exports/           JPEG images (captures, or copies of the TIFFs) + <name>.lp
  assembly-files/         <name>.lp with absolute paths to jpeg-exports
  finished-files/         .ptm, .rti, <name>_fit_report.json, <name>_openrti.json
```

The fit report gives the residual of each RTI file (PSNR) on specimen pixels. `<name>_openrti.json` holds
suggestions for the OpenRTI record fields: capture method, equipment, number of images, software
and settings, and mm per pixel when the model is in metres.

To fit again from existing images, for example with another RTI type, use *Compute RTI from
existing images* in the advanced settings.

## Using the data in other tools

| Tool | Reads | Tested with V-RTI Studio output | Notes |
|---|---|---|---|
| RTIViewer 1.1 | `.ptm`, `.rti` | Yes: opens upright | |
| OpenRTI | PTM-LRGB, HSH `.rti` | Yes: with its own converter, upright | Not PTM-RGB. Phones and tablets stop at 60 MP. 1 GB per member. |
| relight-cli | JPEG | Yes: ran on `jpeg-exports` | `.lp` lines are matched to sorted file names |
| RTIBuilder 2.0.2 | JPEG | Not run | Use `<name>_VRTI` as the project folder, in a path without spaces. The HP PTM fitter handles about 4000 px of height. |
| RelightLab | JPEG, PNG, 16-bit TIFF | Not run | Use `jpeg-exports` or `original-captures` |

## Advanced settings

These panels hold the material (roughness, specular, Lambertian), the light model and size, custom
rings, camera projection and margin, render engine and samples, exposure details, PTM RGB and HSH 4,
the fit colour space, and re-fitting.

**Directional or physical light.** PTM and HSH assume one light direction for the whole image.
A real LED 60 cm from a 20 cm object has direction errors of up to ±9° across the object, plus a
1/r² brightness gradient. The default collimated light has neither. Choose *Physical LED on dome*
only to reproduce a specific real dome.

## Troubleshooting

| Problem | What to do |
|---|---|
| The model comes in tilted or turned | Read the message at the end of the *Model* step. Use the 90° buttons and *Fine rotation*, or try another mode. |
| The model has no colour | Check that the `.mtl` and texture are next to the `.obj`, and that the `.mtl` paths are relative. |
| Rendering is slow | Enable a GPU in *Preferences ▸ System ▸ Cycles Render Devices*. Fitting is CPU only. |
| RTIBuilder rejects the folder | Move the project to a path without spaces. RTIBuilder needs JPEG input. |
