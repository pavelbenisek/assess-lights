"""Optional step 3/8 mode: measure on debayered luminance, not the raw CFA mosaic.

Why this exists
---------------
Shape metrics (eccentricity especially) come out inflated on an undebayered
mosaic: the colour filter array modulates every pixel, so a perfectly round star
does not look round.  Ranking frames consistently is fine either way, but if you
want absolute FWHM / eccentricity numbers to mean something, measure on luminance.

Why it is done here and not in PixInsight
-----------------------------------------
PixInsight reads these FITS as *monochrome* (no CFA flag), so its `Debayer`
process applies successfully but changes nothing - verified: a 1-channel image
stays 1-channel.  Doing the demosaic in numpy removes that dependency and is
fully deterministic.

The demosaic is a masked normalised convolution (a standard, well-behaved
bilinear variant): each channel's known samples are box-filtered and divided by
the box-filtered indicator mask, so no sample is ever fabricated from nothing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .trails import read_fits_data

VALID_PATTERNS = ("RGGB", "BGGR", "GBRG", "GRBG")


def _site_masks(pattern: str, shape: tuple[int, ...]) -> dict[str, np.ndarray]:
    h, w = shape[0], shape[1]
    yy, xx = np.mgrid[0:h, 0:w]
    tile = np.array([[pattern[0], pattern[1]], [pattern[2], pattern[3]]])
    tile_idx = tile[yy % 2, xx % 2]
    return {c: (tile_idx == c) for c in "RGB"}


def _masked_fill(known: np.ndarray, mask: np.ndarray, size: int) -> np.ndarray:
    from scipy.ndimage import uniform_filter

    num = uniform_filter(known * mask, size=size, mode="nearest")
    den = uniform_filter(mask, size=size, mode="nearest")
    return num / np.maximum(den, 1e-9)


def demosaic_to_luminance(data: np.ndarray, pattern: str = "GRBG", kernel: int = 3) -> np.ndarray:
    """Full-resolution luminance (0.25R + 0.5G + 0.25B) from a CFA mosaic.

    `kernel` is the box size of the masked normalised convolution. Keep it small:
    a large kernel sharpens nothing and inflates measured FWHM (a 5x5 kernel on a
    real frame pushed the SubframeSelector FWHM median from 2.7 px to 4.0 px).
    At 3x3 the interpolation is slightly noisier but the PSF stays realistic.
    """
    pat = pattern.upper()
    if pat not in VALID_PATTERNS:
        raise ValueError(f"unsupported CFA pattern {pattern!r}; expected one of {VALID_PATTERNS}")

    img = np.asarray(data, dtype=np.float32)
    masks = _site_masks(pat, img.shape)
    chans: dict[str, np.ndarray] = {}
    for c in "RGB":
        m = masks[c].astype(np.float32)
        known = np.where(masks[c], img, 0.0)
        chans[c] = _masked_fill(known, m, kernel)
    return (0.25 * chans["R"] + 0.5 * chans["G"] + 0.25 * chans["B"]).astype(np.float32)


def write_luminance_fits(path: Path, lum: np.ndarray, exptime: float | None = None) -> None:
    from astropy.io import fits

    data = np.clip(lum, 0.0, 65535.0).astype(np.uint16)
    hdu = fits.PrimaryHDU(data)
    hdu.header["IMAGETYP"] = "Light"
    hdu.header["HISTORY"] = "assess-lights: CFA demosaic to luminance (for SubframeSelector)"
    if exptime is not None:
        hdu.header["EXPTIME"] = float(exptime)
    hdu.writeto(path, overwrite=True)


def cfa_patterns(frames: list[Path], headers: dict) -> dict[Path, str | None]:
    """Per-frame BAYERPAT from the FITS header."""
    out: dict[Path, str | None] = {}
    for f in frames:
        h = headers.get(f)
        pat = None
        if h is not None:
            raw = h.header.get("BAYERPAT") or h.header.get("CFAPATTERN")
            if raw:
                pat = str(raw).strip().upper()
                if pat not in VALID_PATTERNS:
                    pat = None
        out[f] = pat
    return out


def stage_debayered(
    frames: list[Path],
    headers: dict,
    outdir: Path,
    progress=None,
    tick=None,
) -> dict[Path, Path]:
    """Write a luminance FITS per frame; return {original: luminance_file}."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    pats = cfa_patterns(frames, headers)
    mapping: dict[Path, Path] = {}

    for f in frames:
        if tick is not None:
            tick()
        pat = pats.get(f)
        if pat is None:
            if progress:
                progress(f"{f.name}: no usable CFA pattern in header - skipped")
            continue
        h = headers.get(f)
        try:
            data = read_fits_data(f, bitpix=h.bitpix or 16,
                                  header_blocks=h.header_blocks, shape=h.shape)
            lum = demosaic_to_luminance(data, pat)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"{f.name}: demosaic failed ({e}) - skipped")
            continue
        out = outdir / (f.stem + "_lum.fits")
        write_luminance_fits(out, lum, exptime=h.exptime if h else None)
        mapping[f] = out

    if progress:
        progress(f"staged {len(mapping)} luminance frame(s) in {outdir}")
    return mapping
