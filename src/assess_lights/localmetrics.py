"""Step 3/8 without PixInsight: quality metrics via SEP (the SExtractor engine).

Produces the same `PiMetrics` records as the PixInsight bridge, so every
downstream stage (thresholds, ranking, reporting) is unchanged.

Why SEP and not a hand-rolled detector
--------------------------------------
Two earlier attempts here did their own star detection and PSF measurement, and
both failed against SubframeSelector on the same 40 frames:

  1. Thresholded second moments: every frame reported ~7.9 px FWHM with almost
     no frame-to-frame variation. Rank correlation against PI was -0.56 -- i.e.
     anti-correlated by chance -- and 0 of PI's 8 rejects were found. The
     aperture moments were dominated by background pixels, not the star.
  2. Elliptical-Gaussian least-squares fits: more plausible absolute values, but
     still uncorrelated with PI (no separation between PI's sharpest and its
     blurriest frames) with eccentricity pinned near 1.0. Fitting 200 stars per
     frame from a moments-derived start is fragile, and on a CFA mosaic the
     Bayer pattern produces spurious peaks and doubled cores.

SEP is the C implementation of SExtractor's detection, mature and fast, and it
reports per-source ``fwhm`` and ``elongation`` directly:

    bkg     = sep.Background(image)            # local background + global RMS
    objects = sep.extract(image - bkg, thresh_sigma, err=bkg.globalrms)
    fwhm    = 2 * sqrt(ln2 * (a^2 + b^2))      # == 2.35482 * sigma
    ecc     = sqrt(1 - (b/a)^2)                # 0 round, 1 elongated

``fwhm`` uses exactly the Gaussian convention SubframeSelector reports, so the
two are directly comparable. SEP ships prebuilt wheels for both supported
platforms (macOS arm64 and manylinux x86_64), so it needs no compiler.

Calibration
-----------
``stars`` counts everything SEP finds above ``detect_sigma`` and is therefore
larger than SubframeSelector's count for the same frame; the absolute thresholds
in ``config.py`` carry per-backend values for that reason. The FWHM cut is
relative (median + k*MAD) and so needs no adjustment.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np

from .pi_bridge import PiMetrics
from .trails import read_fits_data

DETECT_SIGMA = 5.0     # detection threshold, in units of the background RMS
MINAREA = 5            # minimum connected pixels for a detection
MAX_STARS = 4000       # cap on sources kept for the median (brightest first)
ADU_MAX = 65535.0      # to report noise on the same 0..1 scale as SubframeSelector

# SEP/SExtractor object flags: 1 neighbour, 2 blended, 4 saturated, 8 truncated
FLAG_TRUNCATED = 8


def measure_frame(data: np.ndarray, debayered: bool = False):
    """Quality metrics for one frame. Returns None when nothing is detected."""
    import sep

    img = np.ascontiguousarray(data, dtype=np.float64)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            bkg = sep.Background(img)
            sub = img - bkg
            objs = sep.extract(sub, DETECT_SIGMA, err=bkg.globalrms,
                               minarea=MINAREA, filter_type="matched")
        except Exception:  # noqa: BLE001 -- degenerate frames raise inside SEP
            return None

    if len(objs) == 0:
        return None

    global_rms = float(bkg.globalrms)
    total_detected = int(len(objs))

    # edge-truncated sources have meaningless shapes
    objs = objs[(objs["flag"] & FLAG_TRUNCATED) == 0]
    if len(objs) == 0:
        return None

    objs = objs[np.argsort(objs["flux"])[::-1][:MAX_STARS]]

    a = np.asarray(objs["a"], dtype=float)      # second moments (px), a >= b
    b = np.asarray(objs["b"], dtype=float)
    flux = np.asarray(objs["flux"], dtype=float)
    peak = np.asarray(objs["peak"], dtype=float)

    good = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    if not good.any():
        return None
    a, b, flux, peak = a[good], b[good], flux[good], peak[good]

    # SExtractor's non-circular-PSF convention, identical to SubframeSelector's:
    #   FWHM = 2.35482 * sigma, with sigma = sqrt((a^2 + b^2) / 2)
    fwhm = 2.3548200450309493 * np.sqrt((a * a + b * b) / 2.0)
    # elongation = a/b (>= 1) -> the eccentricity convention SubframeSelector uses
    elong = np.maximum(a / np.maximum(b, 1e-9), 1.0)
    ecc = np.sqrt(np.clip(1.0 - 1.0 / (elong * elong), 0.0, 1.0))
    snr = float(np.median(peak) / global_rms) if global_rms > 0 else None

    return PiMetrics(
        path=Path("."),
        FWHM=float(np.median(fwhm)),
        eccentricity=float(np.median(ecc)),
        stars=total_detected,
        SNR=snr,
        noise=global_rms / ADU_MAX,
        MAD=global_rms / ADU_MAX,
        Mstar=float(np.median(flux)),
        PSFSNR=snr,
        PSFSignalWeight=(float(np.median(flux) / (global_rms * global_rms))
                         if global_rms > 0 else None),
    )


def measure(frames: list[Path], headers: dict, debayer: bool = False,
            progress=None, tick=None) -> dict[Path, PiMetrics]:
    """Measure frames; returns metrics keyed by the original paths."""
    out: dict[Path, PiMetrics] = {}
    for f in frames:
        if tick is not None:
            tick()
        h = headers.get(f)
        if h is None or not h.shape:
            continue
        try:
            data = read_fits_data(f, bitpix=h.bitpix or 16,
                                  header_blocks=h.header_blocks, shape=h.shape)
            if debayer:
                from .debayer import cfa_patterns, demosaic_to_luminance
                pat = cfa_patterns([f], headers).get(f)
                if pat:
                    data = demosaic_to_luminance(data, pat)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"{f.name}: skipped ({e})")
            continue

        m = measure_frame(data, debayered=debayer)
        if m is None:
            if progress:
                progress(f"{f.name}: no usable stars detected")
            continue
        out[f] = PiMetrics(**{**m.__dict__, "path": f})

    return out
