# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a synthetic inscribed stone slab (OBJ + MTL + PNG) to try V-RTI Studio without any data.

    blender -b --factory-startup --python scripts/make_demo_specimen.py -- output_folder

The slab is deliberately turned and tilted a few degrees, like a raw photogrammetry chunk.
"""
from __future__ import annotations

import math
import os
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Matrix

WIDTH, HEIGHT = 0.60, 0.30          # metres
CELLS_X, CELLS_Y = 800, 400
TEXT = ("V-RTI", "STUDIO")
GLYPHS = {
    "V": "10001 10001 10001 10001 01010 01010 00100",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "-": "00000 00000 00000 11111 00000 00000 00000",
}


def gaussian_blur(img: np.ndarray, sigma: float) -> np.ndarray:
    fy = np.fft.fftfreq(img.shape[0])[:, None]
    fx = np.fft.rfftfreq(img.shape[1])[None, :]
    return np.fft.irfft2(np.fft.rfft2(img) * np.exp(-2.0 * (np.pi * sigma) ** 2 * (fx * fx + fy * fy)), img.shape)


def fractal_noise(shape, rng, octaves) -> np.ndarray:
    """Sum of Gaussian-filtered noise layers; ``octaves`` holds (sigma in cells, standard deviation)."""
    out = np.zeros(shape)
    for sigma, amplitude in octaves:
        layer = gaussian_blur(rng.standard_normal(shape), sigma)
        out += amplitude * layer / layer.std()
    return out


def letter_mask(shape) -> np.ndarray:
    """Soft mask (0..1) of the incised text; row 0 is the top of the slab."""
    h, w = shape
    mask = np.zeros(shape)
    stroke = int(h / 26)
    gap = stroke
    for line, word in enumerate(TEXT):
        word_w = len(word) * (5 * stroke + gap) - gap
        y0 = int(h * (0.16 + 0.42 * line))
        x = (w - word_w) // 2
        for ch in word:
            for r, row in enumerate(GLYPHS[ch].split()):
                for c, bit in enumerate(row):
                    if bit == "1":
                        mask[y0 + r * stroke:y0 + (r + 1) * stroke, x + c * stroke:x + (c + 1) * stroke] = 1.0
            x += 5 * stroke + gap
    return np.clip(gaussian_blur(mask, stroke / 6.0), 0.0, 1.0)


def main(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.default_rng(7)
    rows, cols = CELLS_Y + 1, CELLS_X + 1
    letters = letter_mask((rows, cols))
    weathering = fractal_noise((rows, cols), rng, ((30, 1.2e-3), (10, 2.5e-4), (3, 6e-5), (1.2, 1.5e-5)))
    relief = weathering - 0.0025 * letters

    # grid: row 0 = top edge (+Y)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bm = bmesh.new()
    xs = np.linspace(-WIDTH / 2, WIDTH / 2, cols)
    ys = np.linspace(HEIGHT / 2, -HEIGHT / 2, rows)
    verts = [[bm.verts.new((float(x), float(y), float(relief[r, c]))) for c, x in enumerate(xs)]
             for r, y in enumerate(ys)]
    uv_layer = bm.loops.layers.uv.new("UVMap")
    for r in range(rows - 1):
        for c in range(cols - 1):
            face = bm.faces.new((verts[r + 1][c], verts[r + 1][c + 1], verts[r][c + 1], verts[r][c]))
            face.smooth = True
            for loop, (u, v) in zip(face.loops, ((c, r + 1), (c + 1, r + 1), (c + 1, r), (c, r))):
                loop[uv_layer].uv = (u / (cols - 1), 1.0 - v / (rows - 1))
    bmesh.ops.triangulate(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("demo_inscription")
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("demo_inscription", mesh)
    bpy.context.scene.collection.objects.link(obj)

    # colour: weathered limestone with slightly darker letters
    tone = 0.62 + fractal_noise((rows, cols), rng, ((25, 0.035), (6, 0.025), (1.5, 0.02))) - 0.08 * letters
    rgb = np.stack([tone * 1.02, tone, tone * 0.92, np.ones_like(tone)], axis=-1)
    image = bpy.data.images.new("demo_inscription", cols, rows, alpha=False)
    image.pixels.foreach_set(np.clip(rgb[::-1], 0.0, 1.0).astype(np.float32).ravel())
    image.update()      # otherwise save() writes the blank generated image
    image.filepath_raw = os.path.join(out_dir, "demo_inscription.png")
    image.file_format = "PNG"
    image.save()

    material = bpy.data.materials.new("demo_inscription")
    material.use_nodes = True
    tree = material.node_tree
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tree.links.new(tex.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])
    mesh.materials.append(material)

    # a raw chunk is never square to the axes
    obj.matrix_world = (Matrix.Translation((0.4, -0.2, 0.15)) @ Matrix.Rotation(math.radians(4.0), 4, "Z")
                        @ Matrix.Rotation(math.radians(5.0), 4, "X"))
    bpy.context.view_layer.update()
    path = os.path.join(out_dir, "demo_inscription.obj")
    bpy.ops.wm.obj_export(filepath=path, export_materials=True, path_mode="RELATIVE",
                          forward_axis="Y", up_axis="Z")   # coordinates 1:1, as Metashape writes them
    print("Demo specimen:", path)
    return path


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    main(args[0] if args else "demo_specimen")
