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

MODULE = "bl_ext.user_default.gh_stack"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PORT = 9144
failures = []


def check(name, ok, detail=""):
    print("  %-52s %s%s" % (name, "ok" if ok else "FAIL", (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    addon_utils.enable(MODULE, default_set=False)
    import importlib

    server = importlib.import_module(MODULE + ".blender.server")
    engine = importlib.import_module(MODULE + ".blender.engine")

    # Keep the frame cache out of the real ~/.local/share/gh-stack.
    try:
        bpy.context.preferences.addons[MODULE].preferences.data_root = tempfile.mkdtemp(
            prefix="gh-stack-installed-test-"
        )
    except KeyError:
        pass

    check("extension module imported", server is not None)
    check("panel class registered", hasattr(bpy.types, "GHSTACK_PT_panel"))

    import websockets

    ws_file = os.path.abspath(websockets.__file__)
    check("websockets from the extension wheel (not vendor)", "_libs" in ws_file or "site-packages" in ws_file, ws_file)

    engine.start_server(PORT)
    time.sleep(0.5)
    check("server listening", server.SERVER.running)

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    bpy.ops.gh_stack.add_stage(label="Installed")
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
            PYTHONPATH=os.pathsep.join([os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]),
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
    bpy.ops.preferences.addon_disable(module=MODULE)

    if failures:
        print("RESULT: FAIL (%d)" % len(failures))
        sys.exit(1)
    print("RESULT: PASS")


main()
