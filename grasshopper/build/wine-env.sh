#!/usr/bin/env bash
# Shared wine/Mono environment for the C# builds.
#
# Source this from a build script after `set -euo pipefail`:
#
#     HERE="$(cd "$(dirname "$0")" && pwd)"
#     source "$HERE/wine-env.sh"
#
# It provides PREFIX, WINE, CSC_PATH, CSC, RHINO, RC_DLL, GH_DLL, GHIO_DLL,
# win_path(), run_csc() and run_wine(). REPO is derived from this file unless
# the caller already set it.
#
# Do not execute this file directly.

WINE_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$WINE_ENV_DIR/../.." && pwd)}"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
WINE="${WINE:-$HOME/.local/share/wine-rhino/bin/wine}"
# Mono's real csc.exe, not wine's Framework wrapper: the wrapper compiles
# against the 4.0-api profile, whose mscorlib lacks IReadOnlyList and cannot
# resolve RhinoCommon's mesh types at all.
CSC_PATH="${CSC_PATH:-$(find /usr/share/wine/mono -maxdepth 6 \
  -path '*/lib/mono/4.5/csc.exe' -print -quit)}"
[ -f "$CSC_PATH" ] || { echo "mono csc.exe not found" >&2; return 1; }
CSC="$(WINEPREFIX="$PREFIX" "$WINE" winepath -w "$CSC_PATH")"

RHINO="${RHINO:-$PREFIX/drive_c/Program Files/Rhino 8}"
RC_DLL="$RHINO/System/RhinoCommon.dll"
GH_DLL="$RHINO/Plug-ins/Grasshopper/Grasshopper.dll"
GHIO_DLL="$RHINO/Plug-ins/Grasshopper/GH_IO.dll"

win_path() { WINEPREFIX="$PREFIX" "$WINE" winepath -w "$1"; }

# Run csc with the known wine noise filtered out, and return csc's own status.
# A `... | grep ... || true` pipeline hides compiler failures; this does not.
run_csc() {
  local log status had_e=0
  case "$-" in
    *e*) had_e=1 ;;
  esac
  log="$(mktemp)"
  set +e
  WINEPREFIX="$PREFIX" "$WINE" "$CSC" "$@" >"$log" 2>&1
  status=$?
  if [ "$had_e" -eq 1 ]; then set -e; fi
  grep -vE 'fixme:|MESA-EGL|pci id|^$' "$log" || true
  rm -f "$log" || true
  return "$status"
}

run_wine() {
  WINEPREFIX="$PREFIX" "$WINE" "$@"
}
