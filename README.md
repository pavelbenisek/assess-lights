# assess-lights

<sub>v0.6.0 · last updated 2026-10-03 · Pavel Benisek &lt;pavel@benisek.eu&gt;</sub>

Assess a directory of night-sky FITS light frames, mark the bad ones for removal,
and optionally stack what survives.

It culls in one pass:

- **damaged** files — truncated, bad geometry, unreadable headers, blank frames
- **blurred / distorted** frames — FWHM, eccentricity and star count
- **satellite and aircraft trails**
- **the worst N% of the survivors** — a soft mark for lucky imaging

With `--mode combined` — or `--mode stack` on an already-culled folder — it then
registers and combines the surviving frames into a single master.

## Guarantees

- **Nothing is ever deleted.** Marked frames are *moved* into sibling
  directories, so moving them back undoes everything.
- **One master switch.** `--mode {cleanup,stack,stack-mosaic,combined}` selects the
  group of operations; every other flag modifies that choice.
- **Dry run by default.** Nothing moves unless you pass `--move`.
- **No PixInsight required.** Step 3/8 falls back to a built-in SEP engine
  automatically; stacking falls back to `siril-cli`.
- **The source folder stays tidy.** Stacking runs in a scratch directory, and
  only the final stack or mosaic is written back. Cleanup writes assessment manifests even without `--move`.

## Contents

- [Install](#install) · [Quick start](#quick-start) · [Parameters](#command-parameters) · [Examples](#examples)
- [What the pipeline does](#what-the-pipeline-does) · [Output layout](#output-layout) · [Thresholds](#thresholds)
- [Operation modes](#operation-modes-master-switch) · [Measurement engines](#measurement-engines-step-38) · [Stacking](#stacking-step-88) · [Mosaics](#mosaics---mode-stack-mosaic) · [Several directories](#stacking-several-directories-at-once)
- [Installing on another host](#installing-on-another-host-eg-a-nas) · [PixInsight bridge notes](#notes-on-the-pixinsight-bridge)
- [Trail detector](#validating-the-trail-detector) · [FITS gotcha](#fits-reading-gotcha)

## License

This project's code and documentation are released under the [MIT License](LICENSE).
Copyright © 2026 Pavel Beníšek. You may use, modify and redistribute the software,
including commercially, provided you retain the copyright and license notice.
The software is provided without warranty.

Third-party dependencies and external applications retain their own licenses.
PixInsight is not included; using that backend requires a separate PixInsight license.

## Install

```bash
git clone https://github.com/pavelbenisek/assess-lights.git
cd assess-lights
# Requires uv: https://docs.astral.sh/uv/getting-started/installation/
bash install.sh
export PATH="$HOME/.local/bin:$PATH"
```

Creates a private `.venv` (via `uv`), installs the package, and puts an
`assess-lights` command on your `PATH`.

It also re-syncs the version/author line at the top of this README from
`pyproject.toml`, which is the single source of truth for both — so the two
cannot drift. That step is a no-op when they already agree. To check without
writing:

```bash
.venv/bin/python scripts/sync_readme.py --check   # exit 1 if out of sync
```

### Optional external tools

Cleanup needs no external astronomy application. Stacking requires PixInsight or Siril; mosaic stitching requires Siril.

| tool | used for | install |
|---|---|---|
| PixInsight | step 3/8 frame metrics (best quality), and preferred stacker | you already have it |
| `fitsverify` | authoritative structural FITS verification in step 2/8 | `brew install cfitsio` |
| `siril-cli` | fallback stacker when PixInsight is absent | `brew install siril` |

```bash
./install.sh          # reports which of the three it found
```

## Quick start

```bash
# dry run — reports what it would remove, moves nothing
assess-lights "/path/to/C 34_sub"

# actually move the marked frames (prompts before each move)
assess-lights "/path/to/C 34_sub" --move

# cull, then stack the survivors into _final-stacked.fit
assess-lights "/path/to/C 34_sub" --mode combined --move --yes

# stack an already-culled folder, without assessing it again
assess-lights "/path/to/C 34_sub" --mode stack
```

## Command parameters

```
assess-lights [-h] [--version]
                     [--mode {cleanup,stack,stack-mosaic,combined}] [--move]
                     [--yes] [--recursive] [--soft-percent SOFT_PERCENT]
                     [--min-stars MIN_STARS] [--ecc-max ECC_MAX]
                     [--fwhm-k FWHM_K] [--fwhm-max FWHM_MAX]
                     [--trail-min-frac TRAIL_MIN_FRAC] [--no-trails]
                     [--pi-binary PI_BINARY] [--pi-batch PI_BATCH] [--skip-pi]
                     [--backend {auto,pixinsight,local}] [--debayer]
                     [--stack-backend {auto,pixinsight,siril}]
                     [--siril-binary SIRIL_BINARY] [--mosaic-feather PX]
                     [--mosaic-allow-partial] [--keep-workdir]
                     [directory ...]
```

### Operation modes (master switch)

This comes first: it selects which group of operations runs, and every other
option modifies that choice.

| `--mode` | steps run | what it does |
|---|---|---|
| `cleanup` *(default)* | 1/8 – 7/8 | Assess and cull. Reports the marks, moves them with `--move`, writes the manifests. **No stacking.** |
| `stack` | 8/8 only | Register and stack the FITS already in the folder into `_final-stacked.fit` (or a dated name if one is already there). No assessment, no manifests, nothing moved. **The only mode that accepts several directories** — stacked separately, or merged into one file, as you choose. |
| `stack-mosaic` | 8/8 only | Stitch a multi-panel mosaic with **siril-cli** (debayer → register → max-framing stack). One directory. |
| `combined` | 1/8 – 8/8 | Everything: cull first, then stack whatever survives. |

`cleanup` is the default, so an existing cleanup command keeps working unchanged.
`--mode stack` is the one to reach for when a folder was already culled and you
only want to re-stack it — for instance after changing `--stack-backend`. It is
also the only mode that accepts several directories at once; see
[Stacking several directories](#stacking-several-directories-at-once).

Under `--recursive`, `--mode stack` also stops skipping directories that already
carry an assessment manifest, because those are exactly the ones worth stacking.

### Positional

| parameter | description |
|---|---|
| `directory` | Directory containing the FITS series. With `--recursive`, the single root to walk. If omitted, you are prompted. With `--mode stack` you may pass **several** (see [Stacking several directories](#stacking-several-directories-at-once)). |

### Actions

| parameter | default | description |
|---|---|---|
| `-h`, `--help` | | Show help and exit. |
| `--version` | | Show the version and exit. |
| `--move` | off | **Actually move** the marked files. Without it, a dry run: everything is reported, nothing moves. |
| `--yes`, `-y` | off | Skip the interactive confirmation prompts (for scripts and cron). |
| `--recursive` | off | Walk `directory` (default: cwd) and process every sub-directory whose name ends in `_sub` and that has no `assessment.csv`/`.json` yet. |

### Thresholds

| parameter | default | description |
|---|---|---|
| `--soft-percent N` | `30` | Worst N% of the **survivors** to soft-mark for lucky imaging. |
| `--min-stars N` | `12` | Reject frames with fewer detected stars. |
| `--ecc-max X` | `0.90` | Reject frames with eccentricity above this. |
| `--fwhm-k K` | `2.5` | Reject frames with FWHM above `median + K * 1.4826 * MAD`. |
| `--fwhm-max PX` | `5.0` | Absolute FWHM backstop in pixels. |
| `--trail-min-frac F` | `0.15` | Minimum trail length as a fraction of the image diagonal. |
| `--no-trails` | off | Skip trail detection entirely. |

### Step 3/8 engine (frame metrics)

| parameter | default | description |
|---|---|---|
| `--backend {auto,pixinsight,local}` | `auto` | `pixinsight` = SubframeSelector; `local` = built-in SEP engine; `auto` = PixInsight if present, else SEP. |
| `--pi-binary PATH` | auto-detected | PixInsight executable (also `$ASSESS_LIGHTS_PI_BINARY`). |
| `--pi-batch N` | `10` | Frames per SubframeSelector call. |
| `--skip-pi` | off | Skip step 3/8 completely — no metrics, no ranking, no soft marks. |
| `--debayer` | off | Demosaic CFA frames to luminance before measuring shape. Slower, needs temp disk. **Changes the measurement regime — see below.** |
| `--keep-workdir` | off | Keep the PixInsight work dir and staged luminance frames for debugging. |

### Stacking (used by `--mode stack` and `--mode combined`)

| parameter | default | description |
|---|---|---|
| `--stack-backend {auto,pixinsight,siril}` | `auto` | `pixinsight` = StarAlignment + ImageIntegration; `siril` = `siril-cli`; `auto` prefers PixInsight, falls back to Siril. |
| `--siril-binary PATH` | auto-detected | `siril-cli` executable (also `$ASSESS_LIGHTS_SIRIL_BINARY`). |

### Environment variables

| variable | effect |
|---|---|
| `ASSESS_LIGHTS_PROGRESS=1` | Force progress bars on even when stdout is not a terminal. |
| `ASSESS_LIGHTS_PI_BINARY` | PixInsight path, same as `--pi-binary`. |
| `ASSESS_LIGHTS_SIRIL_BINARY` | `siril-cli` path, same as `--siril-binary`. |
| `ASSESS_LIGHTS_KEEP_STACK_WORKDIR=1` | Keep the stacker's scratch directory (and `siril.log`) for debugging. |

### Exit codes

| code | meaning |
|---|---|
| `0` | Success (including a clean dry run). |
| `1` | A requested step failed — e.g. `--mode combined` with no stacking engine available. |
| `2` | Bad usage, e.g. the path is not a directory. |

## Examples

### 1. Look before you leap

```bash
assess-lights "~/Astro/2026/C 34_sub"
```

Dry run. Prints the frames that would be rejected, the soft marks, the statistics
table, and the destination directories. Nothing is written except the manifest.

### 2. Cull a whole season unattended

```bash
assess-lights ~/Astro/2026 --recursive --move --yes
```

Walks `~/Astro/2026`, processes every `*_sub` directory that has no manifest yet,
moves the rejects, and skips anything already assessed — so a re-run after a
failure resumes where it stopped.

### 3. Cull and stack in one pass

```bash
assess-lights "C 34_sub" --mode combined --move --yes
```

Runs all eight steps: culls, moves the rejects, then registers and integrates the
survivors into `C 34_sub/_final-stacked.fit`.

### 4. Stack several folders in one go

```bash
assess-lights "night1_sub" "night2_sub" "night3_sub" --mode stack
```

Step 8/8 only. Each folder is stacked on its own and keeps its own
`_final-stacked.fit`. Give the paths on the command line, or run with no path at
all and let the prompt collect them one at a time.

### 5. Force the fallback engines (no PixInsight at all)

```bash
assess-lights "C 34_sub" --mode combined --backend local --stack-backend siril
```

Step 3/8 uses the built-in SEP engine and stacking uses `siril-cli`. This is the
combination for a machine with no PixInsight licence.

### 6. Be stricter about star loss

```bash
assess-lights "C 34_sub" --min-stars 20 --soft-percent 40
```

Star count is the most reliable degradation signal: frames that lost tracking or
hit cloud collapse from ~40 stars to 2–10. Raising `--min-stars` catches more.

### 7. Only care about trails

```bash
assess-lights "C 34_sub" --no-trails       # skip trail detection
assess-lights "C 34_sub" --trail-min-frac 0.08   # hunt shorter trails
```

### 8. Fit it into a cron job on a NAS

```bash
# non-interactive shells don't read ~/.profile — use the absolute path
~/.local/bin/assess-lights /volume1/homes/benny007/Astro/2026 \
    --recursive --move --yes --backend local
```

`--backend local` avoids a long PixInsight-missing check and makes the intent
explicit; `--yes` removes the prompts.

## What the pipeline does

Resolving the directory (positional argument, prompt, or `--recursive` walk)
happens before the numbered steps.

| step | what it does | implementation | controls it |
|---|---|---|---|
| 1/8 | is it a FITS series? | `discovery.py` — quits if no FITS frames | — |
| 2/8 | **damaged** | `integrity.py` — `fitsverify` when present, plus truncation / geometry / EXPTIME / blank checks | — |
| 3/8 | **blurred / distorted** | `pi_bridge.py` (SubframeSelector) or `localmetrics.py` (SEP) | `--backend`, `--debayer`, `--skip-pi` |
| 4/8 | **trails** | `trails.py` — threshold mask + connected components + PCA straightness | `--trail-min-frac`, `--no-trails` |
| 5/8 | **worst N% of survivors** | `rank.py` — robust composite z-score over the survivors | `--soft-percent` |
| 6/8 | review, confirm, move | `report.py`, `actions.py` — hard marks → `-rejected/`, soft marks → `-unlucky-images/` | `--move`, `--yes` |
| 7/8 | manifest + statistics | `report.py` | — |
| 8/8 | **stack the survivors** | `stacking.py` | `--mode stack\|combined`, `--stack-backend` |

The operation modes map onto these steps directly: `--mode cleanup` runs
1/8–7/8, `--mode stack` runs 8/8 alone, and `--mode combined` runs all eight.

The progress bar and every printed `step N/8` line are numbered from the same
list, so the two can no longer disagree.

Two live progress bars are shown on a terminal:

```
directories A_sub        ━━━━━━━━━━━━━━━━╺━━━━━━━━━━━━━ 1/3  0:00:12
step 4/8 focus / shape   ━━━━━━━━━━━━━━━━╺━━━━━━━━━━━━━ 7/10 0:00:03
```

Bars are disabled automatically when stdout is not a terminal, so logs and pipes
stay free of control codes (`ASSESS_LIGHTS_PROGRESS=1` forces them on).

## Output layout

Marked frames move into **sibling** directories named after the target, so the
folder you are about to stack is never polluted:

```
~/Desktop/tempastro/
├── C 34_sub/                  <- target; survivors stay here, plus _final-stacked.fit
├── C 34_sub-rejected/         <- structural / focus / trail rejects
└── C 34_sub-unlucky-images/   <- lucky-imaging soft marks
```

Each moved FITS carries its `.jpg` and `_thn.jpg` companions. The manifests
`assessment.csv` and `assessment.json` stay in the target directory and double as
the done-marker for `--recursive`.

## Thresholds

Defaults were calibrated on a measured 40-frame ZWO Seestar S30 Pro set
(20 s IRCUT subs, 2160×3840 16-bit OSC CFA):

```
FWHM          min 1.76   median 2.57   max 4.47   px
eccentricity  min 0.44   median 0.70   max 0.92
stars         min 2      median 27     max 54
```

- `--min-stars 12` — hard floor. The single most reliable degradation signal.
- `--fwhm-k 2.5` — relative cut, robust, so one wild outlier does not move it.
- `--ecc-max 0.90` — deliberately loose. Eccentricity is genuinely high in this
  data (~0.73 on the capture app's own debayered JPEG, so it is real star
  elongation, not a CFA artifact); the relative z test does the real work.
- `--soft-percent 30` — applies to the **survivors** of the hard filters.

## Measurement engines (step 3/8)

`--backend` picks the measurement engine. `auto` uses PixInsight when found and
otherwise the built-in SEP engine, printing which it chose:

```
step 3/8  PixInsight not found - falling back to the built-in SEP backend
step 3/8  built-in SEP backend measured 40 frame(s)
        FWHM median 2.150 px, MAD 0.167 -> cut at 2.768 px
        8 flagged for focus/shape
```

With `--skip-pi` step 3/8 is skipped entirely: frames are **not** ranked or
soft-marked, and the statistics table shows `-` for every metric.

### The built-in engine is SEP (the SExtractor engine)

[SEP](https://sep.readthedocs.io/) reports per-source `a`, `b` second moments and
hence FWHM and eccentricity in the same Gaussian convention SubframeSelector
uses. It is **20× faster** than the PixInsight bridge (0.2 s/frame vs 4.1 s/frame)
and needs no external process. It ships prebuilt wheels for `macosx_11_0_arm64`
and `manylinux_2_17_x86_64`, so it installs with no compiler.

### How closely does it agree with PixInsight?

Same 40 frames, identical CFA mosaics:

| metric | PI median | SEP median | Spearman rank correlation |
|---|---|---|---|
| FWHM | 2.574 px | 2.150 px | **0.857** |
| eccentricity | 0.699 | 0.668 | **0.873** |
| stars | 27.0 | 28.5 | **0.955** |
| noise | 0.009 | 0.009 | **0.999** |

- Frames flagged: PI 8, SEP 8, **6 in common**.
- Composite-score rank correlation (what step 5/8 sorts on): **0.944**.
- Worst-30% selection overlaps on 8 of 12 frames.

Two caveats, so it is not mistaken for a drop-in replacement:

- **Absolute FWHM runs tight.** SEP measures isophotal second moments over the
  detection threshold, so a blurred star's wings fall below threshold sooner and
  its size is compressed: PI spread 1.76–4.47 px, SEP 1.83–2.91. The relative cut
  still separates the blurred tail, but `--fwhm-max 5.0` will never fire under SEP.
- **Two of PI's eight rejects are missed** — both frames where PI saw 3 and 2
  stars and SEP saw 13 and 6, so `--min-stars 12` did not trigger. Raise
  `--min-stars` for SEP parity on a known-bad sequence.

### Why not hand-rolled detection

Two earlier implementations were tried and rejected on evidence, both measured
against SubframeSelector on the same 40 frames:

1. **Thresholded second moments** — every frame reported ~7.9 px FWHM with almost
   no frame-to-frame variation; rank correlation **−0.56** and **0 of 8** rejects
   found. The aperture moments were dominated by background pixels, not the star.
2. **Elliptical-Gaussian least-squares fits** — plausible absolute values but still
   uncorrelated with PI, eccentricity pinned near 1.0. On a CFA mosaic the Bayer
   pattern produces spurious peaks and doubled cores.

Both are documented in the `localmetrics.py` docstring so the dead ends are not
re-explored.

### `--debayer` is a different measurement regime

By default shape is measured on the raw CFA mosaic. `--debayer` demosaics to
luminance first. Measured on 8 frames of real data:

| source | FWHM median | eccentricity (kept) |
|---|---|---|
| raw CFA mosaic | 2.702 px | 0.753 |
| `--debayer` (3×3 interp) | 3.447 px | 0.692 |
| `--debayer` (5×5 interp) | 4.018 px | 0.627 |
| the capture app's own debayered JPEG (reference) | 3.698 px | 0.729 |

Our 3×3 demosaic lands closest to the reference; a 5×5 kernel over-smooths and
**understates** eccentricity (0.627 vs a true ~0.73), which would silently make
the blur gate too lenient. Hence the 3×3 default.

Interpolation also suppresses noise by ~an order of magnitude, and detection
thresholds are *relative to that noise*:

| metric (median) | raw CFA | `--debayer` |
|---|---|---|
| stars | 27 | ~200 |
| background noise | 0.0085 | 0.00065 |
| FWHM | 2.57 px | ~3.4 px |

The practical consequence is sharp: `--min-stars 12` is calibrated for the CFA
mosaic, where a healthy frame carries 20–50 stars. In `--debayer` mode every frame
sails past it, and the most reliable degradation signal stops working — a real set
that yields **8** rejects in CFA mode yielded **1** in debayer mode. The tool
detects this and prints the regime plus a suggested threshold:

```
debayered regime: median stars 202, median noise 0.000657074
note: --min-stars 12 was calibrated on the raw CFA mosaic, where frames carry ~20-50 stars.
      On debayered luminance the median is 202, so this test will barely reject anything.
      Raise it (e.g. --min-stars 81) or drop --debayer for culling.
```

**Rule of thumb:** cull in the default (CFA) mode; use `--debayer` only when you
want absolute star-shape numbers, and re-tune `--min-stars`/`--fwhm-*` for it.

PixInsight is deliberately *not* used for the demosaic: it reads these FITS as
monochrome (no CFA flag), so its `Debayer` process succeeds and changes nothing.

## Stacking (step 8/8)

Reached with `--mode stack` (this step alone) or `--mode combined` (after
culling). It registers and combines whatever FITS frames are **in the target
folder** and writes the master beside them as **`_final-stacked.fit`**.

**An existing master is never overwritten.** If `_final-stacked.fit` is already
there, the run writes `_final-stacked_YYYYMMDD-HHMMSS.fit` instead and tells you,
leaving the earlier stack exactly as it was:

```
        _final-stacked.fit already exists - writing
_final-stacked_20261003-121016.fit instead (existing stack left untouched)
```

If two runs happen to land in the same second, a numeric suffix is added
(`_final-stacked_20261003-121016_1.fit`). Every previous master — the plain name
and all dated variants — is excluded from the input set, so a re-run never stacks
a stack.

| `--stack-backend` | behaviour |
|---|---|
| `auto` (default) | PixInsight if installed, otherwise `siril-cli` |
| `pixinsight` | StarAlignment + ImageIntegration, headless |
| `siril` | `siril-cli` with a generated `.ssf` script |

Both engines run in a scratch directory, so the target never gets registration
output or sequence symlinks — only the master (`_final-stacked.fit`, or a dated
`_final-stacked_*.fit` when one already exists) lands there.

### Stacking several directories at once

`--mode stack` is the **only** mode that accepts more than one directory. Give two
or more and, at a terminal, it asks what to do with them:

```
Stack all FITS from all 3 directories into a single output file? [y/N]
```

**No** — the default, and what `--yes` implies — stacks each directory on its own.
**Yes** merges every frame from every directory into one file. Both are described
below.

#### No: one master per directory

Every folder keeps its own master beside its own frames. Three nights of the same
target give you three `_final-stacked.fit` files.

```bash
assess-lights "night1_sub" "night2_sub" "night3_sub" --mode stack
```

Each directory reports its own result and the run ends with a tally:

```
  step 8/8  stacked with siril -> _final-stacked.fit (33.2 MB) in night1_sub
  step 8/8  stacked with siril -> _final-stacked.fit (33.2 MB) in night2_sub
  2/2 directories processed successfully
```

One directory failing does not stop the others; the exit code is `1` if any failed.

#### Yes: one merged master

It then asks where to write it, and integrates every FITS frame from every
directory into that single file:

```
Path and filename for the combined output FITS: /volume1/astro/C 34/final
6 FITS frame(s) from 2 directories (night1_sub: 3, night2_sub: 3) - merging into one master
  step 8/8  stacked with siril -> /volume1/astro/C 34/final.fit (33.2 MB)
```

The path is taken as given, with a few conveniences and one guarantee:

| what you type | what happens |
|---|---|
| no extension | `.fit` is appended |
| a parent directory that does not exist | it is created |
| something that is a directory | re-asked — a filename is needed |
| a non-FITS extension | re-asked (`.fit`, `.fits`, `.fts`, `.xisf`) |
| a file that already exists | **never overwritten** — the dated-suffix rule applies and the new name is reported |

Because both questions are only asked at a terminal without `--yes`, this mode is
opt-in by design: a script or cron run cannot accidentally merge folders.

`--recursive --mode stack` is *not* offered the merge, because it would combine
every sub-directory under the root into one image. Pass the directories
explicitly to merge them.

#### Duplicate frame names across directories

Both engines name their output after the input basename — StarAlignment writes
`<name>_r.fit`, Siril links every frame into one scratch directory — so two frames
called `Light.fit` in different folders would overwrite one another and silently
drop a frame from the integration. When a collision exists the offending frames
are staged under distinct names and the run says so:

```
1 duplicate frame name(s) across the directories - staging those under distinct names so none is dropped
```

This cannot trigger for a single directory, where names are unique by definition,
so the ordinary per-directory path is untouched.

#### Collecting the directories interactively

If you leave the directory off entirely, the prompt offers to collect more:

```
Path to the directory containing the FITS series: /volume1/astro/night1_sub
Add an additional directory to stack? [y/N] y
Path to the additional directory: /volume1/astro/night2_sub
added /volume1/astro/night2_sub
Add another directory to stack? [y/N] n
```

That question defaults to **no** too — a plain <Enter> at the first one keeps the
single-directory behaviour exactly as before. It is asked one directory at a time
and only when a terminal is attached and `--yes` was not passed, so scripts and
cron never block on it. A path that is not a directory, or one already in the
list, is reported and re-asked rather than silently accepted.

Every other mode takes exactly one directory and refuses more:

```
$ assess-lights night1_sub night2_sub --mode cleanup
multiple directories need --mode stack (got 2 with --mode cleanup)
--mode cleanup processes one directory at a time; --mode stack accepts several and stacks each on its own
```

### Why these two engines

**PixInsight** is preferred: `StarAlignment` registers every frame to a reference,
then `ImageIntegration` combines them with Winsorized sigma clipping and BWMV
weighting. The PJSR parameter usage is taken from PixInsight's own bundled
BatchPreprocessing script, which is authoritative for these processes.

**Siril** is the fallback because it is a genuinely headless CLI. Two alternatives
that sound right but are not:

- **"AstroStacker" does not exist as a headless CLI stacker** — the name belongs
  to an Android app, an iOS app and a Rust GUI, none scriptable.
- **ASTAP cannot stack from its command line.** Despite being the *Astrometric
  STAcking Program*, its CLI only plate-solves: the manual's
  `-f/-r/-fov/-ra/-spd/-z/-s/-t/-m/-o/-d/-analyse/-extract/-sqm/-focus1/-annotate/-tofits/-update/-wcs`
  table sits under "used to astrometric solve images", and stacking is GUI-only.
  The `-stack <json>` API is a third-party fork
  (`ortegaalfredo/astap-command-line`), last touched in 2021, with Linux/Windows
  binaries and no macOS build.

### Output format

| engine | output | format |
|---|---|---|
| PixInsight | `_final-stacked.fit` (dated on collision) | `BITPIX=-64` float, values normalised to 0..1 |
| Siril | `_final-stacked.fit` (dated on collision) | `BITPIX=-32` float, values normalised to 0..1 |

Both are float FITS scaled to 0..1 — the convention PixInsight's WBPP writes — not
the 16-bit integer ADU of the input subs. That preserves the full linear range and
avoids clipping, but rescale if a downstream tool requires 16-bit ADU.

### Mosaics (`--mode stack-mosaic`)

A mosaic is not a stack, and stacking a multi-panel set does **not** produce one:
registering panels to a single reference frame and integrating gives you the
*overlap region*, i.e. a smaller image with the panels' outer areas thrown away.
Stitching needs a maximum-framing canvas plus a registration that survives panels
sharing few stars.

```bash
assess-lights "M 42_mosaic_sub" --mode stack-mosaic
```

Runs `siril-cli` (PixInsight has no equivalent path here, so this mode is
Siril-only) through this sequence, all verified against siril-cli 1.4.4:

```
link lights -date                        # symlink the panels into a sequence
calibrate lights -debayer                # -> pp_lights_  (3-channel float)
seqplatesolve pp_lights -catalog=localgaia   # astrometric registration
seqapplyreg pp_lights -framing=max       # -> r_pp_lights_ (max-framing canvas)
stack r_pp_lights rej w 3 3 -norm=addscale -maximize -overlap_norm -feather=N
```

The output is **`_final-mosaic.fit`** beside the panels — a distinct name from
`_final-stacked.fit`, so a mosaic can never be mistaken for a stack, and the same
never-overwrite rule applies (an existing one gets a dated name instead).

| parameter | default | description |
|---|---|---|
| `--mosaic-feather PX` | `20` | Feather the panel borders over this many pixels. The right value depends on your data — raise it if seams show. |
| `--mosaic-allow-partial` | off | Write the mosaic even when some panels failed to register, leaving gaps. |

#### Panels are dropped silently — so this mode refuses to pretend

Siril does **not** fail when a frame cannot be registered; it drops it and carries
on, then reports success. Measured here on a 9-panel Seestar set: **6 of 9 panels
dropped**, and the file it wrote was **20.7 % empty** — a mosaic with holes,
presented as a finished image.

So the integration count is read back from Siril's own log (`Integration of N
images on M of the sequence`) and a partial result is **refused by default**:

```
mosaic stitching failed:
only 3 of 9 frames registered - 6 panel(s) would be missing from the mosaic.
  Panels are dropped silently by star-based registration when they share too few
  stars with the reference frame.
  Install Siril's offline Gaia DR3 astrometry extract so the frames can be
  registered astrometrically, or pass --mosaic-allow-partial to accept the gaps.
```

Exit code `1`, and the half-stitched file is left on disk for inspection. Pass
`--mosaic-allow-partial` to accept the gaps; the run then reports them instead:

```
step 8/8  stitched with siril -> _final-mosaic.fit (271.5 MB, 3686x6139)
        3/9 frame(s) integrated, star registration, feather 20px
        6 panel(s) did not register - the mosaic has gaps (--mosaic-allow-partial)
```

#### Siril's scratch directory is not the target folder

Stitching is disk-hungry: Siril writes every debayered panel *and* every
registered panel to a scratch directory before stacking. For a 174-panel set that
is **~16 GiB for the debayered frames alone**. That scratch directory follows
`$TMPDIR`, **not** the folder holding the frames — and on a Synology `/tmp` is a
small RAM-backed **tmpfs**, so the run can die with the volume 900 GB free:

```
log: Memory per image: 15 MB. Max memory: 11693 MB
log: Not enough free disk space to perform this operation:
     7.7 GiB available for 16.1 GiB needed (missing 8.4 GiB)
log: Not enough space to save the output images, aborting
```

Point `TMPDIR` at the volume and re-run:

```bash
mkdir -p "$HOME/.cache/assess-lights-scratch"
TMPDIR="$HOME/.cache/assess-lights-scratch" \
  assess-lights "NGC 6960_mosaic_sub" --mode stack-mosaic
```

#### Astrometric registration needs a catalogue

Siril's tutorial recommends installing the **offline Gaia DR3 astrometry
extract** before attempting a mosaic, and that is not optional in practice: the
first attempt here failed outright because plate solving fetches a catalogue per
frame from VizieR, which was returning `503 Service Unavailable`. That is an
upstream outage, not a Siril bug, and there is no local fallback unless the
extract is installed.

The mode therefore registers **astrometrically when the extract is present**
(checked at `~/.local/share/siril/gaia_astrometric.dat`, or `$SIRIL_HOME`) and
falls back to star-based registration when it is not — announcing which it used,
because the two are not equivalent:

```
siril-cli: debayering 9 panel frame(s), then star-based registration and stitching
```

Star-based registration is fine for a well-overlapping set and useless across
wide gaps. Install the extract (Siril's `Siril_Catalog_Installer.py`) to make
wide mosaics work.

### Verified behaviour

Checked on a real 6-frame subset. The Siril path was re-checked *properly* after it
silently produced the wrong thing once:

- **PixInsight** — registered 6/6, integrated, 66.4 MB, background ~0.047, zero
  exactly-zero pixels.
- **Siril** — registered 6/6, stacked, **max correlation with any single input
  frame 0.743** (a copy of one sub would read 1.0000) and background noise reduced
  from 0.00773 to 0.00363, a **2.1×** reduction against the ideal 1/√6 = 2.45×.

Two bugs found this way, both now guarded in `stacking.py`:

1. **Siril's `link` creates `<seq>_00001.fit` symlinks back to the inputs.** A
   naive glob for `*.fit` in the work directory returns an *input frame*, so the
   run "succeeds" while copying one sub as the master. Output selection now
   requires a regular file that is neither a symlink nor a sequence member.
2. **`stack <seq>` aborts** with *"Stacking has detected registration data on
   layer 0 with more than simple shifts. You should apply existing registration
   before stacking"* whenever registration involves rotation or scale — which real
   subs do. Stacking the **registered** sequence (`r_lights`) works.

## Installing on another host (e.g. a NAS)

`install.sh` only needs `uv`; it fetches a modern CPython itself, so the host's
system Python can be old (verified on a Synology DSM 7.4 box whose system Python
is 3.8, with no pip and no compiler — `uv` supplied CPython 3.12 and every
dependency arrived as a prebuilt wheel).

```bash
# 1. bootstrap uv (single static binary, no root needed)
mkdir -p ~/.local/bin && cd ~/.cache
curl -sSL -o uv.tar.gz \
  https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz
tar xzf uv.tar.gz && install -m755 uv-*/uv ~/.local/bin/uv

# 2. copy this project over, then install
cd ~/astro/assess-lights && PATH=$HOME/.local/bin:$PATH bash install.sh
```

`install.sh` is idempotent — re-running reuses the existing venv and refreshes the
package.

### The `assess-lights` command

The installer symlinks `bin/assess-lights` into `~/.local/bin` and ensures that
directory is on `PATH`. The launcher **resolves its own path through symlinks** and
execs the venv belonging to that installation, so you never activate the venv, it
works from any working directory, and moving the project needs no other change.

```bash
assess-lights ~/Astro/2026/…/C 34_sub
```

A **non-interactive shell does not read `~/.profile`**, so `ssh host 'assess-lights …'`
and cron jobs will not find it. Use the absolute path or export `PATH` in the script.

**Stacking on a NAS:** PixInsight cannot be installed on Synology at all, but
`siril-cli` can — see the next section. Until you install it, stacking exits `1`
with a message naming both fixes; culling works fully without either engine.

### Getting siril-cli onto a Synology NAS

Synology has no package manager, no compiler and no `flatpak`, so Siril cannot be
built or installed the usual way. It ships a **self-contained x86_64 AppImage**,
which unpacks into a plain directory and needs no root:

```bash
cd ~/.cache
curl -sSLO https://free-astro.org/download/Siril-1.4.4-x86_64.AppImage
chmod +x Siril-1.4.4-x86_64.AppImage
./Siril-1.4.4-x86_64.AppImage --appimage-extract   # works without FUSE
mv squashfs-root ~/.local/opt/siril
```

Then wrap it — because the AppImage **bundles its own glibc and dynamic loader**,
it has to be started through `AppRun`:

```bash
cat > ~/.local/bin/siril-cli <<'EOF'
#!/bin/sh
SIRIL_ROOT="${SIRIL_ROOT:-$HOME/.local/opt/siril}"
export APPDIR="$SIRIL_ROOT"
exec "$SIRIL_ROOT/AppRun" siril-cli "$@"
EOF
chmod 755 ~/.local/bin/siril-cli
```

Verified on a Synology DS620slim (DSM 7.4, glibc 2.36, kernel 4.4): the wrapper
reports `siril 1.4.4` and a real `--mode stack` run registers and integrates
frames normally, producing the same 33.2 MB master as on the Mac. The unpacked
tree is ~258 MB per account.

Two pitfalls, each of which looks like something else:

- **Running `usr/bin/siril-cli` directly aborts with `*** stack smashing detected ***`.**
  That is not a corrupt download or an incompatible CPU. The bundle carries its
  own glibc, and starting the inner binary directly makes it resolve the *host's*
  libraries instead. Always go through `AppRun`, or through the bundled loader at
  `lib/x86_64-linux-gnu/ld-linux-x86-64.so.2`.
- **Do not put the bundle's libraries on `LD_LIBRARY_PATH` for a whole session.**
  Its bundled `libc.so.6` then gets picked up by unrelated programs, and ordinary
  commands such as `ls` start dying with the same abort — which sends you chasing
  a phantom "everything is broken" problem. Keep the environment inside the
  launcher, as above.

`AppRun` prints two harmless warnings (`APPDIR environment variable not set`,
locale-directory fallback); exporting `APPDIR` in the launcher silences the first.

## Notes on the PixInsight bridge

The PJSR script is generated by `pi_bridge.py` and run as:

```
PixInsight -n --automation-mode --no-startup-scripts -r=<script.js> --force-exit
```

Five details are baked in, each verified against PixInsight 1.9.5 on macOS and each
capable of producing a silent failure or a stalled unattended run otherwise:

1. `#engine v8` must be the first line, or the script runs on the legacy
   SpiderMonkey runtime where `Runtime` is undefined and it dies immediately.
2. PJSR console output does **not** reach the process stdout — errors are only
   visible in PixInsight's Process Console. The script therefore reports by writing
   files, and this tool must not judge success from stdout.
3. `File.searchDirectory` needs a wildcard spec (`dir/*`); a bare directory
   silently returns zero entries.
4. Table parameters require 4-column rows `[enabled, filePath, drizzlePath,
   localNormalizationPath]` (see PixInsight's own `BPP-Helper.js`).
5. `ImageWindow.saveAs()` defaults `queryOptions` to **true**, which pops the
   "FITS Options" (Sample Format) sheet **even under `--automation-mode`** and
   stalls an unattended run until someone clicks OK. Passing `queryOptions=false`
   suppresses it, but PixInsight then silently inherits the saved FITS defaults —
   measured here as **32-bit float** (`BITPIX=-32`), where the master is
   documented as `-64`. The stacker therefore writes through `FileFormatInstance`
   with an explicit `ImageDescription` (`bitsPerSample = 64`), which shows no
   dialog *and* pins the sample format, carrying the header keywords across the
   way PixInsight's own `BatchFormatConversion` does. Falling back to the
   high-level call only if that fails, so the master is never lost.

   Note that FITS offers **no hint for floating-point width** — its only write
   hints are `unsigned`, `signed`, `cleanup-headers`, `bottom-up`/`up-bottom` and
   `verbosity n` (the module's own hint table) — so `formatHints` cannot be used
   to fix the bit depth.

If `metrics.csv` is missing, the tool prints the PJSR log and the captured stdout
to make the failure diagnosable.

## Validating the trail detector

The detector was validated by injecting synthetic trails into real frames: a clean
frame yields 0 trails, and an 8σ injected trail is detected at its true length
(coverage 0.986). A 4.5σ trail is below the 6σ design threshold and is correctly
ignored.

## FITS reading gotcha

FITS numbers its axes **fastest-varying first** (`NAXIS1` = number of columns),
while a numpy array is `(rows, columns)`. The numpy shape is therefore
`(NAXIS2, NAXIS1)` — the reverse of header order. Reshaping flat data in FITS order
does **not** transpose the image, it *shears* it (wrong row width), and the result
correlates with reality at ~0.01.

Verify any FITS reader by correlating a downsampled read against a reference
rendering of the same frame; a correct reader scores >0.9, a sheared one ~0.
`astropy.io.fits` also refuses `memmap=True` on these files (BZERO present), so use
`memmap=False`.
