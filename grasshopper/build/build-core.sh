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
source "$HERE/wine-env.sh"
OUT_DIR="$HERE/out"
OUT="$OUT_DIR/GhStack.Core.dll"

[ -f "$RC_DLL" ] || { echo "missing $RC_DLL" >&2; exit 1; }

mkdir -p "$OUT_DIR"
rm -f "$OUT"

SOURCES=()
for f in "$REPO"/grasshopper/src/core/*.cs; do
  SOURCES+=("$(win_path "$f")")
done

run_csc \
  /nologo /target:library \
  /out:"$(win_path "$OUT")" \
  /r:"$(win_path "$RC_DLL")" /r:System.Drawing.dll \
  "${SOURCES[@]}"

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"
