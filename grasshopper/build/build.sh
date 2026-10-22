#!/usr/bin/env bash
# Build the C# core codec and its fixture tests.
#
#     grasshopper/build/build.sh
#
# Produces grasshopper/build/out/StageloomCoreTests.exe, which runs under the wine
# runtime without Rhino (the codec has no RhinoCommon or Grasshopper references):
#
#     WINEPREFIX=... wine grasshopper/build/out/StageloomCoreTests.exe protocol/fixtures
#
# Mono's real csc.exe is used, not wine's Framework wrapper: the wrapper
# compiles against a 4.0 profile that cannot resolve RhinoCommon types (see
# wine-env.sh).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/wine-env.sh"
OUT_DIR="$HERE/out"
OUT="$OUT_DIR/StageloomCoreTests.exe"

mkdir -p "$OUT_DIR"
rm -f "$OUT"

run_csc \
  /nologo /target:exe \
  /out:"$(win_path "$OUT")" \
  "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/ParamValues.cs")" \
  "$(win_path "$REPO/grasshopper/tests/console/Program.cs")"

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"

# The attribute-store tests reference RhinoCommon (a managed assembly: the
# ArchivableDictionary runs without a Rhino process, but the types must be
# present). They build only where the Rhino install is; the codec tests above
# need nothing from Rhino.
ATTR_OUT="$OUT_DIR/StageloomAttrTests.exe"
if [ -f "$RC_DLL" ]; then
  cp -f "$RC_DLL" "$OUT_DIR/"
  rm -f "$ATTR_OUT"
  run_csc \
    /nologo /target:exe \
    /out:"$(win_path "$ATTR_OUT")" \
    /r:"$(win_path "$RC_DLL")" \
    "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Conversion.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Attributes.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/AttrTests.cs")"
  [ -f "$ATTR_OUT" ] || { echo "build failed: $ATTR_OUT not produced" >&2; exit 1; }
  echo "built $ATTR_OUT ($(stat -c%s "$ATTR_OUT") bytes)"
else
  echo "note: no RhinoCommon.dll under the prefix; attribute tests not built" >&2
fi

# The link round trip drives the real GhLink over a fake WebSocket server. It
# needs Grasshopper.dll (GH_Component is a Grasshopper type) and RhinoCommon,
# but no Rhino process: an unattached component's OnPingDocument() returns
# null, so the solve trigger is exercised as a note.
LINK_OUT="$OUT_DIR/StageloomLinkTests.exe"
if [ -f "$GH_DLL" ] && [ -f "$GHIO_DLL" ] && [ -f "$RC_DLL" ]; then
  cp -f "$RC_DLL" "$GH_DLL" "$GHIO_DLL" "$OUT_DIR/"
  rm -f "$LINK_OUT"
  run_csc \
    /nologo /target:exe \
    /out:"$(win_path "$LINK_OUT")" \
    /r:"$(win_path "$GH_DLL")" /r:"$(win_path "$GHIO_DLL")" /r:"$(win_path "$RC_DLL")" \
    /r:System.Drawing.dll \
    "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/GhLink.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/MiniWsClient.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/LinkTests.cs")"
  [ -f "$LINK_OUT" ] || { echo "build failed: $LINK_OUT not produced" >&2; exit 1; }
  echo "built $LINK_OUT ($(stat -c%s "$LINK_OUT") bytes)"
else
  echo "note: no Grasshopper/RhinoCommon under the prefix; link tests not built" >&2
fi

# Stage In's build guard. It has no Rhino types, so it builds and runs without
# a Rhino process; the same guard wraps the real conversion in the component.
STAGE_OUT="$OUT_DIR/StageloomStageTests.exe"
rm -f "$STAGE_OUT"
run_csc \
  /nologo /target:exe \
  /out:"$(win_path "$STAGE_OUT")" \
  "$(win_path "$REPO/grasshopper/src/gh/StageBuildGuard.cs")" \
  "$(win_path "$REPO/grasshopper/tests/console/StageTests.cs")"
[ -f "$STAGE_OUT" ] || { echo "build failed: $STAGE_OUT not produced" >&2; exit 1; }
echo "built $STAGE_OUT ($(stat -c%s "$STAGE_OUT") bytes)"
