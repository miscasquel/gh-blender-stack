// The unified Stage In geometry: one frame, one list of goos.
//
// Everything leaves through `Geometry`, in a fixed order — mesh, SubD cage,
// curves, points. The kind is decided by the protocol sections (`mesh.*`,
// `subd.*`, `crv.*`, `pts.*`); absent and empty groups contribute nothing.
//
// This lives outside Conversion.cs because it builds Grasshopper goos; the core
// conversions stay RhinoCommon-only for the console tests.

using System;
using System.Collections.Generic;
using Grasshopper.Kernel.Types;
using Stageloom.Core;
using Rhino.Geometry;

namespace Stageloom
{
    public static class StageGeometry
    {
        /// <summary>Build the stage's geometry list from a decoded frame, with
        /// every attribute the frame carries attached to its geometry's
        /// UserDictionary (the platform <see cref="AttributeStore"/> puts them
        /// on). <paramref name="note"/> receives conversion remarks (SubD
        /// creases) so the component can surface them without owning the
        /// conversion.</summary>
        public static List<IGH_Goo> Build(DecodedFrame frame, double scale,
                                          List<Dictionary<string, object>> attributes,
                                          Action<string> note)
        {
            var geometry = new List<IGH_Goo>();

            if (frame.Find("mesh.verts") != null)
            {
                Mesh mesh = Conversion.MeshFromSections(frame, scale);
                if (mesh != null && mesh.Vertices.Count > 0)
                {
                    if (mesh.Faces.Count == 0 && note != null)
                        note("mesh has no faces; its edges are not carried by the mesh wire");
                    AttributeStore.Attach(mesh, "mesh", attributes, frame.Sections, null, note);
                    geometry.Add(new GH_Mesh(mesh));
                }
            }

            if (frame.Find("subd.verts") != null)
            {
                var notes = new List<WireWarning>();
                SubD subd = Conversion.SubDFromSections(frame, scale, notes);
                foreach (WireWarning warning in notes)
                    if (note != null) note(warning.Message);
                if (subd != null)
                {
                    AttributeStore.Attach(subd, "subd", attributes, frame.Sections, null, note);
                    geometry.Add(new GH_SubD(subd));
                }
            }

            var curves = Conversion.CurvesFromSections(frame, scale);
            if (curves.Count > 0)
            {
                Section countsSection = frame.Find("crv.counts");
                uint[] counts = countsSection != null ? countsSection.ToU32() : null;
                AttributeStore.AttachCurves(curves, counts, attributes, frame.Sections, null, note);
                foreach (Curve curve in curves)
                    geometry.Add(new GH_Curve(curve));
            }

            // Points travel as one point cloud item: it is the geometry kind
            // Blender sends (`pts.*` is a point cloud), it carries point colours
            // natively, and it is the only point-shaped geometry that can hold
            // attributes (a GH_Point wraps a struct, which cannot).
            PointCloud cloud = Conversion.PointCloudFromSections(frame, scale);
            if (cloud != null)
            {
                AttributeStore.Attach(cloud, "pts", attributes, frame.Sections, null, note);
                geometry.Add(new GH_PointCloud(cloud));
            }

            // Blocks last: the document definitions they need are built from the
            // frame's definition groups, and each row becomes a reference goo.
            Instances.Build(frame, scale, attributes, geometry, note);

            return geometry;
        }

        /// <summary>The attribute names of a frame's descriptors, in frame order.
        /// Stage In publishes them so a definition can see what Blender carries
        /// without guessing; the values come through Attribute Get.</summary>
        public static List<string> AttributeNames(List<Dictionary<string, object>> attributes)
        {
            var names = new List<string>();
            if (attributes == null) return names;
            foreach (Dictionary<string, object> descriptor in attributes)
            {
                string name = Json.GetString(descriptor, "name");
                if (!string.IsNullOrEmpty(name)) names.Add(name);
            }
            return names;
        }
    }
}
