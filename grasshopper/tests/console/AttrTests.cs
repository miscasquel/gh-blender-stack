// Layer C, attribute half: the store that carries attributes on geometry.
//
//     GhStackAttrTests.exe
//
// Rhino's ArchivableDictionary is a managed collection, so this runs under wine
// without a Rhino process (built by grasshopper/build/build.sh, which references
// RhinoCommon.dll for the types). The codec fixture tests stay Rhino-free.
//
// The checks pin what a live canvas depends on:
//   1. two attributes of different types keep their own types and values
//      (the live failure report: "test" Float and "test2" Boolean both came
//      back as Boolean),
//   2. every wire type survives attach -> collect -> attach unchanged,
//   3. STRING keeps its offsets, and names that share a prefix stay distinct,
//   4. the labels exposed to the graph match Blender's own vocabulary.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using GhStack.Core;
using Rhino.Collections;

namespace GhStack.Tests.Console
{
    public static class AttrTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            System.Console.WriteLine("attribute store");

            CheckMixedTypesKeepTheirTypes();
            CheckPrefixNamesStayDistinct();
            CheckEveryTypeRoundTrips();
            CheckLabelsMatchBlender();
            CheckDefinitionAttributes();
            CheckWireTransforms();

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: attribute store PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: attribute store FAIL (" + _failures + ")");
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

        // --------------------------------------------------------------- checks

        private static void CheckMixedTypesKeepTheirTypes()
        {
            var sections = new List<Section>();
            var descriptors = new List<Dictionary<string, object>>();
            AddAttribute(sections, descriptors, "mesh", "test", "POINT", "FLOAT",
                Section.Floats("attr.0.values", new float[] { 1.5f, 2.5f, 3.5f }));
            AddAttribute(sections, descriptors, "mesh", "test2", "POINT", "BOOLEAN",
                Section.Bytes("attr.1.values", new byte[] { 1, 0, 1 }));

            var root = new ArchivableDictionary();
            AttributeStore.Attach(root, "mesh", descriptors, sections);

            string type, domain;
            object values;
            bool gotTest = AttributeStore.TryGet(root, "test", out type, out domain, out values);
            Check("mixed: test reads back", gotTest && type == "FLOAT" && values is double[],
                gotTest ? type + "/" + ValuesName(values) : "missing");
            bool gotTest2 = AttributeStore.TryGet(root, "test2", out type, out domain, out values);
            Check("mixed: test2 reads back", gotTest2 && type == "BOOLEAN" && values is bool[],
                gotTest2 ? type + "/" + ValuesName(values) : "missing");

            var outSections = new List<Section>();
            var outDescriptors = new List<Dictionary<string, object>>();
            AttributeStore.Collect(root, "mesh", outSections, outDescriptors);
            Check("mixed: collect keeps order and types",
                outDescriptors.Count == 2
                && (string)outDescriptors[0]["type"] == "FLOAT"
                && (string)outDescriptors[1]["type"] == "BOOLEAN",
                Describe(outDescriptors));
        }

        private static void CheckPrefixNamesStayDistinct()
        {
            var root = new ArchivableDictionary();
            AttributeStore.Set(root, "test", "FLOAT", "POINT", new double[] { 1.0 });
            AttributeStore.Set(root, "test2", "BOOLEAN", "FACE", new bool[] { true });
            string type, domain;
            object values;
            AttributeStore.TryGet(root, "test", out type, out domain, out values);
            Check("prefix names: test is Float", type == "FLOAT" && values is double[],
                type + "/" + ValuesName(values));
            AttributeStore.TryGet(root, "test2", out type, out domain, out values);
            Check("prefix names: test2 is Boolean", type == "BOOLEAN" && values is bool[],
                type + "/" + ValuesName(values));
        }

        private static void CheckEveryTypeRoundTrips()
        {
            // One attribute per wire type, attached from synthetic sections and
            // collected back; the sections must come out byte-identical.
            var cases = new List<object[]>();
            cases.Add(new object[] { "FLOAT", "POINT", 1, Section.Floats("v", new float[] { 1.5f, -2.25f }) });
            cases.Add(new object[] { "INT", "POINT", 1, Section.Ints("v", new int[] { -3, 7 }) });
            cases.Add(new object[] { "BOOLEAN", "FACE", 1, Section.Bytes("v", new byte[] { 1, 0 }) });
            cases.Add(new object[] { "FLOAT_VECTOR", "POINT", 3, Section.Floats("v", new float[] { 1, 2, 3, 4, 5, 6 }) });
            cases.Add(new object[] { "FLOAT2", "CORNER", 2, Section.Floats("v", new float[] { 0, 1, 2, 3 }) });
            cases.Add(new object[] { "FLOAT4", "POINT", 4, Section.Floats("v", new float[] { 1, 2, 3, 4 }) });
            cases.Add(new object[] { "FLOAT_COLOR", "POINT", 4, Section.Floats("v", new float[] { 1, 0, 0, 1, 0, 1, 0, 1 }) });
            cases.Add(new object[] { "BYTE_COLOR", "CORNER", 4, Section.Bytes("v", new byte[] { 255, 0, 0, 255 }) });
            cases.Add(new object[] { "QUATERNION", "POINT", 4, Section.Floats("v", new float[] { 1, 0, 0, 0 }) });
            cases.Add(new object[] { "FLOAT4X4", "POINT", 16, Section.Floats("v", new float[] { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1 }) });
            cases.Add(new object[] { "INT8", "POINT", 1, Section.Ints("v", new int[] { -1, 5 }) });
            cases.Add(new object[] { "INT16_2D", "FACE", 2, Section.Ints("v", new int[] { 1, 2, 3, 4 }) });
            cases.Add(new object[] { "INT32_2D", "FACE", 2, Section.Ints("v", new int[] { 5, 6 }) });

            var sections = new List<Section>();
            var descriptors = new List<Dictionary<string, object>>();
            foreach (object[] c in cases)
            {
                string type = (string)c[0];
                Section values = (Section)c[3];
                AddAttribute(sections, descriptors, "mesh", "attr_" + type, (string)c[1], type,
                    new Section("attr." + descriptors.Count + ".values", values.Dtype, values.Raw));
            }
            // STRING has its two-section shape.
            var tags = new string[] { "north", "south" };
            var bytes = new List<byte>();
            var lengths = new List<uint>();
            foreach (string tag in tags)
            {
                bytes.AddRange(System.Text.Encoding.UTF8.GetBytes(tag));
                lengths.Add((uint)System.Text.Encoding.UTF8.GetByteCount(tag));
            }
            int index = descriptors.Count;
            sections.Add(Section.Bytes("attr." + index + ".values", bytes.ToArray()));
            sections.Add(Section.U32("attr." + index + ".offsets", lengths.ToArray()));
            var stringEntry = new Dictionary<string, object>();
            stringEntry["group"] = "mesh";
            stringEntry["name"] = "attr_STRING";
            stringEntry["domain"] = "POINT";
            stringEntry["type"] = "STRING";
            stringEntry["components"] = 1L;
            stringEntry["count"] = (long)tags.Length;
            stringEntry["section"] = "attr." + index + ".values";
            stringEntry["offsets"] = "attr." + index + ".offsets";
            descriptors.Add(stringEntry);

            var root = new ArchivableDictionary();
            AttributeStore.Attach(root, "mesh", descriptors, sections);
            var outSections = new List<Section>();
            var outDescriptors = new List<Dictionary<string, object>>();
            AttributeStore.Collect(root, "mesh", outSections, outDescriptors);

            bool allTypes = outDescriptors.Count == descriptors.Count;
            string detail = "collected " + outDescriptors.Count + " of " + descriptors.Count;
            for (int i = 0; allTypes && i < descriptors.Count; i++)
            {
                if ((string)outDescriptors[i]["name"] != (string)descriptors[i]["name"]
                    || (string)outDescriptors[i]["type"] != (string)descriptors[i]["type"])
                {
                    allTypes = false;
                    detail = (string)outDescriptors[i]["name"] + " is "
                        + (string)outDescriptors[i]["type"] + ", expected "
                        + (string)descriptors[i]["name"] + " / " + (string)descriptors[i]["type"];
                }
            }
            Check("every type: names and types survive", allTypes, detail);

            // Byte-exact values for every collected section.
            string firstDiff = null;
            foreach (Section input in sections)
            {
                Section output = Find(outSections, input.Name);
                if (output == null) { firstDiff = input.Name + " missing"; break; }
                if (output.Raw.Length != input.Raw.Length) { firstDiff = input.Name + " length"; break; }
                for (int i = 0; i < input.Raw.Length; i++)
                {
                    if (output.Raw[i] != input.Raw[i]) { firstDiff = input.Name + " byte " + i; break; }
                }
                if (firstDiff != null) break;
            }
            Check("every type: section bytes survive", firstDiff == null, firstDiff ?? "");
        }

        private static void CheckLabelsMatchBlender()
        {
            string[][] wanted = new string[][]
            {
                new string[] { "FLOAT", "Float" },
                new string[] { "INT", "Integer" },
                new string[] { "BOOLEAN", "Boolean" },
                new string[] { "FLOAT_VECTOR", "Vector" },
                new string[] { "FLOAT2", "2D Vector" },
                new string[] { "FLOAT4", "4D Vector" },
                new string[] { "FLOAT_COLOR", "Color" },
                new string[] { "BYTE_COLOR", "Byte Color" },
                new string[] { "QUATERNION", "Quaternion" },
                new string[] { "FLOAT4X4", "4x4 Matrix" },
                new string[] { "INT8", "8-Bit Integer" },
                new string[] { "INT16_2D", "2D Integer Vector" },
                new string[] { "INT32_2D", "2D Integer Vector" },
            };
            string first = null;
            foreach (string[] pair in wanted)
            {
                string got = AttributeStore.Label(pair[0]);
                if (got != pair[1] && first == null)
                    first = pair[0] + " -> " + got + ", expected " + pair[1];
            }
            Check("labels match Blender's vocabulary", first == null, first ?? "");

            string[][] domains = new string[][]
            {
                new string[] { "POINT", "Point" },
                new string[] { "EDGE", "Edge" },
                new string[] { "FACE", "Face" },
                new string[] { "CORNER", "Corner" },
                new string[] { "CURVE", "Curve" },
                new string[] { "INSTANCE", "Instance" },
            };
            string firstDomain = null;
            foreach (string[] pair in domains)
            {
                string got = AttributeStore.DomainLabel(pair[0]);
                if (got != pair[1] && firstDomain == null)
                    firstDomain = pair[0] + " -> " + got + ", expected " + pair[1];
            }
            Check("domains read as words", firstDomain == null, firstDomain ?? "");
        }

        private static void CheckDefinitionAttributes()
        {
            // One base attribute and one definition-0 attribute, attached to the
            // right roots only (protocol §5.2).
            var sections = new List<Section>();
            var descriptors = new List<Dictionary<string, object>>();
            AddAttribute(sections, descriptors, "mesh", "base_load", "POINT", "FLOAT",
                Section.Floats("attr.0.values", new float[] { 1.0f }));
            AddAttribute(sections, descriptors, "mesh", "def_load", "POINT", "FLOAT",
                Section.Floats("attr.1.values", new float[] { 2.0f }));
            descriptors[1]["definition"] = (long)0;
            descriptors[1]["section"] = "idef.0.attr.1.values";
            sections.Add(new Section("idef.0.attr.1.values", "f32", sections[1].Raw));
            sections.RemoveAt(1);

            var baseRoot = new ArchivableDictionary();
            AttributeStore.Attach(baseRoot, "mesh", descriptors, sections);
            string type, domain;
            object values;
            Check("definition: base root skips definition descriptors",
                AttributeStore.TryGet(baseRoot, "base_load", out type, out domain, out values)
                && !AttributeStore.TryGet(baseRoot, "def_load", out type, out domain, out values),
                DescribeRoot(baseRoot));

            var definitionRoot = new ArchivableDictionary();
            AttributeStore.Attach(definitionRoot, "mesh", descriptors, sections, 0);
            Check("definition: definition root takes its own descriptors",
                AttributeStore.TryGet(definitionRoot, "def_load", out type, out domain, out values)
                && !AttributeStore.TryGet(definitionRoot, "base_load", out type, out domain, out values),
                DescribeRoot(definitionRoot));

            // Collecting from a definition geometry namespaces the sections and
            // marks the descriptor.
            var outSections = new List<Section>();
            var outDescriptors = new List<Dictionary<string, object>>();
            AttributeStore.Collect(definitionRoot, "mesh", outSections, outDescriptors, "idef.0.", 0);
            Section prefixed = Find(outSections, "idef.0.attr.0.values");
            Check("definition: collect prefixes the section and marks the descriptor",
                prefixed != null && prefixed.Raw.Length == 4 && outDescriptors.Count == 1
                && Json.GetLong(outDescriptors[0], "definition") == 0
                && (string)outDescriptors[0]["section"] == "idef.0.attr.0.values",
                Describe(outDescriptors));
        }

        private static void CheckWireTransforms()
        {
            // Row-major wire, translation in the fourth column (protocol §5.2).
            var m = new float[]
            {
                1, 0, 0, 5,
                0, 1, 0, 4,
                0, 0, 1, 3,
                0, 0, 0, 1,
            };
            Rhino.Geometry.Transform t = Conversion.TransformFromWire(m);
            Rhino.Geometry.Point3d moved = t * new Rhino.Geometry.Point3d(1, 2, 3);
            Check("transforms: wire translation applies",
                Math.Abs(moved.X - 6) < 1e-6 && Math.Abs(moved.Y - 6) < 1e-6 && Math.Abs(moved.Z - 6) < 1e-6,
                moved.ToString());

            float[] back = Conversion.TransformToWire(t);
            bool same = back.Length == 16;
            for (int i = 0; same && i < 16; i++)
                same = Math.Abs(back[i] - m[i]) < 1e-6f;
            Check("transforms: round trip is byte-stable", same, string.Join(", ", Array.ConvertAll(back, v => v.ToString())));

            var prefixed = Conversion.PrefixSections(new List<Section>
            {
                Section.Floats("mesh.verts", new float[] { 1, 2, 3 }),
            }, "idef.2.");
            Check("transforms: prefixing keeps dtype and bytes",
                prefixed.Count == 1 && prefixed[0].Name == "idef.2.mesh.verts"
                && prefixed[0].Dtype == "f32" && prefixed[0].Raw.Length == 12,
                prefixed.Count == 1 ? prefixed[0].Name : "none");
        }

        private static string DescribeRoot(ArchivableDictionary root)
        {
            return string.Join(", ", AttributeStore.Names(root));
        }

        // -------------------------------------------------------------- helpers

        /// <summary>One descriptor plus the section it names, exactly as the
        /// wire builds them.</summary>
        private static void AddAttribute(List<Section> sections, List<Dictionary<string, object>> descriptors,
                                         string group, string name, string domain, string type, Section values)
        {
            int index = descriptors.Count;
            string section = "attr." + index + ".values";
            if (values != null)
                sections.Add(new Section(section, values.Dtype, values.Raw));
            var entry = new Dictionary<string, object>();
            entry["group"] = group;
            entry["name"] = name;
            entry["domain"] = domain;
            entry["type"] = type;
            entry["components"] = (long)Components(type);
            int raw = values == null ? 0 : values.Raw.Length;
            entry["count"] = (long)Count(type, raw);
            entry["section"] = section;
            descriptors.Add(entry);
        }

        private static int Components(string type)
        {
            switch (type)
            {
                case "FLOAT_VECTOR": return 3;
                case "FLOAT2": return 2;
                case "FLOAT4": return 4;
                case "FLOAT_COLOR": return 4;
                case "BYTE_COLOR": return 4;
                case "QUATERNION": return 4;
                case "FLOAT4X4": return 16;
                case "INT16_2D": return 2;
                case "INT32_2D": return 2;
                default: return 1;
            }
        }

        private static int Count(string type, int rawBytes)
        {
            int size = (type == "BOOLEAN" || type == "BYTE_COLOR" || type == "STRING") ? 1 : 4;
            return rawBytes / size / Components(type);
        }

        private static string ValuesName(object values)
        {
            return values == null ? "null" : values.GetType().Name;
        }

        private static string Describe(List<Dictionary<string, object>> descriptors)
        {
            var parts = new List<string>();
            foreach (Dictionary<string, object> d in descriptors)
                parts.Add((string)d["name"] + ":" + (string)d["type"]);
            return string.Join(", ", parts.ToArray());
        }

        private static Section Find(List<Section> sections, string name)
        {
            foreach (Section section in sections)
                if (section.Name == name) return section;
            return null;
        }
    }
}
