"""assess-lights CLI - the eight-step pipeline."""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from . import __version__
from . import stacking
from .actions import Verdict, move_frames, write_manifest
from .config import DEFAULT_PI_BINARY, Paths, Thresholds
from .debayer import stage_debayered
from .discovery import already_assessed, discover, find_subdirectories
from .integrity import check_integrity
from .pi_bridge import PixInsightError, measure, resolve_pi_binary
from .rank import rank_survivors, soft_mark
from .report import console, show_hard, show_soft, show_statistics
from .trails import detect_trails, read_fits_data

# tracked steps shown in the per-directory progress bar
STEPS = [
    "validate series",
    "integrity",
    "focus / shape",
    "trails",
    "rank",
    "review & move",
    "manifest & stats",
    "stack survivors",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assess-lights",
        description="Assess a directory of night-sky FITS light frames and mark the bad ones.",
        # no prefix matching: without this, a stale "--stack" silently resolves to
        # --stack-backend and fails with a baffling "expected one argument"
        allow_abbrev=False,
    )
    p.add_argument("directories", nargs="*", metavar="directory",
                   help="directory containing the FITS series. With --mode stack you may pass "
                        "several and each is stacked on its own. With --recursive: the single "
                        "root to walk.")
    p.add_argument("--version", action="version", version=f"assess-lights {__version__}")

    # The master switch comes first: it selects which group of operations runs,
    # and everything below it modifies that choice.
    op = p.add_argument_group("operation (master switch)")
    op.add_argument("--mode", choices=("cleanup", "stack", "stack-mosaic", "combined"),
                    default="cleanup",
                    help="which group of operations to run: "
                         "'cleanup' = steps 1-7, assess and cull (default); "
                         "'stack' = step 8 only, stack the FITS already in the folder "
                         "(this is the only mode that accepts several directories); "
                         "'stack-mosaic' = step 8 only via siril-cli, stitching a "
                         "multi-panel mosaic; "
                         "'combined' = all steps, cull and then stack the survivors")

    g = p.add_argument_group("actions")
    g.add_argument("--move", action="store_true",
                   help="actually move marked files (default: dry run, nothing is moved)")
    g.add_argument("--yes", "-y", action="store_true",
                   help="skip the confirmation prompts")
    g.add_argument("--recursive", action="store_true",
                   help="walk the given directory (default: cwd) and process every sub-directory "
                        "whose name ends in '_sub' and that has no assessment manifest yet")

    m = p.add_argument_group("thresholds")
    m.add_argument("--soft-percent", type=float, default=30.0,
                   help="worst N%% of surviving frames to soft-mark (default 30)")
    m.add_argument("--min-stars", type=int, default=12,
                   help="reject frames with fewer detected stars (default 12)")
    m.add_argument("--ecc-max", type=float, default=0.90,
                   help="reject frames with eccentricity above this (default 0.90)")
    m.add_argument("--fwhm-k", type=float, default=2.5,
                   help="reject frames with FWHM above median + k*MAD (default 2.5)")
    m.add_argument("--fwhm-max", type=float, default=5.0,
                   help="absolute FWHM backstop in px (default 5.0)")
    m.add_argument("--trail-min-frac", type=float, default=0.15,
                   help="minimum trail length as a fraction of the diagonal (default 0.15)")
    m.add_argument("--no-trails", action="store_true", help="skip trail detection")

    e = p.add_argument_group("engine")
    e.add_argument("--pi-binary", default=None,
                   help=f"PixInsight binary (default: {DEFAULT_PI_BINARY})")
    e.add_argument("--pi-batch", type=int, default=10,
                   help="frames per SubframeSelector call (default 10)")
    e.add_argument("--skip-pi", action="store_true",
                   help="skip step 3/8 entirely (integrity + trails + ranking only)")
    e.add_argument("--backend", choices=("auto", "pixinsight", "local"), default="auto",
                   help="step 3/8 measurement engine: 'pixinsight' (SubframeSelector), "
                        "'local' (built-in SEP/SExtractor engine, no PixInsight needed), "
                        "or 'auto' to use PixInsight when present and fall back to local "
                        "(default: auto)")
    e.add_argument("--debayer", action="store_true",
                   help="demosaic CFA frames to luminance before measuring shape "
                        "(more correct FWHM/eccentricity, slower, needs temp disk)")
    e.add_argument("--stack-backend", choices=("auto", "pixinsight", "siril"),
                   default="auto",
                   help="stacking engine: 'pixinsight' (StarAlignment + "
                        "ImageIntegration), 'siril' (siril-cli), or 'auto' to prefer "
                        "PixInsight and fall back to Siril (default: auto)")
    e.add_argument("--siril-binary", default=None,
                   help="siril-cli binary (default: found on PATH or "
                        "$ASSESS_LIGHTS_SIRIL_BINARY)")
    e.add_argument("--mosaic-feather", type=int, default=None, metavar="PX",
                   help="mosaic stitching: feather the panel borders over this many "
                        f"pixels (default: {stacking.MOSAIC_FEATHER})")
    e.add_argument("--mosaic-allow-partial", action="store_true",
                   help="mosaic stitching: write the mosaic even when some panels "
                        "failed to register (leaves gaps)")
    e.add_argument("--keep-workdir", action="store_true",
                   help="keep the PixInsight work dir and staged luminance frames for debugging")
    return p


def _make_progress() -> Progress:
    # rich disables live rendering when stdout is not a terminal (so logs and
    # pipes stay clean); ASSESS_LIGHTS_PROGRESS=1 forces the bars on anyway.
    import os
    force = os.environ.get("ASSESS_LIGHTS_PROGRESS") == "1"
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        disable=(not force) and not console.is_terminal,
        transient=False,
    )


def _begin(progress: Progress, steps_task, work_task, idx: int, title: str,
           total: int | None = None) -> None:
    progress.update(steps_task,
                    description=f"[green]step {idx + 1}/{len(STEPS)}[/green] [bold]{title}[/bold]")
    progress.reset(work_task, total=total, completed=0,
                   visible=total is not None, description=f"  [dim]{title}[/dim]")


def _end(progress: Progress, steps_task, work_task, note: str = "") -> None:
    progress.update(work_task, visible=False)
    if note:
        progress.update(steps_task, description=f"[green]step[/green] [dim]{note}[/dim]")
    progress.advance(steps_task)


def _say(idx: int, message: str) -> None:
    """Print a step line numbered from the same STEPS list the progress bar uses.

    These used to disagree: the printed lines followed the original spec (in which
    "request the image directory" was step 1) while the bar indexed STEPS, which
    excludes that step. That produced a consistent off-by-one against the bar and
    gave "step 8" two different meanings. Numbering both from STEPS removes both.
    """
    console.print(f"[green]step {idx + 1}/{len(STEPS)}[/green]  {message}")


def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _interactive() -> bool:
    """True when we are talking to a terminal, so prompting is safe."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_directory() -> Path | None:
    try:
        raw = input("Path to the directory containing the FITS series: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return Path(raw).expanduser() if raw else None


def _ask_extra_directories(known: list[Path]) -> list[Path]:
    """--mode stack only: optionally collect further folders to stack.

    Asked once per additional directory, defaulting to NO, so a plain <Enter>
    leaves the old single-directory behaviour untouched.
    """
    extra: list[Path] = []
    while True:
        which = "another" if extra else "an additional"
        try:
            raw = input(f"Add {which} directory to stack? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if raw not in ("y", "yes"):
            break
        try:
            raw = input("Path to the additional directory: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not raw:
            console.print("[dim]no path given, keeping the current list[/dim]")
            continue
        p = Path(raw).expanduser()
        if not p.is_dir():
            console.print(f"[red]not a directory:[/red] {p}")
            continue
        if p in known or p in extra:
            console.print(f"[yellow]already in the list:[/yellow] {p}")
            continue
        extra.append(p)
        console.print(f"[dim]added {p}[/dim]")
    return extra


def _ask_output_path() -> Path | None:
    """Ask where the merged master should go. None if the user gave up."""
    from . import stacking
    while True:
        try:
            raw = input("Path and filename for the combined output FITS: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        if not raw:
            return None
        p = Path(raw).expanduser()
        if p.is_dir():
            console.print(f"[red]that is a directory:[/red] {p} "
                          f"- give a filename too")
            continue
        if p.suffix.lower() not in stacking.FITS_SUFFIXES:
            if not p.suffix:
                p = p.with_suffix(".fit")
                console.print(f"[dim]no extension given, using {p.name}[/dim]")
            else:
                console.print(f"[red]not a FITS file:[/red] {p.suffix} "
                              f"(use .fit, .fits, .fts or .xisf)")
                continue
        if not p.parent.exists():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                console.print(f"[dim]created {p.parent}[/dim]")
            except OSError as e:
                console.print(f"[red]cannot create {p.parent}:[/red] {e}")
                continue
        return p


def _run_stack(paths, args, progress: Progress, steps_task, work_task) -> int:
    """Step 8/8 on its own: register and stack the FITS left in the folder."""
    from . import stacking
    console.print()
    _begin(progress, steps_task, work_task, 7, STEPS[7])
    try:
        out, engine = stacking.stack(
            paths.target, backend=args.stack_backend,
            pi_binary=args.pi_binary, siril_binary=args.siril_binary,
            progress=lambda s: progress.console.print(f"        [dim]{s}[/dim]"),
            tick=lambda: progress.advance(work_task),
        )
    except stacking.StackError as e:
        _end(progress, steps_task, work_task, "stack failed")
        console.print(f"[red]stacking failed:[/red]\n{e}")
        return 1
    _end(progress, steps_task, work_task, "stacked")
    mb = out.stat().st_size / 1e6
    _say(7, f"stacked with [bold]{engine}[/bold] -> "
            f"[green]{out.name}[/green] ({mb:.1f} MB) in {paths.target}")
    return 0


def _run_mosaic(paths, frames, args, progress: Progress,
                steps_task, work_task) -> int:
    """Step 8/8 via siril-cli: debayer the panels, register, stitch a mosaic."""
    binary = stacking.resolve_siril_binary(args.siril_binary)
    if not binary:
        console.print("[red]mosaic stitching needs siril-cli[/red] - PixInsight has no "
                      "mosaic step here.\n"
                      "[dim]macOS: brew install siril. A NAS can run Siril's AppImage. "
                      "Or pass --siril-binary / set ASSESS_LIGHTS_SIRIL_BINARY[/dim]")
        return 1

    feather = (args.mosaic_feather if args.mosaic_feather is not None
               else stacking.MOSAIC_FEATHER)
    console.print()
    _begin(progress, steps_task, work_task, 7, "stitch mosaic")

    dest, note = stacking.unique_path(paths.target / f"{stacking.MOSAIC_STEM}.fit")
    if note:
        console.print(f"        [dim]{note}[/dim]")

    try:
        out, info = stacking.stack_mosaic(
            frames, dest, binary, feather=feather,
            allow_partial=args.mosaic_allow_partial,
            progress=lambda s: progress.console.print(f"        [dim]{s}[/dim]"),
            tick=lambda: progress.advance(work_task),
        )
    except stacking.StackError as e:
        _end(progress, steps_task, work_task, "mosaic failed")
        console.print(f"[red]mosaic stitching failed:[/red]\n{e}")
        return 1

    _end(progress, steps_task, work_task, "stitched")
    mb = out.stat().st_size / 1e6
    canvas = info.get("canvas")
    dims = f"{canvas[0]}x{canvas[1]}" if canvas else "unknown size"
    stacked = info.get("frames_stacked")
    _say(7, f"stitched with [bold]siril[/bold] -> [green]{out.name}[/green] "
            f"({mb:.1f} MB, {dims}) in {paths.target}")
    console.print(
        f"        [dim]{stacked}/{info['frames_in']} frame(s) integrated, "
        f"{info['registration']} registration, feather {info['feather']}px[/dim]")
    if stacked is not None and stacked < info["frames_in"]:
        console.print(
            f"        [yellow]{info['frames_in'] - stacked} panel(s) did not register - "
            f"the mosaic has gaps (--mosaic-allow-partial)[/yellow]")
    return 0


def _run_merged(dirs, dest, args, progress: Progress,
                steps_task, work_task) -> int:
    """Step 8/8 over several directories at once, merged into one master."""
    from . import stacking
    frames = stacking.gather_frames(dirs)
    counts = ", ".join(f"{d.name}: {len(stacking.source_frames(d))}" for d in dirs)
    console.print(f"[dim]{len(frames)} FITS frame(s) from {len(dirs)} directories "
                  f"({counts}) - merging into one master[/dim]")
    console.print()
    _begin(progress, steps_task, work_task, 7, STEPS[7])
    try:
        out, engine = stacking.stack_frames(
            frames, dest, backend=args.stack_backend,
            pi_binary=args.pi_binary, siril_binary=args.siril_binary,
            progress=lambda s: progress.console.print(f"        [dim]{s}[/dim]"),
            tick=lambda: progress.advance(work_task),
        )
    except stacking.StackError as e:
        _end(progress, steps_task, work_task, "stack failed")
        console.print(f"[red]stacking failed:[/red]\n{e}")
        return 1
    _end(progress, steps_task, work_task, "stacked")
    mb = out.stat().st_size / 1e6
    _say(7, f"stacked with [bold]{engine}[/bold] -> "
            f"[green]{out}[/green] ({mb:.1f} MB)")
    return 0


def process_one(target: Path, args, th: Thresholds,
                progress: Progress, steps_task, work_task) -> int:
    """Run the requested group(s) of operations for a single directory."""
    paths = Paths(target=target)
    progress.update(steps_task, description=f"[green]{target.name}[/green]")

    # --mode stack: step 8/8 alone, on whatever FITS the folder already holds
    if args.mode == "stack":
        from . import stacking
        frames = stacking.source_frames(target)
        console.print(f"[dim]{len(frames)} FITS frame(s) in {target} "
                      f"- stacking only, no assessment[/dim]")
        return _run_stack(paths, args, progress, steps_task, work_task)

    # --mode stack-mosaic: step 8/8 alone, stitching the panels into one image
    if args.mode == "stack-mosaic":
        from . import stacking
        frames = stacking.source_frames(target)
        console.print(f"[dim]{len(frames)} FITS frame(s) in {target} "
                      f"- stitching a mosaic, no assessment[/dim]")
        return _run_mosaic(paths, frames, args, progress, steps_task, work_task)

    # ---- step 1/8: is this a FITS series? ---------------------------------
    _begin(progress, steps_task, work_task, 0, STEPS[0])
    disc = discover(target, min_frames=th.require_min_frames)
    if disc is None:
        _end(progress, steps_task, work_task, "not a FITS series")
        console.print(f"[red]{target}: no FITS frames found - skipped[/red]")
        return 1
    _end(progress, steps_task, work_task, f"{disc.count} FITS frames")
    _say(0, f"{target.name}: [bold]{disc.count}[/bold] FITS frame(s)"
            f" (plus {sum(len(v) for v in disc.companions.values())} companion jpg file(s))")

    # ---- step 2/8: damaged ------------------------------------------------
    _begin(progress, steps_task, work_task, 1, STEPS[1], total=disc.count)
    damages, headers = check_integrity(disc.fits, th.blank_sigma,
                                       tick=lambda: progress.advance(work_task))
    damaged_paths = {d.path for d in damages if d.damaged}
    _end(progress, steps_task, work_task, f"{len(damaged_paths)} damaged")
    _say(1, f"structural checks: [red]{len(damaged_paths)}[/red] flagged")

    # ---- step 3/8: focus / shape (PixInsight SubframeSelector or SEP) -----
    metrics: dict = {}
    survival = [p for p in disc.fits if p not in damaged_paths]
    blur_paths: set[Path] = set()
    blur_reasons: dict[Path, list[str]] = {}
    staged: dict[Path, Path] = {}
    lum_dir: Path | None = None

    if args.skip_pi:
        _begin(progress, steps_task, work_task, 2, STEPS[2])
        _end(progress, steps_task, work_task, "skipped")
        _say(2, "[yellow]skipped (--skip-pi)[/yellow]")
    elif not survival:
        _begin(progress, steps_task, work_task, 2, STEPS[2])
        _end(progress, steps_task, work_task, "nothing to measure")
        _say(2, "no frames left to measure")
    else:
        # ---- choose the measurement engine --------------------------------
        backend = args.backend
        pi = None
        if backend in ("auto", "pixinsight"):
            try:
                pi = resolve_pi_binary(args.pi_binary)
                backend = "pixinsight"
            except PixInsightError as e:
                if backend == "pixinsight":
                    console.print(f"[red]{e}[/red]")
                    console.print("[dim]Use --backend local to measure without "
                                  "PixInsight.[/dim]")
                    return 1
                backend = "local"
                _say(2, "[yellow]PixInsight not found[/yellow] "
                        "[dim]- falling back to the built-in SEP backend[/dim]")

        if backend == "local":
            from .localmetrics import measure as measure_local
            _begin(progress, steps_task, work_task, 2, "measuring (SEP)",
                   total=len(survival))
            try:
                metrics = measure_local(
                    survival, headers, debayer=args.debayer,
                    progress=lambda s: progress.console.print(f"        [dim]{s}[/dim]"),
                    tick=lambda *a: progress.advance(work_task),
                )
            except Exception as e:  # noqa: BLE001
                _end(progress, steps_task, work_task, "local measurement failed")
                console.print(f"[red]built-in SEP measurement failed:[/red]\n{e}")
                return 1
            _end(progress, steps_task, work_task, f"{len(metrics)} measured")
            _say(2, f"built-in SEP backend measured [bold]{len(metrics)}[/bold] frame(s)"
                    + ("  [dim](on debayered luminance)[/dim]" if args.debayer else ""))
            console.print("[dim]        SEP is not SubframeSelector: absolute FWHM runs "
                          "slightly tight and the blur cut is its own. Tune with "
                          "--fwhm-k / --min-stars / --ecc-max if needed.[/dim]")
        else:
            workspace = (paths.target / ".assess-lights-pi") if args.keep_workdir else None
            to_measure = survival

            # optional: measure on debayered luminance rather than the raw CFA mosaic
            if args.debayer:
                import tempfile
                lum_dir = (workspace / "luminance") if workspace else Path(
                    tempfile.mkdtemp(prefix="assess-lights-lum-"))
                _begin(progress, steps_task, work_task, 2, "demosaic to luminance",
                       total=len(survival))
                staged = stage_debayered(survival, headers, lum_dir,
                                         progress=lambda s: progress.console.print(f"        [dim]{s}[/dim]"),
                                         tick=lambda: progress.advance(work_task))
                to_measure = [staged[p] for p in survival if p in staged]
                if not to_measure:
                    _end(progress, steps_task, work_task, "demosaic failed")
                    console.print("[red]--debayer produced no luminance frames[/red]")
                    return 1

            _begin(progress, steps_task, work_task, 2, STEPS[2], total=len(to_measure))
            try:
                measured = measure(
                    to_measure, pi, batch=args.pi_batch, workdir=workspace,
                    progress=lambda s: progress.console.print(f"        [dim]{s}[/dim]"),
                    tick=lambda n: progress.update(work_task, completed=n),
                )
            except PixInsightError as e:
                _end(progress, steps_task, work_task, "PixInsight failed")
                console.print(f"[red]PixInsight measurement failed:[/red]\n{e}")
                return 1

            # map staged luminance results back onto the original frames
            if staged:
                back = {v: k for k, v in staged.items()}
                metrics = {}
                for p, m in measured.items():
                    orig = back.get(p)
                    if orig is not None:
                        metrics[orig] = replace(m, path=orig)
            else:
                metrics = measured

            _end(progress, steps_task, work_task, f"{len(metrics)} measured")
            _say(2, f"SubframeSelector measured [bold]{len(metrics)}[/bold] frame(s)"
                    + ("  [dim](on debayered luminance)[/dim]" if staged else ""))

        # Measurement regimes are not interchangeable: interpolation suppresses
        # noise by ~an order of magnitude, and SubframeSelector thresholds are
        # relative to that noise, so both the star count and FWHM shift a lot.
        if staged and metrics:
            import numpy as np
            st = [m.stars for m in metrics.values()]
            nz = [m.noise for m in metrics.values()]
            med_stars = float(np.median(st))
            med_noise = float(np.median(nz))
            console.print(
                f"        [dim]debayered regime: median stars {med_stars:.0f}, "
                f"median noise {med_noise:.6g}[/dim]"
            )
            if th.min_stars and med_stars > 3 * th.min_stars:
                console.print(
                    f"        [yellow]note: --min-stars {th.min_stars} was calibrated on the raw CFA "
                    f"mosaic, where frames carry ~20-50 stars. On debayered luminance the median is "
                    f"{med_stars:.0f}, so this test will barely reject anything.[/yellow]\n"
                    f"        [yellow]Raise it (e.g. --min-stars {int(med_stars * 0.4)}) or drop "
                    f"--debayer for culling.[/yellow]"
                )

        # thresholds: robust on FWHM, absolute floors for stars / eccentricity
        vals = sorted(m.FWHM for m in metrics.values())
        if vals:
            import numpy as np
            med = float(np.median(vals))
            mad = float(np.median([abs(v - med) for v in vals]))
            fwhm_cut = med + th.fwhm_mad_k * 1.4826 * mad
            console.print(f"        FWHM median {med:.3f} px, MAD {mad:.3f} -> cut at {fwhm_cut:.3f} px")
            for p, m in metrics.items():
                why = []
                if m.stars < th.min_stars:
                    why.append(f"too few stars ({m.stars} < {th.min_stars}) - tracking/cloud failure")
                if m.FWHM > fwhm_cut:
                    why.append(f"FWHM {m.FWHM:.3f} px above {fwhm_cut:.3f} (median+{th.fwhm_mad_k} MAD)")
                if m.FWHM > th.fwhm_abs_max:
                    why.append(f"FWHM {m.FWHM:.3f} px above absolute limit {th.fwhm_abs_max}")
                if m.eccentricity > th.eccentricity_abs_max:
                    why.append(f"eccentricity {m.eccentricity:.3f} above {th.eccentricity_abs_max}")
                if why:
                    blur_paths.add(p)
                    blur_reasons[p] = why
        console.print(f"        [red]{len(blur_paths)}[/red] flagged for focus/shape")

    # ---- step 4/8: trails -------------------------------------------------
    trail_paths: set[Path] = set()
    trail_reasons: dict[Path, list[str]] = {}
    trail_info: dict[Path, list] = {}
    candidates = [p for p in survival if p not in blur_paths]

    if args.no_trails:
        _begin(progress, steps_task, work_task, 3, STEPS[3])
        _end(progress, steps_task, work_task, "skipped")
        _say(3, "[yellow]skipped (--no-trails)[/yellow]")
    else:
        _begin(progress, steps_task, work_task, 3, STEPS[3], total=len(candidates))
        for p in candidates:
            h = headers.get(p)
            if h is None or not h.shape:
                progress.advance(work_task)
                continue
            try:
                data = read_fits_data(p, bitpix=h.bitpix or 16,
                                      header_blocks=h.header_blocks, shape=h.shape)
                found = detect_trails(data, min_length_frac=th.trail_min_length_frac)
            except Exception as e:  # noqa: BLE001
                console.print(f"        [yellow]{p.name}: trail check skipped ({e})[/yellow]")
                progress.advance(work_task)
                continue
            if found:
                trail_paths.add(p)
                trail_info[p] = [t.as_dict() for t in found]
                trail_reasons[p] = [f"{len(found)} linear trail(s), longest {found[0].length:.0f} px"]
            progress.advance(work_task)
        _end(progress, steps_task, work_task, f"{len(trail_paths)} trailed")
        _say(3, f"trail detection: [red]{len(trail_paths)}[/red] flagged")

    # ---- step 5/8: lucky-imaging soft mark --------------------------------
    _begin(progress, steps_task, work_task, 4, STEPS[4])
    rank_pool = [p for p in survival if p not in blur_paths and p not in trail_paths]
    ranked = rank_survivors(rank_pool, metrics, th)
    soft_paths = soft_mark(ranked, th.soft_percent)
    _end(progress, steps_task, work_task, f"{len(soft_paths)} soft-marked")
    _say(4, f"ranked {len(ranked)} survivor(s); soft-marked worst "
            f"[bold]{len(soft_paths)}[/bold] ({th.soft_percent:g}%)")

    # ---- assemble verdicts ------------------------------------------------
    def mdict(p: Path) -> dict | None:
        m = metrics.get(p)
        if m is None:
            return None
        return {"FWHM": m.FWHM, "eccentricity": m.eccentricity,
                "stars": m.stars, "noise": m.noise}

    verdicts: list[Verdict] = []
    for p in disc.fits:
        if p in damaged_paths:
            d = next(x for x in damages if x.path == p)
            verdicts.append(Verdict(p, "damaged", list(d.reasons)))
        elif p in blur_paths:
            verdicts.append(Verdict(p, "blur", blur_reasons.get(p, []), metrics=mdict(p)))
        elif p in trail_paths:
            tdict = dict(mdict(p) or {})
            tdict["trails"] = len(trail_info.get(p, []))
            verdicts.append(Verdict(p, "trail", trail_reasons.get(p, []), metrics=tdict))
        elif p in soft_paths:
            r = next((x for x in ranked if x.path == p), None)
            verdicts.append(Verdict(
                p, "soft", ["worst of the survivors (lucky-imaging soft mark)"],
                metrics=mdict(p), score=None if r is None else r.score))
        else:
            verdicts.append(Verdict(p, "kept", [], metrics=mdict(p)))

    # ---- step 6/8: present hard marks, confirm ----------------------------
    _begin(progress, steps_task, work_task, 5, STEPS[5])
    console.print()
    _say(5, "review and move")
    show_hard(verdicts)
    hard = [v for v in verdicts if v.category in ("damaged", "blur", "trail")]

    moved_hard = 0
    if hard:
        if args.move:
            if _confirm(f"Move these {len(hard)} frame(s) to {paths.rejected.name}/ ?", args.yes):
                moved_hard = move_frames(hard, paths.rejected, disc.companions, apply=True)
                console.print(f"[cyan]moved {moved_hard} frame(s) to {paths.rejected}[/cyan]")
            else:
                console.print("[yellow]not moved[/yellow]")
        else:
            console.print(f"[yellow]dry run - nothing moved. Would move to:[/yellow] {paths.rejected}")

    # ---- step 6/8 (cont.): soft marks -> unlucky-images -------------------
    console.print()
    show_soft(verdicts, th.soft_percent)
    soft = [v for v in verdicts if v.category == "soft"]

    moved_soft = 0
    if soft and args.move:
        if _confirm(f"Move these {len(soft)} soft-marked frame(s) to {paths.unlucky.name}/ ?", args.yes):
            moved_soft = move_frames(soft, paths.unlucky, disc.companions, apply=True)
            console.print(f"[cyan]moved {moved_soft} frame(s) to {paths.unlucky}[/cyan]")
        else:
            console.print("[yellow]not moved[/yellow]")
    _end(progress, steps_task, work_task, "reviewed")

    # ---- step 7/8: manifest + statistics ----------------------------------
    _begin(progress, steps_task, work_task, 6, STEPS[6])
    _say(6, "manifest and statistics")
    write_manifest(verdicts, paths.manifest, paths.manifest_json)
    console.print(f"\nwrote manifests: [dim]{paths.manifest.name}, {paths.manifest_json.name}[/dim]")
    console.print()
    show_statistics(verdicts, paths, moved_hard, moved_soft)
    _end(progress, steps_task, work_task, "done")

    if lum_dir is not None and not args.keep_workdir:
        shutil.rmtree(lum_dir, ignore_errors=True)

    if args.mode == "combined":
        return _run_stack(paths, args, progress, steps_task, work_task)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    th = Thresholds(
        min_stars=args.min_stars,
        eccentricity_abs_max=args.ecc_max,
        fwhm_mad_k=args.fwhm_k,
        fwhm_abs_max=args.fwhm_max,
        trail_min_length_frac=args.trail_min_frac,
        soft_percent=args.soft_percent,
    )

    # ---- directory selection (before the numbered pipeline steps) ---------
    dirs = [Path(d).expanduser() for d in args.directories]
    merged_dest: Path | None = None

    if args.recursive and len(dirs) > 1:
        console.print("[red]--recursive takes a single root directory[/red]")
        return 2
    # several directories only make sense for step 8/8: the culling pipeline
    # numbers its steps per directory and writes a manifest into each
    if len(dirs) > 1 and args.mode != "stack":
        console.print(
            f"[red]multiple directories need --mode stack[/red] "
            f"(got {len(dirs)} with --mode {args.mode})\n"
            f"[dim]--mode {args.mode} processes one directory at a time; "
            f"--mode stack accepts several and stacks each on its own[/dim]")
        return 2

    if args.recursive:
        root = dirs[0] if dirs else Path.cwd()
        if not root.is_dir():
            console.print(f"[red]not a directory:[/red] {root}")
            return 2
        stack_only = args.mode in ("stack", "stack-mosaic")
        # stacking wants the directories that cleanup already assessed
        targets = find_subdirectories(root, skip_assessed=not stack_only)
        if not targets:
            console.print(
                f"[yellow]No directories ending in '_sub' under[/yellow] {root}"
                + ("" if stack_only else
                   "\n[dim]directories that already contain assessment.csv/json are "
                   "skipped (--mode stack includes them)[/dim]")
            )
            return 1
        console.print(Panel(
            f"[bold]{len(targets)}[/bold] "
            f"director{'y' if len(targets) == 1 else 'ies'} to "
            f"{'stack' if stack_only else 'assess'} under\n{root}",
            title=f"assess-lights --recursive --mode {args.mode}", border_style="cyan",
        ))
    else:
        if not dirs:
            first = _ask_directory()
            if first is None:
                console.print("[red]no directory given[/red]")
                return 2
            dirs = [first]
        # --mode stack can take more than one folder; offer it only where a
        # human can answer, and never when --yes asked for no prompts
        if args.mode == "stack" and not args.yes and _interactive():
            dirs += _ask_extra_directories(dirs)
        for d in dirs:
            if not d.is_dir():
                console.print(f"[red]not a directory:[/red] {d}")
                return 2
        for d in dirs:
            if args.mode != "stack" and already_assessed(d):
                console.print(f"[dim]{d} already has an assessment manifest "
                              f"- re-assessing anyway[/dim]")
        # --mode stack with several directories: offer one merged master instead
        # of one master per directory. Both answers are honoured exactly.
        if (args.mode == "stack" and len(dirs) > 1
                and not args.yes and _interactive()):
            if _confirm(f"Stack all FITS from all {len(dirs)} directories into "
                        f"a single output file?", False):
                merged_dest = _ask_output_path()
                if merged_dest is None:
                    console.print("[yellow]no usable output path - falling back to "
                                  "one master per directory[/yellow]")

        targets = dirs
        if merged_dest is not None:
            listing = "\n".join(f"  [bold]{i+1}.[/bold] {d}" for i, d in enumerate(dirs))
            console.print(Panel(
                f"[bold]{len(dirs)}[/bold] directories merged into a single master\n"
                f"{listing}\n\n[dim]output:[/dim] [bold]{merged_dest}[/bold]",
                title="assess-lights --mode stack (merged)", border_style="cyan"))
        elif len(targets) == 1:
            console.print(Panel(f"[bold]{targets[0]}[/bold]  [dim]--mode {args.mode}[/dim]",
                                title="assess-lights", border_style="cyan"))
        else:
            listing = "\n".join(f"  [bold]{i+1}.[/bold] {d}" for i, d in enumerate(targets))
            console.print(Panel(
                f"[bold]{len(targets)}[/bold] directories to stack, each on its own\n{listing}",
                title=f"assess-lights --mode {args.mode}", border_style="cyan"))

    # ---- run ---------------------------------------------------------------
    if merged_dest is not None:
        with _make_progress() as progress:
            steps_task = progress.add_task("[green]steps[/green]", total=1)
            work_task = progress.add_task("  ", total=None, visible=False)
            return _run_merged(dirs, merged_dest, args, progress,
                               steps_task, work_task)

    failures = 0
    with _make_progress() as progress:
        overall = None
        if len(targets) > 1:
            overall = progress.add_task("[cyan]directories[/cyan]", total=len(targets))
        # --mode stack runs step 8/8 alone, so its bar counts one step; showing
        # it against a total of 8 read as "step 8/8 ... 1/8"
        steps_total = 1 if args.mode in ("stack", "stack-mosaic") else len(STEPS)
        steps_task = progress.add_task("[green]steps[/green]", total=steps_total)
        work_task = progress.add_task("  ", total=None, visible=False)

        for i, target in enumerate(targets):
            if overall is not None:
                progress.update(overall,
                                description=f"[cyan]directories[/cyan] [dim]{target.name}[/dim]")
            progress.reset(steps_task, total=steps_total, completed=0)
            if process_one(target, args, th, progress, steps_task, work_task) != 0:
                failures += 1
            if overall is not None:
                progress.advance(overall)
            if i + 1 < len(targets):
                console.print()

    if len(targets) > 1:
        ok = len(targets) - failures
        tail = f", [red]{failures} failed[/red]" if failures else ""
        console.print(f"\n[bold]{ok}/{len(targets)}[/bold] "
                      f"director{'y' if len(targets) == 1 else 'ies'} processed successfully{tail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
