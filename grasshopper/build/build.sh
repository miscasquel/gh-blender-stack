#!/usr/bin/env bash
# Build the C# core codec and its fixture tests.
#
#     grasshopper/build/build.sh
#
# Produces grasshopper/build/out/GhStackCoreTests.exe, which runs under the wine
# runtime without Rhino (the codec has no RhinoCommon or Grasshopper references):
#
#     WINEPREFIX=... wine grasshopper/build/out/GhStackCoreTests.exe protocol/fixtures
#
# This is the same wine csc.exe recipe as the S1 spike (see spikes/s1/build.sh):
# the csc under wine is Mono's mcs, and /sdk:4.5 is how its reference set is
# selected consistently.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
CSC='C:\windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'
OUT_DIR="$HERE/out"
OUT="$OUT_DIR/GhStackCoreTests.exe"

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }

mkdir -p "$OUT_DIR"
rm -f "$OUT"

WINEPREFIX="$PREFIX" "$WINE" "$CSC" \
  /nologo /sdk:4.5 /target:exe \
  /out:"$(win_path "$OUT")" \
  "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
  "$(win_path "$REPO/grasshopper/tests/console/Program.cs")" \
  2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
