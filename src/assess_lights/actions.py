"""Step 6/8 move + 7/8 manifest: move marked files (never delete), write the manifest."""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Verdict:
    path: Path
    category: str                      # kept | damaged | blur | trail | soft
    reasons: list[str] = field(default_factory=list)
    metrics: dict | None = None
    score: float | None = None
    moved_to: str | None = None


def move_frames(
    verdicts: list[Verdict],
    dest: Path,
    companions: dict[Path, list[Path]],
    apply: bool,
) -> int:
    """Move the given verdicts' FITS files (and their .jpg companions) into dest."""
    if not apply:
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for v in verdicts:
        src = v.path
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(dest / src.name))
        except Exception as e:  # noqa: BLE001
            v.reasons.append(f"move failed: {e}")
            continue
        moved += 1
        v.moved_to = str(dest / src.name)
        for c in companions.get(src, []):
            try:
                if c.exists():
                    shutil.move(str(c), str(dest / c.name))
            except Exception:  # noqa: BLE001
                pass
    return moved


def write_manifest(verdicts: list[Verdict], csv_path: Path, json_path: Path) -> None:
    rows = []
    for v in verdicts:
        m = v.metrics or {}
        rows.append({
            "file": v.path.name,
            "category": v.category,
            "reasons": "; ".join(v.reasons),
            "score": "" if v.score is None else f"{v.score:.3f}",
            "FWHM": "" if not m else f"{m.get('FWHM', ''):.4f}" if m.get("FWHM") is not None else "",
            "eccentricity": "" if not m else f"{m.get('eccentricity', ''):.4f}" if m.get("eccentricity") is not None else "",
            "stars": "" if not m else m.get("stars", ""),
            "noise": "" if not m else f"{m.get('noise', ''):.8f}" if m.get("noise") is not None else "",
            "trails": "" if not m else m.get("trails", 0),
            "moved_to": v.moved_to or "",
        })

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                           ["file", "category", "reasons", "score", "FWHM",
                            "eccentricity", "stars", "noise", "trails", "moved_to"])
        w.writeheader()
        w.writerows(rows)

    json_path.write_text(json.dumps({
        "generated_by": "assess-lights",
        "frames": [
            {
                "path": str(v.path),
                "category": v.category,
                "reasons": v.reasons,
                "metrics": v.metrics,
                "score": v.score,
                "moved_to": v.moved_to,
            }
            for v in verdicts
        ],
    }, indent=2, default=str), encoding="utf-8")
