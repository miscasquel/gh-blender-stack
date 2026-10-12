#!/usr/bin/env bash
# Build and run the S6 API probe under wine, with RhinoCommon on the path.
#
#     spikes/s6/probe.sh
#
# This does NOT run: a standalone wine-mono process cannot load RhinoCore.dll
# (its DllMain fails and the module is unloaded), so the probe never starts.
# Kept as the record of that dead end. The API facts the conversions rely on
# were obtained through the spike .gha (`check=s6`, running inside Rhino) and
# through `rhino3dm` for the knot conventions.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
RHINO="$PREFIX/drive_c/Program Files/Rhino 8"
RC_DLL="$RHINO/System/RhinoCommon.dll"
CSC_PATH="$(find /usr/share/wine/mono -maxdepth 6 -path '*/lib/mono/4.5/csc.exe' | head -1)"
[ -f "$CSC_PATH" ] || { echo "mono csc.exe not found" >&2; exit 1; }
[ -f "$RC_DLL" ] || { echo "missing $RC_DLL" >&2; exit 1; }

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }
CSC="$(win_path "$CSC_PATH")"
# Mono probes assemblies next to the exe, not on WINEPATH, so build and run in
# /tmp/opencode/s6/ with RhinoCommon.dll copied alongside.
OUT_DIR="${OUT_DIR:-/tmp/opencode/s6}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/probe_api.exe"
cp -f "$RC_DLL" "$OUT_DIR/RhinoCommon.dll"
# Native dependencies (P/Invoke). Mono does not search the Rhino System dir for
# dangling names, so the ones the probe touches are copied next to the exe.
for dll in rhcommon_c.dll opennurbs.dll; do
  [ -f "$RHINO/System/$dll" ] && cp -f "$RHINO/System/$dll" "$OUT_DIR/$dll"
done

rm -f "$OUT"
WINEPREFIX="$PREFIX" "$WINE" "$CSC" \
  /nologo /target:exe \
  /out:"$(win_path "$OUT")" \
  /r:"$(win_path "$RC_DLL")" /r:System.Drawing.dll \
  "$(win_path "$HERE/probe_api.cs")" 2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true

[ -f "$OUT" ] || { echo "build failed" >&2; exit 1; }

# rhcommon_c.dll lives in Rhino's System dir; run with that directory as the
# working directory so the native P/Invokes resolve.
export WINEPATH
WINEPATH="$(WINEPREFIX="$PREFIX" "$WINE" winepath -w "$RHINO/System")"
(cd "$RHINO/System" && WINEPREFIX="$PREFIX" "$WINE" "$(win_path "$OUT")" 2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$')
