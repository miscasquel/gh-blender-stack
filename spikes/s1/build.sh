#!/usr/bin/env bash
# Build the S1 spike .gha with Mono's csc.exe under wine (plan §10).
#
# Findings that shape this script:
# - wine's Framework csc.exe wrapper compiles against the 4.0-api profile; Mono's
#   real csc.exe (lib/mono/4.5/csc.exe) is used instead, because the 4.0 profile
#   cannot resolve RhinoCommon's mesh types (IReadOnlyList is missing). See
#   grasshopper/build/build-core.sh.
# - DataContractJsonSerializer needs /r:System.ServiceModel.Web.dll plus
#   /r:System.Runtime.Serialization.dll from the GAC.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
CSC_PATH="$(find /usr/share/wine/mono -maxdepth 6 -path '*/lib/mono/4.5/csc.exe' | head -1)"
[ -f "$CSC_PATH" ] || { echo "mono csc.exe not found" >&2; exit 1; }
CSC="$(WINEPREFIX="$PREFIX" "$WINE" winepath -w "$CSC_PATH")"
OUT="$HERE/GhStackSpike.gha"

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }

MONO_SYSTEM="$(find /usr/share/wine/mono -maxdepth 9 \
  -path '*/lib/mono/gac/System/4.0.0.0__b77a5c561934e089/System.dll' | head -1)"
if [ -z "$MONO_SYSTEM" ] || [ ! -f "$MONO_SYSTEM" ]; then
  echo "mono System.dll not found under /usr/share/wine/mono" >&2
  exit 1
fi

RHINO="$PREFIX/drive_c/Program Files/Rhino 8"
GH_DLL="$RHINO/Plug-ins/Grasshopper/Grasshopper.dll"
GHIO_DLL="$RHINO/Plug-ins/Grasshopper/GH_IO.dll"
RC_DLL="$RHINO/System/RhinoCommon.dll"
for f in "$GH_DLL" "$GHIO_DLL" "$RC_DLL"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

RS_DLL="$(find /usr/share/wine/mono -maxdepth 9 \
  -path '*/lib/mono/gac/System.Runtime.Serialization*/System.Runtime.Serialization.dll' | head -1)"
SCW_DLL="$(find /usr/share/wine/mono -maxdepth 9 \
  -path '*/lib/mono/gac/System.ServiceModel.Web/*/System.ServiceModel.Web.dll' | head -1)"
[ -f "$RS_DLL" ] || { echo "missing System.Runtime.Serialization.dll" >&2; exit 1; }
[ -f "$SCW_DLL" ] || { echo "missing System.ServiceModel.Web.dll" >&2; exit 1; }

SRC_W="$(win_path "$HERE/GhStackSpike.cs")"
OUT_W="$(win_path "$OUT")"
rm -f "$OUT"

WINEPREFIX="$PREFIX" "$WINE" "$CSC" \
  /nologo /target:library \
  /out:"$OUT_W" \
  /r:"$(win_path "$GH_DLL")" /r:"$(win_path "$GHIO_DLL")" /r:"$(win_path "$RC_DLL")" \
  /r:"$(win_path "$RS_DLL")" /r:"$(win_path "$SCW_DLL")" \
  "$(win_path "$HERE/miniws.cs")" "$(win_path "$HERE/GhStackSpike.cs")" \
  2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true

if [ ! -f "$OUT" ]; then
  echo "build failed: $OUT not produced" >&2
  exit 1
fi
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
