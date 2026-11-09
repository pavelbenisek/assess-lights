"""Stack the surviving frames into a single master image.

Two engines, chosen automatically:

* **PixInsight** (preferred): StarAlignment registers every frame to a reference,
  then ImageIntegration combines them with Winsorized sigma clipping and BWMV
  weighting. The exact PJSR parameter usage is taken from PixInsight's own
  bundled BatchPreprocessing script (``BPP-Operations.js`` / ``BPP-Processing.js``),
  which is the authoritative reference for these two processes.

* **Siril** (fallback): ``siril-cli`` runs a generated ``.ssf`` script
  (link -> register -> stack). Siril is a real headless CLI, unlike ASTAP whose
  command line can only plate-solve -- stacking there is GUI-only, and the
  JSON ``-stack`` API lives in an unmaintained 2021 fork with no macOS build.

Both engines work in a scratch directory and write the finished master into the
target folder as ``_final-stacked.fit``, so neither litters the source folder
with registration output or sequence symlinks.

An existing ``_final-stacked.fit`` is never overwritten: the second run writes
``_final-stacked_YYYYMMDD-HHMMSS.fit`` instead and says so.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# names the output and the extensions we treat as FITS
OUTPUT_STEM = "_final-stacked"
MOSAIC_STEM = "_final-mosaic"
# pixels of feathering across panel borders; the right value depends on the data,
# so it is exposed as --mosaic-feather
MOSAIC_FEATHER = 20
FITS_SUFFIXES = (".fit", ".fits", ".fts")

# Siril's offline Gaia DR3 astrometry extract. Plate solving otherwise fetches a
# catalogue per frame from VizieR, which fails outright when that server is down
# (observed returning 503), so astrometric registration is only attempted when
# the local extract is actually installed.
_OFFLINE_ASTROMETRY = (
    ".local/share/siril/gaia_astrometric.dat",
    "Library/Application Support/siril/gaia_astrometric.dat",
)

SIRIL_CANDIDATES = (
    "siril-cli",
    "/opt/homebrew/bin/siril-cli",
    "/usr/local/bin/siril-cli",
    "/Applications/Siril.app/Contents/MacOS/siril-cli",
    "/usr/bin/siril-cli",
)
PI_DEFAULT = "/Applications/PixInsight/PixInsight.app/Contents/MacOS/PixInsight"


class StackError(RuntimeError):
    pass


def resolve_siril_binary(explicit: str | None = None) -> str | None:
    """Path to a usable siril-cli, or None."""
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    env = os.environ.get("ASSESS_LIGHTS_SIRIL_BINARY")
    if env and os.access(env, os.X_OK):
        return env
    for c in SIRIL_CANDIDATES:
        p = shutil.which(c) or (c if os.access(c, os.X_OK) else None)
        if p:
            return p
    return None


def resolve_pi_binary(explicit: str | None = None) -> str | None:
    """Path to a usable PixInsight, or None."""
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    env = os.environ.get("ASSESS_LIGHTS_PI_BINARY")
    if env and os.access(env, os.X_OK):
        return env
    return PI_DEFAULT if os.access(PI_DEFAULT, os.X_OK) else None


def source_frames(target: Path) -> list[Path]:
    """FITS frames left in `target`, excluding our own outputs.

    Anything named `_final-*` is skipped, so both the plain and the dated
    variants of `_final-stacked` and `_final-mosaic` are excluded. Otherwise a
    second run feeds the previous master back in as an input frame -- and for a
    mosaic that would even mean stitching the last mosaic into the new one.
    """
    out = []
    for p in sorted(target.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in FITS_SUFFIXES:
            continue
        if p.stem.startswith("_final-"):
            continue
        out.append(p)
    return out


def unique_path(path: Path, now: datetime.datetime | None = None) -> tuple[Path, str | None]:
    """`path` if it is free, otherwise a dated sibling, plus a note to show.

    A stacked master is never overwritten. If the wanted name is taken the run
    writes `<stem>_YYYYMMDD-HHMMSS<suffix>` instead. A same-second collision (two
    runs finishing within one second) falls back to a numeric suffix.
    """
    path = Path(path)
    if not path.exists():
        return path, None

    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    cand = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
    n = 1
    while cand.exists():
        cand = path.with_name(f"{path.stem}_{stamp}_{n}{path.suffix}")
        n += 1
    return cand, (f"{path.name} already exists - writing {cand.name} instead "
                  f"(existing stack left untouched)")


def resolve_output(target: Path, now: datetime.datetime | None = None) -> tuple[Path, str | None]:
    """Where to write the per-directory master: `_final-stacked.fit` in `target`."""
    return unique_path(target / f"{OUTPUT_STEM}.fit", now)


def gather_frames(dirs) -> list[Path]:
    """Every stackable FITS frame across `dirs`, in order, deduped by real path.

    Reads each directory through `source_frames`, so an existing master (plain or
    dated) is never fed back in as an input frame.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        for f in source_frames(Path(d)):
            key = f.resolve()
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def _unique_inputs(frames: list[Path], stage_dir: Path) -> tuple[list[Path], int]:
    """Absolute input paths with unique basenames, and how many had to be renamed.

    Both engines derive their output names from the input basename -- StarAlignment
    writes `<basename>_r.fit` into its output directory, and siril links every frame
    into one flat scratch directory -- so two frames with the same name from
    different folders would silently overwrite each other and drop frames from the
    integration. Frames are only staged when that actually happens, so a normal
    single-folder run is passed through untouched.
    """
    names = [f.name for f in frames]
    if len(set(names)) == len(names):
        return [f.resolve() for f in frames], 0

    stage_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    renamed = 0
    seen: dict[str, int] = {}
    for f in frames:
        n = seen.get(f.name, 0)
        seen[f.name] = n + 1
        name = f.name if n == 0 else f"{n}_{f.name}"
        while (stage_dir / name).exists():
            name = "x" + name
        os.symlink(f.resolve(), stage_dir / name)
        out.append(stage_dir / name)
        if n:
            renamed += 1
    return out, renamed


# --------------------------------------------------------------------------- #
# Siril
# --------------------------------------------------------------------------- #

def _siril_script(workdir: Path, out_stem: str) -> str:
    # 'link' only picks up FITS from the current working directory, which is why
    # everything runs in a scratch dir holding symlinks to the frames we want.
    #
    # Stack the REGISTERED sequence, not the original: `stack lights` aborts with
    #   "Stacking has detected registration data on layer 0 with more than simple
    #    shifts. You should apply existing registration before stacking"
    # whenever the registration involves rotation/scale (which real subs do).
    # `register` already writes the resampled frames as r_lights_NNNNN.fit, and
    # stacking those succeeds. Verified against siril-cli 1.4.4: the plain
    # "stack lights" form fails, "stack r_lights" works.
    return "\n".join([
        "requires 1.2.0",
        "setext fit",
        f'cd "{workdir}"',
        "link lights -date",
        "register lights",
        f"stack r_lights rej w 3 3 -weight=wfwhm -out={out_stem}",
        "",
    ])


def stack_siril(frames: list[Path], dest: Path, binary: str,
                progress=None, tick=None) -> Path:
    """Register + stack with siril-cli. Returns the written master path."""
    import re

    workdir = Path(tempfile.mkdtemp(prefix="assess-lights-siril-"))
    keep = os.environ.get("ASSESS_LIGHTS_KEEP_STACK_WORKDIR") == "1"
    try:
        # symlink so nothing is copied for what can be gigabytes of input.
        # Siril's 'link' picks up every FITS file in the cwd, so these names only
        # have to be unique -- and with several source directories two frames can
        # legitimately share a name.
        seen: dict[str, int] = {}
        for f in frames:
            n = seen.get(f.name, 0)
            seen[f.name] = n + 1
            name = f.name if n == 0 else f"{n}_{f.name}"
            while (workdir / name).exists():
                name = "x" + name
            os.symlink(f.resolve(), workdir / name)

        script = workdir / "stack.ssf"
        script.write_text(_siril_script(workdir, OUTPUT_STEM), encoding="utf-8")

        if progress:
            progress(f"siril-cli: registering {len(frames)} frame(s) and stacking")

        proc = subprocess.run(
            [binary, "-d", str(workdir), "-s", str(script)],
            cwd=str(workdir), capture_output=True, text=True, timeout=7200,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        if tick is not None:
            tick()

        if keep:
            (workdir / "siril.log").write_text(log, encoding="utf-8")

        # Siril's 'link' creates <seq>_00001.fit SYMLINKS back to the inputs, so a
        # naive glob for "*.fit" happily returns an input frame and the run looks
        # like a success while copying one sub as the master. Only regular files
        # that are not part of the sequence count.
        seq_re = re.compile(r"^(r_|reg_)?lights_\d+$")

        def usable(p: Path) -> bool:
            return (p.is_file() and not p.is_symlink() and p.stat().st_size > 0
                    and not seq_re.match(p.stem))

        produced = None
        for cand in (workdir / f"{OUTPUT_STEM}.fit", workdir / f"{OUTPUT_STEM}.fits",
                     workdir / "lights_stacked.fit", workdir / "lights.fit"):
            if cand.exists() and usable(cand):
                produced = cand
                break
        if produced is None:
            extras = [p for p in sorted(workdir.glob("*.fit*")) if usable(p)]
            if extras:
                produced = max(extras, key=lambda p: p.stat().st_size)

        if produced is None:
            listing = "\n".join(f"  {p.name}" for p in sorted(workdir.iterdir()))
            tail = "\n".join(log.strip().splitlines()[-25:])
            raise StackError(
                "siril-cli produced no stacked image (every candidate was an input "
                f"symlink or missing).\n{_space_hint(log)}Work dir contained:\n"
                f"{listing}\n\nsiril-cli output:\n{tail}")

        shutil.copy2(produced, dest)
        return dest
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


# Siril stages every frame in a scratch directory and is bound by $TMPDIR, which
# is NOT the folder holding the frames. On a Synology /tmp is a small tmpfs, so a
# big run dies with "Not enough free disk space" while the volume looks empty.
_SCRATCH_SPACE_HINT = (
    "  Note: that is Siril's SCRATCH directory, not the folder holding the frames.\n"
    "  It follows $TMPDIR, which on a Synology is usually a small tmpfs (/tmp) even\n"
    "  when the volume is nearly empty. Point TMPDIR at the big volume and re-run:\n"
    "    TMPDIR=/volume1/homes/<user>/.cache/assess-lights-scratch assess-lights ...\n"
)


def _space_hint(log: str) -> str:
    """A actionable note when Siril gave up for want of scratch space."""
    if ("Not enough free disk space" in log or "Not enough space to save" in log):
        return _SCRATCH_SPACE_HINT
    return ""


def offline_astrometry_catalog() -> Path | None:
    """The local Gaia DR3 astrometry extract, if one is installed."""
    cands = [Path.home() / p for p in _OFFLINE_ASTROMETRY]
    env = os.environ.get("SIRIL_HOME")
    if env:
        cands.insert(0, Path(env) / "gaia_astrometric.dat")
    for p in cands:
        if p.exists():
            return p
    return None


def _mosaic_script(workdir: Path, out_stem: str, astrometric: bool, feather: int) -> str:
    """The .ssf that stitches one multi-panel mosaic (verified on siril-cli 1.4.4).

    Sequence names are not guessable and must be tracked: `link` creates `lights_`,
    `calibrate -debayer` writes `pp_lights_`, `seqapplyreg` writes `r_pp_lights_`.

    Note that Siril's GUI/video shorthand `calibrate . -debayer` is NOT valid in
    the CLI: "." is parsed as a sequence base name and the run dies with
    "Loading sequence `.' failed ... invalid input sequence". The sequence has to
    be named explicitly.

    Astrometric registration is the tutorial's "Apply Existing Registration" and
    is what stitches panels that barely overlap. Plain star matching silently
    drops any panel sharing too few stars with the reference frame -- measured
    here: 6 of 9 panels dropped, with the run still reporting success and writing
    a canvas that was 20.7% empty.
    """
    registration = ("seqplatesolve pp_lights -catalog=localgaia" if astrometric
                    else "register pp_lights -2pass")
    return "\n".join([
        "requires 1.4.0",
        "setext fit",
        f'cd "{workdir}"',
        "link lights -date",              # symlink the panels into a sequence
        "calibrate lights -debayer",      # -> pp_lights_ (3-channel float)
        registration,                     # writes/uses astrometric registration
        "seqapplyreg pp_lights -framing=max",   # -> r_pp_lights_, max framing
        f"stack r_pp_lights rej w 3 3 -norm=addscale -maximize -overlap_norm "
        f"-feather={feather} -out={out_stem}",
        "",
    ])


def _stacked_count(log: str) -> int | None:
    """How many frames Siril actually integrated, read from its own log."""
    m = re.findall(r"Integration of (\d+) images on (\d+) of the sequence", log)
    if m:
        return int(m[-1][0])
    m = re.findall(r"(\d+) images have been stacked", log)
    return int(m[-1]) if m else None


def _fits_dims(path: Path) -> tuple[int, int] | None:
    """NAXIS1 x NAXIS2 from the primary header, without loading the pixels."""
    try:
        raw = path.open("rb").read(2880 * 2)
    except OSError:
        return None
    dims = {}
    for i in range(0, len(raw), 80):
        c = raw[i:i + 80].decode("ascii", "replace")
        if c.startswith("END"):
            break
        k, _, v = c.partition("=")
        k = k.strip()
        if k in ("NAXIS1", "NAXIS2"):
            try:
                dims[k] = int(v.split("/")[0].strip())
            except ValueError:
                pass
    if "NAXIS1" in dims and "NAXIS2" in dims:
        return dims["NAXIS1"], dims["NAXIS2"]
    return None


def _mosaic_master(workdir: Path) -> Path | None:
    """The stacked mosaic in the scratch dir.

    Only regular files count, and only ones that are not sequence members: the
    scratch dir also holds the debayered `pp_lights_NNNNN.fit` frames and the
    registered `r_pp_lights_NNNNN.fit` ones, plus the input symlinks.
    """
    seq_re = re.compile(r"^(r_|reg_)?(lights|pp_lights)_\d+$")

    def usable(p: Path) -> bool:
        return (p.is_file() and not p.is_symlink() and p.stat().st_size > 0
                and not seq_re.match(p.stem))

    for cand in (workdir / f"{MOSAIC_STEM}.fit", workdir / f"{MOSAIC_STEM}.fits"):
        if cand.exists() and usable(cand):
            return cand
    extras = [p for p in sorted(workdir.glob("*.fit*")) if usable(p)]
    return max(extras, key=lambda p: p.stat().st_size) if extras else None


def stack_mosaic(frames: list[Path], dest: Path, binary: str,
                 feather: int = MOSAIC_FEATHER, allow_partial: bool = False,
                 progress=None, tick=None):
    """Stitch a multi-panel mosaic with siril-cli. Returns (dest, info).

    Siril drops panels that fail registration *without* failing the run, so the
    integrated frame count is read back from its own log. A mosaic missing panels
    is a mosaic with holes in it, so by default that is refused rather than
    written out as if it were complete.
    """
    if len(frames) < 2:
        raise StackError(
            f"need at least 2 FITS frames to build a mosaic, found {len(frames)}")

    workdir = Path(tempfile.mkdtemp(prefix="assess-lights-mosaic-"))
    keep = os.environ.get("ASSESS_LIGHTS_KEEP_STACK_WORKDIR") == "1"
    # the scratch dir is discarded unless asked for, or unless we are about to
    # fail in a way where the stitched file is the only evidence of what happened
    preserve = keep
    try:
        seen: dict[str, int] = {}
        for f in frames:
            n = seen.get(f.name, 0)
            seen[f.name] = n + 1
            name = f.name if n == 0 else f"{n}_{f.name}"
            while (workdir / name).exists():
                name = "x" + name
            os.symlink(f.resolve(), workdir / name)

        catalog = offline_astrometry_catalog()
        astro = catalog is not None
        script = workdir / "mosaic.ssf"
        script.write_text(_mosaic_script(workdir, MOSAIC_STEM, astro, feather),
                          encoding="utf-8")

        if progress:
            progress(f"siril-cli: debayering {len(frames)} panel frame(s), then "
                     f"{'astrometric' if astro else 'star-based'} registration "
                     f"and stitching (feather {feather}px)")

        proc = subprocess.run(
            [binary, "-d", str(workdir), "-s", str(script)],
            cwd=str(workdir), capture_output=True, text=True, timeout=43200,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        if tick is not None:
            tick()

        stacked = _stacked_count(log)
        produced = _mosaic_master(workdir)
        if produced is None:
            if keep:
                (workdir / "siril.log").write_text(log, encoding="utf-8")
            tail = "\n".join(log.strip().splitlines()[-25:])
            raise StackError("siril-cli produced no mosaic.\n"
                             + _space_hint(log) + tail)

        info = {
            "frames_in": len(frames),
            "frames_stacked": stacked,
            "registration": "astrometric" if astro else "star",
            "feather": feather,
            "canvas": _fits_dims(produced),
            "bytes": produced.stat().st_size,
        }

        dropped = len(frames) - (stacked if stacked is not None else len(frames))
        if dropped > 0 and not allow_partial:
            # keep the scratch dir: the stitched file is the only evidence of
            # which panels made it, and it is about to be thrown away
            preserve = True
            (workdir / "siril.log").write_text(log, encoding="utf-8")
            raise StackError(
                f"only {stacked} of {len(frames)} frames registered - "
                f"{dropped} panel(s) would be missing from the mosaic.\n"
                + ("  Panels are dropped silently by star-based registration when "
                   "they share too few stars with the reference frame.\n"
                   "  Install Siril's offline Gaia DR3 astrometry extract so the "
                   "frames can be registered astrometrically, or pass "
                   "--mosaic-allow-partial to accept the gaps.\n"
                   if not astro else "")
                + f"  Stitched so far, kept for inspection: {produced}\n"
                + f"  (set ASSESS_LIGHTS_KEEP_STACK_WORKDIR=1 to keep the whole "
                  f"scratch dir at {workdir})")

        shutil.copy2(produced, dest)
        return dest, info
    finally:
        if not preserve:
            shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# PixInsight
# --------------------------------------------------------------------------- #

_PI_TEMPLATE = r"""#engine v8
// Generated by assess-lights. Registers all frames with StarAlignment, then
// combines with ImageIntegration. Parameter usage mirrors PixInsight's own
// BatchPreprocessing script, which is authoritative for these processes.

var FRAMES  = __FRAMES__;
var REF     = "__REF__";
var REGDIR  = "__REGDIR__";
var OUTPATH = "__OUTPATH__";
var LOG     = "__LOG__";

var log = [];
function W( s ) { log.push( String( s ) ); File.writeTextFile( LOG, log.join( "\n" ) + "\n" ); }

W( "frames: " + FRAMES.length );
W( "reference: " + REF );

// ---- 1. register -----------------------------------------------------------
var SA = new StarAlignment;
SA.referenceImage = REF;
SA.referenceIsFile = true;
SA.outputDirectory = REGDIR;
SA.outputExtension = ".fit";
SA.outputPrefix = "";
SA.outputPostfix = "_r";
SA.outputSampleFormat = StarAlignment.f32;
SA.overwriteExistingFiles = true;
SA.generateDrizzleData = false;

// Row shape as produced by BPP's WBPPUtils.enableTargetFrames( paths, 3 ):
// [ enabled, enabled, filePath, "" ]
var saTargets = [];
for ( var i = 0; i < FRAMES.length; ++i )
   saTargets.push( [ true, true, FRAMES[i], "" ] );
SA.targets = saTargets;

var saOk = false;
try { saOk = SA.executeGlobal(); }
catch ( e ) { W( "StarAlignment threw: " + e ); }
W( "StarAlignment ok=" + saOk );
if ( !saOk ) { W( "FAILED: registration" ); }

// ---- 2. collect whatever StarAlignment actually wrote ----------------------
// Do not guess the registered file names: output postfix, extension and
// numbering have all varied across PixInsight releases. List the directory.
var regPaths = [];
var listing = File.searchDirectory( REGDIR + "/*" );
W( "regdir entries: " + listing.length );
for ( var i = 0; i < listing.length; ++i )
{
   var f = listing[i];
   W( "  " + f );
   if ( /\.(fit|fits|fts|xisf)$/i.test( f ) && File.exists( f ) )
      regPaths.push( f );
}
W( "registered files found: " + regPaths.length );
if ( regPaths.length < 3 ) { W( "FAILED: fewer than 3 registered frames" ); }

// ---- 3. integrate ----------------------------------------------------------
// Build the row array first and assign it in ONE go: `II.images = []` followed
// by pushes does not stick (the property setter stores a copy), and the process
// then reports "less than three source images".
var iiRows = [];
for ( var i = 0; i < regPaths.length; ++i )
   iiRows.push( [ true, regPaths[i], "", "" ] );

var II = new ImageIntegration;
II.images = iiRows;
W( "II.images length: " + II.images.length );
II.combination = ImageIntegration.Average;
II.rejection = ImageIntegration.WinsorizedSigmaClip;
II.sigmaLow = 3.0;
II.sigmaHigh = 3.0;
II.weightScale = ImageIntegration.WeightScale_BWMV;
II.generateRejectionMaps = false;
II.generateDrizzleData = false;
II.subtractPedestals = false;
II.autoMemorySize = true;

var iiOk = false;
try { iiOk = II.executeGlobal(); }
catch ( e ) { W( "ImageIntegration threw: " + e ); }
W( "ImageIntegration ok=" + iiOk );
if ( !iiOk ) { W( "FAILED: integration" ); }

// ---- 4. save --------------------------------------------------------------
try
{
   var w = ImageWindow.windowById( II.integrationImageId );
   if ( !w.isNull )
   {
      // LOW-LEVEL write, deliberately. ImageWindow.saveAs() defaults queryOptions
      // to TRUE, which pops the "FITS Options" (Sample Format) sheet even under
      // --automation-mode and stalls an unattended run until someone clicks OK.
      // Passing queryOptions=false does suppress it, but PixInsight then silently
      // inherits the saved FITS defaults -- measured here as 32-bit float, where
      // this tool documents a 64-bit master. FileFormatInstance has no dialog at
      // all AND lets us pin the sample format, so use it and keep both.
      var saved = false;
      try
      {
         var F = new FileFormat( ".fit", false /*toRead*/, true /*toWrite*/ );
         if ( F.isNull )
            W( "no FITS writer available" );
         else
         {
            var f = new FileFormatInstance( F );
            if ( f.isNull )
               W( "cannot instantiate the FITS writer" );
            else
            {
               if ( !f.create( OUTPATH, "verbosity 0" ) )
                  W( "create failed: " + OUTPATH );
               else
               {
                  var d = new ImageDescription;
                  d.bitsPerSample = 64;
                  d.ieeefpSampleFormat = true;
                  if ( !f.setOptions( d ) )
                     W( "setOptions failed" );
                  else
                  {
                     // carry the header across; writeImage() alone would write a
                     // bare one (this is what BPP's own writer does)
                     if ( w.keywords.length > 0 )
                        f.keywords = w.keywords;
                     if ( !f.writeImage( w.mainView.image ) )
                        W( "writeImage failed" );
                     else
                        saved = true;
                  }
                  f.close();
               }
            }
         }
      }
      catch ( e ) { W( "low-level save threw: " + e ); }

      if ( !saved )
      {
         // Never lose the master over this: fall back to the high-level call with
         // queryOptions off. No dialog either way; the sample format then follows
         // the FITS format defaults.
         W( "falling back to saveAs() with the format defaults" );
         if ( !w.saveAs( OUTPATH,
                         false /*queryOptions*/,
                         false /*allowMessages*/,
                         true  /*strict*/,
                         false /*verifyOverwrite*/ ) )
            W( "saveAs returned false" );
      }
      W( "saved: " + OUTPATH );
      w.forceClose();
   }
   else W( "integration window not found" );
}
catch ( e ) { W( "save threw: " + e ); }

W( "DONE" );
"""


def stack_pixinsight(frames: list[Path], dest: Path, binary: str,
                     progress=None, tick=None) -> Path:
    """Register + integrate with PixInsight headless. Returns the master path."""
    workdir = Path(tempfile.mkdtemp(prefix="assess-lights-pi-stack-"))
    # same escape hatch siril has: keep the scratch dir (and stack.log) for
    # debugging, e.g. to read back how many frames were actually registered
    keep = os.environ.get("ASSESS_LIGHTS_KEEP_STACK_WORKDIR") == "1"
    regdir = workdir / "registered"
    regdir.mkdir()
    log = workdir / "stack.log"
    script = workdir / "stack.js"
    out = dest

    # StarAlignment names its output <basename>_r.fit, so duplicate basenames
    # would collide in REGDIR and quietly drop frames
    inputs, renamed = _unique_inputs(frames, workdir / "inputs")
    if renamed and progress:
        progress(f"{renamed} duplicate frame name(s) across the directories - "
                 f"staging those under distinct names so none is dropped")

    # register to the frame with the most stars would be ideal; the first frame
    # is a safe default (PI falls back gracefully if the reference is poor)
    # NOTE: no .resolve() -- a staged symlink must keep its unique name, or the
    # collision returns via the back door
    ref = str(inputs[0])

    def jslist(paths):
        return "[" + ", ".join('"' + str(p).replace("\\", "\\\\") + '"'
                               for p in paths) + "]"

    src = (_PI_TEMPLATE
           .replace("__FRAMES__", jslist(inputs))
           .replace("__REF__", ref.replace("\\", "\\\\"))
           .replace("__REGDIR__", str(regdir).replace("\\", "\\\\"))
           .replace("__OUTPATH__", str(out).replace("\\", "\\\\"))
           .replace("__LOG__", str(log).replace("\\", "\\\\")))
    script.write_text(src, encoding="utf-8")

    if progress:
        progress(f"PixInsight: registering {len(frames)} frame(s) and integrating")

    try:
        proc = subprocess.run(
            [binary, "-n", "--automation-mode", "--no-startup-scripts",
             f"-r={script}", "--force-exit"],
            cwd=str(workdir), capture_output=True, text=True, timeout=21600,
        )
        if tick is not None:
            tick()
    finally:
        pass

    if out.exists() and out.stat().st_size > 0:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
        return out

    detail = log.read_text(encoding="utf-8") if log.exists() else ""
    detail += "\n" + (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    raise StackError(f"PixInsight produced no stacked image.\n{detail[-2500:]}"
                     + (f"\n\nwork dir kept: {workdir}" if keep else ""))


# --------------------------------------------------------------------------- #

def stack(target: Path, backend: str = "auto", pi_binary: str | None = None,
          siril_binary: str | None = None, progress=None, tick=None):
    """Stack the FITS left in `target`; returns (path, engine_used)."""
    frames = source_frames(target)
    if len(frames) < 2:
        raise StackError(
            f"need at least 2 FITS frames to stack, found {len(frames)} in {target}")

    # decide the output name before either engine starts
    dest, note = resolve_output(target)
    if note and progress:
        progress(note)

    return _dispatch(frames, dest, backend, pi_binary, siril_binary, progress, tick)


def stack_frames(frames: list[Path], dest: Path, backend: str = "auto",
                 pi_binary: str | None = None, siril_binary: str | None = None,
                 progress=None, tick=None):
    """Stack an explicit frame list into an explicit output path.

    Used by the merged multi-directory mode, where the master goes wherever the
    user asked instead of `<dir>/_final-stacked.fit`. Returns (path, engine_used).
    """
    if len(frames) < 2:
        raise StackError(f"need at least 2 FITS frames to stack, found {len(frames)}")

    dest, note = unique_path(dest)
    if note and progress:
        progress(note)

    return _dispatch(frames, dest, backend, pi_binary, siril_binary, progress, tick)


def _dispatch(frames: list[Path], dest: Path, backend: str,
              pi_binary: str | None, siril_binary: str | None,
              progress, tick):
    """Choose an engine and run it. Returns (path, engine_used)."""
    eng = backend
    if eng == "auto":
        if resolve_pi_binary(pi_binary):
            eng = "pixinsight"
        elif resolve_siril_binary(siril_binary):
            eng = "siril"
        else:
            raise StackError(
                "no stacking engine available: neither PixInsight nor siril-cli "
                "was found.\n"
                "  PixInsight: install it, or pass --pi-binary / set "
                "ASSESS_LIGHTS_PI_BINARY\n"
                "  Siril:      brew install siril (macOS), or pass --siril-binary / "
                "set ASSESS_LIGHTS_SIRIL_BINARY")

    if eng == "pixinsight":
        binary = resolve_pi_binary(pi_binary)
        if not binary:
            raise StackError("PixInsight not found; pass --pi-binary or use "
                             "--stack-backend siril")
        return stack_pixinsight(frames, dest, binary, progress, tick), eng

    if eng == "siril":
        binary = resolve_siril_binary(siril_binary)
        if not binary:
            raise StackError("siril-cli not found; brew install siril, or pass "
                             "--siril-binary")
        return stack_siril(frames, dest, binary, progress, tick), eng

    raise StackError(f"unknown stacking backend {eng!r}")
