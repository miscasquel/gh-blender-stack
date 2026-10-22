#!/usr/bin/env bash
# Deploy Stageloom.gha into the Grasshopper Libraries folder (plan §10).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
GHA="$(cd "$HERE/.." && pwd)/build/out/Stageloom.gha"
DEST="$PREFIX/drive_c/users/miquel/AppData/Roaming/Grasshopper/Libraries/Stageloom"

[ -f "$GHA" ] || { echo "no $GHA; run grasshopper/build/build-gha.sh first" >&2; exit 1; }
mkdir -p "$DEST"
cp "$GHA" "$DEST/Stageloom.gha"
ls -la "$DEST"
echo "deployed to $DEST/Stageloom.gha"
echo "note: Grasshopper only scans Libraries on load, so start (or restart) Rhino"
