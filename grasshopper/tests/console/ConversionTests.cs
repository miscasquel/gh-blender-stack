// Layer C, conversion-boundary half: decoded sections must be validated
// before any vertex, face or curve is indexed or allocated.
//
//     StageloomConversionTests.exe
//
// The wine console harness cannot load RhinoCommon's native geometry library
// (RhinoCore's DllMain fails outside Rhino), so this suite exercises the
// section boundary directly: MeshFromSections/CurvesFromSections reject every
// malformed field as FrameException("bad_frame") before touching Rhino
// geometry, and the validated wire data round-trips through the same
// MeshDataSections/CurveDataSections helpers the encoders use. When the
// process can construct Rhino geometry (a Windows console with RhinoCore),
// the same suite also runs the real Mesh/Curve round trip.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using Stageloom.Core;
using Rhino.Geometry;

namespace Stageloom.Tests.Console
{
    public static class ConversionTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            System.Console.WriteLine("conversion boundaries");

            CheckValidMeshData();
            CheckMeshRoundTrip();
            CheckMeshGroupSelection();
            CheckMalformedMeshSections();
            CheckValidCurveData();
            CheckCurveRoundTrip();
            CheckMalformedCurveSections();
            CheckNullCurveWarning();

            if (CanBuildRhinoGeometry())
            {
                CheckNativeMeshRoundTrip();
                CheckNativeNgonFallback();
                CheckNativeCurveRoundTrip();
                CheckNativeUnrepresentableCurve();
            }
            else
            {
                System.Console.WriteLine("SKIP native Rhino geometry round trip: RhinoCore is unavailable in this harness");
            }

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: conversion boundaries PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: conversion boundaries FAIL (" + _failures + ")");
            return 1;
        }

        private static void Check(string what, bool ok, string detail)
        {
            if (ok)
            {
                System.Console.WriteLine("PASS " + what);
                return;
            }
            _failures++;
            System.Console.WriteLine("FAIL " + what + ": " + detail);
        }

        private static void ExpectBadFrame(string what, Action action)
        {
            try
            {
                action();
            }
            catch (FrameException ex)
            {
                Check(what + " -> bad_frame", ex.Code == "bad_frame",
                    "code " + ex.Code + ": " + ex.Message);
                return;
            }
            catch (Exception ex)
            {
                Check(what + " -> bad_frame", false,
                    "unclassified " + ex.GetType().Name + ": " + ex.Message);
                return;
            }
            Check(what + " -> bad_frame", false, "no exception");
        }

        // --------------------------------------------------------------- meshes

        private static void CheckValidMeshData()
        {
            Conversion.MeshData triangle = ReadMesh(MeshSections(
                new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 },
                new uint[] { 3 }, new uint[] { 0, 1, 2 }));
            Check("mesh: triangle decodes three vertices and one face",
                triangle.Positions.Length == 9 && triangle.FaceSizes.Length == 1
                && triangle.FaceSizes[0] == 3
                && Same(triangle.FaceIndices, new int[] { 0, 1, 2 }),
                Describe(triangle));

            Conversion.MeshData quad = ReadMesh(MeshSections(
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0 },
                new uint[] { 4 }, new uint[] { 0, 1, 2, 3 }));
            Check("mesh: quad decodes one four-vertex face",
                quad.FaceSizes.Length == 1 && quad.FaceSizes[0] == 4
                && Same(quad.FaceIndices, new int[] { 0, 1, 2, 3 }), Describe(quad));

            Conversion.MeshData ngon = ReadMesh(MeshSections(
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, -0.5f, 0.5f, 0 },
                new uint[] { 5 }, new uint[] { 0, 1, 2, 3, 4 }));
            Check("mesh: n-gon keeps its five-vertex boundary",
                ngon.FaceSizes.Length == 1 && ngon.FaceSizes[0] == 5
                && Same(ngon.FaceIndices, new int[] { 0, 1, 2, 3, 4 }), Describe(ngon));

            Conversion.MeshData mixed = ReadMesh(MeshSections(
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, -0.5f, 0.5f, 0 },
                new uint[] { 3, 4, 5 },
                new uint[] { 0, 1, 2, 0, 2, 3, 4, 0, 1, 2, 3, 4 }));
            Check("mesh: tri, quad and n-gon keep their order",
                mixed.FaceSizes.Length == 3 && mixed.FaceSizes[0] == 3
                && mixed.FaceSizes[1] == 4 && mixed.FaceSizes[2] == 5
                && mixed.FaceIndices.Length == 12, Describe(mixed));

            Conversion.MeshData empty = ReadMesh(MeshSections(
                new float[0], new uint[0], new uint[0]));
            Check("mesh: an empty mesh is valid",
                empty.Positions.Length == 0 && empty.FaceSizes.Length == 0
                && empty.FaceIndices.Length == 0, Describe(empty));

            var colors = new byte[] { 255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255 };
            List<Section> withColors = MeshSections(
                new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 },
                new uint[] { 3 }, new uint[] { 0, 1, 2 });
            withColors.Add(Section.Bytes("mesh.colors", colors));
            Conversion.MeshData colored = ReadMesh(
                DecodedFrame.FromSections(new Dictionary<string, object>(), withColors));
            Check("mesh: colours keep one RGBA entry per vertex",
                colored.Colors != null && colored.Colors.Length == 12
                && colored.Colors[0] == 255 && colored.Colors[5] == 255,
                colored.Colors == null ? "null" : colored.Colors.Length + " bytes");

            Check("mesh: no sections means no mesh",
                Conversion.MeshFromSections(DecodedFrame.FromSections(
                    new Dictionary<string, object>(), new List<Section>()), 1.0) == null,
                "returned a mesh");
        }

        private static void CheckMeshRoundTrip()
        {
            var cases = new List<object[]>();
            cases.Add(new object[] { "triangle",
                new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 },
                new uint[] { 3 }, new uint[] { 0, 1, 2 }, null });
            cases.Add(new object[] { "quad",
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0 },
                new uint[] { 4 }, new uint[] { 0, 1, 2, 3 }, null });
            cases.Add(new object[] { "n-gon",
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, -0.5f, 0.5f, 0 },
                new uint[] { 5 }, new uint[] { 0, 1, 2, 3, 4 }, null });
            cases.Add(new object[] { "coloured quad",
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0 },
                new uint[] { 4 }, new uint[] { 0, 1, 2, 3 },
                new byte[] { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 } });

            foreach (object[] c in cases)
            {
                List<Section> sections = MeshSections((float[])c[1], (uint[])c[2], (uint[])c[3]);
                if (c[4] != null) sections.Add(Section.Bytes("mesh.colors", (byte[])c[4]));
                DecodedFrame frame = Frame(sections);
                Conversion.MeshData data = ReadMesh(frame);
                string diff = SectionDiff(sections, Conversion.MeshDataSections(data, "mesh"));
                Check("mesh round trip: " + (string)c[0] + " is byte-identical",
                    diff == null, diff ?? "");
            }
        }

        private static void CheckMeshGroupSelection()
        {
            List<Section> mesh = MeshSections(
                new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 },
                new uint[] { 3 }, new uint[] { 0, 1, 2 });
            List<Section> subd = SubdSections(
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0 },
                new uint[] { 4 }, new uint[] { 0, 1, 2, 3 });

            Conversion.MeshGroup onlyMesh = Conversion.SelectMeshGroup(Frame(mesh), "");
            Check("mesh groups: mesh-only selects mesh",
                onlyMesh != null && onlyMesh.Kind == "mesh"
                && onlyMesh.Verts.Name == "mesh.verts", Kind(onlyMesh));

            Conversion.MeshGroup onlySubd = Conversion.SelectMeshGroup(Frame(subd), "");
            Check("mesh groups: subd-only selects subd",
                onlySubd != null && onlySubd.Kind == "subd"
                && onlySubd.Verts.Name == "subd.verts" && onlySubd.Colors == null,
                Kind(onlySubd));

            var both = new List<Section>(mesh);
            both.AddRange(subd);
            Conversion.MeshGroup chosen = Conversion.SelectMeshGroup(Frame(both), "");
            Check("mesh groups: both complete chooses the mesh group",
                chosen != null && chosen.Kind == "mesh" && chosen.Verts.Name == "mesh.verts",
                Kind(chosen));

            List<Section> prefixed = Conversion.PrefixSections(mesh, "idef.0.");
            Conversion.MeshGroup definition = Conversion.SelectMeshGroup(Frame(prefixed), "idef.0.");
            Check("mesh groups: definition prefixes remain addressable",
                definition != null && definition.Kind == "mesh"
                && definition.Verts.Name == "idef.0.mesh.verts", Kind(definition));

            Conversion.MeshGroup subdGroup = Conversion.SelectSubdGroup(Frame(subd));
            Check("mesh groups: the SubD cage is never taken from mesh.*",
                subdGroup != null && subdGroup.Kind == "subd"
                && subdGroup.Verts.Name == "subd.verts", Kind(subdGroup));

            ExpectBadFrame("mesh groups: verts alone is partial", delegate
            {
                Conversion.SelectMeshGroup(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", new float[] { 0, 0, 0 }),
                }), "");
            });
            ExpectBadFrame("mesh groups: verts and counts are partial", delegate
            {
                Conversion.SelectMeshGroup(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", new float[] { 0, 0, 0 }),
                    Section.U32("mesh.face_counts", new uint[] { 3 }),
                }), "");
            });
            ExpectBadFrame("mesh groups: counts and indices without verts are partial", delegate
            {
                Conversion.SelectMeshGroup(Frame(new List<Section>
                {
                    Section.U32("mesh.face_counts", new uint[] { 3 }),
                    Section.U32("mesh.face_indices", new uint[] { 0, 1, 2 }),
                }), "");
            });
            ExpectBadFrame("mesh groups: colours alone are partial", delegate
            {
                Conversion.SelectMeshGroup(Frame(new List<Section>
                {
                    Section.Bytes("mesh.colors", new byte[] { 1, 2, 3, 4 }),
                }), "");
            });
            ExpectBadFrame("mesh groups: partial mesh is not completed from subd", delegate
            {
                var sections = new List<Section>
                {
                    Section.Floats("mesh.verts", new float[] { 0, 0, 0 }),
                };
                sections.AddRange(subd);
                Conversion.SelectMeshGroup(Frame(sections), "");
            });
            ExpectBadFrame("mesh groups: partial subd is rejected beside a complete mesh", delegate
            {
                var sections = new List<Section>(mesh);
                sections.Add(Section.Floats("subd.verts", new float[] { 0, 0, 0 }));
                Conversion.SelectMeshGroup(Frame(sections), "");
            });
            ExpectBadFrame("mesh groups: partial subd cage is rejected", delegate
            {
                Conversion.SelectSubdGroup(Frame(new List<Section>
                {
                    Section.Floats("subd.verts", new float[] { 0, 0, 0 }),
                }));
            });
        }

        private static void CheckMalformedMeshSections()
        {
            float[] triangle = { 0, 0, 0, 1, 0, 0, 0, 1, 0 };

            ExpectBadFrame("mesh: vertex floats must be xyz triples", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    new float[] { 0, 0, 0, 1 }, new uint[0], new uint[0])), 1.0);
            });
            ExpectBadFrame("mesh: vertex bytes must be whole f32 values", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    new Section("mesh.verts", "f32", new byte[6]),
                    Section.U32("mesh.face_counts", new uint[0]),
                    Section.U32("mesh.face_indices", new uint[0]),
                }), 1.0);
            });
            ExpectBadFrame("mesh: verts dtype must be f32", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    new Section("mesh.verts", "i32", Section.Floats("x", triangle).Raw),
                    Section.U32("mesh.face_counts", new uint[] { 3 }),
                    Section.U32("mesh.face_indices", new uint[] { 0, 1, 2 }),
                }), 1.0);
            });
            ExpectBadFrame("mesh: counts dtype must be u32", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", triangle),
                    new Section("mesh.face_counts", "f32", Section.U32("x", new uint[] { 3 }).Raw),
                    Section.U32("mesh.face_indices", new uint[] { 0, 1, 2 }),
                }), 1.0);
            });
            ExpectBadFrame("mesh: indices dtype must be u32", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", triangle),
                    Section.U32("mesh.face_counts", new uint[] { 3 }),
                    new Section("mesh.face_indices", "u8", new byte[] { 0, 1, 2 }),
                }), 1.0);
            });
            ExpectBadFrame("mesh: counts bytes must be whole u32 values", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", triangle),
                    new Section("mesh.face_counts", "u32", new byte[3]),
                    Section.U32("mesh.face_indices", new uint[] { 0, 1, 2 }),
                }), 1.0);
            });
            ExpectBadFrame("mesh: indices bytes must be whole u32 values", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", triangle),
                    Section.U32("mesh.face_counts", new uint[] { 3 }),
                    new Section("mesh.face_indices", "u32", new byte[6]),
                }), 1.0);
            });
            ExpectBadFrame("mesh: counts sum must match the index list (short)", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 3 }, new uint[] { 0, 1 })), 1.0);
            });
            ExpectBadFrame("mesh: counts sum must match the index list (long)", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 3 }, new uint[] { 0, 1, 2, 0 })), 1.0);
            });
            ExpectBadFrame("mesh: zero-vertex faces are rejected", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 0 }, new uint[0])), 1.0);
            });
            ExpectBadFrame("mesh: one-vertex faces are rejected", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 1 }, new uint[] { 0 })), 1.0);
            });
            ExpectBadFrame("mesh: two-vertex faces are rejected", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 2 }, new uint[] { 0, 1 })), 1.0);
            });
            ExpectBadFrame("mesh: an index past the vertex list is rejected", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 3 }, new uint[] { 0, 1, 3 })), 1.0);
            });
            ExpectBadFrame("mesh: a face that repeats a vertex is rejected", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { 3 }, new uint[] { 0, 0, 1 })), 1.0);
            });
            ExpectBadFrame("mesh: a count above int.MaxValue is rejected", delegate
            {
                Conversion.MeshFromSections(Frame(MeshSections(
                    triangle, new uint[] { uint.MaxValue }, new uint[0])), 1.0);
            });
            ExpectBadFrame("mesh: colour bytes must be whole RGBA entries", delegate
            {
                var sections = MeshSections(triangle, new uint[] { 3 }, new uint[] { 0, 1, 2 });
                sections.Add(new Section("mesh.colors", "u8", new byte[10]));
                Conversion.MeshFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("mesh: colour count must match the vertex list", delegate
            {
                var sections = MeshSections(triangle, new uint[] { 3 }, new uint[] { 0, 1, 2 });
                sections.Add(Section.Bytes("mesh.colors", new byte[8]));
                Conversion.MeshFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("mesh: colours dtype must be u8", delegate
            {
                var sections = MeshSections(triangle, new uint[] { 3 }, new uint[] { 0, 1, 2 });
                sections.Add(new Section("mesh.colors", "f32", new byte[12]));
                Conversion.MeshFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("mesh: verts-only group is bad_frame through MeshFromSections", delegate
            {
                Conversion.MeshFromSections(Frame(new List<Section>
                {
                    Section.Floats("mesh.verts", triangle),
                }), 1.0);
            });
        }

        // --------------------------------------------------------------- curves

        private static void CheckValidCurveData()
        {
            List<Section> sections = ThreeCurves();
            Conversion.CurveData data = Conversion.ReadCurveData(Frame(sections), "");
            Check("curves: valid poly, NURBS and Bezier counts",
                data != null && data.Counts.Length == 3
                && data.Counts[0] == 3 && data.Counts[1] == 4 && data.Counts[2] == 4,
                data == null ? "null" : string.Join(",", data.Counts));
            Check("curves: types and knot modes keep their wire values",
                data != null && data.Types[0] == 0 && data.Types[1] == 2 && data.Types[2] == 2
                && data.Modes[0] == 0 && data.Modes[1] == 1 && data.Modes[2] == 2,
                CurveDescription(data));
            Check("curves: NURBS order and cyclic flag survive",
                data != null && data.Orders[1] == 3 && data.Cyclic[0] == 0
                && data.Cyclic[1] == 0 && data.Cyclic[2] == 0,
                CurveDescription(data));
            Check("curves: a positive rational weight is accepted",
                data != null && Math.Abs(data.Points[4 * 4 + 3] - 0.5f) < 1e-6f,
                data == null ? "null" : data.Points[4 * 4 + 3].ToString());

            List<Section> prefixed = Conversion.PrefixSections(sections, "idef.1.");
            Conversion.CurveData definition = Conversion.ReadCurveData(Frame(prefixed), "idef.1.");
            Check("curves: definition prefixes remain addressable",
                definition != null && definition.Counts.Length == 3
                && definition.Points.Length == 44,
                definition == null ? "null" : definition.Points.Length.ToString());

            List<Section> empty = new List<Section>();
            empty.Add(Section.Floats("crv.points", new float[0]));
            empty.Add(Section.U32("crv.counts", new uint[0]));
            empty.Add(Section.Bytes("crv.types", new byte[0]));
            empty.Add(Section.Bytes("crv.orders", new byte[0]));
            empty.Add(Section.Bytes("crv.knots_mode", new byte[0]));
            empty.Add(Section.Bytes("crv.cyclic", new byte[0]));
            Conversion.CurveData emptyData = Conversion.ReadCurveData(Frame(empty), "");
            Check("curves: an empty curve group is valid",
                emptyData != null && emptyData.Counts.Length == 0
                && Conversion.CurveDataSections(emptyData).Count == 0,
                emptyData == null ? "null" : emptyData.Counts.Length.ToString());

            Check("curves: no sections means no curves",
                Conversion.CurvesFromSections(Frame(new List<Section>()), 1.0).Count == 0,
                "returned curves");
        }

        private static void CheckCurveRoundTrip()
        {
            List<Section> sections = ThreeCurves();
            Conversion.CurveData data = Conversion.ReadCurveData(Frame(sections), "");
            string diff = SectionDiff(sections, Conversion.CurveDataSections(data));
            Check("curves: valid sections round-trip byte-identically", diff == null, diff ?? "");
        }

        private static void CheckMalformedCurveSections()
        {
            float[] threePoints = Points(3);

            ExpectBadFrame("curves: point floats must be xyzw tuples", delegate
            {
                var sections = CurveSections(
                    new float[] { 0, 0, 0, 1, 1, 0, 0, 1, 0, 1 },
                    new uint[] { 2 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 });
                Conversion.CurvesFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("curves: point bytes must be whole f32 values", delegate
            {
                var sections = new List<Section>
                {
                    new Section("crv.points", "f32", new byte[6]),
                    Section.U32("crv.counts", new uint[] { 2 }),
                    Section.Bytes("crv.types", new byte[] { 0 }),
                    Section.Bytes("crv.orders", new byte[] { 0 }),
                    Section.Bytes("crv.knots_mode", new byte[] { 0 }),
                    Section.Bytes("crv.cyclic", new byte[] { 0 }),
                };
                Conversion.CurvesFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("curves: points dtype must be f32", delegate
            {
                var sections = new List<Section>
                {
                    new Section("crv.points", "i32", Section.Floats("x", threePoints).Raw),
                    Section.U32("crv.counts", new uint[] { 3 }),
                    Section.Bytes("crv.types", new byte[] { 0 }),
                    Section.Bytes("crv.orders", new byte[] { 0 }),
                    Section.Bytes("crv.knots_mode", new byte[] { 0 }),
                    Section.Bytes("crv.cyclic", new byte[] { 0 }),
                };
                Conversion.CurvesFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("curves: counts dtype must be u32", delegate
            {
                var sections = new List<Section>
                {
                    Section.Floats("crv.points", threePoints),
                    new Section("crv.counts", "f32", Section.U32("x", new uint[] { 3 }).Raw),
                    Section.Bytes("crv.types", new byte[] { 0 }),
                    Section.Bytes("crv.orders", new byte[] { 0 }),
                    Section.Bytes("crv.knots_mode", new byte[] { 0 }),
                    Section.Bytes("crv.cyclic", new byte[] { 0 }),
                };
                Conversion.CurvesFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("curves: counts sum must match the point list", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 2 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a one-point curve is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    new float[] { 0, 0, 0, 1 }, new uint[] { 1 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a zero-point curve is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    new float[0], new uint[] { 0 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });

            ExpectBadFrame("curves: a short types array is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 0, 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a short orders array is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 0 }, new byte[0],
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a short knots_mode array is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[0], new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a long cyclic array is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0, 0 })), 1.0);
            });
            ExpectBadFrame("curves: a missing per-curve array is rejected", delegate
            {
                var sections = new List<Section>
                {
                    Section.Floats("crv.points", threePoints),
                    Section.U32("crv.counts", new uint[] { 3 }),
                    Section.Bytes("crv.orders", new byte[] { 0 }),
                    Section.Bytes("crv.knots_mode", new byte[] { 0 }),
                    Section.Bytes("crv.cyclic", new byte[] { 0 }),
                };
                Conversion.CurvesFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("curves: a bad types dtype is rejected", delegate
            {
                var sections = new List<Section>
                {
                    Section.Floats("crv.points", threePoints),
                    Section.U32("crv.counts", new uint[] { 3 }),
                    new Section("crv.types", "u32", Section.U32("x", new uint[] { 0 }).Raw),
                    Section.Bytes("crv.orders", new byte[] { 0 }),
                    Section.Bytes("crv.knots_mode", new byte[] { 0 }),
                    Section.Bytes("crv.cyclic", new byte[] { 0 }),
                };
                Conversion.CurvesFromSections(Frame(sections), 1.0);
            });
            ExpectBadFrame("curves: points without counts is a partial group", delegate
            {
                Conversion.CurvesFromSections(Frame(new List<Section>
                {
                    Section.Floats("crv.points", threePoints),
                }), 1.0);
            });
            ExpectBadFrame("curves: counts without points is a partial group", delegate
            {
                Conversion.CurvesFromSections(Frame(new List<Section>
                {
                    Section.U32("crv.counts", new uint[] { 3 }),
                }), 1.0);
            });
            ExpectBadFrame("curves: an unknown type is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 3 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: an unknown knot mode is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 2 }, new byte[] { 3 },
                    new byte[] { 3 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a cyclic flag above one is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 2 })), 1.0);
            });
            ExpectBadFrame("curves: a NURBS order below two is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 2 }, new byte[] { 1 },
                    new byte[] { 1 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a NURBS order above six is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 2 }, new byte[] { 7 },
                    new byte[] { 1 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a NURBS order above its control-point count is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 2 }, new byte[] { 4 },
                    new byte[] { 1 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a malformed Bezier net is rejected", delegate
            {
                Conversion.CurvesFromSections(Frame(CurveSections(
                    threePoints, new uint[] { 3 }, new byte[] { 2 }, new byte[] { 4 },
                    new byte[] { 2 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a zero weight is rejected", delegate
            {
                float[] points = Points(3);
                points[1 * 4 + 3] = 0f;
                Conversion.CurvesFromSections(Frame(CurveSections(
                    points, new uint[] { 3 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a negative weight is rejected", delegate
            {
                float[] points = Points(3);
                points[2 * 4 + 3] = -1f;
                Conversion.CurvesFromSections(Frame(CurveSections(
                    points, new uint[] { 3 }, new byte[] { 2 }, new byte[] { 3 },
                    new byte[] { 1 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: a non-finite weight is rejected", delegate
            {
                float[] points = Points(3);
                points[1 * 4 + 3] = float.NaN;
                Conversion.CurvesFromSections(Frame(CurveSections(
                    points, new uint[] { 3 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
            ExpectBadFrame("curves: an infinite weight is rejected", delegate
            {
                float[] points = Points(3);
                points[2 * 4 + 3] = float.PositiveInfinity;
                Conversion.CurvesFromSections(Frame(CurveSections(
                    points, new uint[] { 3 }, new byte[] { 0 }, new byte[] { 0 },
                    new byte[] { 0 }, new byte[] { 0 })), 1.0);
            });
        }

        private static void CheckNullCurveWarning()
        {
            var warnings = new List<WireWarning>();
            int approximated = -1;
            List<Section> sections = Conversion.SectionsFromCurves(
                new Curve[] { null }, 1.0, warnings, out approximated);

            string skipped = WarningMessage(warnings, WarningCodes.CurveSkipped);
            Check("curves: a null curve is reported as curve_skipped",
                skipped != null && skipped.IndexOf("null", StringComparison.Ordinal) >= 0,
                WarningText(warnings));
            Check("curves: a null curve is not reported as sampled",
                WarningMessage(warnings, WarningCodes.CurveSampled) == null,
                WarningText(warnings));
            Check("curves: a null curve emits no sections and no approximation",
                sections.Count == 0 && approximated == 0, "sections " + sections.Count
                + ", approximated " + approximated);

            warnings.Clear();
            sections = Conversion.SectionsFromCurves(
                new Curve[] { null, null }, 1.0, warnings, out approximated);
            skipped = WarningMessage(warnings, WarningCodes.CurveSkipped);
            Check("curves: every null curve is counted",
                skipped != null && skipped.IndexOf("2 null", StringComparison.Ordinal) >= 0,
                WarningText(warnings));
        }

        // ------------------------------------------------- optional native half

        private static bool CanBuildRhinoGeometry()
        {
            try
            {
                new Mesh();
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static void CheckNativeMeshRoundTrip()
        {
            var cases = new List<object[]>();
            cases.Add(new object[] { "triangle",
                new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 },
                new uint[] { 3 }, new uint[] { 0, 1, 2 } });
            cases.Add(new object[] { "quad",
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0 },
                new uint[] { 4 }, new uint[] { 0, 1, 2, 3 } });
            cases.Add(new object[] { "n-gon",
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, -0.5f, 0.5f, 0 },
                new uint[] { 5 }, new uint[] { 0, 1, 2, 3, 4 } });

            foreach (object[] c in cases)
            {
                List<Section> sections = MeshSections((float[])c[1], (uint[])c[2], (uint[])c[3]);
                Mesh mesh = Conversion.MeshFromSections(Frame(sections), 1.0);
                bool ok = mesh != null && mesh.Vertices.Count == ((float[])c[1]).Length / 3;
                if (ok && (string)c[0] == "n-gon")
                    ok = mesh.Ngons.Count == 1 && mesh.Faces.Count == 3;
                else if (ok)
                    ok = mesh.Faces.Count == 1;
                Check("native mesh: " + (string)c[0] + " builds the expected topology", ok,
                    mesh == null ? "null mesh" : mesh.Vertices.Count + " vertices, "
                    + mesh.Faces.Count + " faces, " + mesh.Ngons.Count + " n-gons");

                List<Section> encoded = Conversion.SectionsFromMesh(mesh, 1.0);
                Conversion.MeshData data = ReadMesh(encoded);
                Check("native mesh: " + (string)c[0] + " round-trips positions and faces",
                    sameFloats(data.Positions, (float[])c[1])
                    && Same(data.FaceIndices, ToInts((uint[])c[3])),
                    Describe(data));
            }

            Mesh empty = Conversion.MeshFromSections(Frame(MeshSections(
                new float[0], new uint[0], new uint[0])), 1.0);
            Check("native mesh: an empty mesh builds without geometry",
                empty != null && empty.Vertices.Count == 0 && empty.Faces.Count == 0,
                empty == null ? "null mesh" : empty.Vertices.Count + " vertices");
        }

        private static void CheckNativeNgonFallback()
        {
            // A square ring of four quads registered as one n-gon with an
            // inner boundary. The wire has no place for the hole, so the fan
            // faces must travel and the loss must be warned.
            var mesh = new Mesh();
            mesh.Vertices.Add(0, 0, 0);
            mesh.Vertices.Add(4, 0, 0);
            mesh.Vertices.Add(4, 4, 0);
            mesh.Vertices.Add(0, 4, 0);
            mesh.Vertices.Add(1, 1, 0);
            mesh.Vertices.Add(3, 1, 0);
            mesh.Vertices.Add(3, 3, 0);
            mesh.Vertices.Add(1, 3, 0);
            mesh.Faces.AddFace(0, 1, 5, 4);
            mesh.Faces.AddFace(1, 2, 6, 5);
            mesh.Faces.AddFace(2, 3, 7, 6);
            mesh.Faces.AddFace(3, 0, 4, 7);
            mesh.Ngons.AddNgon(MeshNgon.Create(new int[] { 0, 1, 2, 3 },
                new List<int> { 0, 1, 2, 3 }));

            var warnings = new List<WireWarning>();
            List<Section> sections = Conversion.SectionsFromMesh(mesh, 1.0, warnings);
            Conversion.MeshData data = ReadMesh(sections);
            Check("native mesh: an n-gon with a hole sends its four quads",
                data.FaceSizes.Length == 4 && data.FaceSizes[0] == 4
                && data.FaceSizes[1] == 4 && data.FaceSizes[2] == 4
                && data.FaceSizes[3] == 4 && data.FaceIndices.Length == 16,
                Describe(data));
            Check("native mesh: an n-gon with a hole warns ngon_holes",
                WarningMessage(warnings, WarningCodes.NgonHoles) != null,
                WarningText(warnings));

            ExpectBadFrame("native mesh: the no-warning overload rejects a holey n-gon", delegate
            {
                Conversion.SectionsFromMesh(mesh, 1.0);
            });
        }

        private static void CheckNativeCurveRoundTrip()
        {
            var polyline = new PolylineCurve(new Point3d[]
            {
                new Point3d(0, 0, 0), new Point3d(1, 0, 0), new Point3d(1, 1, 0),
            });
            var warnings = new List<WireWarning>();
            int approximated;
            List<Section> sections = Conversion.SectionsFromCurves(
                new Curve[] { polyline }, 1.0, warnings, out approximated);
            List<Curve> back = Conversion.CurvesFromSections(Frame(sections), 1.0);
            Polyline poly = new Polyline();
            bool isPolyline = back.Count == 1 && back[0].TryGetPolyline(out poly);
            Check("native curves: a polyline round-trips as a polyline",
                isPolyline && poly.Count == 3 && approximated == 0,
                back.Count + " curve(s), approximated " + approximated);

            var points = new List<Point3d>
            {
                new Point3d(0, 0, 0), new Point3d(1, 0, 0),
                new Point3d(1, 1, 0), new Point3d(2, 1, 0),
            };
            NurbsCurve nurbs = NurbsCurve.Create(false, 2, points);
            warnings.Clear();
            sections = Conversion.SectionsFromCurves(
                new Curve[] { nurbs }, 1.0, warnings, out approximated);
            back = Conversion.CurvesFromSections(Frame(sections), 1.0);
            NurbsCurve returned = back.Count == 1 ? back[0] as NurbsCurve : null;
            Check("native curves: a NURBS round-trips with its order and control points",
                returned != null && returned.Order == 3 && returned.Points.Count == 4,
                back.Count == 0 ? "no curve" : back[0].GetType().Name);
        }

        private static void CheckNativeUnrepresentableCurve()
        {
            // An empty PolyCurve has neither a polyline nor a NURBS form. It is
            // dropped, and the loss must be visible under curve_skipped, not
            // mislabelled as curve_sampled.
            var warnings = new List<WireWarning>();
            int approximated;
            List<Section> sections = Conversion.SectionsFromCurves(
                new Curve[] { new PolyCurve() }, 1.0, warnings, out approximated);
            Check("native curves: a curve with no representation reports curve_skipped",
                WarningMessage(warnings, WarningCodes.CurveSkipped) != null
                && WarningMessage(warnings, WarningCodes.CurveSampled) == null,
                WarningText(warnings));
            Check("native curves: a dropped curve emits no sections",
                sections.Count == 0 && approximated == 0,
                "sections " + sections.Count + ", approximated " + approximated);
        }

        // -------------------------------------------------------------- helpers

        private static List<Section> MeshSections(float[] positions, uint[] counts, uint[] indices)
        {
            var sections = new List<Section>();
            sections.Add(Section.Floats("mesh.verts", positions));
            sections.Add(Section.U32("mesh.face_counts", counts));
            sections.Add(Section.U32("mesh.face_indices", indices));
            return sections;
        }

        private static List<Section> SubdSections(float[] positions, uint[] counts, uint[] indices)
        {
            var sections = new List<Section>();
            sections.Add(Section.Floats("subd.verts", positions));
            sections.Add(Section.U32("subd.face_counts", counts));
            sections.Add(Section.U32("subd.face_indices", indices));
            return sections;
        }

        private static List<Section> ThreeCurves()
        {
            var points = new List<float>();
            AddPoint(points, 0, 0, 0, 1);
            AddPoint(points, 1, 0, 0, 1);
            AddPoint(points, 1, 1, 0, 1);
            AddPoint(points, 2, 0, 0, 1);
            AddPoint(points, 3, 0, 0, 0.5f);
            AddPoint(points, 3, 1, 0, 2);
            AddPoint(points, 2, 1, 0, 1);
            AddPoint(points, 4, 0, 0, 1);
            AddPoint(points, 5, 0, 0, 1);
            AddPoint(points, 6, 0, 0, 1);
            AddPoint(points, 7, 0, 0, 1);

            return CurveSections(points.ToArray(),
                new uint[] { 3, 4, 4 },
                new byte[] { 0, 2, 2 },
                new byte[] { 0, 3, 4 },
                new byte[] { 0, 1, 2 },
                new byte[] { 0, 0, 0 });
        }

        private static List<Section> CurveSections(float[] points, uint[] counts, byte[] types,
                                                   byte[] orders, byte[] modes, byte[] cyclic)
        {
            var sections = new List<Section>();
            sections.Add(Section.Floats("crv.points", points));
            sections.Add(Section.U32("crv.counts", counts));
            sections.Add(Section.Bytes("crv.types", types));
            sections.Add(Section.Bytes("crv.orders", orders));
            sections.Add(Section.Bytes("crv.knots_mode", modes));
            sections.Add(Section.Bytes("crv.cyclic", cyclic));
            return sections;
        }

        private static float[] Points(int count)
        {
            var points = new List<float>();
            for (int i = 0; i < count; i++) AddPoint(points, i, 0, 0, 1);
            return points.ToArray();
        }

        private static void AddPoint(List<float> points, float x, float y, float z, float w)
        {
            points.Add(x);
            points.Add(y);
            points.Add(z);
            points.Add(w);
        }

        private static DecodedFrame Frame(List<Section> sections)
        {
            return DecodedFrame.FromSections(new Dictionary<string, object>(), sections);
        }

        private static Conversion.MeshData ReadMesh(DecodedFrame frame)
        {
            Conversion.MeshGroup group = Conversion.SelectMeshGroup(frame, "");
            return Conversion.ReadMeshData(group);
        }

        private static Conversion.MeshData ReadMesh(List<Section> sections)
        {
            return ReadMesh(Frame(sections));
        }

        private static string Kind(Conversion.MeshGroup group)
        {
            return group == null ? "none" : group.Kind + " " + group.Verts.Name;
        }

        private static string Describe(Conversion.MeshData data)
        {
            return data.Positions.Length + " floats, sizes "
                + string.Join(",", Array.ConvertAll(data.FaceSizes, v => v.ToString()))
                + ", indices " + string.Join(",", Array.ConvertAll(data.FaceIndices, v => v.ToString()));
        }

        private static string CurveDescription(Conversion.CurveData data)
        {
            if (data == null) return "null";
            return "types " + string.Join(",", Array.ConvertAll(data.Types, v => v.ToString()))
                + " orders " + string.Join(",", Array.ConvertAll(data.Orders, v => v.ToString()))
                + " modes " + string.Join(",", Array.ConvertAll(data.Modes, v => v.ToString()));
        }

        private static string WarningMessage(List<WireWarning> warnings, string code)
        {
            foreach (WireWarning warning in warnings)
                if (warning.Code == code) return warning.Message;
            return null;
        }

        private static string WarningText(List<WireWarning> warnings)
        {
            var parts = new List<string>();
            foreach (WireWarning warning in warnings)
                parts.Add(warning.Code + ": " + warning.Message);
            return string.Join(" | ", parts.ToArray());
        }

        private static bool Same(int[] a, int[] b)
        {
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++)
                if (a[i] != b[i]) return false;
            return true;
        }

        private static bool sameFloats(float[] a, float[] b)
        {
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++)
                if (Math.Abs(a[i] - b[i]) > 1e-6f) return false;
            return true;
        }

        private static int[] ToInts(uint[] values)
        {
            var result = new int[values.Length];
            for (int i = 0; i < values.Length; i++) result[i] = (int)values[i];
            return result;
        }

        private static string SectionDiff(List<Section> expected, List<Section> actual)
        {
            if (expected.Count != actual.Count)
                return "expected " + expected.Count + " sections, got " + actual.Count;
            for (int i = 0; i < expected.Count; i++)
            {
                Section a = expected[i];
                Section b = actual[i];
                if (a.Name != b.Name) return "section " + i + " is " + b.Name + ", expected " + a.Name;
                if (a.Dtype != b.Dtype) return a.Name + " dtype " + b.Dtype + ", expected " + a.Dtype;
                if (a.Raw.Length != b.Raw.Length)
                    return a.Name + " length " + b.Raw.Length + ", expected " + a.Raw.Length;
                for (int k = 0; k < a.Raw.Length; k++)
                    if (a.Raw[k] != b.Raw[k]) return a.Name + " byte " + k;
            }
            return null;
        }
    }
}
