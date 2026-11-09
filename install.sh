#!/usr/bin/env bash
# Installer for assess-lights: creates a private venv, installs the package, and
# puts a self-locating launcher on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY_VERSION="${PY_VERSION:-3.12}"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is required but not on PATH." >&2
  echo "Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if [ -x .venv/bin/python ]; then
  echo "==> reusing the existing virtualenv (.venv)"
else
  echo "==> creating virtualenv (.venv, python $PY_VERSION)"
  uv venv --python "$PY_VERSION" .venv
fi

echo "==> installing assess-lights + dependencies"
uv pip install --python .venv/bin/python -e .

# --- README version/author line ----------------------------------------------
# pyproject.toml is the single source of truth; this keeps the small meta line at
# the top of README.md from drifting. It is a no-op when they already agree.
echo "==> syncing README version/author line"
.venv/bin/python scripts/sync_readme.py || true

# --- self-locating launcher ---------------------------------------------------
# bin/assess-lights resolves symlinks to find its own installation, so it always
# runs the venv that sits next to the code, whatever directory you call it from.
echo "==> installing launcher"
LAUNCHER="$HERE/bin/assess-lights"
chmod +x "$LAUNCHER"

BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"
ln -sf "$LAUNCHER" "$BIN_DIR/assess-lights"
echo "    $BIN_DIR/assess-lights -> $LAUNCHER"

add_path() {
  local f="$1"
  [ -e "$f" ] || : > "$f"
  if ! grep -q 'local/bin' "$f" 2>/dev/null; then
    printf '\n# added by the assess-lights installer\ncase ":$PATH:" in\n  *":$HOME/.local/bin:"*) ;;\n  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;\nesac\n' >> "$f"
    echo "    added ~/.local/bin to PATH in $f"
  fi
}
add_path "$HOME/.profile"
if [ -n "${BASH_VERSION:-}" ]; then add_path "$HOME/.bashrc"; fi

# --- external tools -----------------------------------------------------------
echo
echo "==> optional external tools"
if command -v fitsverify >/dev/null 2>&1; then
  echo "    fitsverify : found  ($(command -v fitsverify))"
else
  hint=""
  if command -v apt-get >/dev/null 2>&1; then hint="apt install libcfitsio-bin"
  elif command -v dnf >/dev/null 2>&1; then hint="dnf install cfitsio"
  elif command -v apk >/dev/null 2>&1; then hint="apk add cfitsio"
  elif command -v brew >/dev/null 2>&1; then hint="brew install cfitsio"
  fi
  echo "    fitsverify : NOT found - structural FITS verification falls back to the"
  echo "                 built-in checks."
  if [ -n "$hint" ]; then
    echo "                 To add it:  $hint"
  else
    echo "                 It ships with cfitsio; this host has no package manager we"
    echo "                 recognise, so the built-in checks will be used (they catch"
    echo "                 truncation, header, geometry and blank-frame problems)."
  fi
fi

PI_DEFAULT="/Applications/PixInsight/PixInsight.app/Contents/MacOS/PixInsight"
if [ -x "$PI_DEFAULT" ] || [ -n "${ASSESS_LIGHTS_PI_BINARY:-}" ]; then
  echo "    PixInsight : found - SubframeSelector will be used for step 3/8"
else
  echo "    PixInsight : NOT found"
  echo "                 Step 3/8 (focus/shape) will use the built-in SEP backend"
  echo "                 (--backend local, the automatic fallback). SEP is the"
  echo "                 SExtractor engine and needs no PixInsight."
  echo "                 Use --skip-pi to skip step 3/8 entirely."
fi

if command -v siril-cli >/dev/null 2>&1 || [ -n "${ASSESS_LIGHTS_SIRIL_BINARY:-}" ]; then
  echo "    siril-cli  : found - available for --stack (fallback stacker)"
else
  echo "    siril-cli  : NOT found"
  echo "                 --stack needs PixInsight or siril-cli. Install with"
  echo "                 'brew install siril' (macOS) or your distro's siril"
  echo "                 package. Without either, --stack exits with a message."
fi

echo
echo "Done. From a NEW shell, just run:"
echo "    assess-lights <fits-directory>"
echo
echo "In this shell, open a new one or run:"
echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
