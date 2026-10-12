// S6 probe: the RhinoCommon behaviours the geometry conversions rely on.
//
// Runs as a console exe under wine with RhinoCommon on WINEPATH, so the facts
// come from the same build the plugin uses. Questions:
//   - NurbsCurve knot conventions (Create vs the explicit-knot constructor)
//   - clamped / uniform / Bezier-span patterns, rational handling
//   - PolylineCurve and Circle/Arc NURBS forms
//   - IncreaseDegree and MakePiecewiseBezier results
//   - SubD cage extraction: Mesh.CreateFromSubDControlNet vs the input cage
//   - SubD edge tags: mapping cage vertex pairs to SubD edges
//   - Brep -> mesh
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using Rhino.Geometry;

public static class S6Probe
{
    public static void Main()
    {
        Section("curves");
        CurveFacts();
        Section("subd");
        SubDFacts();
        Section("brep");
        BrepFacts();
    }

    private static void Section(string s) { Console.WriteLine("== " + s); }
    private static void P(string k, object v) { Console.WriteLine(k + " = " + v); }

    private static string F(double d)
    {
        return d.ToString("0.####", CultureInfo.InvariantCulture);
    }

    private static string Knots(NurbsCurve c)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < c.Knots.Count; i++)
        {
            if (i > 0) sb.Append(",");
            sb.Append(F(c.Knots[i]));
        }
        return sb.ToString();
    }

    private static string Cps(NurbsCurve c)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < c.Points.Count; i++)
        {
            ControlPoint cp = c.Points[i];
            if (i > 0) sb.Append(" ");
            sb.Append("(" + F(cp.X) + "," + F(cp.Y) + "," + F(cp.Z) + " w=" + F(cp.Weight) + ")");
        }
        return sb.ToString();
    }

    private static string CurveFactsStr(NurbsCurve c)
    {
        return "order=" + c.Order + " cps=" + c.Points.Count + " knots[" + c.Knots.Count + "]=" + Knots(c)
            + " rational=" + c.IsRational + " spans=" + c.SpanCount + " bezierSpans=" + c.HasBezierSpans
            + " closed=" + c.IsClosed + " periodic=" + c.IsPeriodic
            + " clampedStart=" + c.Knots.IsClampedStart + " clampedEnd=" + c.Knots.IsClampedEnd;
    }

    private static void CurveFacts()
    {
        var pts = new List<Point3d>();
        for (int i = 0; i < 6; i++) pts.Add(new Point3d(i, Math.Sin(i * 0.9) * 2.0, 0));

        NurbsCurve a = NurbsCurve.Create(false, 3, pts);
        P("create.degree2.order3", CurveFactsStr(a));

        var pts8 = new List<Point3d>();
        for (int i = 0; i < 8; i++) pts8.Add(new Point3d(i, Math.Sin(i * 0.7) * 2.0, 0));
        NurbsCurve b = NurbsCurve.Create(false, 4, pts8);
        P("create.degree3.order4", CurveFactsStr(b));

        // explicit-knot constructor: can we fill Blender's clamped vector?
        try
        {
            NurbsCurve e = new NurbsCurve(3, false, 3, 6);
            P("ctor.order3.cps6.alloc", "points=" + e.Points.Count + " knots=" + e.Knots.Count);
            for (int i = 0; i < 6; i++) e.Points.SetPoint(i, new Point4d(pts[i].X, pts[i].Y, pts[i].Z, 1.0));
            // Blender clamped vector for 6 cps, order 3: [0,0,0,1,2,3,4,4,4]
            double[] clamped = new double[] { 0, 0, 0, 1, 2, 3, 4, 4, 4 };
            if (e.Knots.Count == clamped.Length)
            {
                for (int i = 0; i < clamped.Length; i++) e.Knots[i] = clamped[i];
                P("ctor.filled", CurveFactsStr(e));
                P("ctor.valid", e.IsValid + " domain=" + F(e.Domain.Min) + ".." + F(e.Domain.Max));
                // Compare the shape with the clamped Create() curve.
                bool same = true;
                for (int i = 0; i <= 10; i++)
                {
                    Point3d p1 = a.PointAt(a.Domain.ParameterAt(i / 10.0));
                    Point3d p2 = e.PointAt(e.Domain.ParameterAt(i / 10.0));
                    if (p1.DistanceTo(p2) > 1e-6) { same = false; break; }
                }
                P("ctor.shape_matches_create", same);
            }
            else
            {
                P("ctor.knot_count_mismatch", "expected " + clamped.Length + " got " + e.Knots.Count);
            }
        }
        catch (Exception ex)
        {
            P("ctor.error", ex.GetType().Name + ": " + ex.Message);
        }

        // Polyline
        var polyPts = new List<Point3d> { new Point3d(0, 0, 0), new Point3d(1, 0, 0), new Point3d(1, 2, 0) };
        Curve poly = new PolylineCurve(polyPts);
        Polyline pl;
        bool hasPolyline = poly.TryGetPolyline(out pl);
        P("polyline.tryGetPolyline", hasPolyline);
        P("polyline.nurbs", CurveFactsStr(poly.ToNurbsCurve()));

        // Circle and arc: order 3 rational, closed
        NurbsCurve circle = new Circle(Point3d.Origin, 5.0).ToNurbsCurve();
        P("circle", CurveFactsStr(circle));
        Arc arc = new Arc(new Point3d(0, 0, 0), new Point3d(2, 2, 0), new Point3d(4, 0, 0));
        P("arc", CurveFactsStr(arc.ToNurbsCurve()));

        // Non-uniform interior knot -> MakePiecewiseBezier
        NurbsCurve moved = b.DuplicateCurve() as NurbsCurve;
        if (moved.Knots.Count > 5)
        {
            moved.Knots[5] = moved.Knots[5] + 0.35;
            P("moved", CurveFactsStr(moved));
            NurbsCurve piecewise = moved.DuplicateCurve() as NurbsCurve;
            bool ok = piecewise != null && piecewise.MakePiecewiseBezier(true);
            P("piecewise.ok", ok);
            if (ok) P("piecewise", CurveFactsStr(piecewise));
        }

        // Circle through MakePiecewiseBezier, and IncreaseDegree
        NurbsCurve circleSpans = circle.DuplicateCurve() as NurbsCurve;
        bool circleOk = circleSpans != null && circleSpans.MakePiecewiseBezier(true);
        P("circle.piecewise.ok", circleOk);
        if (circleOk)
        {
            P("circle.piecewise", CurveFactsStr(circleSpans));
            NurbsCurve elevated = circleSpans.DuplicateCurve() as NurbsCurve;
            bool inc = elevated != null && elevated.IncreaseDegree(3);
            P("circle.piecewise.increase_degree", inc);
            if (inc) P("circle.elevated", CurveFactsStr(elevated));
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
                int v = i * ny + j;
                mesh.Faces.AddFace(v, v + ny, v + ny + 1, v + 1);
            }
        mesh.Normals.ComputeNormals();
        mesh.Compact();
        return mesh;
    }

    private static void SubDFacts()
    {
        try
        {
            Mesh cage = GridCage(4, 4);
            SubD subd = SubD.CreateFromMesh(cage);
            P("subd.created", subd != null);
            if (subd == null) return;
            P("subd.counts", "verts=" + subd.Vertices.Count + " edges=" + subd.Edges.Count + " faces=" + subd.Faces.Count);
            P("subd.valid", subd.IsValid);

            // Cage extraction: does the control-net mesh match the input cage?
            Mesh back = Mesh.CreateFromSubDControlNet(subd);
            P("cage.back", "verts=" + back.Vertices.Count + " faces=" + back.Faces.Count + " ngons=" + back.Ngons.Count);
            bool vertsMatch = back.Vertices.Count == cage.Vertices.Count;
            if (vertsMatch)
                for (int i = 0; i < cage.Vertices.Count; i++)
                    if (back.Vertices[i].DistanceTo(cage.Vertices[i]) > 1e-9) { vertsMatch = false; break; }
            P("cage.verts_match_input", vertsMatch);

            // Component index order vs enumeration order.
            bool indexOrder = true;
            var vertexIndexByEnum = new List<int>();
            for (int vi = 0; vi < subd.Vertices.Count; vi++)
            {
                SubDVertex vv = subd.Vertices.Find(vi);
                if (vv == null) { indexOrder = false; continue; }
                vertexIndexByEnum.Add(vv.ComponentIndex().Index);
                if (vv.ComponentIndex().Index != vi) indexOrder = false;
            }
            P("subd.vertex_component_index_is_enum_order", indexOrder);

            // Faces: enumeration geometry vs cage mesh faces.
            bool facesMatch = subd.Faces.Count == back.Faces.Count;
            P("cage.faces_count_match", facesMatch);

            // Edge mapping by vertex component indices.
            var edgeByPair = new Dictionary<long, int>();
            int creaseDefault = 0, smoothDefault = 0;
            foreach (SubDEdge e in subd.Edges)
            {
                int a = e.VertexFrom.ComponentIndex().Index;
                int c = e.VertexTo.ComponentIndex().Index;
                long key = ((long)Math.Min(a, c) << 32) | (uint)Math.Max(a, c);
                if (!edgeByPair.ContainsKey(key)) edgeByPair[key] = e.ComponentIndex().Index;
                if (e.Tag == SubDEdgeTag.Crease) creaseDefault++;
                else if (e.Tag == SubDEdgeTag.Smooth) smoothDefault++;
            }
            P("subd.edge_pair_map_size", edgeByPair.Count);
            P("subd.default_tags", "crease=" + creaseDefault + " smooth=" + smoothDefault);

            // Every cage edge pair must be found.
            int missing = 0;
            for (int i = 0; i < back.TopologyEdges.Count; i++)
            {
                var pair = back.TopologyEdges.GetTopologyVertices(i);
                long key = ((long)Math.Min(pair.I, pair.J) << 32) | (uint)Math.Max(pair.I, pair.J);
                if (!edgeByPair.ContainsKey(key)) missing++;
            }
            P("cage.edges_not_in_subd", missing);

            // Set a tag through SetEdgeTags with the mapped index, read it back.
            int target = -1;
            foreach (SubDEdge e in subd.Edges)
            {
                int a = e.VertexFrom.ComponentIndex().Index;
                int c = e.VertexTo.ComponentIndex().Index;
                if (a != 0 && a != 3 && a != 12 && a != 15 && c != 0 && c != 3 && c != 12 && c != 15)
                {
                    target = e.ComponentIndex().Index;
                    break;
                }
            }
            if (target >= 0)
            {
                var ids = new List<int> { target };
                subd.Edges.SetEdgeTags(ids, SubDEdgeTag.Crease);
                bool found = false;
                foreach (SubDEdge e in subd.Edges)
                    if (e.ComponentIndex().Index == target)
                    {
                        found = e.Tag == SubDEdgeTag.Crease;
                        break;
                    }
                P("subd.set_edge_tag_works", found);
                Mesh again = Mesh.CreateFromSubDControlNet(subd);
                P("subd.cage_after_tag", "verts=" + again.Vertices.Count + " faces=" + again.Faces.Count);
            }
            else
            {
                P("subd.set_edge_tag_works", "no interior edge found");
            }
        }
        catch (Exception ex)
        {
            P("subd.error", ex.GetType().Name + ": " + ex.Message);
        }
    }

    private static void BrepFacts()
    {
        try
        {
            Sphere sphere = new Sphere(Point3d.Origin, 10.0);
            Brep brep = sphere.ToBrep();
            P("brep.faces", brep.Faces.Count);
            Mesh[] meshes = Mesh.CreateFromBrep(brep, MeshingParameters.Default);
            int verts = 0, faces = 0;
            if (meshes != null)
                foreach (Mesh m in meshes) { verts += m.Vertices.Count; faces += m.Faces.Count; }
            P("brep.mesh", "meshes=" + (meshes == null ? 0 : meshes.Length) + " verts=" + verts + " faces=" + faces);
        }
        catch (Exception ex)
        {
            P("brep.error", ex.GetType().Name + ": " + ex.Message);
        }
    }
}
