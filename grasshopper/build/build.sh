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
  /r:System.Numerics.dll \
  "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/ParamValues.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/Endpoint.cs")" \
  "$(win_path "$REPO/grasshopper/tests/console/Program.cs")"

[ -f "$OUT" ] || { echo "build failed: $OUT not produced" >&2; exit 1; }
echo "built $OUT ($(stat -c%s "$OUT") bytes)"

# The MiniWs transport: handshake verification, frame caps, fragmentation,
# control frames and close handling against an in-process scripted peer. The
# transport and the frame cap live in core/Frame.cs (no Rhino types), so this
# suite builds and runs without Rhino's managed assemblies.
MINIWS_OUT="$OUT_DIR/StageloomMiniWsTests.exe"
rm -f "$MINIWS_OUT"
run_csc \
  /nologo /target:exe \
  /out:"$(win_path "$MINIWS_OUT")" \
  /r:System.Numerics.dll \
  "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
  "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
  "$(win_path "$REPO/grasshopper/src/gh/MiniWsClient.cs")" \
  "$(win_path "$REPO/grasshopper/tests/console/FakeServer.cs")" \
  "$(win_path "$REPO/grasshopper/tests/console/MiniWsTests.cs")"
[ -f "$MINIWS_OUT" ] || { echo "build failed: $MINIWS_OUT not produced" >&2; exit 1; }
echo "built $MINIWS_OUT ($(stat -c%s "$MINIWS_OUT") bytes)"

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
    /r:"$(win_path "$RC_DLL")" /r:System.Numerics.dll \
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

# The conversion boundary tests use RhinoCommon's managed Section-side types
# (and, when the process can load RhinoCore, the real Mesh/Curve types). They
# are built wherever RhinoCommon exists; the wine harness runs the
# section-boundary half.
CONV_OUT="$OUT_DIR/StageloomConversionTests.exe"
if [ -f "$RC_DLL" ]; then
  cp -f "$RC_DLL" "$OUT_DIR/"
  rm -f "$CONV_OUT"
  run_csc \
    /nologo /target:exe \
    /out:"$(win_path "$CONV_OUT")" \
    /r:"$(win_path "$RC_DLL")" /r:System.Drawing.dll /r:System.Numerics.dll \
    "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Conversion.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/ConversionTests.cs")"
  [ -f "$CONV_OUT" ] || { echo "build failed: $CONV_OUT not produced" >&2; exit 1; }
  echo "built $CONV_OUT ($(stat -c%s "$CONV_OUT") bytes)"
else
  echo "note: no RhinoCommon.dll under the prefix; conversion tests not built" >&2
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
    /r:System.Drawing.dll /r:System.Numerics.dll \
    "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Conversion.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Attributes.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Endpoint.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/GhLink.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/StageSlot.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/StageRegistry.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/LinkConnection.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/RequestScheduler.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/ProtocolHandler.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/Publication.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/EndpointSettings.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/MiniWsClient.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/Instances.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/DefinitionTable.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/DefinitionIdentity.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/InstancePlan.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/InstanceBuild.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/InstanceEmit.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/DefinitionTableWriter.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/FakeServer.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/LinkTests.cs")"
  [ -f "$LINK_OUT" ] || { echo "build failed: $LINK_OUT not produced" >&2; exit 1; }
  echo "built $LINK_OUT ($(stat -c%s "$LINK_OUT") bytes)"
else
  echo "note: no Grasshopper/RhinoCommon under the prefix; link tests not built" >&2
fi

# The stage components' status halves. This compiles the full component source
# set (the same one build-gha.sh ships), so a compile break in Components.cs,
# GooDispatch.cs or Instances.cs cannot pass this suite unnoticed.
COMP_OUT="$OUT_DIR/StageloomComponentTests.exe"
if [ -f "$GH_DLL" ] && [ -f "$GHIO_DLL" ] && [ -f "$RC_DLL" ]; then
  cp -f "$RC_DLL" "$GH_DLL" "$GHIO_DLL" "$OUT_DIR/"
  rm -f "$COMP_OUT"
  COMP_SOURCES=()
  for f in "$REPO"/grasshopper/src/core/*.cs "$REPO"/grasshopper/src/gh/*.cs; do
    COMP_SOURCES+=("$(win_path "$f")")
  done
  run_csc \
    /nologo /target:exe \
    /out:"$(win_path "$COMP_OUT")" \
    /r:"$(win_path "$GH_DLL")" /r:"$(win_path "$GHIO_DLL")" /r:"$(win_path "$RC_DLL")" \
    /r:System.Drawing.dll /r:System.Numerics.dll \
    "${COMP_SOURCES[@]}" \
    "$(win_path "$REPO/grasshopper/tests/console/FakeServer.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/ComponentTests.cs")"
  [ -f "$COMP_OUT" ] || { echo "build failed: $COMP_OUT not produced" >&2; exit 1; }
  echo "built $COMP_OUT ($(stat -c%s "$COMP_OUT") bytes)"
else
  echo "note: no Grasshopper/RhinoCommon under the prefix; component tests not built" >&2
fi

# The inbound instance grammar: the definition table, the single- and
# multi-part namespaces, part attribute filters, row pairs and the content hash.
# It needs Grasshopper/RhinoCommon types but no Rhino process; the native build
# half is guarded inside the suite.
INSTANCE_OUT="$OUT_DIR/StageloomInstanceTests.exe"
if [ -f "$GH_DLL" ] && [ -f "$GHIO_DLL" ] && [ -f "$RC_DLL" ]; then
  cp -f "$RC_DLL" "$GH_DLL" "$GHIO_DLL" "$OUT_DIR/"
  rm -f "$INSTANCE_OUT"
  run_csc \
    /nologo /target:exe \
    /out:"$(win_path "$INSTANCE_OUT")" \
    /r:"$(win_path "$GH_DLL")" /r:"$(win_path "$GHIO_DLL")" /r:"$(win_path "$RC_DLL")" \
    /r:System.Drawing.dll /r:System.Numerics.dll \
    "$(win_path "$REPO/grasshopper/src/core/Json.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Frame.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Conversion.cs")" \
    "$(win_path "$REPO/grasshopper/src/core/Attributes.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/Instances.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/DefinitionTable.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/DefinitionIdentity.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/InstancePlan.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/InstanceBuild.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/InstanceEmit.cs")" \
    "$(win_path "$REPO/grasshopper/src/gh/DefinitionTableWriter.cs")" \
    "$(win_path "$REPO/grasshopper/tests/console/InstanceTests.cs")"
  [ -f "$INSTANCE_OUT" ] || { echo "build failed: $INSTANCE_OUT not produced" >&2; exit 1; }
  echo "built $INSTANCE_OUT ($(stat -c%s "$INSTANCE_OUT") bytes)"
else
  echo "note: no Grasshopper/RhinoCommon under the prefix; instance tests not built" >&2
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
