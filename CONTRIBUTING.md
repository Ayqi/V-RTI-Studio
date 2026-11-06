# Contributing

Bug reports, RTI files that do not open somewhere, and pull requests are all welcome. For a larger
change, please open an issue first so we can agree on the approach.

## Project layout

```text
vrti/            the add-on (what gets packaged)
  rti/           fitting, file formats, image I/O: needs only NumPy, no Blender
  presets.py     dome layouts and size ladder: plain Python
  align.py       automatic pose
  dome.py        the viewport rig, lights and camera
  ops_*.py       operators: import, capture, fit
  ui.py          the sidebar panel
tests/           test scripts
scripts/         test runner, version check, demo specimen
docs/            user guide, scientific notes
```

## Set up

Clone the repository and install a build in Blender (*Install from Disk…*). The output folder must exist:

```bash
mkdir dist
blender --command extension build --source-dir vrti --output-dir dist
```

Or link `vrti` into your extensions folder while you work on it.

## Run the checks

```bash
pip install ruff numpy
ruff check vrti tests scripts
python scripts/check_version.py
python tests/test_core_roundtrip.py
python scripts/run_tests.py --blender /path/to/blender
```

The runner uses `--python-exit-code 1`, because without it Blender returns 0 even when a script
raises. Its `installed` test builds the zip and installs it into a temporary user profile, so your
own Blender configuration is not touched.

CI runs all of this on the oldest supported Blender, the current LTS and the latest release.

## Guidelines

- **Python 3.11.** Blender 4.2 to 4.5 ship it, so newer syntax breaks them. Ruff is set to this target.
- **No extra dependencies.** The add-on uses only what ships with Blender.
- **Keep `vrti/rti` and `vrti/presets.py` free of `bpy`,** so they stay testable in plain Python.
- **Do not break the file formats.** PTM rows go bottom to top and HSH rows top to bottom. If you touch a writer, open the result in RTIViewer and add a check to the round-trip test.
- **Add a test for a bug fix** when one can reproduce it.
- **Do not commit real specimens** or anything else you do not have the rights to publish. Use `scripts/make_demo_specimen.py`.

## Releases

1. Update the version in `vrti/version.py`, `vrti/__init__.py`, `vrti/blender_manifest.toml` and `CITATION.cff`, and add a section to `CHANGELOG.md`. `scripts/check_version.py` fails if they disagree.
2. Tag the commit: `git tag v1.2.3 && git push origin v1.2.3`.
3. The release workflow builds the zip and attaches it to a GitHub release.

## License

By contributing, you agree that your contributions are licensed under GPL-3.0-or-later.
