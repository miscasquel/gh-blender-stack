// Spike S6: curves, points, SubD, and Brep through the *product* Conversion.
//
// Runs inside Rhino (RhinoCommon), driven by spikes/s6/driver.py through the
// spike .gha (check=s6), like S4. Each check round-trips real geometry through
// the sections the wire carries and reports deviations, so the product code is
// verified against Rhino's own evaluation, not against itself.

using System;
using System.Collections.Generic;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Types;
using Rhino.Geometry;
using GhStack.Core;

namespace GhStack.Spike.S6
{
    public static class S6Checks
    {
        public static Dictionary<string, object> Run()
        {
            var r = new Dictionary<string, object>();
            try { Curves(r); } catch (Exception ex) { r["curves_error"] = Err(ex); }
            try { Points(r); } catch (Exception ex) { r["points_error"] = Err(ex); }
            try { Subd(r); } catch (Exception ex) { r["subd_error"] = Err(ex); }
            try { BrepCheck(r); } catch (Exception ex) { r["brep_error"] = Err(ex); }
            try { Mixed(r); } catch (Exception ex) { r["mixed_error"] = Err(ex); }
            try { Dispatch(r); } catch (Exception ex) { r["dispatch_error"] = Err(ex); }
            return r;
        }

        private static DecodedFrame Frame(List<Section> sections)
        {
            return new DecodedFrame(new Dictionary<string, object>(), sections);
        }

        private static string Err(Exception ex)
        {
            string m = ex.Message;
            int i = m.IndexOf('\n');
            if (i > 0) m = m.Substring(0, i);
            return ex.GetType().Name + ": " + m;
        }

        /// <summary>Largest distance from samples on one curve to another.</summary>
        private static double Deviation(Curve a, Curve b, int samples)
        {
            double max = 0.0;
            for (int i = 0; i <= samples; i++)
            {
                Point3d p = a.PointAt(a.Domain.ParameterAt(i / (double)samples));
                double t;
                if (b.ClosestPoint(p, out t))
                {
                    double d = b.PointAt(t).DistanceTo(p);
                    if (d > max) max = d;
                }
            }
            return max;
        }

        private static List<Point3d> SinePoints(int count)
        {
            var points = new List<Point3d>();
            for (int i = 0; i < count; i++)
                points.Add(new Point3d(i, Math.Sin(i * 0.7) * 2.0, 0.0));
            return points;
        }

        // ----------------------------------------------------------------- curves

        private static void Curves(Dictionary<string, object> r)
        {
            // Every curve below is exact now: the piecewise-Bezier spans carry
            // weights, and Blender holds a single span exactly. expectedApprox is
            // 0 and the spans travel with mode 1 (endpoint) for all of them.

            // 1. clamped uniform degree-3
            NurbsCurve uniform = NurbsCurve.Create(false, 3, SinePoints(8));
            RoundTrip(r, "uniform", uniform, 0, 1);

            // 1b. rational weights on clamped-uniform knots
            NurbsCurve weighted = uniform.DuplicateCurve() as NurbsCurve;
            weighted.Points.SetWeight(3, 2.5);
            RoundTrip(r, "weighted", weighted, 0, 1);

            // 1c. unclamped uniform knots
            NurbsCurve unclamped = uniform.DuplicateCurve() as NurbsCurve;
            for (int i = 0; i < unclamped.Knots.Count; i++) unclamped.Knots[i] = i;
            RoundTrip(r, "unclamped", unclamped, 0, 1);

            // 2. interior non-uniform knot
            NurbsCurve moved = uniform.DuplicateCurve() as NurbsCurve;
            moved.Knots[5] = moved.Knots[5] + 0.35;
            RoundTrip(r, "nonuniform", moved, 0, 1);

            // 3. circle: four rational quadratic spans, exact
            NurbsCurve circle = new Circle(Point3d.Origin, 5.0).ToNurbsCurve();
            RoundTrip(r, "circle", circle, 0, 1);

            // 3b. ellipse: same, different radii
            NurbsCurve ellipse = new Ellipse(Plane.WorldXY, 8.0, 3.0).ToNurbsCurve();
            RoundTrip(r, "ellipse", ellipse, 0, 1);

            // 3c. arcs: one span <=90 degrees; 120/270 degrees arrive as several
            // spans, each exact on its own
            var arcPts = new List<Point3d>();
            for (int i = 0; i < 3; i++)
                arcPts.Add(new Point3d(5.0 * Math.Cos(i * Math.PI / 4.0), 5.0 * Math.Sin(i * Math.PI / 4.0), 0));
            RoundTrip(r, "arc90", new Arc(arcPts[0], arcPts[1], arcPts[2]).ToNurbsCurve(), 0, 1);
            var arcPts120 = new List<Point3d>();
            for (int i = 0; i < 3; i++)
                arcPts120.Add(new Point3d(5.0 * Math.Cos(i * Math.PI / 3.0), 5.0 * Math.Sin(i * Math.PI / 3.0), 0));
            RoundTrip(r, "arc120", new Arc(arcPts120[0], arcPts120[1], arcPts120[2]).ToNurbsCurve(), 0, 1);
            RoundTrip(r, "arc270",
                new Arc(new Point3d(5, 0, 0), new Point3d(0, 5, 0), new Point3d(0, -5, 0)).ToNurbsCurve(),
                0, 1);

            // 4. polyline: exact, type 0
            var poly = new PolylineCurve(new List<Point3d> {
                new Point3d(0, 0, 0), new Point3d(1, 0, 0), new Point3d(1, 2, 0), new Point3d(3, 2, 0) });
            var curves = new List<Curve> { poly };
            var warnings = new List<string>();
            int approximated;
            List<Section> sections = Conversion.SectionsFromCurves(curves, 1.0, warnings, out approximated);
            var back = Conversion.CurvesFromSections(Frame(sections), 1.0);
            r["poly.approx"] = (long)approximated;
            r["poly.count"] = (long)back.Count;
            r["poly.deviation"] = back.Count == 1 ? Deviation(poly, back[0], 16) : -1.0;
            r["poly.type"] = sections.Count > 2 ? (long)sections[2].Raw[0] : -1;
        }

        private static void RoundTrip(Dictionary<string, object> r, string name, NurbsCurve original,
                                      int expectedApprox, int expectedMode)
        {
            var curves = new List<Curve> { original };
            var warnings = new List<string>();
            int approximated;
            List<Section> sections = Conversion.SectionsFromCurves(curves, 1.0, warnings, out approximated);
            var back = Conversion.CurvesFromSections(Frame(sections), 1.0);

            r[name + ".approx"] = (long)approximated;
            r[name + ".mode"] = sections.Count > 4 && sections[4].Raw.Length > 0 ? (long)sections[4].Raw[0] : -1;
            r[name + ".order"] = sections.Count > 3 && sections[3].Raw.Length > 0 ? (long)sections[3].Raw[0] : -1;
            r[name + ".cps"] = sections.Count > 1 ? (long)(sections[1].ToU32().Length > 0 ? sections[1].ToU32()[0] : 0) : -1;
            r[name + ".spans"] = sections.Count > 1 ? (long)sections[1].ToU32().Length : -1;
            r[name + ".back"] = (long)back.Count;
            r[name + ".deviation"] = DeviationToCurves(original, back, 32);
            r[name + ".warnings"] = warnings.Count > 0 ? warnings[0] : "(none)";
            if (approximated != expectedApprox) r[name + ".approx_UNEXPECTED"] = true;
            if (sections.Count > 4 && sections[4].Raw.Length > 0 && sections[4].Raw[0] != expectedMode)
                r[name + ".mode_UNEXPECTED"] = (long)sections[4].Raw[0];
        }

        /// <summary>Largest distance from samples of one curve to the nearest of
        /// several (a multi-span curve comes back as one wire curve per span).</summary>
        private static double DeviationToCurves(Curve original, List<Curve> curves, int samples)
        {
            if (curves == null || curves.Count == 0) return -1.0;
            double max = 0.0;
            for (int i = 0; i <= samples; i++)
            {
                Point3d p = original.PointAt(original.Domain.ParameterAt(i / (double)samples));
                double best = double.MaxValue;
                foreach (Curve c in curves)
                {
                    double t;
                    if (c.ClosestPoint(p, out t))
                    {
                        double d = c.PointAt(t).DistanceTo(p);
                        if (d < best) best = d;
                    }
                }
                if (best != double.MaxValue && best > max) max = best;
            }
            return max;
        }

        // ----------------------------------------------------------------- points

        private static void Points(Dictionary<string, object> r)
        {
            var points = new List<Point3d> {
                new Point3d(0, 0, 0), new Point3d(1, 2, 3), new Point3d(-1, 0.5, 2) };
            List<Section> sections = Conversion.SectionsFromPoints(points, 1.0);
            List<Point3d> back = Conversion.PointsFromSections(Frame(sections), 1.0);
            r["points.count"] = (long)back.Count;
            double max = 0.0;
            for (int i = 0; i < Math.Min(points.Count, back.Count); i++)
                max = Math.Max(max, points[i].DistanceTo(back[i]));
            r["points.deviation"] = max;
        }

        // ------------------------------------------------------------------- SubD

        private static Mesh GridCage(int nx, int ny)
        {
            var mesh = new Mesh();
            for (int i = 0; i < nx; i++)
                for (int j = 0; j < ny; j++)
                    mesh.Vertices.Add(i, j, 0);
            for (int i = 0; i < nx - 1; i++)
                for (int j = 0; j < ny - 1; j++)
                {
                    int v = i * ny + j;
                    mesh.Faces.AddFace(v, v + ny, v + ny + 1, v + 1);
                }
            mesh.Normals.ComputeNormals();
            mesh.Compact();
            return mesh;
        }

        private static long PairKey(int a, int b)
        {
            int lo = Math.Min(a, b);
            int hi = Math.Max(a, b);
            return ((long)lo << 32) | (uint)hi;
        }

        private static Dictionary<long, SubDEdge> EdgeMap(SubD subd)
        {
            var map = new Dictionary<long, SubDEdge>();
            foreach (SubDEdge e in subd.Edges)
            {
                int a = e.VertexFrom.ComponentIndex().Index;
                int b = e.VertexTo.ComponentIndex().Index;
                long key = PairKey(a, b);
                if (!map.ContainsKey(key)) map[key] = e;
            }
            return map;
        }

        private static void Subd(Dictionary<string, object> r)
        {
            Mesh cage = GridCage(4, 4);
            SubD subd = SubD.CreateFromMesh(cage);
            r["subd.cage_verts"] = (long)cage.Vertices.Count;

            // Crease one interior edge (find one not on the boundary).
            int targetA = -1, targetB = -1;
            foreach (SubDEdge e in subd.Edges)
            {
                int a = e.VertexFrom.ComponentIndex().Index;
                int b = e.VertexTo.ComponentIndex().Index;
                if (a > 0 && a < cage.Vertices.Count - 1 && b > 0 && b < cage.Vertices.Count - 1
                    && a != b)
                {
                    // interior if both endpoints are not on the cage boundary
                    bool interiorA = (a % 4) != 0 && (a % 4) != 3 && a >= 4 && a < 12;
                    bool interiorB = (b % 4) != 0 && (b % 4) != 3 && b >= 4 && b < 12;
                    if (interiorA && interiorB)
                    {
                        targetA = a; targetB = b;
                        e.Tag = SubDEdgeTag.Crease;
                        break;
                    }
                }
            }
            r["subd.creased_pair"] = targetA + "-" + targetB;

            var warnings = new List<string>();
            List<Section> sections = Conversion.SectionsFromSubD(subd, 1.0, warnings);
            r["subd.sections"] = SectionNames(sections);
            r["subd.warnings"] = warnings.Count > 0 ? warnings[0] : "(none)";

            var backWarnings = new List<string>();
            SubD back = Conversion.SubDFromSections(Frame(sections), 1.0, backWarnings);
            r["subd.back"] = back != null;
            if (back == null) return;
            r["subd.back_verts"] = (long)back.Vertices.Count;
            r["subd.back_faces"] = (long)back.Faces.Count;

            var map = EdgeMap(back);
            SubDEdge target;
            if (map.TryGetValue(PairKey(targetA, targetB), out target))
                r["subd.crease_survived"] = target.Tag.ToString();
            else
                r["subd.crease_survived"] = "(edge not found)";

            // Fractional values round to the nearest tag, with a warning.
            var fractional = new List<Section>();
            foreach (Section s in sections)
            {
                if (s.Name == "subd.crease_values")
                {
                    float[] vals = s.ToFloats();
                    for (int i = 0; i < vals.Length; i++) vals[i] = 0.6f;
                    fractional.Add(Section.Floats(s.Name, vals));
                }
                else
                {
                    fractional.Add(s);
                }
            }
            var roundWarnings = new List<string>();
            Conversion.SubDFromSections(Frame(fractional), 1.0, roundWarnings);
            r["subd.fractional_warning"] = roundWarnings.Count > 0 ? roundWarnings[0] : "(none)";

            // The control net must be the cage that came in. Vertex order is not
            // guaranteed across SubD round trips, so compare as point sets and
            // check that every point has a match.
            Mesh control = Mesh.CreateFromSubDControlNet(back);
            r["subd.control_verts"] = (long)control.Vertices.Count;
            int unmatched = 0;
            double maxDist = 0.0;
            foreach (Point3f p in control.Vertices)
            {
                double best = double.MaxValue;
                for (int i = 0; i < cage.Vertices.Count; i++)
                {
                    double d = cage.Vertices[i].DistanceTo(new Point3f(p.X, p.Y, p.Z));
                    if (d < best) best = d;
                }
                if (best > 1e-6) unmatched++;
                if (best > maxDist) maxDist = best;
            }
            r["subd.cage_round_trip"] = unmatched == 0;
            r["subd.cage_unmatched"] = (long)unmatched;
            r["subd.cage_max_distance"] = maxDist;
        }

        private static string SectionNames(List<Section> sections)
        {
            var names = new List<string>();
            foreach (Section s in sections) names.Add(s.Name);
            return string.Join(", ", names.ToArray());
        }

        // ------------------------------------------------------------------ Brep

        private static void BrepCheck(Dictionary<string, object> r)
        {
            Brep sphere = new Sphere(Point3d.Origin, 10.0).ToBrep();
            var warnings = new List<string>();
            Dictionary<string, object> counts;
            List<Section> sections = Conversion.SectionsFromGeometry(
                new List<Mesh>(), new List<Curve>(), new List<Point3d>(),
                new List<SubD>(), new List<Brep> { sphere },
                1.0, warnings, out counts);

            r["brep.sections"] = SectionNames(sections);
            r["brep.counts.meshes"] = counts["meshes"];
            r["brep.warning"] = warnings.Count > 0 ? warnings[0] : "(none)";
            Mesh mesh = Conversion.MeshFromSections(Frame(sections), 1.0);
            r["brep.mesh_verts"] = mesh == null ? -1L : (long)mesh.Vertices.Count;
        }

        // ----------------------------------------------------------------- mixed

        private static void Mixed(Dictionary<string, object> r)
        {
            var mesh = new Mesh();
            mesh.Vertices.Add(0, 0, 0); mesh.Vertices.Add(1, 0, 0); mesh.Vertices.Add(0, 1, 0);
            mesh.Faces.AddFace(0, 1, 2);
            var curve = NurbsCurve.Create(false, 3, SinePoints(5));
            var points = new List<Point3d> { new Point3d(1, 1, 1), new Point3d(2, 2, 2) };
            var warnings = new List<string>();
            Dictionary<string, object> counts;
            List<Section> sections = Conversion.SectionsFromGeometry(
                new List<Mesh> { mesh }, new List<Curve> { curve }, points,
                new List<SubD>(), new List<Brep>(), 1.0, warnings, out counts);

            r["mixed.sections"] = SectionNames(sections);
            r["mixed.counts"] = "meshes=" + counts["meshes"] + " curves=" + counts["curves"]
                + " points=" + counts["points"] + " approximated=" + counts["approximated_curves"];
            r["mixed.mesh_verts"] = (long)Conversion.MeshFromSections(Frame(sections), 1.0).Vertices.Count;
            r["mixed.curves"] = (long)Conversion.CurvesFromSections(Frame(sections), 1.0).Count;
            r["mixed.points"] = (long)Conversion.PointsFromSections(Frame(sections), 1.0).Count;
        }

        // -------------------------------------------------------------- dispatch

        private static void Dispatch(Dictionary<string, object> r)
        {
            var goos = new List<IGH_Goo>();
            goos.Add(new GH_Circle(new Circle(Point3d.Origin, 5.0)));
            goos.Add(new GH_Arc(new Arc(new Point3d(0, 0, 0), new Point3d(2, 2, 0), new Point3d(4, 0, 0))));
            goos.Add(new GH_Line(new Line(new Point3d(0, 0, 0), new Point3d(1, 1, 1))));
            goos.Add(new GH_Point(new Point3d(1, 1, 1)));
            var mesh = new Mesh();
            mesh.Vertices.Add(0, 0, 0);
            mesh.Vertices.Add(1, 0, 0);
            mesh.Vertices.Add(0, 1, 0);
            mesh.Faces.AddFace(0, 1, 2);
            goos.Add(new GH_Mesh(mesh));
            goos.Add(new GH_Integer(7)); // not geometry: must be ignored

            var meshes = new List<Mesh>();
            var curves = new List<Curve>();
            var points = new List<Point3d>();
            var subds = new List<SubD>();
            var breps = new List<Brep>();
            var ignored = new List<string>();
            GooDispatch.Collect(goos, meshes, curves, points, subds, breps, ignored);

            r["dispatch.curves"] = (long)curves.Count;
            r["dispatch.points"] = (long)points.Count;
            r["dispatch.meshes"] = (long)meshes.Count;
            r["dispatch.ignored"] = string.Join(", ", ignored.ToArray());
        }
    }
}
