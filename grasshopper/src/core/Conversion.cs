// Protocol sections <-> Rhino geometry. RhinoCommon only; no Grasshopper and no
// bpy. The twin of blender/gh_stack/blender/geometry_io.py.
//
// Meshes (n-gons, vertex colours), SubD cages with tag creases, curves (poly,
// NURBS, Bezier nets), points, and Breps (meshed, warned). Approximations and
// losses are always reported through the warnings list (plan §3, principle 6).
//
// Units: the wire is meters (protocol §8.3), so both directions take the document
// scale (document units per meter). The caller gets it from
// RhinoMath.UnitScale(UnitSystem.Meters, doc.ModelUnitSystem).
//
// Curve exactness: Blender NURBS splines can express clamped-uniform or uniform
// knots, any order 2..6, with weights. Anything else is converted to Bezier
// spans (MakePiecewiseBezier, lossless) and sent as a degree-3 Bezier net;
// dropping weights off a rational curve is the lossy step D3 warns about.

using System;
using System.Collections.Generic;
using System.Drawing;
using Rhino;
using Rhino.Geometry;

namespace GhStack.Core
{
    public static class Conversion
    {
        // ------------------------------------------------------- sections -> mesh

        /// <summary>Build a Rhino mesh from mesh.* or subd.* sections.
        /// Returns null when the frame carries no mesh.</summary>
        public static Mesh MeshFromSections(DecodedFrame frame, double scale)
        {
            Section verts = frame.Find("mesh.verts") ?? frame.Find("subd.verts");
            Section counts = frame.Find("mesh.face_counts") ?? frame.Find("subd.face_counts");
            Section indices = frame.Find("mesh.face_indices") ?? frame.Find("subd.face_indices");
            if (verts == null || counts == null || indices == null) return null;

            var mesh = new Mesh();
            float[] positions = verts.ToFloats();
            // Vertices.AddVertices(IEnumerable<Point3f>) does not resolve with the
            // Mono profile mcs compiles against (its metadata mentions
            // IReadOnlyList, absent from the 4.0-api mscorlib mcs loads), so the
            // loop is deliberate. S3 measures what this actually costs.
            for (int i = 0; i < positions.Length; i += 3)
            {
                mesh.Vertices.Add(
                    (float)(positions[i] * scale),
                    (float)(positions[i + 1] * scale),
                    (float)(positions[i + 2] * scale));
            }

            AddFaces(mesh, counts.ToU32(), indices.ToU32());

            Section colors = frame.Find("mesh.colors");
            if (colors != null) SetMeshColors(mesh, colors);

            mesh.Normals.ComputeNormals();
            mesh.Compact();
            return mesh;
        }

        private static void AddFaces(Mesh mesh, uint[] counts, uint[] indices)
        {
            int at = 0;
            for (int f = 0; f < counts.Length; f++)
            {
                int n = (int)counts[f];
                if (n == 3)
                {
                    mesh.Faces.AddFace((int)indices[at], (int)indices[at + 1], (int)indices[at + 2]);
                }
                else if (n == 4)
                {
                    mesh.Faces.AddFace((int)indices[at], (int)indices[at + 1],
                                       (int)indices[at + 2], (int)indices[at + 3]);
                }
                else if (n > 4)
                {
                    // Rhino keeps n-gons as a boundary over triangles. Fan the
                    // boundary, remember those face indexes, then register the
                    // ngon over them.
                    var boundary = new List<int>();
                    var fan = new List<int>();
                    for (int k = 0; k < n; k++) boundary.Add((int)indices[at + k]);
                    for (int k = 1; k + 1 < n; k++)
                    {
                        int faceIndex = mesh.Faces.Count;
                        mesh.Faces.AddFace(boundary[0], boundary[k], boundary[k + 1]);
                        fan.Add(faceIndex);
                    }
                    MeshNgon ngon = MeshNgon.Create(boundary, fan);
                    mesh.Ngons.AddNgon(ngon);
                }
                else
                {
                    throw new FrameException("bad_frame", "face with " + n + " vertices");
                }
                at += n;
            }
        }

        private static void SetMeshColors(Mesh mesh, Section colors)
        {
            byte[] rgba = colors.Raw;
            int count = rgba.Length / 4;
            if (count != mesh.Vertices.Count)
                throw new FrameException("bad_frame",
                    "mesh.colors has " + count + " entries for " + mesh.Vertices.Count + " vertices");
            var list = new Color[count];
            for (int i = 0; i < count; i++)
            {
                // The wire is RGBA bytes; System.Drawing.Color is ARGB.
                list[i] = Color.FromArgb(rgba[i * 4 + 3], rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2]);
            }
            mesh.VertexColors.SetColors(list);
        }

        // ------------------------------------------------------- mesh -> sections

        /// <summary>Encode a Rhino mesh as mesh.* sections. Vertex colours are
        /// included only when the mesh has one colour per vertex.</summary>
        public static List<Section> SectionsFromMesh(Mesh mesh, double scale)
        {
            return MeshSections(mesh, scale, "mesh", true);
        }

        private static List<Section> MeshSections(Mesh mesh, double scale, string prefix, bool includeColors)
        {
            var sections = new List<Section>();

            Point3f[] points = mesh.Vertices.ToPoint3fArray();
            var positions = new float[points.Length * 3];
            for (int i = 0; i < points.Length; i++)
            {
                positions[i * 3] = (float)(points[i].X / scale);
                positions[i * 3 + 1] = (float)(points[i].Y / scale);
                positions[i * 3 + 2] = (float)(points[i].Z / scale);
            }
            sections.Add(Section.Floats(prefix + ".verts", positions));

            var counts = new List<uint>();
            var indices = new List<uint>();
            bool[] inNgon = NgonFaceMask(mesh);
            for (int f = 0; f < mesh.Faces.Count; f++)
            {
                if (inNgon[f]) continue;
                MeshFace face = mesh.Faces[f];
                counts.Add(face.IsQuad ? 4u : 3u);
                indices.Add((uint)face.A);
                indices.Add((uint)face.B);
                indices.Add((uint)face.C);
                if (face.IsQuad) indices.Add((uint)face.D);
            }
            for (int i = 0; i < mesh.Ngons.Count; i++)
            {
                MeshNgon ngon = mesh.Ngons.GetNgon(i);
                var faceList = new List<int>();
                for (int k = 0; k < ngon.FaceCount; k++) faceList.Add(ngon[k]);
                int[] boundary = mesh.Ngons.GetNgonBoundary(faceList);
                counts.Add((uint)boundary.Length);
                foreach (int v in boundary) indices.Add((uint)v);
            }
            sections.Add(Section.U32(prefix + ".face_counts", counts.ToArray()));
            sections.Add(Section.U32(prefix + ".face_indices", indices.ToArray()));

            if (includeColors && mesh.VertexColors.Count == mesh.Vertices.Count && mesh.Vertices.Count > 0)
            {
                var rgba = new byte[mesh.Vertices.Count * 4];
                for (int i = 0; i < mesh.Vertices.Count; i++)
                {
                    Color c = mesh.VertexColors[i];
                    rgba[i * 4] = c.R;
                    rgba[i * 4 + 1] = c.G;
                    rgba[i * 4 + 2] = c.B;
                    rgba[i * 4 + 3] = c.A;
                }
                sections.Add(Section.Bytes("mesh.colors", rgba));
            }

            return sections;
        }

        private static bool[] NgonFaceMask(Mesh mesh)
        {
            var mask = new bool[mesh.Faces.Count];
            for (int i = 0; i < mesh.Ngons.Count; i++)
            {
                MeshNgon ngon = mesh.Ngons.GetNgon(i);
                int count = ngon.FaceCount;
                for (int k = 0; k < count; k++) mask[ngon[k]] = true;
            }
            return mask;
        }

        // ------------------------------------------------------- curves -> wire

        /// <summary>Encode curves as crv.* sections. Exact wherever Blender can
        /// express the curve; otherwise a degree-3 Bezier net with a warning.
        /// approximated counts every approximated curve; warnings carry the
        /// deviation when weights had to be dropped (D3).</summary>
        public static List<Section> SectionsFromCurves(
            IList<Curve> curves, double scale, List<string> warnings, out int approximated)
        {
            approximated = 0;
            var pts = new List<float>();
            var counts = new List<uint>();
            var types = new List<byte>();
            var orders = new List<byte>();
            var modes = new List<byte>();
            var cyclic = new List<byte>();
            int sampled = 0;
            double maxDeviation = 0.0;

            foreach (Curve curve in curves)
            {
                if (curve == null) continue;

                Polyline poly;
                if (curve.TryGetPolyline(out poly) && poly.Count >= 2)
                {
                    var list = new List<Point3d>(poly);
                    bool closed = list[0].DistanceTo(list[list.Count - 1]) < 1e-9;
                    if (closed) list.RemoveAt(list.Count - 1);
                    AppendCurveSection(list, null, 0, 0, 0, closed, scale,
                        pts, counts, types, orders, modes, cyclic);
                    continue;
                }

                NurbsCurve nurbs = curve.ToNurbsCurve();
                if (nurbs == null)
                {
                    sampled++;
                    continue;
                }

                if (CanBeBlenderNurbs(nurbs))
                {
                    AppendNurbsSection(nurbs, false, scale, pts, counts, types, orders, modes, cyclic);
                    continue;
                }

                NurbsCurve piece = nurbs.DuplicateCurve() as NurbsCurve;
                bool converted = piece != null && piece.MakePiecewiseBezier(true);
                if (!converted)
                {
                    AppendSampled(curve, scale, pts, counts, types, orders, modes, cyclic);
                    sampled++;
                    continue;
                }

                if (piece.IsRational)
                {
                    // Dropping the weights is the lossy step (D3): measure the
                    // deviation between the original and what will actually be
                    // sent, then flatten the weights.
                    for (int i = 0; i < piece.Points.Count; i++) piece.Points.SetWeight(i, 1.0);
                    double deviation = SpanDeviation(nurbs, piece, 24);
                    if (deviation > maxDeviation) maxDeviation = deviation;
                }

                if (piece.Order > 4)
                {
                    AppendSampled(curve, scale, pts, counts, types, orders, modes, cyclic);
                    sampled++;
                    continue;
                }
                if (piece.Order < 4) piece.IncreaseDegree(3);
                if (piece.Order != 4 || piece.Points.Count < 4 || (piece.Points.Count - 1) % 3 != 0)
                {
                    AppendSampled(curve, scale, pts, counts, types, orders, modes, cyclic);
                    sampled++;
                    continue;
                }
                AppendNurbsSection(piece, true, scale, pts, counts, types, orders, modes, cyclic);
                approximated++;
            }

            if (approximated > 0)
            {
                string message = approximated + " curve(s) approximated as Bezier spans";
                if (maxDeviation > 0)
                    message += "; max deviation " + maxDeviation.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture);
                warnings.Add(message);
            }
            if (sampled > 0)
                warnings.Add(sampled + " curve(s) sampled to polylines; no exact representation was available");

            var sections = new List<Section>();
            if (counts.Count == 0) return sections;
            sections.Add(Section.Floats("crv.points", pts.ToArray()));
            sections.Add(Section.U32("crv.counts", counts.ToArray()));
            sections.Add(Section.Bytes("crv.types", types.ToArray()));
            sections.Add(Section.Bytes("crv.orders", orders.ToArray()));
            sections.Add(Section.Bytes("crv.knots_mode", modes.ToArray()));
            sections.Add(Section.Bytes("crv.cyclic", cyclic.ToArray()));
            return sections;
        }

        private static void AppendCurveSection(
            List<Point3d> points, List<double> weights, byte type, byte order, byte mode, bool cyclic,
            double scale, List<float> pts, List<uint> counts, List<byte> types, List<byte> orders,
            List<byte> modes, List<byte> cyc)
        {
            foreach (Point3d p in points)
            {
                pts.Add((float)(p.X / scale));
                pts.Add((float)(p.Y / scale));
                pts.Add((float)(p.Z / scale));
                pts.Add(1.0f);
            }
            counts.Add((uint)points.Count);
            types.Add(type);
            orders.Add(order);
            modes.Add(mode);
            cyc.Add((byte)(cyclic ? 1 : 0));
        }

        private static void AppendNurbsSection(
            NurbsCurve curve, bool bezier, double scale, List<float> pts, List<uint> counts,
            List<byte> types, List<byte> orders, List<byte> modes, List<byte> cyclic)
        {
            for (int i = 0; i < curve.Points.Count; i++)
            {
                ControlPoint cp = curve.Points[i];
                pts.Add((float)(cp.X / scale));
                pts.Add((float)(cp.Y / scale));
                pts.Add((float)(cp.Z / scale));
                pts.Add((float)cp.Weight);
            }
            counts.Add((uint)curve.Points.Count);
            types.Add(2);
            orders.Add((byte)curve.Order);
            if (bezier)
            {
                modes.Add(2);
            }
            else
            {
                modes.Add((byte)(curve.Knots.IsClampedStart && curve.Knots.IsClampedEnd ? 1 : 0));
            }
            cyclic.Add(0);
        }

        private static void AppendSampled(
            Curve curve, double scale, List<float> pts, List<uint> counts, List<byte> types,
            List<byte> orders, List<byte> modes, List<byte> cyclic)
        {
            int segments = Math.Max(8, curve.SpanCount * 8);
            double[] ts = curve.DivideByCount(segments, true);
            var list = new List<Point3d>();
            if (ts != null)
                foreach (double t in ts) list.Add(curve.PointAt(t));
            if (list.Count < 2)
            {
                list.Clear();
                list.Add(curve.PointAtStart);
                list.Add(curve.PointAtEnd);
            }
            AppendCurveSection(list, null, 0, 0, 0, curve.IsClosed, scale,
                pts, counts, types, orders, modes, cyclic);
        }

        /// <summary>Whether Blender can hold this curve exactly: a non-periodic
        /// NURBS of order 2..6 with clamped-uniform or uniform knots.</summary>
        private static bool CanBeBlenderNurbs(NurbsCurve curve)
        {
            if (curve.IsPeriodic) return false;
            int order = curve.Order;
            if (order < 2 || order > 6) return false;
            int count = curve.Knots.Count;
            if (count < order) return false;

            if (curve.Knots.IsClampedStart && curve.Knots.IsClampedEnd)
            {
                // Interior single knots, evenly spaced.
                int first = order - 1;
                int last = count - order;
                if (last <= first) return true;
                double step = curve.Knots[first + 1] - curve.Knots[first];
                for (int i = first + 2; i <= last; i++)
                    if (!Close(curve.Knots[i] - curve.Knots[i - 1], step)) return false;
                return true;
            }

            double uniform = curve.Knots[1] - curve.Knots[0];
            for (int i = 2; i < count; i++)
                if (!Close(curve.Knots[i] - curve.Knots[i - 1], uniform)) return false;
            return true;
        }

        private static bool Close(double a, double b)
        {
            return Math.Abs(a - b) <= 1e-9 * Math.Max(1.0, Math.Abs(a) + Math.Abs(b));
        }

        /// <summary>Largest distance from samples on the approximation back to the
        /// original (the S4 recipe).</summary>
        private static double SpanDeviation(NurbsCurve original, NurbsCurve approximation, int samples)
        {
            double max = 0.0;
            for (int span = 0; span < approximation.SpanCount; span++)
            {
                BezierCurve bezier = approximation.ConvertSpanToBezier(span);
                if (bezier == null) continue;
                for (int i = 0; i <= samples; i++)
                {
                    Point3d p = bezier.PointAt(i / (double)samples);
                    double t;
                    if (original.ClosestPoint(p, out t))
                    {
                        double d = original.PointAt(t).DistanceTo(p);
                        if (d > max) max = d;
                    }
                }
            }
            return max;
        }

        // ------------------------------------------------------- wire -> curves

        /// <summary>Rebuild curves from crv.* sections.</summary>
        public static List<Curve> CurvesFromSections(DecodedFrame frame, double scale)
        {
            var curves = new List<Curve>();
            Section points = frame.Find("crv.points");
            Section counts = frame.Find("crv.counts");
            if (points == null || counts == null) return curves;

            float[] raw = points.ToFloats();
            uint[] cnt = counts.ToU32();
            byte[] types = BytesOf(frame.Find("crv.types"));
            byte[] orders = BytesOf(frame.Find("crv.orders"));
            byte[] modes = BytesOf(frame.Find("crv.knots_mode"));
            byte[] cyclic = BytesOf(frame.Find("crv.cyclic"));

            int at = 0;
            for (int i = 0; i < cnt.Length; i++)
            {
                int n = (int)cnt[i];
                if (n < 2 || at + n > raw.Length / 4) break;
                var ps = new List<Point3d>();
                var ws = new List<double>();
                for (int k = 0; k < n; k++)
                {
                    int baseIndex = (at + k) * 4;
                    ps.Add(new Point3d(raw[baseIndex] * scale, raw[baseIndex + 1] * scale, raw[baseIndex + 2] * scale));
                    double w = raw[baseIndex + 3];
                    ws.Add(w == 0 ? 1.0 : w);
                }
                at += n;

                byte type = ByteAt(types, i, 2);
                byte mode = ByteAt(modes, i, 0);
                byte order = ByteAt(orders, i, 0);
                bool isCyclic = ByteAt(cyclic, i, 0) != 0;

                Curve curve = BuildCurve(ps, ws, type, mode, order, isCyclic);
                if (curve != null) curves.Add(curve);
            }
            return curves;
        }

        private static Curve BuildCurve(
            List<Point3d> ps, List<double> ws, byte type, byte mode, byte order, bool cyclic)
        {
            if (ps.Count < 2) return null;

            if (type == 0)
            {
                var poly = new Polyline(ps);
                if (cyclic && ps.Count > 2 && ps[0].DistanceTo(ps[ps.Count - 1]) > 1e-9)
                    poly.Add(ps[0]);
                return new PolylineCurve(poly);
            }

            if (mode == 2 || type == 1)
            {
                NurbsCurve bezier = BuildBezier(ps);
                if (bezier != null) return bezier;
            }
            return BuildNurbs(ps, ws, order == 0 ? 4 : order, mode == 1, cyclic);
        }

        /// <summary>A degree-3 curve with Bezier spans from a co0,hr0,hl1,co1...
        /// net (3k+1 control points).</summary>
        private static NurbsCurve BuildBezier(List<Point3d> ps)
        {
            int n = ps.Count;
            if (n < 4 || (n - 1) % 3 != 0) return null;
            int spans = (n - 1) / 3;

            var curve = new NurbsCurve(3, false, 4, n);
            for (int i = 0; i < n; i++)
                curve.Points.SetPoint(i, new Point4d(ps[i].X, ps[i].Y, ps[i].Z, 1.0));

            // Rhino's knot count for n control points and order 4 is n+2:
            // order-1 repeats at each end, order-1 at every interior boundary.
            var knots = new List<double>();
            for (int k = 0; k < 3; k++) knots.Add(0.0);
            for (int b = 1; b < spans; b++)
                for (int k = 0; k < 3; k++) knots.Add(b);
            for (int k = 0; k < 3; k++) knots.Add(spans);
            if (knots.Count != curve.Knots.Count) return null;
            for (int i = 0; i < knots.Count; i++) curve.Knots[i] = knots[i];
            return curve.IsValid ? curve : null;
        }

        private static NurbsCurve BuildNurbs(
            List<Point3d> ps, List<double> ws, int order, bool endpoint, bool cyclic)
        {
            int n = ps.Count;
            int ord = Math.Max(2, Math.Min(order, n));
            bool rational = false;
            foreach (double w in ws)
                if (Math.Abs(w - 1.0) > 1e-12) { rational = true; break; }

            if (cyclic && !rational)
            {
                NurbsCurve periodic = NurbsCurve.Create(true, ord, ps);
                if (periodic != null) return periodic;
            }

            var curve = new NurbsCurve(3, rational, ord, n);
            for (int i = 0; i < n; i++)
                curve.Points.SetPoint(i, new Point4d(ps[i].X, ps[i].Y, ps[i].Z, rational ? ws[i] : 1.0));

            int count = curve.Knots.Count;
            if (endpoint)
            {
                int interior = count - 2 * (ord - 1);
                for (int i = 0; i < count; i++)
                {
                    if (i < ord - 1) curve.Knots[i] = 0.0;
                    else if (i >= count - (ord - 1)) curve.Knots[i] = 1.0;
                    else curve.Knots[i] = (double)(i - (ord - 2)) / (double)(interior + 1);
                }
            }
            else
            {
                for (int i = 0; i < count; i++) curve.Knots[i] = i;
            }
            return curve;
        }

        private static byte[] BytesOf(Section section)
        {
            return section == null ? null : section.Raw;
        }

        private static byte ByteAt(byte[] values, int index, byte fallback)
        {
            return values != null && index < values.Length ? values[index] : fallback;
        }

        // --------------------------------------------------------------- points

        /// <summary>Points from pts.* sections.</summary>
        public static List<Point3d> PointsFromSections(DecodedFrame frame, double scale)
        {
            var result = new List<Point3d>();
            Section positions = frame.Find("pts.positions");
            if (positions == null) return result;
            float[] f = positions.ToFloats();
            for (int i = 0; i + 2 < f.Length; i += 3)
                result.Add(new Point3d(f[i] * scale, f[i + 1] * scale, f[i + 2] * scale));
            return result;
        }

        /// <summary>Encode points as pts.positions.</summary>
        public static List<Section> SectionsFromPoints(IList<Point3d> points, double scale)
        {
            var sections = new List<Section>();
            if (points.Count == 0) return sections;
            var positions = new float[points.Count * 3];
            for (int i = 0; i < points.Count; i++)
            {
                positions[i * 3] = (float)(points[i].X / scale);
                positions[i * 3 + 1] = (float)(points[i].Y / scale);
                positions[i * 3 + 2] = (float)(points[i].Z / scale);
            }
            sections.Add(Section.Floats("pts.positions", positions));
            return sections;
        }

        // ----------------------------------------------------------------- SubD

        /// <summary>Build a SubD from subd.* sections and apply the crease tags.
        /// Blender sends fractional crease values; they are rounded to the nearest
        /// tag and the count of affected edges is warned (D2).</summary>
        public static SubD SubDFromSections(DecodedFrame frame, double scale, List<string> warnings)
        {
            Mesh cage = MeshFromSections(frame, scale);
            if (cage == null || cage.Vertices.Count == 0) return null;
            SubD subd = SubD.CreateFromMesh(cage);
            if (subd == null) return null;

            Section edges = frame.Find("subd.crease_edges");
            Section values = frame.Find("subd.crease_values");
            if (edges == null || values == null) return subd;

            uint[] pairs = edges.ToU32();
            float[] vals = values.ToFloats();
            var byPair = new Dictionary<long, SubDEdge>();
            foreach (SubDEdge e in subd.Edges)
            {
                int a = e.VertexFrom.ComponentIndex().Index;
                int b = e.VertexTo.ComponentIndex().Index;
                long key = PairKey(a, b);
                if (!byPair.ContainsKey(key)) byPair[key] = e;
            }

            int fractional = 0;
            int changed = 0;
            for (int i = 0; i + 1 < pairs.Length; i += 2)
            {
                float v = (i / 2) < vals.Length ? vals[i / 2] : 0f;
                if (Math.Abs(v) > 1e-6 && Math.Abs(v - 1f) > 1e-6) fractional++;
                SubDEdgeTag tag = v >= 0.5f ? SubDEdgeTag.Crease : SubDEdgeTag.Smooth;
                SubDEdge edge;
                if (!byPair.TryGetValue(PairKey((int)pairs[i], (int)pairs[i + 1]), out edge)) continue;
                if (edge.Tag != tag)
                {
                    edge.Tag = tag;
                    changed++;
                }
            }
            if (fractional > 0)
                warnings.Add(fractional + " fractional crease value(s) rounded to the nearest tag; "
                    + changed + " edge(s) changed");
            return subd;
        }

        /// <summary>Encode a SubD as its control cage plus crease tags (D2).
        /// Boundary edges of an open cage read Crease by default, so only tagged
        /// creases are sent.</summary>
        public static List<Section> SectionsFromSubD(SubD subd, double scale, List<string> warnings)
        {
            Mesh cage = Mesh.CreateFromSubDControlNet(subd);
            var sections = MeshSections(cage, scale, "subd", false);

            var creaseEdges = new List<uint>();
            var creaseValues = new List<float>();
            foreach (SubDEdge e in subd.Edges)
            {
                if (e.Tag != SubDEdgeTag.Crease) continue;
                int a = e.VertexFrom.ComponentIndex().Index;
                int b = e.VertexTo.ComponentIndex().Index;
                if (a < 0 || b < 0) continue;
                creaseEdges.Add((uint)a);
                creaseEdges.Add((uint)b);
                creaseValues.Add(1.0f);
            }
            if (creaseEdges.Count > 0)
            {
                sections.Add(Section.U32("subd.crease_edges", creaseEdges.ToArray()));
                sections.Add(Section.Floats("subd.crease_values", creaseValues.ToArray()));
            }

            warnings.Add("creases transferred as tags; semi-sharp and vertex creases are not available from RhinoCommon");
            return sections;
        }

        private static long PairKey(int a, int b)
        {
            int lo = Math.Min(a, b);
            int hi = Math.Max(a, b);
            return ((long)lo << 32) | (uint)hi;
        }

        // ------------------------------------------------------- stage out glue

        /// <summary>Everything a Stage Out can receive, turned into sections.
        /// Meshes are joined; Breps are meshed (warned); multiple SubDs are
        /// joined. counts feeds the response header.</summary>
        public static List<Section> SectionsFromGeometry(
            IList<Mesh> meshes, IList<Curve> curves, IList<Point3d> points, IList<SubD> subds,
            IList<Brep> breps, double scale, List<string> warnings, out Dictionary<string, object> counts)
        {
            var sections = new List<Section>();
            var allMeshes = new List<Mesh>();
            foreach (Mesh m in meshes)
                if (m != null && m.Vertices.Count > 0) allMeshes.Add(m);

            int meshedBreps = 0;
            foreach (Brep b in breps)
            {
                if (b == null) continue;
                Mesh[] ms = Mesh.CreateFromBrep(b, MeshingParameters.Default);
                if (ms == null) continue;
                foreach (Mesh m in ms)
                {
                    if (m != null && m.Vertices.Count > 0) allMeshes.Add(m);
                    meshedBreps++;
                }
            }
            if (breps.Count > 0)
                warnings.Add(breps.Count + " surface(s)/Brep(s) converted to mesh; Blender has no B-rep");

            int meshCount = 0;
            if (allMeshes.Count > 0)
            {
                Mesh joined = allMeshes.Count == 1 ? allMeshes[0] : new Mesh();
                if (allMeshes.Count > 1)
                {
                    joined.Append(allMeshes);
                    joined.Normals.ComputeNormals();
                    joined.Compact();
                }
                sections.AddRange(MeshSections(joined, scale, "mesh", true));
                meshCount = 1;
            }

            int subdCount = 0;
            if (subds.Count > 0)
            {
                SubD subd = subds[0];
                if (subds.Count > 1)
                {
                    SubD[] joined = SubD.JoinSubDs(subds, RhinoMath.ZeroTolerance, false);
                    if (joined != null && joined.Length == 1) subd = joined[0];
                    else warnings.Add("several SubDs arrived; only the first was sent");
                }
                sections.AddRange(SectionsFromSubD(subd, scale, warnings));
                subdCount = 1;
            }

            int approximated = 0;
            if (curves.Count > 0)
                sections.AddRange(SectionsFromCurves(curves, scale, warnings, out approximated));

            if (points.Count > 0)
                sections.AddRange(SectionsFromPoints(points, scale));

            counts = new Dictionary<string, object>();
            counts["meshes"] = (long)meshCount;
            counts["subd"] = (long)subdCount;
            counts["curves"] = (long)curves.Count;
            counts["points"] = (long)points.Count;
            counts["approximated_curves"] = (long)approximated;
            return sections;
        }
    }
}
