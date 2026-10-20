#!/usr/bin/env bash
# Build GhStack.gha: the core codec/conversion plus the GH components.
#
#     grasshopper/build/build-gha.sh
#
# Uses Mono's real csc.exe (see wine-env.sh for why wine's Framework wrapper
# is not used). References: Grasshopper, GH_IO, RhinoCommon, System.Drawing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/wine-env.sh"
OUT="$HERE/out/GhStack.gha"

for f in "$GH_DLL" "$GHIO_DLL" "$RC_DLL"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

mkdir -p "$HERE/out"
rm -f "$OUT"

SOURCES=()
for f in "$REPO"/grasshopper/src/core/*.cs "$REPO"/grasshopper/src/gh/*.cs; do
  SOURCES+=("$(win_path "$f")")
done

# Component icons: the official Blender glyphs, embedded so the .gha is one file.
RESOURCES=()
for png in "$REPO"/grasshopper/src/gh/icons/*.png; do
  name="$(basename "$png" .png)"
  RESOURCES+=("/resource:$(win_path "$png"),GhStack.icons.$name.png")
done

run_csc \
  /nologo /target:library \
  /out:"$(win_path "$OUT")" \
  /r:"$(win_path "$GH_DLL")" /r:"$(win_path "$GHIO_DLL")" /r:"$(win_path "$RC_DLL")" \
  /r:System.Drawing.dll \
  "${SOURCES[@]}" "${RESOURCES[@]}"

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
