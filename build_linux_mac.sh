#!/usr/bin/env bash
# ============================================================
#  iAmped — Build Script (Linux / macOS)
#  Produces a portable, self-contained app:
#    macOS -> dist/iAmped.app   (double-click to run)
#    Linux -> dist/iAmped       (single binary)
#
#  Saved settings (Plex URL + token) live in an "iAmped-data" folder
#  created next to the app on first run, so the whole thing is portable.
# ============================================================
set -e
cd "$(dirname "$0")"

echo ""
echo " ========================================"
echo "  iAmped Build Script — Linux / macOS"
echo " ========================================"
echo ""

# --- Python environment + deps -------------------------------------
# Avoid Apple's old system Python/pip on macOS. Reuse the same project-local
# environment as run.sh so dependency wheels and PyInstaller stay consistent.
PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv
fi
echo "[1/3] Installing Python packages (app deps + PyInstaller)..."
./.venv/bin/python -m pip install -q --upgrade pip
./.venv/bin/python -m pip install -q -r requirements.txt pyinstaller

# --- Clean ----------------------------------------------------------
echo "[2/3] Cleaning old build artefacts..."
rm -rf build dist

# --- Build ----------------------------------------------------------
echo "[3/3] Building self-contained app..."
./.venv/bin/python -m PyInstaller --noconfirm build_tools/iamped.spec

echo ""
echo " Done!"
if [ "$(uname)" = "Darwin" ]; then
    echo " App:  dist/iAmped.app   (double-click to run)"
    echo " Settings folder created next to it on first launch: dist/iAmped-data/"
else
    echo " App:  dist/iAmped       (single self-contained binary)"
    echo " Settings folder created next to it on first launch: dist/iAmped-data/"
fi
echo ""
