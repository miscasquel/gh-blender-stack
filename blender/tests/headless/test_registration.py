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
        bpy.ops.preferences.addon_enable(module="stageloom")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_prefs_register_before_the_engine_starts_the_server(self):
        import stageloom

        order = list(stageloom.MODULE_ORDER)
        self.assertLess(
            order.index("prefs"),
            order.index("engine"),
            "prefs must register before the engine: engine.register() starts the "
            "server, which reads the port preference",
        )

    def test_saved_old_default_port_is_migrated(self):
        """A preference Blender saved while the default was 9130 would keep
        winning over the new default — and 9130 is BlenderKit's. Only the old
        default moves; a deliberate port is left alone."""
        from stageloom.blender import prefs as prefs_mod

        settings = bpy.context.preferences.addons["stageloom"].preferences
        original = settings.port
        try:
            settings.port = prefs_mod.OLD_DEFAULT_PORT
            prefs_mod._migrate_old_port()
            self.assertEqual(settings.port, prefs_mod.DEFAULT_PORT)
            settings.port = 9158
            prefs_mod._migrate_old_port()
            self.assertEqual(settings.port, 9158, "a deliberate port was moved")
        finally:
            settings.port = original

    def test_the_server_follows_the_preference(self):
        import stageloom.blender.engine as engine
        from stageloom.blender import server as server_mod

        prefs = bpy.context.preferences.addons["stageloom"].preferences
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


class DataRootTests(unittest.TestCase):
    """The default data root is a real absolute path, not a hard-coded Linux one
    in the extension install."""

    def test_the_development_fallback_is_absolute_and_branded(self):
        import stageloom.blender.prefs as prefs

        root = prefs.DEFAULT_DATA_ROOT
        self.assertTrue(os.path.isabs(root), root)
        self.assertIn("stageloom", root, root)


class RegistrationReportTests(unittest.TestCase):
    """The Phase 0 report: one visible generation, both install paths, no writes.

    The reload investigation starts by counting handlers. This report is the
    console-facing tripwire; if it says two, the handoff's duplicate-generation
    cause is confirmed before any fix is written.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_report_finds_one_engine_generation(self):
        import stageloom.blender.engine as engine

        report = engine.registration_report()
        self.assertTrue(report["timer_registered"], "the timer is not registered")
        self.assertEqual(
            report["handler_counts"],
            {
                "depsgraph_update_post": 1,
                "frame_change_post": 1,
                "load_post": 1,
                "undo_post": 1,
                "redo_post": 1,
            },
        )
        self.assertEqual(report["enabled_addons"], ["stageloom"])
        self.assertTrue(
            os.path.normpath(report["engine_file"]).endswith(
                os.path.join("engine", "__init__.py")
            ),
            report["engine_file"],
        )
        self.assertEqual(report["timer_module"], "stageloom.blender.engine.triggers")
        self.assertEqual(report["stack_objects"], 0)

    def test_a_foreign_handler_is_not_counted(self):
        import stageloom.blender.engine as engine

        def foreign(scene, depsgraph=None):
            return None

        foreign.__module__ = "other_addon.handlers"
        handlers = bpy.app.handlers.depsgraph_update_post
        handlers.append(foreign)
        try:
            report = engine.registration_report()
            self.assertEqual(len(handlers), 2)
            self.assertEqual(report["handler_counts"]["depsgraph_update_post"], 1)
        finally:
            handlers.remove(foreign)

    def test_load_restores_a_missing_timer(self):
        import stageloom.blender.engine as engine

        bpy.app.timers.unregister(engine._timer)
        try:
            engine._on_load()
            self.assertTrue(
                bpy.app.timers.is_registered(engine._timer),
                "a file load did not re-register the lost engine timer",
            )
        finally:
            if not bpy.app.timers.is_registered(engine._timer):
                bpy.app.timers.register(
                    engine._timer,
                    first_interval=engine.IDLE_INTERVAL,
                    persistent=True,
                )

    def test_predicate_covers_development_and_extension_installs(self):
        import stageloom.blender.engine as engine

        for module in (
            "stageloom.blender.engine",
            "stageloom.blender.engine.triggers",
            "bl_ext.user_default.stageloom.blender.engine",
            "bl_ext.user_default.stageloom.blender.engine.triggers",
            "bl_ext.system.stageloom.blender.engine.messages",
        ):
            self.assertTrue(engine._is_engine_module(module), module)
        for module in (
            "",
            "other.addon",
            "stageloom.blender.engine_extra",
            "other.stageloom.blender.engine_extra",
            "my_stageloom.blender.engine",
        ):
            self.assertFalse(engine._is_engine_module(module), module)
