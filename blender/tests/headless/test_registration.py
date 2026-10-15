"""Layer B, registration: what `register()` can see, in the order it runs.

Run with:

    scripts/test-blender

`engine.register()` starts the server, and the server reads the port preference.
That only works if the preferences class is registered *first* — with `prefs`
last in `MODULE_ORDER` the engine saw no preferences, fell back to the default,
and the panel (which reads the preference) reported a port nothing was
listening on. The order assertion below is the tripwire; the installed layer
(`tests/installed/`) reproduces the failure across two Blender launches, which is
the shape it actually takes for a user.
"""

import os
import sys
import time
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)


class RegistrationOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_prefs_register_before_the_engine_starts_the_server(self):
        import gh_stack

        order = list(gh_stack.MODULE_ORDER)
        self.assertLess(
            order.index("prefs"),
            order.index("engine"),
            "prefs must register before the engine: engine.register() starts the "
            "server, which reads the port preference",
        )

    def test_the_server_follows_the_preference(self):
        import gh_stack.blender.engine as engine
        from gh_stack.blender import server as server_mod

        prefs = bpy.context.preferences.addons["gh_stack"].preferences
        original = prefs.port
        try:
            prefs.port = 9157
            self.assertTrue(engine.start_server())
            deadline = time.time() + 5.0
            while time.time() < deadline and server_mod.SERVER.port != 9157:
                time.sleep(0.05)
            self.assertEqual(
                server_mod.SERVER.port,
                9157,
                "the server did not bind the preferred port",
            )
        finally:
            prefs.port = original
            engine.start_server()
