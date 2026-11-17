"""Layer B: registration order, reports and preference migration."""

import os
import socket
import tempfile
import threading
import time
import unittest

import bpy

from harness import free_port, start_fake, stop_fake
from stageloom.blender import bridge
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import diagnostics as engine_diagnostics
from stageloom.blender.engine import triggers as engine_triggers


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
            # Any port that is not the legacy default; nothing binds it.
            settings.port = 9158
            prefs_mod._migrate_old_port()
            self.assertEqual(settings.port, 9158, "a deliberate port was moved")
        finally:
            settings.port = original

    def test_saved_old_data_root_is_migrated(self):
        """A preference saved on the pre-rebrand path moves to the extension
        data directory; a deliberate root is left alone."""
        from stageloom.blender import prefs as prefs_mod

        settings = bpy.context.preferences.addons["stageloom"].preferences
        original = settings.data_root
        try:
            settings.data_root = prefs_mod.OLD_DATA_ROOT
            prefs_mod._migrate_old_data_root()
            self.assertEqual(settings.data_root, prefs_mod.DEFAULT_DATA_ROOT)
            settings.data_root = os.path.join(tempfile.gettempdir(), "stageloom-root")
            prefs_mod._migrate_old_data_root()
            self.assertEqual(
                settings.data_root,
                os.path.join(tempfile.gettempdir(), "stageloom-root"),
                "a deliberate data root was moved",
            )
        finally:
            settings.data_root = original

    def test_the_server_follows_the_preference(self):
        import stageloom.blender.engine as engine
        from stageloom.blender import server as server_mod

        prefs = bpy.context.preferences.addons["stageloom"].preferences
        original = prefs.port
        port = free_port()
        try:
            prefs.port = port
            self.assertTrue(engine.start_server())
            deadline = time.time() + 5.0
            while time.time() < deadline and server_mod.SERVER.port != port:
                time.sleep(0.05)
            self.assertEqual(
                server_mod.SERVER.port,
                port,
                "the server did not bind the preferred port",
            )
        finally:
            prefs.port = original
            engine.start_server()


class ServerStopTests(unittest.TestCase):
    """Stopping the socket thread is bounded and start/stop/start leaves no
    worker behind."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        from stageloom.blender import server as server_mod

        cls.engine = engine
        cls.server_mod = server_mod

    @classmethod
    def tearDownClass(cls):
        cls.server_mod.SERVER.stop()
        # Leave the add-on's normal server running for the next class/module.
        cls.engine.start_server()
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _stop_elapsed(self):
        started = time.monotonic()
        self.server_mod.SERVER.stop()
        return time.monotonic() - started

    def test_01_start_stop_start_does_not_leak_a_worker(self):
        server = self.server_mod.SERVER
        for _attempt in range(3):
            port = free_port()
            self.assertTrue(server.start(port))
            deadline = time.time() + 5.0
            while time.time() < deadline and not (
                server.running and server.port == port
            ):
                time.sleep(0.01)
            self.assertTrue(server.running, "the server never started")
            elapsed = self._stop_elapsed()
            self.assertLess(elapsed, 1.5, "stop blocked the main thread for %.2f s" % elapsed)
            self.assertFalse(server.running)
            self.assertFalse(
                any(
                    thread.name == "stageloom-server" and thread.is_alive()
                    for thread in threading.enumerate()
                ),
                "a socket worker outlived stop()",
            )

    def test_02_a_wedged_worker_is_a_bounded_fallback(self):
        server = self.server_mod.SERVER
        wedged = threading.Thread(
            target=lambda: time.sleep(5.0), name="stageloom-server", daemon=True
        )
        wedged.start()
        real_thread, real_loop, real_event = (
            server._thread,
            server._loop,
            server._stop_event,
        )
        server._thread = wedged
        server._loop = None
        server._stop_event = None
        try:
            started = time.monotonic()
            server.stop()
            elapsed = time.monotonic() - started
            self.assertLess(
                elapsed,
                2.5,
                "a wedged worker blocked for %.2f s (the old 3 s join)" % elapsed,
            )
        finally:
            server._thread = real_thread
            server._loop = real_loop
            server._stop_event = real_event
            wedged.join(timeout=6.0)
        self.assertFalse(wedged.is_alive())

    def test_03_stop_with_a_connected_client_closes_and_returns(self):
        server = self.server_mod.SERVER
        port = free_port()
        report = os.path.join(tempfile.gettempdir(), "fake_gh_server_stop.json")
        if os.path.exists(report):
            os.remove(report)
        fake = start_fake("server-stop-test", report, port=port)
        try:
            self.assertTrue(server.start(port))
            deadline = time.time() + 10.0
            while time.time() < deadline and not bridge.state["clients"]:
                time.sleep(0.02)
            self.assertGreater(bridge.state["clients"], 0, "the fake never connected")
            started = time.monotonic()
            server.stop()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.5, "a connected stop blocked for %.2f s" % elapsed)
            self.assertFalse(server.running)
        finally:
            stop_fake(fake)


class ServerBindFailureTests(unittest.TestCase):
    """A taken port is a visible failure, and a later start recovers."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        from stageloom.blender import server as server_mod

        cls.engine = engine
        cls.server_mod = server_mod

    @classmethod
    def tearDownClass(cls):
        cls.server_mod.SERVER.stop()
        # Leave the add-on's normal server running for the next class/module.
        cls.engine.start_server()
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_a_taken_port_is_visible_and_a_later_start_recovers(self):
        from stageloom.blender import bridge

        server = self.server_mod.SERVER
        server.stop()
        # start() failure must set this; a leftover stop message would make
        # the visibility check pass without a real bind attempt.
        bridge.state["error"] = ""
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]

            # start_server returns once the worker thread exists; the bind is
            # what fails. Known finding (docs/seams-0.8.md): the worker's exit
            # path currently replaces the bind error with "server stopped", so
            # this asserts visibility and recovery, not the exact text.
            self.engine.start_server(port)
            deadline = time.time() + 5.0
            while time.time() < deadline and (
                server.running or not bridge.state["error"]
            ):
                time.sleep(0.02)
            self.assertFalse(server.running, "the server claimed a taken port")
            self.assertTrue(bridge.state["error"], "a bind failure was silent")
        finally:
            blocker.close()

        self.engine.start_server(port)
        deadline = time.time() + 5.0
        while time.time() < deadline and not (
            server.running and server.port == port
        ):
            time.sleep(0.02)
        self.assertTrue(server.running, "the server never bound the freed port")
        self.assertEqual(server.port, port)

        deadline = time.time() + 2.0
        while time.time() < deadline and bridge.state["error"]:
            time.sleep(0.02)
        self.assertEqual(bridge.state["error"], "", "a successful bind kept the old error")


class DataRootTests(unittest.TestCase):
    """The default data root is a real absolute path, not a hard-coded Linux one
    in the extension install."""

    def test_the_development_fallback_is_absolute_and_branded(self):
        import stageloom.blender.prefs as prefs

        root = prefs.DEFAULT_DATA_ROOT
        self.assertTrue(os.path.isabs(root), root)
        self.assertIn("stageloom", root, root)


class RegistrationReportTests(unittest.TestCase):
    """The registration report: one visible generation, both install paths."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _empty_scene(self):

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete()
        # A load (headless has no event loop for the activation settle) leaves
        # a stage-less file dormant.
        engine_triggers._on_load()

    def test_report_finds_one_engine_generation(self):

        self._empty_scene()
        report = ENGINE.registration_report()
        self.assertFalse(report["active"], "a file with no stages kept the engine active")
        self.assertFalse(
            report["timer_registered"],
            "a file with no stages kept the engine timer registered",
        )
        self.assertEqual(
            report["handler_counts"],
            {
                # A stage-less file has no per-update or per-frame handlers: the
                # lifecycle hooks are what reactivate a saved stage.
                "depsgraph_update_post": 0,
                "frame_change_post": 0,
                "animation_playback_pre": 0,
                "animation_playback_post": 0,
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

        self._empty_scene()

        def foreign(scene, depsgraph=None):
            return None

        foreign.__module__ = "other_addon.handlers"
        handlers = bpy.app.handlers.depsgraph_update_post
        handlers.append(foreign)
        try:
            report = ENGINE.registration_report()
            self.assertEqual(len(handlers), 1)
            self.assertEqual(
                report["handler_counts"]["depsgraph_update_post"],
                0,
                "the report counted a foreign handler",
            )
        finally:
            handlers.remove(foreign)

    def test_adding_and_removing_a_stage_activates_and_deactivates(self):
        import stageloom.blender.nodes as nodes

        self._empty_scene()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        bpy.ops.stageloom.add_stage(label="Dormant")
        stage = obj.gh_stack.stages[0]

        self.assertTrue(ENGINE.active, "adding a stage did not activate the engine")
        self.assertTrue(bpy.app.timers.is_registered(engine_triggers._timer))
        self.assertIn(engine_triggers._on_depsgraph, bpy.app.handlers.depsgraph_update_post)

        modifier = nodes.modifier_of(obj, stage)
        self.assertIsNotNone(modifier)
        obj.modifiers.remove(modifier)
        ENGINE.mark_object_dirty(obj)
        self.assertIsNone(engine_triggers._timer(), "the last-stage pass kept the timer alive")

        self.assertEqual(len(obj.gh_stack.stages), 0)
        self.assertFalse(ENGINE.active, "the last stage left the engine active")
        self.assertFalse(bpy.app.timers.is_registered(engine_triggers._timer))

        bpy.data.objects.remove(obj, do_unlink=True)
        ENGINE.ensure_active()

    def test_load_restores_a_missing_timer(self):
        import stageloom.blender.nodes as nodes

        self._empty_scene()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        bpy.ops.stageloom.add_stage(label="Reload Timer")
        stage = obj.gh_stack.stages[0]
        self.assertTrue(ENGINE.active, "adding a stage did not activate the engine")

        bpy.app.timers.unregister(engine_triggers._timer)
        try:
            engine_triggers._on_load()
            self.assertTrue(
                bpy.app.timers.is_registered(engine_triggers._timer),
                "a file load with a stage did not re-register the lost engine timer",
            )
        finally:
            modifier = nodes.modifier_of(obj, stage)
            if modifier is not None:
                obj.modifiers.remove(modifier)
            ENGINE.mark_object_dirty(obj)
            engine_triggers._timer()
            bpy.data.objects.remove(obj, do_unlink=True)
            ENGINE.ensure_active()

    def test_predicate_covers_development_and_extension_installs(self):

        for module in (
            "stageloom.blender.engine",
            "stageloom.blender.engine.triggers",
            "bl_ext.user_default.stageloom.blender.engine",
            "bl_ext.user_default.stageloom.blender.engine.triggers",
            "bl_ext.system.stageloom.blender.engine.messages",
        ):
            self.assertTrue(engine_diagnostics._is_engine_module(module), module)
        for module in (
            "",
            "other.addon",
            "stageloom.blender.engine_extra",
            "other.stageloom.blender.engine_extra",
            "my_stageloom.blender.engine",
        ):
            self.assertFalse(engine_diagnostics._is_engine_module(module), module)
