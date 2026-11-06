# SPDX-License-Identifier: GPL-3.0-or-later
"""Light-direction bases used by the classic RTI formats.

Conventions (identical to RTIBuilder / RTIViewer / relight):

* A light direction is the **unit vector from the surface towards the light**
  expressed in image space: ``x`` to the right, ``y`` up, ``z`` towards the
  camera. ``z = sqrt(1 - x^2 - y^2)`` for a normalised vector.
* PTM uses the projection ``(lu, lv) = (x, y)`` and the biquadratic
  ``L = a0*lu^2 + a1*lv^2 + a2*lu*lv + a3*lu + a4*lv + a5`` (Malzbender 2001).
* HSH uses hemispherical harmonics (Gautron et al. 2004) with the exact
  normalisation and term order of the CHI ``hshfitter`` / RTIViewer
  ``getHSH`` implementation so that ``.rti`` files are interchangeable.
"""
from __future__ import annotations

import math

import numpy as np

HSH_TERMS = {1: 4, 2: 9, 3: 16}


def normalize_directions(dirs) -> np.ndarray:
    """Return an (N, 3) float64 array of unit vectors.

    Directions must point into the upper hemisphere (towards the camera).
    Tiny negative ``z`` from rounding is clamped to 0; anything clearly below
    the horizon raises, because silently mirroring it would produce plausible
    but wrong coefficients.
    """
    d = np.asarray(dirs, dtype=np.float64).reshape(-1, 3)
    n = np.linalg.norm(d, axis=1, keepdims=True)
    if np.any(n == 0):
        raise ValueError("zero-length light direction")
    d = d / n
    if np.any(d[:, 2] < -1e-4):
        bad = int(np.argmin(d[:, 2]))
        raise ValueError(f"light direction {bad + 1} points below the horizon (z={d[bad, 2]:.3f}); "
                         "the .lp convention is x right, y up, z towards the camera")
    d[:, 2] = np.clip(d[:, 2], 0.0, 1.0)
    return d


def ptm_basis(dirs) -> np.ndarray:
    """Design matrix (N, 6) of the PTM biquadratic for light directions."""
    d = normalize_directions(dirs)
    lu, lv = d[:, 0], d[:, 1]
    return np.stack([lu * lu, lv * lv, lu * lv, lu, lv, np.ones_like(lu)], axis=1)


def hsh_basis(dirs, order: int = 2) -> np.ndarray:
    """Design matrix (N, T) of hemispherical harmonics (T = 4, 9 or 16).

    ``order`` 1, 2, 3 give 4, 9 and 16 terms. Angles follow RTIViewer:
    ``phi = atan2(y, x)``, ``theta = acos(z)`` (theta clamped just below
    90 degrees so that ``sqrt(cos(theta) - cos(theta)^2)`` stays real).
    """
    if order not in HSH_TERMS:
        raise ValueError("HSH order must be 1, 2 or 3")
    d = normalize_directions(dirs)
    phi = np.arctan2(d[:, 1], d[:, 0])
    phi = np.where(phi < 0.0, phi + 2.0 * math.pi, phi)
    theta = np.minimum(np.arccos(np.clip(d[:, 2], -1.0, 1.0)), math.pi / 2.0 - 0.01)

    ct = np.cos(theta)
    ct2 = ct * ct
    cp, sp = np.cos(phi), np.sin(phi)
    s = np.sqrt(np.maximum(ct - ct2, 0.0))  # sqrt(cos t - cos^2 t)
    pi = math.pi

    terms = [
        np.full_like(ct, 1.0 / math.sqrt(2.0 * pi)),
        math.sqrt(6.0 / pi) * (cp * s),
        math.sqrt(3.0 / (2.0 * pi)) * (-1.0 + 2.0 * ct),
        math.sqrt(6.0 / pi) * (s * sp),
    ]
    if order >= 2:
        terms += [
            math.sqrt(30.0 / pi) * (np.cos(2.0 * phi) * (-ct + ct2)),
            math.sqrt(30.0 / pi) * (cp * (-1.0 + 2.0 * ct) * s),
            math.sqrt(5.0 / (2.0 * pi)) * (1.0 - 6.0 * ct + 6.0 * ct2),
            math.sqrt(30.0 / pi) * ((-1.0 + 2.0 * ct) * s * sp),
            math.sqrt(30.0 / pi) * ((-ct + ct2) * np.sin(2.0 * phi)),
        ]
    if order >= 3:
        s3 = s ** 3  # (cos t - cos^2 t)^1.5
        terms += [
            2.0 * math.sqrt(35.0 / pi) * (np.cos(3.0 * phi) * s3),
            math.sqrt(210.0 / pi) * (np.cos(2.0 * phi) * (-1.0 + 2.0 * ct) * (-ct + ct2)),
            2.0 * math.sqrt(21.0 / pi) * (cp * s * (1.0 - 5.0 * ct + 5.0 * ct2)),
            math.sqrt(7.0 / (2.0 * pi)) * (-1.0 + 12.0 * ct - 30.0 * ct2 + 20.0 * ct2 * ct),
            2.0 * math.sqrt(21.0 / pi) * (s * (1.0 - 5.0 * ct + 5.0 * ct2) * sp),
            math.sqrt(210.0 / pi) * ((-1.0 + 2.0 * ct) * (-ct + ct2) * np.sin(2.0 * phi)),
            2.0 * math.sqrt(35.0 / pi) * (s3 * np.sin(3.0 * phi)),
        ]
    return np.stack(terms, axis=1)


def least_squares_operator(A: np.ndarray, rcond: float = 1e-4) -> np.ndarray:
    """Return ``pinv(A)`` (T, N): coefficients = pinv(A) @ observations.

    The pseudo-inverse is computed once per light configuration with an SVD
    (exactly as described in the HP PTM paper) and then applied to every pixel
    as a single matrix product. Singular values below ``rcond`` x the largest
    one are truncated: basis terms that the light layout cannot determine
    (e.g. HSH order 3 with only three elevation rings) are then set to zero
    instead of exploding, which keeps the 8-bit quantisation of the file sane.
    """
    return np.linalg.pinv(np.asarray(A, dtype=np.float64), rcond=rcond)


def design_rank(A: np.ndarray, rel_tol: float = 1e-4) -> int:
    """Numerical rank of the design matrix (same tolerance as the pseudo-inverse)."""
    sv = np.linalg.svd(np.asarray(A, dtype=np.float64), compute_uv=False)
    if sv.size == 0 or sv[0] <= 0:
        return 0
    return int(np.count_nonzero(sv > rel_tol * sv[0]))


def condition_number(A: np.ndarray) -> float:
    sv = np.linalg.svd(np.asarray(A, dtype=np.float64), compute_uv=False)
    return float(sv[0] / sv[-1]) if sv[-1] > 0 else float("inf")
