# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] - 2026-11-06

First public release.

### Added

- *Clear scene* option on import, on by default. Importing replaces the objects in the scene. A file that cannot be read or aligned leaves the scene untouched, Ctrl+Z restores it, and what other scenes use is kept.
- `scripts/make_demo_specimen.py`: writes a synthetic inscribed slab, so the add-on can be tried without any data.
- Test runner (`scripts/run_tests.py`), version check (`scripts/check_version.py`), Ruff configuration, CI and release workflows.
- Documentation: user guide and scientific notes.

### Changed

- The active light's cone in the viewport is much more transparent, so it no longer veils the specimen.
- `vrti.rti` and `vrti.presets` can be imported without Blender.
- The capture record stores the file name of the model instead of its full path.

### Fixed

- The *Dome* button now shows and hides the rig at once.
- A model whose file name starts with `VRTI_` was deleted as part of the rig.
- A second scene no longer takes its model from the specimen collection of the first.

### Removed

- The Turkish interface and the language switch. The add-on is now English only.

## [1.5.3] - 2026-10-14

### Fixed

- Stale light keyframes could disagree with the `.lp` file when the dome layout changed. The dome is now always rebuilt before a capture, the directions are verified against the `.lp` file, and the `.lp` file and metadata are written only when a capture finishes.

### Changed

- The 48-light dome is now 15°×16, 30°×14, 45°×11, 65°×7, with the most lights at grazing angles.

## [1.5.0] - 2026-10-14

### Changed

- Captures default to JPEG q95, written straight to `jpeg-exports`. TIFF is used only in the Archive tier.
- Quality tiers are now Quick ≤ 8 MP, Web ≤ 16 MP and Archive ≤ 36 MP.

## [1.4.1] - 2026-10-14

### Fixed

- Installed extensions failed with an import error for `bl_info`, because Blender strips it from extension modules. The version now lives in `vrti/version.py`.

## [1.4.0] - 2026-10-14

### Added

- *Level and straighten*, the new default orientation: the facing surface is turned to the camera, and the model is straightened by its text lines or, failing that, by the minimum-area rectangle of its outline.

## [1.3.0] - 2026-10-14

### Added

- RTIBuilder folder layout: `original-captures`, `jpeg-exports`, `assembly-files`, `finished-files`.
- Quality tiers, and a ★ size recommendation that counts only camera-facing texels and faces.

### Fixed

- HSH `.rti` rows are written top row first. Earlier versions wrote them bottom-up, which flipped the files vertically. Fit old projects again with *Compute RTI from existing images*.

## [1.2.1] - 2026-10-14

### Added

- *Fine rotation* slider, and the pose at capture time in the capture record.
- *Fit to object* frame aspect, now the default.

### Fixed

- Top-ring frames could clip. Calibration now uses full-resolution renders of the two highest rings and reads specimen pixels only.
- The PSNR in the fit report is now measured on specimen pixels.

## [1.2.0] - 2026-10-14

### Changed

- Simplified panel: three steps and one *Create RTI* button, with everything else under *Advanced settings*.
- New dome visuals: ring frames, coloured LED mounts, glowing rings, a platform with an azimuth scale, and a camera on a gantry.

### Added

- *Quick test* at 10 % size.

## [1.1.0] - 2026-10-14

### Added

- Orientation modes and in-plane turns.
- Vertex colours for untextured models.
- 8-bit lossless and greyscale captures, and deleting the captures after the fit.

### Fixed

- OBJ axes default to Z up, so a model keeps the orientation it had in Metashape.

## [1.0.0] - 2026-10-14

Initial version: virtual dome, zenith camera, multi-light capture, `.lp` file, and PTM and HSH fitting.

[1.6.0]: https://github.com/Ayqi/V-RTI-Studio/releases/tag/v1.6.0
