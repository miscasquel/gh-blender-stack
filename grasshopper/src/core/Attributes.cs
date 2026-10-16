// Attributes ride on the geometry, natively, on both platforms.
//
// Blender stores named typed arrays on its datablocks, so Rhino's twin is the
// geometry's own UserDictionary (ArchivableDictionary): it is part of the
// geometry, it is duplicated and transformed with it, and it serializes into
// the document. This file is the one place that maps the wire's attribute
// vocabulary (Blender's data types) onto dictionary values, for both directions
// and for the Attribute Get/Set components.
//
// Wire shape (protocol §5): one `attr.<i>.values` section per attribute plus a
// header descriptor carrying name, domain, type and shape. STRING adds an
// `attr.<i>.offsets` section with each string's byte length.
//
// Compound values are stored as flat arrays of their elements, so the dictionary
// holds only array types it can serialize natively: double[], int[], bool[],
// byte[] and string[]. `components` says how many entries make one element.
//
// The mechanism lives on the root ArchivableDictionary, not on GeometryBase:
// that is the part worth testing, and it runs under the console suite without
// Rhino's native half (the geometry methods below are thin wrappers).

using System;
using System.Collections.Generic;
using System.Text;
using Rhino.Collections;
using Rhino.Geometry;

namespace GhStack.Core
{
    public static class AttributeStore
    {
        public const string RootKey = "GHStack.Attributes";

        // Blender data type -> [storage kind, components per element].
        private static readonly Dictionary<string, string> KindOf = new Dictionary<string, string>
        {
            { "FLOAT", "double" },
            { "INT", "int" },
            { "BOOLEAN", "bool" },
            { "FLOAT_VECTOR", "double" },
            { "FLOAT2", "double" },
            { "FLOAT4", "double" },
            { "FLOAT_COLOR", "double" },
            { "BYTE_COLOR", "byte" },
            { "QUATERNION", "double" },
            { "FLOAT4X4", "double" },
            { "INT8", "int" },
            { "INT16_2D", "int" },
            { "INT32_2D", "int" },
            { "STRING", "string" },
        };

        private static readonly Dictionary<string, int> ComponentsOf = new Dictionary<string, int>
        {
            { "FLOAT", 1 }, { "INT", 1 }, { "BOOLEAN", 1 },
            { "FLOAT_VECTOR", 3 }, { "FLOAT2", 2 }, { "FLOAT4", 4 },
            { "FLOAT_COLOR", 4 }, { "BYTE_COLOR", 4 }, { "QUATERNION", 4 },
            { "FLOAT4X4", 16 }, { "INT8", 1 }, { "INT16_2D", 2 }, { "INT32_2D", 2 },
            { "STRING", 1 },
        };

        // The names Blender's own UI uses for the data types (its "Store Named
        // Attribute" menu and the mesh Attributes panel). The wire keeps the
        // internal ids above; this is what the graph sees.
        private static readonly Dictionary<string, string> LabelOf = new Dictionary<string, string>
        {
            { "FLOAT", "Float" },
            { "INT", "Integer" },
            { "BOOLEAN", "Boolean" },
            { "FLOAT_VECTOR", "Vector" },
            { "FLOAT2", "2D Vector" },
            { "FLOAT4", "4D Vector" },
            { "FLOAT_COLOR", "Color" },
            { "BYTE_COLOR", "Byte Color" },
            { "QUATERNION", "Quaternion" },
            { "FLOAT4X4", "4x4 Matrix" },
            { "INT8", "8-Bit Integer" },
            { "INT16_2D", "2D Integer Vector" },
            { "INT32_2D", "2D Integer Vector" },
            { "STRING", "String" },
        };

        /// <summary>The Blender UI name of a wire type id ("FLOAT" -> "Float").</summary>
        public static string Label(string type)
        {
            string label;
            return LabelOf.TryGetValue(type ?? "", out label) ? label : type;
        }

        /// <summary>The domain as a display word ("POINT" -> "Point"). The wire
        /// and the dictionary keep the protocol's upper-case names.</summary>
        public static string DomainLabel(string domain)
        {
            if (string.IsNullOrEmpty(domain)) return "";
            return char.ToUpperInvariant(domain[0]) + domain.Substring(1).ToLowerInvariant();
        }

        /// <summary>The protocol pool a geometry object belongs to, or null when
        /// attributes cannot be attached to it.</summary>
        public static string GroupOf(GeometryBase geometry)
        {
            if (geometry is Mesh) return "mesh";
            if (geometry is SubD) return "subd";
            if (geometry is PointCloud) return "pts";
            return null;
        }

        // ------------------------------------------------------------- storage

        public static bool Has(ArchivableDictionary root)
        {
            return root != null && root.Count > 0;
        }

        public static string[] Names(ArchivableDictionary root)
        {
            return root == null ? new string[0] : root.Keys;
        }

        /// <summary>One attribute's metadata and flat values, or false.</summary>
        public static bool TryGet(ArchivableDictionary root, string name,
                                  out string type, out string domain, out object values)
        {
            type = null;
            domain = null;
            values = null;
            if (root == null) return false;
            ArchivableDictionary record;
            if (!root.TryGetDictionary(name, out record) || record == null) return false;
            type = record.GetString("type", "");
            domain = record.GetString("domain", "");
            return record.TryGetValue("values", out values) && type.Length > 0;
        }

        /// <summary>Store one attribute (values as a flat native array).</summary>
        public static void Set(ArchivableDictionary root, string name, string type, string domain, object values)
        {
            if (!KindOf.ContainsKey(type))
                throw new FrameException("bad_attribute", "unknown attribute type " + type);
            var record = new ArchivableDictionary();
            record.Set("type", type);
            record.Set("domain", domain ?? "");
            record.Set("components", ComponentsOf[type]);
            SetValues(record, values);
            root.Set(name, record);
        }

        private static void SetValues(ArchivableDictionary record, object values)
        {
            var doubles = values as double[];
            if (doubles != null) { record.Set("values", doubles); return; }
            var ints = values as int[];
            if (ints != null) { record.Set("values", ints); return; }
            var bools = values as bool[];
            if (bools != null) { record.Set("values", bools); return; }
            var bytes = values as byte[];
            if (bytes != null) { record.Set("values", bytes); return; }
            var strings = values as string[];
            if (strings != null) { record.Set("values", strings); return; }
            throw new FrameException("bad_attribute", "attribute values must be a flat array");
        }

        // ------------------------------------------------- wire -> dictionary

        /// <summary>Attach every descriptor of one group from a decoded frame.
        /// Descriptors naming sections the frame does not carry are skipped: a
        /// truncated frame must not take the stage down.</summary>
        public static void Attach(ArchivableDictionary root, string group,
                                  List<Dictionary<string, object>> descriptors, List<Section> sections)
        {
            if (root == null || descriptors == null || sections == null) return;
            foreach (Dictionary<string, object> descriptor in descriptors)
            {
                if (Json.GetString(descriptor, "group") != group) continue;
                string type = Json.GetString(descriptor, "type");
                string name = Json.GetString(descriptor, "name");
                string domain = Json.GetString(descriptor, "domain");
                if (string.IsNullOrEmpty(type) || string.IsNullOrEmpty(name) || !KindOf.ContainsKey(type))
                    continue;
                Section values = Find(sections, Json.GetString(descriptor, "section"));
                if (values == null) continue;

                object flat = Decode(type, values, Find(sections, Json.GetString(descriptor, "offsets")));
                if (flat != null)
                    Set(root, name, type, domain, flat);
            }
        }

        private static object Decode(string type, Section values, Section offsets)
        {
            switch (KindOf[type])
            {
                case "double":
                    {
                        float[] raw = values.ToFloats();
                        var target = new double[raw.Length];
                        for (int i = 0; i < raw.Length; i++) target[i] = raw[i];
                        return target;
                    }
                case "int":
                    return values.ToI32();
                case "bool":
                    {
                        byte[] raw = values.Raw;
                        var target = new bool[raw.Length];
                        for (int i = 0; i < raw.Length; i++) target[i] = raw[i] != 0;
                        return target;
                    }
                case "byte":
                    return values.Raw;
                default: // string
                    return SplitStrings(values.Raw, offsets);
            }
        }

        private static string[] SplitStrings(byte[] raw, Section offsets)
        {
            if (offsets == null) return new string[0];
            uint[] lengths = offsets.ToU32();
            var strings = new string[lengths.Length];
            int at = 0;
            for (int i = 0; i < lengths.Length; i++)
            {
                int length = (int)lengths[i];
                strings[i] = Encoding.UTF8.GetString(raw, at, Math.Min(length, raw.Length - at));
                at += length;
            }
            return strings;
        }

        // ------------------------------------------------- dictionary -> wire

        /// <summary>Append every attribute under a root as wire sections plus
        /// descriptors. The attribute index continues from the descriptor list,
        /// so several groups can be collected into one frame.</summary>
        public static void Collect(ArchivableDictionary root, string group,
                                   List<Section> sections, List<Dictionary<string, object>> descriptors)
        {
            if (root == null) return;
            foreach (string name in root.Keys)
            {
                ArchivableDictionary record;
                if (!root.TryGetDictionary(name, out record) || record == null) continue;
                string type = record.GetString("type", "");
                if (!KindOf.ContainsKey(type)) continue;
                object values;
                if (!record.TryGetValue("values", out values) || values == null) continue;
                int components = ComponentsOf[type];
                int count = ElementCount(values, components);
                int index = descriptors.Count;
                string sectionName = "attr." + index + ".values";

                var entry = new Dictionary<string, object>();
                entry["group"] = group;
                entry["name"] = name;
                entry["domain"] = record.GetString("domain", "");
                entry["type"] = type;
                entry["components"] = (long)components;
                entry["count"] = (long)count;
                entry["section"] = sectionName;

                string offsetsName = "attr." + index + ".offsets";
                Section valueSection = Encode(type, values, sectionName);
                if (valueSection == null) continue;
                sections.Add(valueSection);
                if (type == "STRING")
                {
                    var lengths = new List<uint>();
                    foreach (string item in (string[])values)
                        lengths.Add((uint)Encoding.UTF8.GetByteCount(item ?? ""));
                    sections.Add(Section.U32(offsetsName, lengths.ToArray()));
                    entry["offsets"] = offsetsName;
                }
                descriptors.Add(entry);
            }
        }

        private static Section Encode(string type, object values, string name)
        {
            switch (KindOf[type])
            {
                case "double":
                    {
                        double[] raw = (double[])values;
                        var target = new float[raw.Length];
                        for (int i = 0; i < raw.Length; i++) target[i] = (float)raw[i];
                        return Section.Floats(name, target);
                    }
                case "int":
                    return Section.Ints(name, (int[])values);
                case "bool":
                    {
                        bool[] raw = (bool[])values;
                        var target = new byte[raw.Length];
                        for (int i = 0; i < raw.Length; i++) target[i] = raw[i] ? (byte)1 : (byte)0;
                        return Section.Bytes(name, target);
                    }
                case "byte":
                    return Section.Bytes(name, (byte[])values);
                default: // string
                    {
                        var lines = (string[])values;
                        var bytes = new List<byte>();
                        foreach (string item in lines)
                            bytes.AddRange(Encoding.UTF8.GetBytes(item ?? ""));
                        return Section.Bytes(name, bytes.ToArray());
                    }
            }
        }

        private static int ElementCount(object values, int components)
        {
            if (values is double[]) return ((double[])values).Length / components;
            if (values is int[]) return ((int[])values).Length / components;
            if (values is bool[]) return ((bool[])values).Length / components;
            if (values is byte[]) return ((byte[])values).Length / components;
            if (values is string[]) return ((string[])values).Length;
            return 0;
        }

        private static Section Find(List<Section> sections, string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            foreach (Section section in sections)
                if (section.Name == name) return section;
            return null;
        }

        // ------------------------------------------------------- geometry glue

        private static ArchivableDictionary Root(GeometryBase geometry, bool create)
        {
            ArchivableDictionary root;
            if (geometry.UserDictionary.TryGetDictionary(RootKey, out root) && root != null)
                return root;
            if (!create) return null;
            root = new ArchivableDictionary();
            geometry.UserDictionary.Set(RootKey, root);
            return root;
        }

        public static void Attach(GeometryBase geometry, string group,
                                  List<Dictionary<string, object>> descriptors, List<Section> sections)
        {
            ArchivableDictionary existing = Root(geometry, false);
            if (existing == null && (descriptors == null || descriptors.Count == 0)) return;
            Attach(Root(geometry, true), group, descriptors, sections);
        }

        public static void Collect(GeometryBase geometry, string group,
                                   List<Section> sections, List<Dictionary<string, object>> descriptors)
        {
            Collect(Root(geometry, false), group, sections, descriptors);
        }

        public static bool TryGet(GeometryBase geometry, string name,
                                  out string type, out string domain, out object values)
        {
            return TryGet(Root(geometry, false), name, out type, out domain, out values);
        }

        public static void Set(GeometryBase geometry, string name, string type, string domain, object values)
        {
            Set(Root(geometry, true), name, type, domain, values);
        }

        public static string[] Names(GeometryBase geometry)
        {
            return Names(Root(geometry, false));
        }

        public static bool Has(GeometryBase geometry)
        {
            return Has(Root(geometry, false));
        }
    }
}
