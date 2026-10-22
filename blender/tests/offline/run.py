"""Layer B, offline gate: the `network` permission contract.

Run by ``scripts/test-blender`` as a *second* Blender process, without
``--online-mode``. The main suite runs with online access enabled because the
rest of the engine tests need the server; this process starts with factory
settings, where ``bpy.app.online_access`` is false, and proves the add-on does
not listen and explains why. It then enables online access, presses Start, and
proves the server comes up without a restart.
"""

import os
import sys
import time

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "ok" if ok else "FAIL",
                            (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    check("online access starts off", not bpy.app.online_access)

    bpy.ops.preferences.addon_enable(module="stageloom")
    import importlib

    server = importlib.import_module("stageloom.blender.server")
    bridge = importlib.import_module("stageloom.blender.bridge")

    time.sleep(0.2)
    check("offline: no server listens", not server.SERVER.running)
    notice = bridge.state.get("notice", "")
    check("offline: the panel explains the state", "Online access is disabled" in notice, notice)

    bpy.context.preferences.system.use_online_access = True
    check("online access now on", bpy.app.online_access)
    result = bpy.ops.stageloom.start_server()
    time.sleep(0.3)
    check("Start brings the server up", "FINISHED" in result and server.SERVER.running)
    check("online: the notice is cleared", not bridge.state.get("notice"), bridge.state.get("notice", ""))

    server.SERVER.stop()
    bpy.ops.preferences.addon_disable(module="stageloom")

    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("offline gate: PASS")


main()
