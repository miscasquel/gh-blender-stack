"""Layer D-lite: the *installed* extension, end to end.

Run by ``scripts/test-extension``, which builds the zip, installs it into a
throwaway Blender config, and then runs this file under ``blender -b`` with that
config. Proves what the repository tests cannot: the extension registers from
its install location, websockets comes from the declared wheel, the server
listens, and a full round trip against tools/fake_gh lands in the stage.
"""

import os
import subprocess
import sys
import tempfile
import time

import addon_utils
import bpy

MODULE = "bl_ext.user_default.stageloom"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PORT = 9144
failures = []


def check(name, ok, detail=""):
    print("  %-52s %s%s" % (name, "ok" if ok else "FAIL", (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


SAVED_PORT = 9149


def saved_port_check(server, prefs):
    """Save a non-default port, restart Blender on the same config, and require
    the server to be on it.

    The failure this exists for: `prefs` was registered after `engine`, so
    `engine.register()` started the server before the preference existed, fell
    back to the default, and the panel reported a port nothing listened on. The
    second process is the whole point — in-process, the preference is already
    registered and the bug cannot happen.
    """
    original = prefs.port
    prefs.port = SAVED_PORT
    bpy.ops.wm.save_userpref()
    server.SERVER.stop()
    phase2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "port_phase2.py")
    # `sys.executable` inside Blender is the embedded Python, not Blender itself;
    # `bpy.app.binary_path` is the interpreter that can run a second headless pass.
    # --online-mode: a fresh throwaway config reports online access as off, and
    # the add-on gates its server on it (the `network` permission contract).
    result = subprocess.run(
        [bpy.app.binary_path, "-b", "--online-mode", "--python", phase2],
        env=dict(os.environ, STAGELOOM_PORT_EXPECT=str(SAVED_PORT)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    lines = (result.stdout or "").splitlines()
    for line in lines:
        if line.startswith(("saved preference:", "PASS:", "FAIL:")):
            print("  " + line)
    detail = "exit %d" % result.returncode
    if result.returncode != 0:
        detail += " :: " + " | ".join(lines[-4:])
    check("a saved port survives a restart", result.returncode == 0, detail)
    prefs.port = original
    bpy.ops.wm.save_userpref()


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # default_set=True matters twice: it creates the preferences entry this test
    # needs to write (under --factory-startup nothing is enabled yet), and it is
    # what makes the data-root redirect below take effect instead of silently
    # raising the KeyError that used to be swallowed here.
    addon_utils.enable(MODULE, default_set=True)
    import importlib

    server = importlib.import_module(MODULE + ".blender.server")
    engine = importlib.import_module(MODULE + ".blender.engine")

    # The default must be the extension's own user directory, not a hard-coded
    # path or the extension install directory.
    prefs = importlib.import_module(MODULE + ".blender.prefs")
    default_root = prefs.data_root()
    extensions_root = os.environ.get("BLENDER_USER_EXTENSIONS", "")
    check(
        "default data root under the extension user directory",
        bool(extensions_root) and default_root.startswith(extensions_root),
        default_root,
    )

    # Keep the frame cache out of the real user data root for the round trip.
    try:
        bpy.context.preferences.addons[MODULE].preferences.data_root = tempfile.mkdtemp(
            prefix="stageloom-installed-test-"
        )
    except KeyError:
        pass
    check(
        "frame cache kept out of the real data root",
        bpy.context.preferences.addons[MODULE].preferences.data_root.startswith(tempfile.gettempdir()),
        bpy.context.preferences.addons[MODULE].preferences.data_root,
    )

    check("extension module imported", server is not None)
    check("panel class registered", hasattr(bpy.types, "STAGELOOM_PT_panel"))

    import websockets

    ws_file = os.path.abspath(websockets.__file__)
    check("websockets from the extension wheel (not vendor)", "_libs" in ws_file or "site-packages" in ws_file, ws_file)

    engine.start_server(PORT)
    time.sleep(0.5)
    check("server listening", server.SERVER.running)

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    bpy.ops.stageloom.add_stage(label="Installed")
    stage = obj.gh_stack.stages[0]

    report = os.path.join(tempfile.gettempdir(), "fake_gh_installed_report.json")
    if os.path.exists(report):
        os.remove(report)
    fake = subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port", str(PORT),
            "--stage", stage.key,
            "--report", report,
        ],
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join([os.path.join(REPO, "blender", "stageloom", "vendor"), REPO]),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + 15
    while time.time() < deadline and stage.state not in ("ok", "error"):
        engine._timer()
        time.sleep(0.01)
    check("stage reached ok", stage.state == "ok", stage.error or stage.state)

    fake.terminate()
    try:
        fake.wait(timeout=5)
    except subprocess.TimeoutExpired:
        fake.kill()

    saved_port_check(server, bpy.context.preferences.addons[MODULE].preferences)

    bpy.ops.preferences.addon_disable(module=MODULE)

    if failures:
        print("RESULT: FAIL (%d)" % len(failures))
        sys.exit(1)
    print("RESULT: PASS")


main()
