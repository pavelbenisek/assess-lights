#!/usr/bin/env python3
"""Keep the version/author line at the top of README.md in sync with pyproject.toml.

`pyproject.toml` is the single source of truth for both the version and the
author. This rewrites only the `<sub>` meta line near the top of the README, and
only when the version or author actually differs -- so ordinary installs do not
churn the file or move its "last updated" date.

    python scripts/sync_readme.py            # rewrite if out of sync
    python scripts/sync_readme.py --check    # write nothing; exit 1 if out of sync

Requires Python 3.11+ for `tomllib` (the project venv is 3.12).
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"

# the meta line is the only <sub>...</sub> line in the README
META_RE = re.compile(r"(?m)^<sub>.*?</sub>$")


def _escape(text: str) -> str:
    """< and > must be entities inside the HTML tag or they end the tag."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def read_pyproject() -> tuple[str, str]:
    """Return (version, author string) from pyproject.toml."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.11+ expected
        sys.exit("sync_readme.py needs Python 3.11+ (tomllib); "
                 "run it with the project venv: .venv/bin/python scripts/sync_readme.py")

    with PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]

    version = str(project["version"]).strip()

    authors = []
    for entry in project.get("authors") or []:
        name = (entry.get("name") or "").strip()
        email = (entry.get("email") or "").strip()
        if name and email:
            authors.append(f"{name} <{email}>")
        elif name or email:
            authors.append(name or email)
    if not authors:
        sys.exit("pyproject.toml has no [project] authors to put in the README")
    return version, ", ".join(authors)


def build_line(version: str, author: str, date: str) -> str:
    return f"<sub>v{version} · last updated {date} · {_escape(author)}</sub>"


def split_line(line: str) -> list[str]:
    return [p.strip() for p in line.removeprefix("<sub>").removesuffix("</sub>").split("·")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if out of sync; write nothing")
    args = ap.parse_args()

    version, author = read_pyproject()
    text = README.read_text(encoding="utf-8")

    match = META_RE.search(text)
    if match is None:
        print("README.md has no '<sub>...</sub>' meta line to sync", file=sys.stderr)
        return 1

    current = match.group(0)
    fields = split_line(current)

    # fields are [v<version>, last updated <date>, <author>]; compare the parts
    # this script owns and ignore the date, so unchanged installs are no-ops.
    in_sync = (len(fields) == 3
               and fields[0] == f"v{version}"
               and fields[2] == _escape(author))

    if in_sync:
        print(f"README meta line in sync: v{version}")
        return 0

    if args.check:
        print("README meta line is OUT OF SYNC with pyproject.toml", file=sys.stderr)
        print(f"  README : {current}", file=sys.stderr)
        print(f"  wanted : {build_line(version, author, '<today>')}", file=sys.stderr)
        return 1

    date = datetime.date.today().isoformat()
    new = build_line(version, author, date)
    README.write_text(text[:match.start()] + new + text[match.end():], encoding="utf-8")
    print(f"README meta line updated: {current}  ->  {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
