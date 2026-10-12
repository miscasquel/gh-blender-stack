#!/usr/bin/env bash
# Build the headless pre-flight for MiniWsClient (miniws.cs + wsclienttest.cs).
# No Grasshopper references are needed: the client is transport-only, so it runs
# under the wine runtime without Rhino. See spikes/s1/wsecho.py for the peer.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
CSC_PATH="$(find /usr/share/wine/mono -maxdepth 6 -path '*/lib/mono/4.5/csc.exe' | head -1)"
[ -f "$CSC_PATH" ] || { echo "mono csc.exe not found" >&2; exit 1; }
CSC="$(WINEPREFIX="$PREFIX" "$WINE" winepath -w "$CSC_PATH")"
OUT="$HERE/WsTest.exe"

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }

RS_DLL="$(find /usr/share/wine/mono -maxdepth 9 \
  -path '*/lib/mono/gac/System.Runtime.Serialization*/System.Runtime.Serialization.dll' | head -1)"
SCW_DLL="$(find /usr/share/wine/mono -maxdepth 9 \
  -path '*/lib/mono/gac/System.ServiceModel.Web/*/System.ServiceModel.Web.dll' | head -1)"
for f in "$RS_DLL" "$SCW_DLL"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done

rm -f "$OUT"
WINEPREFIX="$PREFIX" "$WINE" "$CSC" \
  /nologo /target:exe \
  /out:"$(win_path "$OUT")" \
  /r:"$(win_path "$RS_DLL")" /r:"$(win_path "$SCW_DLL")" \
  "$(win_path "$HERE/miniws.cs")" "$(win_path "$HERE/../../grasshopper/src/gh/MiniWsClient.cs")" "$(win_path "$HERE/wsclienttest.cs")" \
  2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
