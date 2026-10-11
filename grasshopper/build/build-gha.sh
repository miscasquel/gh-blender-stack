#!/usr/bin/env bash
# Build GhStack.gha: the core codec/conversion plus the GH components.
#
#     grasshopper/build/build-gha.sh
#
# Uses Mono's real csc.exe (see build-core.sh for why wine's Framework wrapper
# is not used). References: Grasshopper, GH_IO, RhinoCommon, System.Drawing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
CSC_PATH="$(find /usr/share/wine/mono -maxdepth 6 -path '*/lib/mono/4.5/csc.exe' | head -1)"
[ -f "$CSC_PATH" ] || { echo "mono csc.exe not found" >&2; exit 1; }
CSC="$(WINEPREFIX="$PREFIX" "$WINE" winepath -w "$CSC_PATH")"
OUT="$HERE/out/GhStack.gha"

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }

RHINO="$PREFIX/drive_c/Program Files/Rhino 8"
GH_DLL="$RHINO/Plug-ins/Grasshopper/Grasshopper.dll"
GHIO_DLL="$RHINO/Plug-ins/Grasshopper/GH_IO.dll"
RC_DLL="$RHINO/System/RhinoCommon.dll"
for f in "$GH_DLL" "$GHIO_DLL" "$RC_DLL"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

mkdir -p "$HERE/out"
rm -f "$OUT"

SOURCES=()
for f in "$REPO"/grasshopper/src/core/*.cs "$REPO"/grasshopper/src/gh/*.cs; do
  SOURCES+=("$(win_path "$f")")
done

WINEPREFIX="$PREFIX" "$WINE" "$CSC" \
  /nologo /target:library \
  /out:"$(win_path "$OUT")" \
  /r:"$(win_path "$GH_DLL")" /r:"$(win_path "$GHIO_DLL")" /r:"$(win_path "$RC_DLL")" \
  /r:System.Drawing.dll \
  "${SOURCES[@]}" 2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
