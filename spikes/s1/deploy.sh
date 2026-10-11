#!/usr/bin/env bash
# Deploy the S1 spike .gha into the Grasshopper Libraries folder.
#
# Grasshopper scans Libraries/<folder>/ for .gha files on load, so a spike-only
# folder keeps it separate from the real GhStack plugin that M4 will build.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
GHA="$HERE/GhStackSpike.gha"
DEST="$PREFIX/drive_c/users/miquel/AppData/Roaming/Grasshopper/Libraries/GhStackSpike"

[ -f "$GHA" ] || { echo "no $GHA; run spikes/s1/build.sh first" >&2; exit 1; }
mkdir -p "$DEST"
cp "$GHA" "$DEST/GhStackSpike.gha"
ls -la "$DEST"
echo "deployed to $DEST/GhStackSpike.gha"
echo "note: Grasshopper only scans Libraries on load, so start (or restart) Rhino"
