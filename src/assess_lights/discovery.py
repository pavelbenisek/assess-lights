"""Step 1/8: does this directory contain a usable series of FITS frames?"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import FITS_SUFFIXES


@dataclass
class Discovery:
    directory: Path
    fits: list[Path]
    companions: dict[Path, list[Path]]   # fits -> sibling jpg/thumbnail files

    @property
    def count(self) -> int:
        return len(self.fits)


def find_companions(fits: Path) -> list[Path]:
    """Full-size .jpg and *_thn.jpg written alongside the sub by the capture app."""
    out: list[Path] = []
    for cand in (
        fits.with_suffix(".jpg"),
        fits.with_suffix(".jpeg"),
        fits.parent / (fits.stem + "_thn.jpg"),
        fits.parent / (fits.stem + "_thn.jpeg"),
    ):
        if cand.exists():
            out.append(cand)
    return out


def discover(directory: Path, min_frames: int = 1) -> Discovery | None:
    """Return a Discovery, or None when the directory is not a FITS series."""
    if not directory.is_dir():
        return None

    fits = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in FITS_SUFFIXES
    )
    if len(fits) < min_frames:
        return None

    companions = {p: find_companions(p) for p in fits}
    return Discovery(directory=directory, fits=fits, companions=companions)


MANIFEST_FILES = ("assessment.csv", "assessment.json")


def already_assessed(directory: Path) -> bool:
    """A directory that carries our manifest has been assessed before."""
    return any((directory / name).exists() for name in MANIFEST_FILES)


def find_subdirectories(root: Path, suffix: str = "_sub",
                        skip_assessed: bool = True) -> list[Path]:
    """Directories whose name ends with `suffix`, sorted for determinism.

    With `skip_assessed` (the default, and what cleanup wants) directories that
    already carry a manifest are skipped, so a re-run after a partial failure
    only processes what is left. Stacking wants the opposite -- the directories
    worth stacking are exactly the assessed ones -- so `--mode stack` passes
    skip_assessed=False.
    """
    root = root.expanduser().resolve()
    found: set[Path] = set()

    def wanted(p: Path) -> bool:
        return not (skip_assessed and already_assessed(p))

    if root.is_dir() and root.name.endswith(suffix) and wanted(root):
        found.add(root)

    for dirpath, dirnames, _files in os.walk(root):
        dirnames.sort()
        for name in dirnames:
            if name.startswith("."):
                continue
            if not name.endswith(suffix):
                continue
            cand = Path(dirpath) / name
            if wanted(cand):
                found.add(cand)

    return sorted(found)
