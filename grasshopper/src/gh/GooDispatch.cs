// Sorts GH goo into the geometry kinds Stage Out accepts. One source of truth:
// the component and the S6 checks both call this, so a missing goo type cannot
// hide in the component.
//
// The generic path is goo.CastTo<T> (IGH_Goo.CastTo<T> under the
// hood), which handles every goo that stands for a kind: GH_Circle, GH_Arc,
// GH_Line, GH_Polyline, GH_Rectangle, custom goos. An earlier hand-written
// cast list silently dropped the struct-backed ones (the live Circle bug).
// Breps and SubDs are matched before the cast chain so their lossy mesh
// conversion keeps its warning.

using System.Collections.Generic;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Types;
using Rhino.Geometry;

namespace GhStack
{
    public static class GooDispatch
    {
        /// <summary>Appends each goo to the right list; unsupported types are
        /// named in <paramref name="ignored"/>. Geometry that can carry
        /// attributes is also appended to <paramref name="sources"/> (when the
        /// caller passes a list): Stage Out harvests the dictionaries from
        /// there, because a flattened point cloud otherwise loses them.
        /// Instance references go to <paramref name="instances"/> when the
        /// caller collects them (protocol §5.2); otherwise they are ignored.</summary>
        public static void Collect(
            IEnumerable<IGH_Goo> goos,
            List<Mesh> meshes, List<Curve> curves, List<Point3d> points,
            List<SubD> subds, List<Brep> breps, List<string> ignored,
            List<Rhino.Geometry.GeometryBase> sources = null,
            List<GH_InstanceReference> instances = null)
        {
            foreach (IGH_Goo goo in goos)
            {
                if (goo == null) continue;

                GH_InstanceReference instance = goo as GH_InstanceReference;
                if (instance != null)
                {
                    if (instances != null) instances.Add(instance);
                    else ignored.Add(goo.TypeName);
                    continue;
                }

                // GH_Surface derives from GH_Brep, so surfaces land here too.
                GH_Brep brepGoo = goo as GH_Brep;
                if (brepGoo != null)
                {
                    breps.Add(brepGoo.Value);
                    continue;
                }
                GH_Mesh meshGoo = goo as GH_Mesh;
                if (meshGoo != null)
                {
                    meshes.Add(meshGoo.Value);
                    if (sources != null && meshGoo.Value != null) sources.Add(meshGoo.Value);
                    continue;
                }
                GH_SubD subdGoo = goo as GH_SubD;
                if (subdGoo != null)
                {
                    subds.Add(subdGoo.Value);
                    if (sources != null && subdGoo.Value != null) sources.Add(subdGoo.Value);
                    continue;
                }
                // A point cloud is how Stage In hands points over; expand it so
                // both representations travel the same way (the wire carries a
                // flat point list either way).
                GH_PointCloud cloudGoo = goo as GH_PointCloud;
                if (cloudGoo != null && cloudGoo.Value != null)
                {
                    points.AddRange(cloudGoo.Value.GetPoints());
                    if (sources != null) sources.Add(cloudGoo.Value);
                    continue;
                }

                Curve curve = CurveFromGoo(goo);
                if (curve != null)
                {
                    curves.Add(curve);
                    continue;
                }
                Point3d point;
                if (goo.CastTo<Point3d>(out point))
                {
                    points.Add(point);
                    continue;
                }
                // Last resort: anything that can become a mesh or a Brep
                // (extrusions, custom geometry goos).
                Mesh mesh;
                if (goo.CastTo<Mesh>(out mesh) && mesh != null)
                {
                    meshes.Add(mesh);
                    continue;
                }
                Brep brep;
                if (goo.CastTo<Brep>(out brep) && brep != null)
                {
                    breps.Add(brep);
                    continue;
                }

                ignored.Add(goo.TypeName);
            }
        }

        /// <summary>A Curve for any curve goo, or null.
        ///
        /// The struct-backed goos (GH_Circle, GH_Arc, GH_Line, GH_Rectangle) have
        /// no CastTo<Curve> override and fail GH_Convert.ToCurve(Primary) — an
        /// isinst on Curve — so they are unwrapped by hand. That gap is the live
        /// Circle bug. Everything else goes through the goo's own cast, then
        /// Secondary (which must not turn a Brep into its edge curves).</summary>
        private static Curve CurveFromGoo(IGH_Goo goo)
        {
            GH_Circle circle = goo as GH_Circle;
            if (circle != null) return circle.Value.ToNurbsCurve();
            GH_Arc arc = goo as GH_Arc;
            if (arc != null) return arc.Value.ToNurbsCurve();
            GH_Line line = goo as GH_Line;
            if (line != null) return new LineCurve(line.Value);
            GH_Rectangle rectangle = goo as GH_Rectangle;
            if (rectangle != null) return rectangle.Value.ToNurbsCurve();

            Curve curve;
            if (goo.CastTo<Curve>(out curve) && curve != null) return curve;
            if (!(goo is GH_Brep) && GH_Convert.ToCurve(goo, ref curve, GH_Conversion.Secondary))
                return curve;
            return null;
        }
    }
}
