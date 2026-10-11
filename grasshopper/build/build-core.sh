#!/usr/bin/env bash
# Build the C# core library: codec, hashing, conversion.
#
#     grasshopper/build/build-core.sh
#
# Produces grasshopper/build/out/GhStack.Core.dll. The codec and hashing halves
# are also compiled into the console fixture tests (which run without Rhino);
# this build additionally compiles the RhinoCommon conversion, so it is the
# compile check for conversion code.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
# Mono's real csc.exe, not wine's Framework wrapper: the wrapper compiles against
# the 4.0-api profile, whose mscorlib lacks IReadOnlyList and therefore cannot
# resolve RhinoCommon's mesh types at all (found building Conversion.cs).
CSC_PATH="$(find /usr/share/wine/mono -maxdepth 6 -path '*/lib/mono/4.5/csc.exe' | head -1)"
[ -f "$CSC_PATH" ] || { echo "mono csc.exe not found" >&2; exit 1; }
CSC="$(WINEPREFIX="$PREFIX" "$WINE" winepath -w "$CSC_PATH")"
OUT_DIR="$HERE/out"
OUT="$OUT_DIR/GhStack.Core.dll"

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }

RHINO="$PREFIX/drive_c/Program Files/Rhino 8"
RC_DLL="$RHINO/System/RhinoCommon.dll"
[ -f "$RC_DLL" ] || { echo "missing $RC_DLL" >&2; exit 1; }

mkdir -p "$OUT_DIR"
rm -f "$OUT"

SOURCES=()
for f in "$REPO"/grasshopper/src/core/*.cs; do
  SOURCES+=("$(win_path "$f")")
done

WINEPREFIX="$PREFIX" "$WINE" "$CSC" \
  /nologo /target:library \
  /out:"$(win_path "$OUT")" \
  /r:"$(win_path "$RC_DLL")" /r:System.Drawing.dll \
  "${SOURCES[@]}" 2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
