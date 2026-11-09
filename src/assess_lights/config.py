"""Tunable thresholds.

Defaults were chosen from a measured 40-frame ZWO Seestar S30 Pro dataset
(20 s IRCUT subs, 2160x3840 16-bit OSC CFA):

    FWHM          min 1.76   median 2.57   max 4.47   (px)
    eccentricity  min 0.44   median 0.70   max 0.92
    stars         min 2      median 27     max 54
    bg noise      min 0.0084 median 0.0090 max 0.0103

The high eccentricity is REAL, not a CFA artifact: the capture app's own
debayered JPEG measures 0.73 (median) through the same SubframeSelector, versus
0.75 on the raw mosaic. The stars genuinely are elongated (this is a Seestar
alt-az rig), so the eccentricity gate is deliberately loose and the relative
(robust z) test does the real work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_PI_BINARY = "/Applications/PixInsight/PixInsight.app/Contents/MacOS/PixInsight"

FITS_SUFFIXES = (".fit", ".fits", ".fts")


@dataclass
class Thresholds:
    # --- step 2/8: damaged / unusable -----------------------------------
    require_min_frames: int = 1          # fewer FITS than this -> not a series
    blank_sigma: float = 3.0             # subsampled std below this -> blank frame

    # --- step 3/8: focus / shape (SubframeSelector or SEP) --------------
    min_stars: int = 12                  # hard floor: failed tracking / cloud
    eccentricity_abs_max: float = 0.90   # hard ceiling (CFA-inflated, so loose)
    fwhm_mad_k: float = 2.5              # robust: median + k*1.4826*MAD
    fwhm_abs_max: float = 5.0            # generous absolute backstop (px)

    # --- step 4/8: satellite / aircraft trails --------------------------
    trail_min_length_frac: float = 0.15  # of the image diagonal
    trail_sigma: float = 6.0             # brightness above background sigma
    trail_max_count: int = 0             # >this many trails -> reject frame

    # --- step 5/8: lucky-imaging soft mark ------------------------------
    soft_percent: float = 30.0           # worst N% of surviving frames
    soft_over: str = "survivors"         # "survivors" | "all"


@dataclass
class Paths:
    target: Path
    rejected_dir_name: str = "rejected"
    unlucky_dir_name: str = "unlucky-images"
    manifest_name: str = "assessment.csv"
    manifest_json_name: str = "assessment.json"

    # Output directories are siblings of the target directory, prefixed with the
    # target's own name, e.g. for ~/Desktop/tempastro/C 34_sub:
    #     ~/Desktop/tempastro/C 34_sub-rejected
    #     ~/Desktop/tempastro/C 34_sub-unlucky-images
    # This keeps the target directory itself clean for stacking.

    @property
    def rejected(self) -> Path:
        return self.target.parent / f"{self.target.name}-{self.rejected_dir_name}"

    @property
    def unlucky(self) -> Path:
        return self.target.parent / f"{self.target.name}-{self.unlucky_dir_name}"

    @property
    def manifest(self) -> Path:
        return self.target / self.manifest_name

    @property
    def manifest_json(self) -> Path:
        return self.target / self.manifest_json_name
