"""Steps 7 and 9: present the findings, then the statistics."""

from __future__ import annotations

import statistics as st
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .actions import Verdict

console = Console()


def _fmt(v, nd=2, dash="-"):  # noqa: ANN001
    if v is None:
        return dash
    if isinstance(v, (int,)):
        return str(v)
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def show_hard(verdicts: list[Verdict]) -> None:
    """Step 6/8: files marked for removal by the hard filters (not soft-marked)."""
    hard = [v for v in verdicts if v.category in ("damaged", "blur", "trail")]
    if not hard:
        console.print("[green]No frames marked for removal.[/green]")
        return

    t = Table(title=f"Marked for removal ({len(hard)})", show_lines=False)
    t.add_column("frame", overflow="fold")
    t.add_column("category", style="red")
    t.add_column("FWHM", justify="right")
    t.add_column("ecc", justify="right")
    t.add_column("stars", justify="right")
    t.add_column("reason", overflow="fold", style="dim")
    for v in hard:
        m = v.metrics or {}
        t.add_row(v.path.name, v.category, _fmt(m.get("FWHM")),
                  _fmt(m.get("eccentricity")), _fmt(m.get("stars")),
                  "; ".join(v.reasons))
    console.print(t)


def show_soft(verdicts: list[Verdict], percent: float) -> None:
    """Step 6/8: the lucky-imaging soft marks."""
    soft = [v for v in verdicts if v.category == "soft"]
    if not soft:
        console.print("[green]Nothing soft-marked.[/green]")
        return
    t = Table(title=f"Soft-marked for removal - unlucky images, worst {percent:g}% ({len(soft)})")
    t.add_column("frame", overflow="fold")
    t.add_column("score", justify="right", style="yellow")
    t.add_column("FWHM", justify="right")
    t.add_column("ecc", justify="right")
    t.add_column("stars", justify="right")
    for v in sorted(soft, key=lambda x: (x.score is None, -(x.score or 0))):
        m = v.metrics or {}
        t.add_row(v.path.name, _fmt(v.score, 3), _fmt(m.get("FWHM")),
                  _fmt(m.get("eccentricity")), _fmt(m.get("stars")))
    console.print(t)


def show_statistics(verdicts: list[Verdict], paths, moved_hard: int, moved_soft: int) -> None:
    """Step 7/8: summary. `paths` is a config.Paths."""
    target = paths.target
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.category] = counts.get(v.category, 0) + 1

    t = Table(title="Assessment summary", show_header=True)
    t.add_column("category")
    t.add_column("frames", justify="right")
    t.add_column("note", style="dim")
    notes = {
        "kept": "surviving frames, ready to stack",
        "damaged": "structural / header / blank problems",
        "blur": "focus, shape or star-count failure",
        "trail": "satellite or aircraft trail",
        "soft": "lucky-imaging soft mark (worst of the survivors)",
    }
    for cat in ("kept", "damaged", "blur", "trail", "soft"):
        if cat in counts:
            t.add_row(cat, str(counts[cat]), notes.get(cat, ""))
    console.print(t)

    # metric spread: kept vs removed
    def col(pred, key):
        vals = [ (v.metrics or {}).get(key) for v in verdicts if pred(v) ]
        vals = [float(x) for x in vals if x is not None]
        return vals

    kept = lambda v: v.category == "kept"  # noqa: E731
    out = lambda v: v.category != "kept"   # noqa: E731

    m = Table(title="Metric distribution")
    m.add_column("metric")
    m.add_column("kept (median)", justify="right")
    m.add_column("kept (range)", justify="right")
    m.add_column("removed (median)", justify="right")
    m.add_column("removed (range)", justify="right")
    for key, nd in (("FWHM", 3), ("eccentricity", 3), ("stars", 1), ("noise", 6)):
        k = col(kept, key)
        r = col(out, key)
        m.add_row(
            key,
            _fmt(st.median(k), nd) if k else "-",
            f"{_fmt(min(k), nd)}..{_fmt(max(k), nd)}" if k else "-",
            _fmt(st.median(r), nd) if r else "-",
            f"{_fmt(min(r), nd)}..{_fmt(max(r), nd)}" if r else "-",
        )
    console.print(m)

    if moved_hard or moved_soft:
        lines = []
        if moved_hard:
            lines.append(f"moved {moved_hard} frame(s) to [bold]{paths.rejected}[/bold]")
        if moved_soft:
            lines.append(f"moved {moved_soft} frame(s) to [bold]{paths.unlucky}[/bold]")
        lines.append("[dim]nothing was deleted - move them back to undo[/dim]")
        console.print(Panel("\n".join(lines), title="Files moved", border_style="cyan"))
    else:
        console.print(Panel(
            "Dry run - no files were moved.\n"
            f"hard-marked frames would go to  [bold]{paths.rejected}[/bold]\n"
            f"soft-marked frames would go to  [bold]{paths.unlucky}[/bold]",
            title="Dry run", border_style="yellow",
        ))
