#!/usr/bin/env bash
# Build the S1 spike .gha with the wine csc.exe (C# 5, plan §10).
#
# Findings that shape this script:
# - The wine csc.exe is Mono's mcs, not Microsoft's csc.
# - wine's .NET 4.0 System.dll has no WebSockets: ClientWebSocket lives in Mono's
#   real System.dll (gac/System/4.0.0.0__b77a5c561934e089). Referencing it with
#   /sdk:4.5 compiles and runs (verified 2026-10-11).
# - /nostdlib and /noconfig are not accepted by this mcs build; use /sdk:4.5.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
CSC='C:\windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'
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
  /nologo /sdk:4.5 /target:library \
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
