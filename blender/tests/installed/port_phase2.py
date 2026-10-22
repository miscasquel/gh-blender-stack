"""Layer D, phase 2 of the saved-port check.

Launched by ``blender/tests/installed/run.py`` as a *second* Blender process on
the same throwaway config, with no ``--factory-startup``: the point is that the
saved preference is read while the add-on registers at startup, which is the path
a user takes and the one a single-process test cannot reach. Phase 1 set the port
and saved the preferences; this process asserts the server bound it.

    blender -b --python port_phase2.py     (BLENDER_USER_CONFIG must be the config
                                            phase 1 saved into; STAGELOOM_PORT_EXPECT
                                            is the port to expect)

Exits non-zero when the server is on anything else.
"""

import os
import sys
import time

import addon_utils
import bpy

MODULE = "bl_ext.user_default.stageloom"
EXPECTED = int(os.environ["STAGELOOM_PORT_EXPECT"])


def main():
    addon_utils.enable(MODULE, default_set=False)
    import importlib

    server = importlib.import_module(MODULE + ".blender.server")
    prefs = bpy.context.preferences.addons[MODULE].preferences

    deadline = time.time() + 5.0
    while time.time() < deadline and server.SERVER.port != EXPECTED:
        time.sleep(0.05)
    bound = server.SERVER.port
    print("saved preference: %s | bound: %s" % (prefs.port, bound))
    if bound != EXPECTED:
        print("FAIL: the saved port was ignored at startup")
        sys.exit(1)
    print("PASS: the server bound the saved port")
    server.SERVER.stop()


main()
