# Scientific notes

V-RTI Studio is the digital counterpart of the two established acquisition methods.

| Real world | V-RTI Studio |
|---|---|
| **Dome RTI:** LEDs fixed on a hemisphere, camera at the zenith, one photo per LED | A virtual dome with 36 to 120 lights in rings, camera at the zenith, one render per light |
| **Highlight RTI:** hand-held flash, direction recovered from a glossy sphere | Light directions are known analytically, so there is no highlight-detection error |
| Full-frame camera, RAW to sRGB JPEG or TIFF | A virtual 36 × 24 mm sensor, 50 to 200 mm lens, *Standard* (sRGB) view transform |
| Inverse-square fall-off and divergent light | **Directional light** by default (the PTM/HSH assumption), or a physical LED on the dome |

## Conventions

- **Light directions** are unit vectors from the surface to the light in image space: x right,
  y up, z towards the camera. The camera looks down −Z with +Y as image up, so world directions
  equal `.lp` directions.
- **The `.lp` file** is written only when a capture finishes, and the keyframed light directions
  are checked against it first (tolerance 0.1°).

## Models and file formats

**PTM** (Malzbender, Gelb, Wolters 2001). Per pixel:

```text
L(lu, lv) = a0·lu² + a1·lv² + a2·lu·lv + a3·lu + a4·lv + a5
```

The coefficients are solved by least squares with a single pseudo-inverse of the light matrix.
LRGB stores the luminance polynomial and an unscaled RGB chroma (`colour = RGB · L / 255`), and RGB
stores one polynomial per channel. Files are `PTM_1.2`, **bottom row first**, with
`coefficient = (byte − bias) · scale`.

**HSH** (hemispherical harmonics, Gautron et al. 2004). The 4, 9 and 16 basis functions use the
normalisation and term order of CHI's hshfitter and RTIViewer, so the `.rti` files are
interchangeable. The header is `#HSH1.2`, type 3, `w h 3`, `terms 2 1`, with float32 scale and bias
and `coefficient = byte / 255 · scale + bias`. Terms are stored per pixel as R, G, B, **top row
first**, unlike PTM.

**Fit domain.** By default the display-referred values are fitted, which is what RTIBuilder does
with camera JPEG and TIFF files. *Linear radiance* fitting is available for radiometric work; use
16-bit captures for it.

## Exposure

In Cycles, a sun of strength *S* W/m² gives a white Lambertian surface at normal incidence the value
*S/π*, so the default strength of 3.0 exposes a white surface at about 0.95.

Calibration then measures the real peak of your specimen and rescales, following the H-RTI "expose
to the right" rule: the brightest light peaks at 0.95, with no clipping. The test renders use the
two highest rings at full resolution and read a percentile of specimen pixels only, because
downscaled tests average specular highlights away.

## Capture bit depth

Renders are noise-free and never clipped, so 8-bit captures lose little: in the core round-trip
test, the held-out relighting PSNR of 8-bit and 16-bit captures differs by 0.01 dB. The RTI files
store 8-bit coefficients in any case.

Size of one 6 MP capture of a stone inscription (TIFF, deflate):

| Format | Size | Relative |
|---|---|---|
| 16-bit RGB | 14.7 MB | 1 |
| 16-bit grey | 5.1 MB | 1/3 |
| 8-bit RGB | 3.7 MB | 1/4 |
| 8-bit grey | 1.7 MB | 1/9 |

See the [user guide](USER_GUIDE.md#jpeg-or-tiff) for the JPEG measurements.

## Upload size

OpenRTI converts PTM-LRGB and HSH `.rti` in the browser and stores lossless WebP tiles. The black
background costs almost nothing, so the panel estimates the upload from specimen pixels. Measured with
OpenRTI's own converter on an inscription at 16 MP:

| Frame | PTM-LRGB | HSH 9 |
|---|---|---|
| 3:2, 25 % stone | 144 MB → 23 MB | 431 MB → 47 MB |
| 4:1 fitted, 66 % stone | 144 MB → 56 MB | 432 MB → 124 MB |
| 4:1 fitted, 66 % stone, greyscale | 144 MB → 50 MB | 432 MB → 67 MB |

## Limitations

The colour texture of a photogrammetric model already contains the baked lighting of the original
photographs. The material (roughness, specular) is a user assumption, not a measurement. Geometry
finer than the mesh does not exist in the render. Treat the result as a virtual RTI of the digital
model, not as a measurement of the reflectance of the physical object.

## References

- T. Malzbender, D. Gelb, H. Wolters. *Polynomial Texture Maps.* SIGGRAPH 2001.
- HP Labs. *Polynomial Texture Map (.ptm) File Format*, version 1.2.
- P. Gautron, J. Křivánek, S. Pattanaik, K. Bouatouch. *A Novel Hemispherical Basis for Accurate and Efficient Rendering.* EGSR 2004.
- M. Mudge et al. *New Reflection Transformation Imaging Methods for Rock Art and Multiple-Viewpoint Display.* VAST 2006.
- W. Postl. *Detection of Linear Oblique Structures and Skew Scan in Digitized Documents.* ICPR 1986.
- Cultural Heritage Imaging: RTIBuilder, HSHfitter, RTIViewer.
- ISTI-CNR Visual Computing Lab: relight.
