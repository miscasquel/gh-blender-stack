// The graph side of attributes: read and write the values that travel with
// geometry. AttributeStore keeps them in the geometry's own UserDictionary, so
// Stage In fills them and Stage Out collects them with no wiring at all; these
// components are for using the values (and for writing new ones) inside a
// definition.
//
// The vocabulary is Blender's (protocol §5): FLOAT, INT, BOOLEAN, STRING,
// FLOAT_VECTOR, FLOAT_COLOR/BYTE_COLOR and the compound types. GH values map
// onto it by type: numbers, integers, booleans, texts, vectors/points, colours.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Types;
using GhStack.Core;
using Rhino.Geometry;

namespace GhStack
{
    /// <summary>Shared conversions between GH values and the attribute
    /// vocabulary, so Get and Set cannot drift apart.</summary>
    internal static class AttributeValues
    {
        /// <summary>The attribute-capable geometry of a goo, or null.</summary>
        internal static GeometryBase GeometryOf(IGH_Goo goo)
        {
            if (goo == null) return null;
            GH_Mesh mesh = goo as GH_Mesh;
            if (mesh != null) return mesh.Value;
            GH_SubD subd = goo as GH_SubD;
            if (subd != null) return subd.Value;
            GH_PointCloud cloud = goo as GH_PointCloud;
            if (cloud != null) return cloud.Value;
            GeometryBase geometry;
            return goo.CastTo<GeometryBase>(out geometry) ? geometry : null;
        }

        /// <summary>The attribute type a list of GH values should travel as, or
        /// null when the list is empty or the types are mixed.</summary>
        internal static string TypeOf(IList<IGH_Goo> values)
        {
            if (values == null || values.Count == 0) return null;
            string type = TypeOfValue(values[0]);
            if (type == null) return null;
            for (int i = 1; i < values.Count; i++)
                if (TypeOfValue(values[i]) != type) return null;
            return type;
        }

        private static string TypeOfValue(IGH_Goo goo)
        {
            if (goo is GH_Number) return "FLOAT";
            if (goo is GH_Integer) return "INT";
            if (goo is GH_Boolean) return "BOOLEAN";
            if (goo is GH_String) return "STRING";
            if (goo is GH_Vector || goo is GH_Point) return "FLOAT_VECTOR";
            if (goo is GH_Colour) return "FLOAT_COLOR";
            return null;
        }

        /// <summary>GH values as the flat native array AttributeStore keeps
        /// (double[], int[], bool[], byte[], string[]), or null on a mismatch.</summary>
        internal static object Flatten(string type, IList<IGH_Goo> values)
        {
            switch (type)
            {
                case "FLOAT":
                    {
                        var target = new double[values.Count];
                        for (int i = 0; i < values.Count; i++)
                        {
                            GH_Number number = values[i] as GH_Number;
                            if (number == null) return null;
                            target[i] = number.Value;
                        }
                        return target;
                    }
                case "INT":
                    {
                        var target = new int[values.Count];
                        for (int i = 0; i < values.Count; i++)
                        {
                            GH_Integer integer = values[i] as GH_Integer;
                            if (integer == null) return null;
                            target[i] = integer.Value;
                        }
                        return target;
                    }
                case "BOOLEAN":
                    {
                        var target = new bool[values.Count];
                        for (int i = 0; i < values.Count; i++)
                        {
                            GH_Boolean boolean = values[i] as GH_Boolean;
                            if (boolean == null) return null;
                            target[i] = boolean.Value;
                        }
                        return target;
                    }
                case "STRING":
                    {
                        var target = new string[values.Count];
                        for (int i = 0; i < values.Count; i++)
                        {
                            GH_String text = values[i] as GH_String;
                            if (text == null) return null;
                            target[i] = text.Value;
                        }
                        return target;
                    }
                case "FLOAT_VECTOR":
                    {
                        var target = new double[values.Count * 3];
                        for (int i = 0; i < values.Count; i++)
                        {
                            Vector3d vector;
                            GH_Vector vectorGoo = values[i] as GH_Vector;
                            GH_Point pointGoo = values[i] as GH_Point;
                            if (vectorGoo != null) vector = vectorGoo.Value;
                            else if (pointGoo != null) vector = new Vector3d(pointGoo.Value);
                            else return null;
                            target[i * 3] = vector.X;
                            target[i * 3 + 1] = vector.Y;
                            target[i * 3 + 2] = vector.Z;
                        }
                        return target;
                    }
                default: // FLOAT_COLOR
                    {
                        var target = new double[values.Count * 4];
                        for (int i = 0; i < values.Count; i++)
                        {
                            GH_Colour colour = values[i] as GH_Colour;
                            if (colour == null) return null;
                            Color c = colour.Value;
                            target[i * 4] = c.R / 255.0;
                            target[i * 4 + 1] = c.G / 255.0;
                            target[i * 4 + 2] = c.B / 255.0;
                            target[i * 4 + 3] = c.A / 255.0;
                        }
                        return target;
                    }
            }
        }

        /// <summary>The wire arrays as GH values. Scalar and colour/vector types
        /// come out as their native goos; compound types (quaternions, 4x4
        /// matrices, 2D pairs) come out flattened as numbers.</summary>
        internal static List<IGH_Goo> ToGoos(object raw, string type)
        {
            var result = new List<IGH_Goo>();
            var doubles = raw as double[];
            if (doubles != null)
            {
                if (type == "FLOAT_VECTOR" && doubles.Length % 3 == 0)
                {
                    for (int i = 0; i + 2 < doubles.Length; i += 3)
                        result.Add(new GH_Vector(new Vector3d(doubles[i], doubles[i + 1], doubles[i + 2])));
                    return result;
                }
                if (type == "FLOAT_COLOR" && doubles.Length % 4 == 0)
                {
                    for (int i = 0; i + 3 < doubles.Length; i += 4)
                        result.Add(new GH_Colour(ToColor(doubles[i], doubles[i + 1], doubles[i + 2], doubles[i + 3])));
                    return result;
                }
                foreach (double value in doubles) result.Add(new GH_Number(value));
                return result;
            }
            var ints = raw as int[];
            if (ints != null)
            {
                foreach (int value in ints) result.Add(new GH_Integer(value));
                return result;
            }
            var bools = raw as bool[];
            if (bools != null)
            {
                foreach (bool value in bools) result.Add(new GH_Boolean(value));
                return result;
            }
            var strings = raw as string[];
            if (strings != null)
            {
                foreach (string value in strings) result.Add(new GH_String(value));
                return result;
            }
            var bytes = raw as byte[];
            if (bytes != null && type == "BYTE_COLOR" && bytes.Length % 4 == 0)
            {
                for (int i = 0; i + 3 < bytes.Length; i += 4)
                    result.Add(new GH_Colour(Color.FromArgb(bytes[i + 3], bytes[i], bytes[i + 1], bytes[i + 2])));
            }
            return result;
        }

        private static Color ToColor(double r, double g, double b, double a)
        {
            return Color.FromArgb(Channel(a), Channel(r), Channel(g), Channel(b));
        }

        private static int Channel(double value)
        {
            return (int)Math.Round(Math.Min(1.0, Math.Max(0.0, value)) * 255.0);
        }

        /// <summary>The domains of the geometry whose element count equals
        /// ``count``. POINT, EDGE and FACE only: CORNER data is per face-corner,
        /// which a Rhino mesh does not index, so a corner attribute has to name
        /// its domain explicitly.</summary>
        internal static List<string> DomainsFor(GeometryBase geometry, int count)
        {
            var matches = new List<string>();
            Mesh mesh = geometry as Mesh;
            if (mesh != null)
            {
                if (count == mesh.Vertices.Count) matches.Add("POINT");
                if (count == mesh.TopologyEdges.Count) matches.Add("EDGE");
                if (count == mesh.Faces.Count) matches.Add("FACE");
                return matches;
            }
            SubD subd = geometry as SubD;
            if (subd != null)
            {
                if (count == subd.Vertices.Count) matches.Add("POINT");
                if (count == subd.Edges.Count) matches.Add("EDGE");
                if (count == subd.Faces.Count) matches.Add("FACE");
                return matches;
            }
            PointCloud cloud = geometry as PointCloud;
            if (cloud != null && count == cloud.Count) matches.Add("POINT");
            return matches;
        }
    }

    // ------------------------------------------------------------------- Get

    public class AttributeGetComponent : GH_Component
    {
        public AttributeGetComponent()
            : base("Attribute Get", "Attr Get",
                   "Reads one attribute of a geometry. Attributes travel with the geometry: Stage In attaches what Blender sent, and this reads them as values.",
                   "GH Stack", "Attributes")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("e5b4c3d2-6f70-4b81-9d2e-1f2a3b4c5d67"); }
        }

        protected override System.Drawing.Bitmap Icon
        {
            get { return Icons.Get("driver"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddGenericParameter("Geometry", "G", "Geometry carrying attributes", GH_ParamAccess.item);
            p.AddTextParameter("Name", "N", "Attribute name", GH_ParamAccess.item);
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddGenericParameter("Values", "V", "The attribute's values, one item per element (compound types come out flattened)", GH_ParamAccess.list);
            p.AddTextParameter("Type", "T", "Attribute type (FLOAT, INT, BOOLEAN, STRING, FLOAT_VECTOR, FLOAT_COLOR, ...)", GH_ParamAccess.item);
            p.AddTextParameter("Domain", "D", "Attribute domain (POINT, EDGE, FACE, CORNER, ...)", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            IGH_Goo goo = null;
            string name = "";
            da.GetData("Geometry", ref goo);
            da.GetData("Name", ref name);

            var values = new List<IGH_Goo>();
            string type = "";
            string domain = "";
            GeometryBase geometry = AttributeValues.GeometryOf(goo);
            if (geometry == null)
            {
                AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "geometry cannot carry attributes");
            }
            else if (string.IsNullOrEmpty(name))
            {
                AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "give the attribute a name");
            }
            else
            {
                object raw;
                if (AttributeStore.TryGet(geometry, name, out type, out domain, out raw))
                    values = AttributeValues.ToGoos(raw, type);
                else
                    AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "no attribute named " + name);
            }
            da.SetDataList("Values", values);
            da.SetData("Type", type);
            da.SetData("Domain", domain);
        }
    }

    // ------------------------------------------------------------------- Set

    public class AttributeSetComponent : GH_Component
    {
        public AttributeSetComponent()
            : base("Attribute Set", "Attr Set",
                   "Attaches an attribute to a geometry. The attribute rides on the geometry, so Stage Out sends it to Blender.",
                   "GH Stack", "Attributes")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("f6c5d4e3-7081-4c92-ae3f-2a3b4c5d6e78"); }
        }

        protected override System.Drawing.Bitmap Icon
        {
            get { return Icons.Get("import"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddGenericParameter("Geometry", "G", "Geometry to attach the attribute to", GH_ParamAccess.item);
            p.AddTextParameter("Name", "N", "Attribute name", GH_ParamAccess.item);
            p.AddGenericParameter("Values", "V", "One value per element: numbers, integers, booleans, texts, vectors/points or colours", GH_ParamAccess.list);
            p.AddTextParameter("Domain", "D", "POINT, EDGE, FACE or CORNER. Empty infers it from the value count.", GH_ParamAccess.item);
            p[3].Optional = true;
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddGenericParameter("Geometry", "G", "The geometry, with the attribute attached", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            IGH_Goo goo = null;
            string name = "";
            var values = new List<IGH_Goo>();
            string domain = "";
            da.GetData("Geometry", ref goo);
            da.GetData("Name", ref name);
            da.GetDataList("Values", values);
            da.GetData("Domain", ref domain);

            GeometryBase geometry = AttributeValues.GeometryOf(goo);
            if (geometry == null || string.IsNullOrEmpty(name) || values.Count == 0)
            {
                if (goo != null && geometry == null)
                    AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "geometry cannot carry attributes");
                else if (string.IsNullOrEmpty(name))
                    AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "give the attribute a name");
                else if (values.Count == 0)
                    AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "give the attribute values");
                da.SetData("Geometry", goo);
                return;
            }

            string type = AttributeValues.TypeOf(values);
            object flat = type == null ? null : AttributeValues.Flatten(type, values);
            if (flat == null)
            {
                AddRuntimeMessage(GH_RuntimeMessageLevel.Warning,
                    "values must all be numbers, integers, booleans, texts, vectors or colours");
                da.SetData("Geometry", goo);
                return;
            }

            if (string.IsNullOrEmpty(domain))
            {
                // Keep an existing attribute's domain when the name is reused,
                // otherwise infer it from the value count.
                string existingType, existingDomain;
                object existingValues;
                if (AttributeStore.TryGet(geometry, name, out existingType, out existingDomain, out existingValues)
                    && !string.IsNullOrEmpty(existingDomain))
                {
                    domain = existingDomain;
                }
                else
                {
                    List<string> matches = AttributeValues.DomainsFor(geometry, values.Count);
                    if (matches.Count == 1)
                    {
                        domain = matches[0];
                    }
                    else if (matches.Count == 0)
                    {
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Warning,
                            values.Count + " values match no domain of this geometry; set Domain explicitly");
                        da.SetData("Geometry", goo);
                        return;
                    }
                    else
                    {
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Warning,
                            "the count matches " + string.Join("/", matches.ToArray())
                            + "; set Domain explicitly");
                        da.SetData("Geometry", goo);
                        return;
                    }
                }
            }

            AttributeStore.Set(geometry, name, type, domain.ToUpperInvariant(), flat);
            da.SetData("Geometry", goo);
        }
    }
}
