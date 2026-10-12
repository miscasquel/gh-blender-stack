"""Layer B, prefix evaluation: a stage sees everything before its own modifier.

Run with:

    scripts/test-blender

What it proves (plan §9.1, acceptance A3):

1. A native modifier placed before the first stage is part of that stage's input.
2. A second stage receives the first stage's output, with the modifier between
   them applied: the `GH -> GN -> GH` chain works.
3. A waiting stage stops the stack; later stages never run on a stale prefix.
4. Base edits propagate through the whole chain.
5. Steady state is quiet: prefix clones do not keep the engine busy.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)

PORT = 9145
SCALE_PARAM = "00000000-0000-4000-8000-0000000000aa"


def pump(seconds=8.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b (S2)."""
    import gh_stack.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


def start_fake(stage_key, report, delay_ms=0):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port", str(PORT),
            "--stage", stage_key,
            "--report", report,
            "--delay-ms", str(delay_ms),
        ],
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]
            ),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop_fake(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def report_of(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))
    except ValueError:
        return {}


def isolate_data_root():
    """Point the frame cache at a temp dir so tests never write into the real
    ``~/.local/share/gh-stack``."""
    prefs = bpy.context.preferences.addons["gh_stack"].preferences
    prefs.data_root = tempfile.mkdtemp(prefix="gh-stack-test-")


class PrefixChainTests(unittest.TestCase):
    """PreBevel -> A -> MidBevel -> B on one cube."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine

        cls.engine = engine

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Chain"

        engine.start_server(PORT)
        time.sleep(0.5)

        pre = cls.obj.modifiers.new("PreBevel", "BEVEL")
        pre.width = 0.2
        cls.obj.modifiers.active = pre
        bpy.ops.gh_stack.add_stage(label="A")
        cls.stage_a = cls.obj.gh_stack.stages[0]

        mid = cls.obj.modifiers.new("MidBevel", "BEVEL")
        mid.width = 0.1
        cls.obj.modifiers.active = mid
        bpy.ops.gh_stack.add_stage(label="B")
        cls.stage_b = cls.obj.gh_stack.stages[1]

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_prefix_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_prefix_b.json")
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)

        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b)

        def ready():
            return cls.stage_a.state in ("ok", "error") and cls.stage_b.state in ("ok", "error")

        pump(until=ready)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_native_modifier_before_the_first_stage(self):
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)
        report = report_of(self.report_a)
        # The cube has 8 vertices; the pre-bevel adds more, and nothing scales yet.
        self.assertGreater(report.get("received_verts", 0), 8, report)
        self.assertAlmostEqual(report.get("received_max", 0.0), 1.0, places=2)

    def test_02_second_stage_sees_the_first_stages_output(self):
        # Scale A by 2: B's prefix must show the doubled extent, not the base.
        self.obj["gh/%s/%s" % (self.stage_a.key, SCALE_PARAM)] = 2.0
        self.engine.push(self.obj)

        def b_updated():
            return abs(report_of(self.report_b).get("received_max", 0.0) - 2.0) < 0.05

        self.assertTrue(pump(until=b_updated), "stage B never saw the scaled prefix")
        report = report_of(self.report_b)
        self.assertGreater(report.get("received_verts", 0), 8, report)
        self.assertGreaterEqual(report.get("full", 0), 1)

    def test_03_base_edit_propagates_through_the_chain(self):
        rid_a = self.stage_a.rid
        rid_b = self.stage_b.rid
        mesh = self.obj.data
        mesh.vertices[0].co.x += 1.0
        mesh.update()
        bpy.context.view_layer.update()

        def updated():
            return (
                self.stage_a.rid != rid_a
                and self.stage_b.rid != rid_b
                and self.stage_a.state == "ok"
                and self.stage_b.state == "ok"
            )

        self.assertTrue(pump(until=updated), "the chain did not settle after a base edit")
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)

    def test_04_steady_state_is_quiet(self):
        pump(seconds=0.5)
        self.engine._timer()
        self.assertFalse(self.engine._dirty, "prefix clones keep the engine dirty")
        responses_b = report_of(self.report_b).get("responses", 0)
        for _ in range(5):
            self.engine._timer()
        self.assertEqual(report_of(self.report_b).get("responses", 0), responses_b)
        self.assertFalse(self.engine._dirty)


class WaitingPrefixTests(unittest.TestCase):
    """A slow stage A stops stage B until A has answered.

    The chain settles first; then a push with a changed A output creates the
    condition deterministically (no dependence on which fake connected first).
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine

        cls.engine = engine

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Waits"

        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.gh_stack.add_stage(label="Slow")
        cls.stage_a = cls.obj.gh_stack.stages[0]
        bpy.ops.gh_stack.add_stage(label="Fast")
        cls.stage_b = cls.obj.gh_stack.stages[1]

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_wait_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_wait_b.json")
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)

        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a, delay_ms=1000)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b)

        def settled():
            return cls.stage_a.state == "ok" and cls.stage_b.state == "ok"

        pump(seconds=15.0, until=settled)
        cls.responses_b_before = report_of(cls.report_b).get("responses", 0)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_downstream_does_not_start_while_upstream_waits(self):
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)

        # Change A's output and push: A goes waiting, B must not run yet.
        self.obj["gh/%s/%s" % (self.stage_a.key, SCALE_PARAM)] = 2.0
        self.engine.push(self.obj)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: self.stage_a.state == "waiting"),
            "stage A never went waiting",
        )
        self.assertNotEqual(self.stage_b.state, "waiting")
        self.assertEqual(
            report_of(self.report_b).get("responses", 0),
            self.responses_b_before,
            "stage B ran on a stale prefix",
        )

    def test_02_downstream_runs_after_upstream_answers(self):
        def both_ok():
            return self.stage_a.state == "ok" and self.stage_b.state == "ok"

        self.assertTrue(pump(seconds=15.0, until=both_ok), "the chain never settled")
        report_b = report_of(self.report_b)
        self.assertGreater(report_b.get("responses", 0), self.responses_b_before)
        self.assertAlmostEqual(report_b.get("received_max", 0.0), 2.0, places=2)


class ComponentPrefixTests(unittest.TestCase):
    """Prefixes of a curve stack and a point-cloud stack: the evaluated
    component (not the base datablock) is what the second stage receives."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine

        cls.engine = engine
        engine.start_server(PORT)
        time.sleep(0.5)

        cls.curve = cls._build_curve_chain()
        cls.points = cls._build_point_chain()

        def ready():
            for chain in (cls.curve, cls.points):
                if chain["a"].state not in ("ok", "error") or chain["b"].state not in ("ok", "error"):
                    return False
            return True

        pump(seconds=10.0, until=ready)

    @classmethod
    def tearDownClass(cls):
        for chain in (cls.curve, cls.points):
            stop_fake(chain["fake_a"])
            stop_fake(chain["fake_b"])
        bpy.ops.preferences.addon_disable(module="gh_stack")

    @classmethod
    def _offset_group(cls, name):
        """A GN modifier that translates geometry by +5 in X: the stage after it
        must see the translated component."""
        ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
        ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        group_in = ng.nodes.new("NodeGroupInput")
        group_out = ng.nodes.new("NodeGroupOutput")
        transform = ng.nodes.new("GeometryNodeTransform")
        transform.inputs["Translation"].default_value = (5.0, 0.0, 0.0)
        ng.links.new(group_in.outputs["Geometry"], transform.inputs["Geometry"])
        ng.links.new(transform.outputs["Geometry"], group_out.inputs["Geometry"])
        return ng

    @classmethod
    def _chain(cls, obj, name):
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.gh_stack.add_stage(label=name + "A")
        stage_a = obj.gh_stack.stages[0]

        offset = obj.modifiers.new("Offset", "NODES")
        offset.node_group = cls._offset_group(name + "Offset")
        obj.modifiers.active = offset
        bpy.ops.gh_stack.add_stage(label=name + "B")
        stage_b = obj.gh_stack.stages[1]

        report_a = os.path.join(tempfile.gettempdir(), "fake_gh_%s_a.json" % name)
        report_b = os.path.join(tempfile.gettempdir(), "fake_gh_%s_b.json" % name)
        for path in (report_a, report_b):
            if os.path.exists(path):
                os.remove(path)
        return {
            "obj": obj,
            "a": stage_a,
            "b": stage_b,
            "report_a": report_a,
            "report_b": report_b,
            "fake_a": start_fake(stage_a.key, report_a),
            "fake_b": start_fake(stage_b.key, report_b),
        }

    @classmethod
    def _build_curve_chain(cls):
        curve = bpy.data.curves.new("prefix-curve", "CURVE")
        curve.dimensions = "3D"
        spline = curve.splines.new("NURBS")
        spline.points.add(4)
        for i, point in enumerate(spline.points):
            point.co = (float(i), 0.0, 0.0, 1.0)
        spline.order_u = 3
        spline.use_endpoint_u = True
        obj = bpy.data.objects.new("PrefixCurve", curve)
        bpy.context.scene.collection.objects.link(obj)
        return cls._chain(obj, "curve")

    @classmethod
    def _build_point_chain(cls):
        cloud = bpy.data.pointclouds.new("prefix-points")
        cloud.resize(3)
        positions = np.array([(0, 0, 0), (1, 0, 0), (2, 0, 0)], dtype=np.float32)
        cloud.points.foreach_set("co", positions.reshape(-1))
        cloud.points.update()
        obj = bpy.data.objects.new("PrefixPoints", cloud)
        bpy.context.scene.collection.objects.link(obj)
        return cls._chain(obj, "points")

    def test_01_curve_prefix_is_the_evaluated_component(self):
        def seen():
            report = report_of(self.curve["report_b"])
            first = report.get("received_first") or [None]
            return (
                report.get("received_curve_points") == 5
                and first[0] is not None
                and abs(first[0] - 5.0) < 1e-3
            )

        self.assertTrue(pump(seconds=10.0, until=seen), "stage B never saw the offset curve prefix")
        report_a = report_of(self.curve["report_a"])
        report_b = report_of(self.curve["report_b"])
        self.assertIn("crv.points", report_b.get("sections", []))
        self.assertEqual(report_b.get("received_curve_points", 0), 5)
        # The base spline starts at x=0; the offset modifier moves the prefix to x=5.
        self.assertAlmostEqual(report_a.get("received_first", [None])[0], 0.0, places=4)
        self.assertAlmostEqual(report_b.get("received_first", [None])[0], 5.0, places=4)

    def test_02_point_prefix_is_the_evaluated_component(self):
        def seen():
            report = report_of(self.points["report_b"])
            first = report.get("received_first") or [None]
            return (
                "pts.positions" in report.get("sections", [])
                and first[0] is not None
                and abs(first[0] - 5.0) < 1e-3
            )

        self.assertTrue(pump(seconds=10.0, until=seen), "stage B never saw the offset point prefix")
        report_a = report_of(self.points["report_a"])
        report_b = report_of(self.points["report_b"])
        self.assertIn("pts.positions", report_b.get("sections", []))
        self.assertAlmostEqual(report_a.get("received_first", [None])[0], 0.0, places=4)
        self.assertAlmostEqual(report_b.get("received_first", [None])[0], 5.0, places=4)


class LateClientTests(unittest.TestCase):
    """A request sent before a stage's client connected is re-sent when that
    client says hello (protocol §8.8), not left waiting for the timeout. The
    late client answers the ref with `unknown_ref`, so the engine must also
    resend the geometry in full (protocol §4.3).

    Reproduces the multi-client race: one client is connected, so the stage
    sends; the frame is broadcast only to connected clients, so it is lost for
    the client that is still starting.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine

        cls.engine = engine

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Late"

        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.gh_stack.add_stage(label="Late")
        cls.stage_a = cls.obj.gh_stack.stages[0]
        bpy.ops.gh_stack.add_stage(label="Early")
        cls.stage_b = cls.obj.gh_stack.stages[1]

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_late_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_late_b.json")
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)

        # Only the second stage's client exists; the first stage's request has
        # nobody to receive it.
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b)
        pump(seconds=5.0, until=lambda: cls.stage_a.state == "waiting")
        cls.fake_a = None

    @classmethod
    def tearDownClass(cls):
        if cls.fake_a is not None:
            stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_request_waits_without_its_client(self):
        self.assertEqual(self.stage_a.state, "waiting")
        self.assertEqual(report_of(self.report_a).get("responses", 0), 0)

    def test_02_late_client_is_answered_without_a_timeout(self):
        self.fake_a = start_fake(self.stage_a.key, self.report_a)
        type(self).fake_a = self.fake_a

        def answered():
            return self.stage_a.state == "ok" and report_of(self.report_a).get("full", 0) >= 1

        self.assertTrue(pump(seconds=10.0, until=answered), "the late client never got the request")
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)


class PrefixCostTests(unittest.TestCase):
    """A9 numbers: what one prefix clone evaluation costs per pass."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()

    def _grid(self, nx, ny):
        mesh = bpy.data.meshes.new("grid")
        xs = np.linspace(0.0, 10.0, nx, dtype=np.float32)
        ys = np.linspace(0.0, 10.0, ny, dtype=np.float32)
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        verts = np.stack(
            [gx.reshape(-1), gy.reshape(-1), np.zeros(nx * ny, dtype=np.float32)], axis=1
        )
        faces = []
        for i in range(nx - 1):
            for j in range(ny - 1):
                a = i * ny + j
                b = a + 1
                c = a + ny
                d = c + 1
                faces.append((a, c, d, b))
        mesh.from_pydata(verts.tolist(), [], faces)
        mesh.update()
        return mesh

    def _measure(self, nx, ny, budget_ms):
        import gh_stack.blender.engine as engine
        import gh_stack.blender.stack as stack_mod

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete()
        obj = bpy.data.objects.new("perf", self._grid(nx, ny))
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)

        # A cheap native modifier before the stage forces the clone path.
        mod = obj.modifiers.new("Triangulate", "TRIANGULATE")
        obj.modifiers.active = mod
        bpy.ops.gh_stack.add_stage(label="Perf")
        stage = obj.gh_stack.stages[0]
        record = stack_mod.stack_of(obj)

        t0 = time.perf_counter()
        sections = engine._prefix_sections(obj, record, stage)
        took_ms = (time.perf_counter() - t0) * 1000.0
        tris = (nx - 1) * (ny - 1) * 2
        print(
            "[prefix] %6d tris: prefix eval %.1f ms, %d sections"
            % (tris, took_ms, len(sections))
        )
        self.assertLess(took_ms, budget_ms)
        self.assertTrue(any(name.startswith("mesh.") for name, _, _ in sections))

    def test_10k_triangles(self):
        self._measure(72, 72, 1000)

    def test_100k_triangles(self):
        self._measure(225, 225, 4000)


if __name__ == "__main__":
    unittest.main()
