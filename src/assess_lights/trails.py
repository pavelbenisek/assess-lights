"""Step 4/8: satellite / aircraft trail detection.

A trail is a long, straight, *continuously* bright structure.  Rather than a
Hough transform - which on a star field produces thousands of spurious collinear
verdicts and, empirically, refused to return a synthetic perfect line at useful
`line_length` settings - this uses the structure of the threshold mask directly:

    1. 2x2 MAX bin        -> removes the CFA checkerboard modulation and halves
                             the resolution (a trail survives; it is long)
    2. background removal -> large-scale box mean (integral image)
    3. z = (residual - median) / (1.4826 * MAD)
    4. mask = z > snr     -> a star field at z>6 leaves ~0.01% of pixels set
    5. connected components, then per component:
         length      : extent along the principal axis (full-res pixels)
         straightness: RMS deviation perpendicular to that axis
         coverage    : fraction of the axis occupied by component pixels
    accept when the component is long, straight, and essentially unbroken.

Stars fail on size; star chains and merged blobs fail on straightness.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

BLOCK = 2880


@dataclass
class Trail:
    length: float
    angle_deg: float
    x0: int
    y0: int
    x1: int
    y1: int
    coverage: float
    median_snr: float
    rms_perp: float

    def as_dict(self) -> dict:
        return {
            "length": round(self.length, 1),
            "angle_deg": round(self.angle_deg, 2),
            "coverage": round(self.coverage, 3),
            "median_snr": round(self.median_snr, 2),
            "rms_perp": round(self.rms_perp, 2),
        }


def read_fits_data(path: Path, bitpix: int = 16, header_blocks: int = 2,
                   shape: tuple[int, ...] = ()) -> np.ndarray:
    """Load the primary image with numpy only."""
    dt = {16: ">u2", 32: ">i4", -32: ">f4", -64: ">f8"}.get(bitpix)
    if dt is None:
        raise ValueError(f"unsupported BITPIX {bitpix}")
    count = 1
    for d in shape:
        count *= d
    with path.open("rb") as f:
        f.seek(BLOCK * header_blocks)
        arr = np.fromfile(f, dtype=dt, count=count)
    if arr.size != count:
        raise ValueError("short data section")
    return arr.reshape(shape).astype("f4")


def background_box_mean(img: np.ndarray, k: int = 31) -> np.ndarray:
    """Separable box mean via an integral image - O(n), no scipy needed."""
    p = k // 2
    a = np.pad(img, p, mode="reflect")
    cs = np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    h, w = img.shape
    total = (cs[k:k + h, k:k + w] - cs[0:h, k:k + w]
             - cs[k:k + h, 0:w] + cs[0:h, 0:w])
    return total / float(k * k)


def max_bin2(img: np.ndarray) -> np.ndarray:
    """2x2 max pooling: kills CFA checkerboard, keeps thin bright features."""
    h2, w2 = img.shape[0] - img.shape[0] % 2, img.shape[1] - img.shape[1] % 2
    return img[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2).max(axis=(1, 3))


def _z_image(data: np.ndarray, binning: bool = True, k: int = 31):
    b = max_bin2(data) if binning else np.asarray(data, dtype=np.float32)
    scale = 2.0 if binning else 1.0
    res = b - background_box_mean(b, k)
    med = float(np.median(res))
    mad = float(np.median(np.abs(res - med)))
    s = 1.4826 * mad
    if s <= 0:
        return None, None, scale
    return (res - med) / s, s, scale


def detect_trails(
    data: np.ndarray,
    min_length_frac: float = 0.15,
    snr: float = 6.0,
    min_coverage: float = 0.80,
    max_rms_perp: float = 2.5,
    max_trails: int = 20,
) -> list[Trail]:
    """Return accepted trails, longest first."""
    from skimage.measure import label, regionprops

    z, _s, scale = _z_image(data)
    if z is None:
        return []

    h, w = z.shape
    diag = float(np.hypot(h, w)) * scale
    min_len = max(40.0, min_length_frac * diag)
    min_len_b = min_len / scale

    mask = z > snr
    if not mask.any():
        return []

    lab = label(mask, connectivity=2)
    trails: list[Trail] = []

    for rp in regionprops(lab, intensity_image=z):
        if rp.area < 0.5 * min_len_b:
            continue

        ys = rp.coords[:, 0].astype(float)
        xs = rp.coords[:, 1].astype(float)
        pts = np.stack([xs, ys], axis=1)
        centre = pts.mean(axis=0)
        p = pts - centre

        cov = (p.T @ p) / max(len(p), 1)
        try:
            evals, evecs = np.linalg.eigh(cov)
        except np.linalg.LinAlgError:
            continue
        major = evecs[:, int(np.argmax(evals))]
        minor = evecs[:, int(np.argmin(evals))]

        proj = p @ major
        perp = p @ minor
        length_b = float(proj.max() - proj.min())
        if length_b < min_len_b:
            continue

        rms_perp = float(np.sqrt(np.mean(perp ** 2)))
        if rms_perp > max_rms_perp:
            continue

        # continuity: fraction of the axis occupied by component pixels
        bins = max(int(length_b), 2)
        idx = np.clip(((proj - proj.min()) / max(length_b, 1e-9) * (bins - 1)).astype(int),
                      0, bins - 1)
        coverage = float(len(np.unique(idx)) / bins)
        if coverage < min_coverage:
            continue

        vals = z[rp.coords[:, 0], rp.coords[:, 1]]
        t0 = centre + major * proj.min()
        t1 = centre + major * proj.max()
        trails.append(Trail(
            length=length_b * scale,
            angle_deg=float(np.degrees(np.arctan2(major[1], major[0]))),
            x0=int(t0[0] * scale), y0=int(t0[1] * scale),
            x1=int(t1[0] * scale), y1=int(t1[1] * scale),
            coverage=coverage,
            median_snr=float(np.median(vals)),
            rms_perp=rms_perp,
        ))

    trails.sort(key=lambda t: t.length, reverse=True)
    return trails[:max_trails]
