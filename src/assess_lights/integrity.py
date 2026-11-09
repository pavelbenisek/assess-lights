"""Step 2/8: structural integrity and 'otherwise damaged' detection.

Runs `fitsverify` when available (authoritative, from cfitsio), and always runs
the built-in checks, which catch things fitsverify cannot know about:

  * truncated / trailing data      -> expected on-disk size from the header
  * header that cannot be parsed   -> not a FITS file at all
  * geometry / dtype inconsistency -> frame does not belong to the series
  * exposure inconsistency         -> aborted sub
  * blank / dead frame             -> no signal at all
"""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BLOCK = 2880


@dataclass
class FrameHeader:
    path: Path
    simple: bool = False
    bitpix: int | None = None
    naxis: int | None = None
    shape: tuple[int, ...] = ()
    bscale: float | None = None
    bzero: float | None = None
    imagetyp: str | None = None
    exptime: float | None = None
    header_blocks: int = 0
    header: dict = field(default_factory=dict)

    @property
    def dtype_bytes(self) -> int:
        return abs(self.bitpix or 0) // 8


def _parse_header(path: Path, max_blocks: int = 64) -> FrameHeader:
    """Read FITS header cards the cheap way - no data, no dependencies."""
    fh = FrameHeader(path=path)
    cards: dict = {}
    try:
        with open(path, "rb") as f:
            for bi in range(1, max_blocks + 1):
                blk = f.read(BLOCK)
                if len(blk) < BLOCK:
                    raise ValueError("truncated header block")
                fh.header_blocks = bi
                done = False
                for i in range(36):
                    card = blk[i * 80:(i + 1) * 80].decode("ascii", "replace")
                    key = card[:8].strip()
                    if key == "END":
                        done = True
                        break
                    if not key or key == "COMMENT" or key == "HISTORY":
                        continue
                    if card[8:10] == "= ":
                        raw = card[10:].split("/")[0].strip()
                        if raw.startswith("'"):
                            cards[key] = raw.strip("'").strip()
                        else:
                            try:
                                cards[key] = int(raw)
                            except ValueError:
                                try:
                                    cards[key] = float(raw.replace("D", "E"))
                                except ValueError:
                                    cards[key] = raw
                if done:
                    break
    except Exception:
        return fh

    fh.header = cards
    fh.simple = str(cards.get("SIMPLE", "")).upper() in ("T", "TRUE")
    fh.bitpix = cards.get("BITPIX") if isinstance(cards.get("BITPIX"), int) else None
    fh.naxis = cards.get("NAXIS") if isinstance(cards.get("NAXIS"), int) else None
    fh.bscale = cards.get("BSCALE") if isinstance(cards.get("BSCALE"), (int, float)) else None
    fh.bzero = cards.get("BZERO") if isinstance(cards.get("BZERO"), (int, float)) else None
    fh.imagetyp = str(cards.get("IMAGETYP", "")).strip() or None
    for k in ("EXPTIME", "EXPOSURE"):
        v = cards.get(k)
        if isinstance(v, (int, float)):
            fh.exptime = float(v)
            break
    dims = []
    for i in range(1, (fh.naxis or 0) + 1):
        d = cards.get(f"NAXIS{i}")
        if isinstance(d, int):
            dims.append(d)
    # FITS axes are numbered fastest-varying first (NAXIS1 = columns), while a
    # numpy array is (rows, columns). Reverse so the shape is in numpy order --
    # reshaping with FITS order produces a sheared image, not a transpose.
    fh.shape = tuple(reversed(dims))
    return fh


def _expected_size(h: FrameHeader) -> int | None:
    if h.bitpix is None or not h.shape:
        return None
    data_bytes = h.dtype_bytes
    for d in h.shape:
        data_bytes *= d
    data_bytes = math.ceil(data_bytes / BLOCK) * BLOCK
    return BLOCK * h.header_blocks + data_bytes


def _has_fitsverify() -> bool:
    return shutil.which("fitsverify") is not None


def _fitsverify_ok(path: Path, timeout: int = 60) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["fitsverify", "-q", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"fitsverify failed to run: {e}"
    if p.returncode != 0:
        msg = (p.stdout + p.stderr).strip().splitlines()
        return False, "fitsverify: " + (msg[-1] if msg else "non-zero exit")
    return True, ""


def _blank_check(path: Path, h: FrameHeader, sigma_floor: float) -> tuple[bool, str]:
    """Subsampled standard deviation - catches dead / all-zero frames cheaply."""
    try:
        import numpy as np

        offset = BLOCK * h.header_blocks
        count = 1
        for d in h.shape:
            count *= d
        dt = {16: ">u2", 32: ">i4", -32: ">f4", -64: ">f8"}.get(h.bitpix or 0)
        if dt is None:
            return True, ""
        with open(path, "rb") as f:
            f.seek(offset)
            arr = np.fromfile(f, dtype=dt, count=count)
        if arr.size != count:
            return False, f"data section short: {arr.size} of {count} pixels"
        sub = arr[::16].astype("f8")
        if sub.size == 0:
            return True, ""
        sd = float(sub.std())
        span = float(sub.max() - sub.min())
        if sd < sigma_floor and span < 8 * sigma_floor:
            return False, f"blank / dead frame (std={sd:.4g}, span={span:.4g})"
        return True, ""
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        return False, f"unreadable data section: {e}"
    return True, ""


@dataclass
class Damage:
    path: Path
    reasons: list[str]

    @property
    def damaged(self) -> bool:
        return bool(self.reasons)


def check_integrity(paths: list[Path], sigma_floor: float = 3.0,
                    tick=None) -> tuple[list[Damage], dict[Path, FrameHeader]]:
    """Per-frame structural checks + series-consistency checks.

    `tick` is called once per frame so callers can drive a progress bar.
    """
    use_fv = _has_fitsverify()
    headers: dict[Path, FrameHeader] = {}
    damages: dict[Path, list[str]] = {p: [] for p in paths}

    for p in paths:
        if tick is not None:
            tick()
        h = _parse_header(p)
        headers[p] = h

        if not h.simple:
            damages[p].append("not a FITS file (SIMPLE missing)")
            continue
        if h.bitpix is None or not h.shape:
            damages[p].append("header unreadable or incomplete (BITPIX/NAXIS missing)")
            continue

        exp = _expected_size(h)
        actual = p.stat().st_size
        if exp is not None and actual != exp:
            damages[p].append(
                f"truncated or padded: {actual} bytes on disk, header implies {exp}"
            )

        try:
            import numpy as np
        except Exception:  # noqa: BLE001
            np = None
        if np is not None:
            ok, why = _blank_check(p, h, sigma_floor)
            if not ok:
                damages[p].append(why)

        if use_fv and not damages[p]:
            ok, why = _fitsverify_ok(p)
            if not ok:
                damages[p].append(why)

    # --- series consistency (only over frames whose header parsed) -------
    good = [p for p in paths if headers[p].simple and headers[p].shape]
    if good:
        shapes = {}
        for p in good:
            shapes.setdefault((headers[p].bitpix, headers[p].shape), []).append(p)
        if len(shapes) > 1:
            majority = max(shapes.items(), key=lambda kv: len(kv[1]))[0]
            for key, group in shapes.items():
                if key == majority:
                    continue
                for p in group:
                    damages[p].append(
                        f"geometry/dtype differs from the series: BITPIX={key[0]} shape={key[1]}"
                    )

        exps = {}
        for p in good:
            if headers[p].exptime is not None:
                exps.setdefault(round(headers[p].exptime, 3), []).append(p)
        if len(exps) > 1:
            majority = max(exps.items(), key=lambda kv: len(kv[1]))[0]
            for val, group in exps.items():
                if val == majority:
                    continue
                for p in group:
                    damages[p].append(f"EXPTIME differs from the series: {val}s")

        typs = {}
        for p in good:
            if headers[p].imagetyp:
                typs.setdefault(headers[p].imagetyp.upper(), []).append(p)
        if len(typs) > 1:
            majority = max(typs.items(), key=lambda kv: len(kv[1]))[0]
            for val, group in typs.items():
                if val == majority:
                    continue
                for p in group:
                    damages[p].append(f"IMAGETYP differs from the series: {val}")

    return [Damage(p, damages[p]) for p in paths], headers
