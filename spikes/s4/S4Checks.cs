// Spike S4: SubD tags and the curve approximation paths (plan §15, D2/D3).
//
// Questions:
//   a. Can SubDEdge.Tag be set? (If not, Blender -> GH drops creases and warns.)
//   b. Does SubD.CreateFromMesh produce a usable SubD from a cage?
//   c. Do the curve approximation paths behave: MakePiecewiseBezier and the
//      deviation between the original and the approximation?
//
// Runs inside Rhino (RhinoCommon), so it is exercised through the spike .gha by
// spikes/s4/driver.py. Results are plain JSON-able values.

using System;
using System.Collections.Generic;
using Rhino.Geometry;

namespace GhStack.Spike.S4
{
    public static class S4Checks
    {
        public static Dictionary<string, object> Run()
        {
            var r = new Dictionary<string, object>();
            CheckSubd(r);
            CheckCurves(r);
            return r;
        }

        // ------------------------------------------------------------------ SubD

        private static void CheckSubd(Dictionary<string, object> r)
        {
            try
            {
                Mesh cage = GridCage(4, 4);
                r["cage_verts"] = (long)cage.Vertices.Count;
                r["cage_faces"] = (long)cage.Faces.Count;

                SubD subd = SubD.CreateFromMesh(cage);
                if (subd == null)
                {
                    r["subd_created"] = false;
                    return;
                }
                r["subd_created"] = true;
                r["subd_verts"] = (long)subd.Vertices.Count;
                r["subd_edges"] = (long)subd.Edges.Count;
                r["subd_faces"] = (long)subd.Faces.Count;
                r["subd_is_valid"] = subd.IsValid;

                // (a) the setter. The assembly exposes set_Tag, so the gate is
                // about behaviour: does the value stick, and does the bulk
                // SetEdgeTags path work (what the plugin will use)?
                SubDEdge edge = subd.Edges.Find(0);
                r["tag_before"] = edge.Tag.ToString();
                try
                {
                    edge.Tag = SubDEdgeTag.Crease;
                    r["tag_setter"] = true;
                    r["tag_after"] = subd.Edges.Find(0).Tag.ToString();
                }
                catch (Exception ex)
                {
                    r["tag_setter"] = false;
                    r["tag_error"] = ex.GetType().Name + ": " + FirstLine(ex);
                }

                try
                {
                    var ids = new List<int>();
                    for (int i = 0; i < Math.Min(3, subd.Edges.Count); i++) ids.Add(i);
                    subd.Edges.SetEdgeTags(ids, SubDEdgeTag.Smooth);
                    var tags = new List<object>();
                    foreach (int i in ids) tags.Add(subd.Edges.Find(i).Tag.ToString());
                    r["bulk_tags"] = tags;
                    // Restore the first edge so the readback below is meaningful.
                    subd.Edges.SetEdgeTags(ids, SubDEdgeTag.Crease);
                    r["bulk_after_crease"] = subd.Edges.Find(0).Tag.ToString();
                }
                catch (Exception ex)
                {
                    r["bulk_error"] = ex.GetType().Name + ": " + FirstLine(ex);
                }

                // (b) usable as a SubD: to a Brep and back to a mesh without throwing.
                try
                {
                    Brep brep = subd.ToBrep();
                    r["subd_to_brep"] = brep != null;
                    if (brep != null)
                    {
                        Mesh[] meshes = Mesh.CreateFromBrep(brep, MeshingParameters.Default);
                        int total = 0;
                        if (meshes != null)
                            foreach (Mesh m in meshes) total += m.Vertices.Count;
                        r["brep_mesh_verts"] = (long)total;
                    }
                }
                catch (Exception ex)
                {
                    r["subd_to_brep"] = false;
                    r["subd_to_brep_error"] = ex.GetType().Name + ": " + FirstLine(ex);
                }
            }
            catch (Exception ex)
            {
                r["subd_error"] = ex.GetType().Name + ": " + FirstLine(ex);
            }
        }

        private static Mesh GridCage(int nx, int ny)
        {
            var mesh = new Mesh();
            for (int i = 0; i < nx; i++)
                for (int j = 0; j < ny; j++)
                    mesh.Vertices.Add(i, j, 0);
            for (int i = 0; i < nx - 1; i++)
                for (int j = 0; j < ny - 1; j++)
                {
                    int a = i * ny + j;
                    mesh.Faces.AddFace(a, a + ny, a + ny + 1, a + 1);
                }
            mesh.Normals.ComputeNormals();
            mesh.Compact();
            return mesh;
        }

        // ---------------------------------------------------------------- curves

        private static void CheckCurves(Dictionary<string, object> r)
        {
            try
            {
                // A degree-3 curve with interior non-uniform knots: the case the
                // plan says Blender cannot express exactly (D3).
                var points = new List<Point3d>();
                for (int i = 0; i < 8; i++)
                    points.Add(new Point3d(i, Math.Sin(i * 0.7) * 2.0, 0.0));
                NurbsCurve curve = NurbsCurve.Create(false, 3, points);
                r["curve_spans"] = (long)curve.SpanCount;
                r["curve_order"] = (long)curve.Order;
                r["curve_rational"] = curve.IsRational;

                // Make it non-uniform: pull an interior knot.
                if (curve.Knots.Count > 5)
                {
                    double k = curve.Knots[4];
                    curve.Knots[4] = k + 0.35;
                    r["knot_moved"] = true;
                }

                // MakePiecewiseBezier converts in place and returns success.
                NurbsCurve piecewise = curve.DuplicateCurve() as NurbsCurve;
                bool converted = piecewise != null && piecewise.MakePiecewiseBezier(true);
                r["bezier_converted"] = converted;
                r["bezier_spans"] = piecewise == null ? 0L : (long)piecewise.SpanCount;
                if (converted)
                {
                    r["bezier_max_deviation"] = SpanDeviation(curve, piecewise, 24);
                }

                // The exact-representable case: a uniform clamped curve must
                // convert exactly (deviation 0).
                NurbsCurve uniform = NurbsCurve.Create(false, 3, points);
                NurbsCurve uniformPiecewise = uniform.DuplicateCurve() as NurbsCurve;
                bool uniformConverted = uniformPiecewise != null && uniformPiecewise.MakePiecewiseBezier(true);
                r["uniform_converted"] = uniformConverted;
                if (uniformConverted)
                {
                    r["uniform_max_deviation"] = SpanDeviation(uniform, uniformPiecewise, 16);
                }
            }
            catch (Exception ex)
            {
                r["curve_error"] = ex.GetType().Name + ": " + FirstLine(ex);
            }
        }

        /// <summary>Largest distance from samples on the piecewise curve back to the
        /// original, a practical deviation measure for the D3 warning.</summary>
        private static double SpanDeviation(NurbsCurve original, NurbsCurve piecewise, int samples)
        {
            double max = 0.0;
            for (int span = 0; span < piecewise.SpanCount; span++)
            {
                BezierCurve bezier = piecewise.ConvertSpanToBezier(span);
                if (bezier == null) continue;
                // BezierCurve has no Domain; it is a single span, so 0..1 is the
                // parameter range (PointAt clamps).
                for (int i = 0; i <= samples; i++)
                {
                    Point3d p = bezier.PointAt(i / (double)samples);
                    double ct;
                    if (original.ClosestPoint(p, out ct))
                    {
                        double d = original.PointAt(ct).DistanceTo(p);
                        if (d > max) max = d;
                    }
                }
            }
            return max;
        }

        private static string FirstLine(Exception ex)
        {
            string m = ex.Message;
            if (string.IsNullOrEmpty(m)) return ex.GetType().Name;
            int i = m.IndexOf('\n');
            return i > 0 ? m.Substring(0, i) : m;
        }
    }
}
