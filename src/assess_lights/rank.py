"""Step 5/8: lucky-imaging soft mark.

Ranks the frames that survived the hard filters and soft-marks the worst N%.
The composite is the sum of per-metric robust z scores (median / MAD based), so
it is insensitive to a single wild outlier and needs no manual scaling:

    z big  = bad,  so FWHM and eccentricity enter directly
                    and star count enters as 1/stars
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Thresholds
from .pi_bridge import PiMetrics


def _mad(vals: list[float]) -> tuple[float, float]:
    import numpy as np

    a = np.asarray(vals, dtype=float)
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med)))
    return med, mad


def _z(vals: list[float], med: float, mad: float) -> list[float]:
    scale = 1.4826 * mad
    if scale <= 0:
        return [0.0] * len(vals)
    return [(v - med) / scale for v in vals]


@dataclass
class Ranked:
    path: Path
    metrics: PiMetrics
    score: float
    components: dict[str, float]


def rank_survivors(
    paths: list[Path],
    metrics: dict[Path, PiMetrics],
    thresholds: Thresholds,
) -> list[Ranked]:
    """Return the ranked list, worst (highest score) first."""
    usable = [p for p in paths if p in metrics]
    if not usable:
        return []

    fwhm = [metrics[p].FWHM for p in usable]
    ecc = [metrics[p].eccentricity for p in usable]
    inv_stars = [1.0 / max(metrics[p].stars, 1) for p in usable]

    f_med, f_mad = _mad(fwhm)
    e_med, e_mad = _mad(ecc)
    s_med, s_mad = _mad(inv_stars)

    zf = _z(fwhm, f_med, f_mad)
    ze = _z(ecc, e_med, e_mad)
    zs = _z(inv_stars, s_med, s_mad)

    ranked: list[Ranked] = []
    for i, p in enumerate(usable):
        ranked.append(Ranked(
            path=p,
            metrics=metrics[p],
            score=zf[i] + ze[i] + zs[i],
            components={"fwhm": zf[i], "eccentricity": ze[i], "stars": zs[i]},
        ))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def soft_mark(ranked: list[Ranked], percent: float) -> set[Path]:
    """Worst `percent`% of the ranked list."""
    if not ranked or percent <= 0:
        return set()
    n = max(1, round(len(ranked) * percent / 100.0))
    return {r.path for r in ranked[:n]}
