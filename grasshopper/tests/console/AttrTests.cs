// Layer C, attribute half: the store that carries attributes on geometry.
//
//     StageloomAttrTests.exe
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
using Stageloom.Core;
using Rhino.Collections;

namespace Stageloom.Tests.Console
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
            CheckLegacyRootKey();
            CheckLabelsMatchBlender();
            CheckDefinitionAttributes();
            CheckCurveAttributes();
            CheckWireTransforms();
            CheckMalformedStringOffsets();
            CheckDescriptorValidation();
            CheckDtypeValidation();
            CheckShortValues();
            CheckCurveValidation();
            CheckCollectValidation();
            CheckHasIgnoresNonAttributes();

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

        /// <summary>Geometry saved before the rebrand stores attributes under
        /// this literal. Renaming the constant would silently hide them.</summary>
        private static void CheckLegacyRootKey()
        {
            Check("legacy: the persisted root key is the pre-rebrand literal",
                AttributeStore.RootKey == "GHStack.Attributes", AttributeStore.RootKey);
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
                new string[] { "CURVE_POINT", "Curve Point" },
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

        private static void CheckCurveAttributes()
        {
            var roots = new List<ArchivableDictionary>
            {
                new ArchivableDictionary(),
                new ArchivableDictionary(),
            };

            var sections = new List<Section>();
            var descriptors = new List<Dictionary<string, object>>();
            AddAttribute(sections, descriptors, "crv", "class", "CURVE", "BOOLEAN",
                Section.Bytes("unused", new byte[] { 1, 0 }));
            AddAttribute(sections, descriptors, "crv", "pidx", "POINT", "INT",
                Section.Ints("unused", new int[] { 10, 11, 12, 20, 21 }));

            AttributeStore.AttachCurves(roots, new uint[] { 3, 2 }, descriptors, sections);

            string type, domain;
            object values;
            bool gotClass0 = AttributeStore.TryGet(roots[0], "class", out type, out domain, out values);
            Check("curve: per-curve value is one item",
                gotClass0 && type == "BOOLEAN" && domain == "CURVE"
                && values is bool[] && ((bool[])values).Length == 1 && ((bool[])values)[0],
                gotClass0 ? type + "/" + domain + "/" + ValuesName(values) : "missing");
            bool gotClass1 = AttributeStore.TryGet(roots[1], "class", out type, out domain, out values);
            Check("curve: second per-curve value is separate",
                gotClass1 && values is bool[] && ((bool[])values).Length == 1 && !((bool[])values)[0],
                gotClass1 ? ValuesName(values) : "missing");

            bool gotPoints0 = AttributeStore.TryGet(roots[0], "pidx", out type, out domain, out values);
            Check("curve: per-point values are one list per curve",
                gotPoints0 && domain == "CURVE_POINT" && values is int[]
                && string.Join(",", Array.ConvertAll((int[])values, v => v.ToString())) == "10,11,12",
                gotPoints0 ? ValuesName(values) : "missing");
            bool gotPoints1 = AttributeStore.TryGet(roots[1], "pidx", out type, out domain, out values);
            Check("curve: second per-point list is sliced by crv.counts",
                gotPoints1 && values is int[]
                && string.Join(",", Array.ConvertAll((int[])values, v => v.ToString())) == "20,21",
                gotPoints1 ? ValuesName(values) : "missing");
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

        private static void CheckMalformedStringOffsets()
        {
            // A malformed STRING is skipped with a note; a valid attribute in
            // the same frame still attaches. Neither overlong nor sum-mismatched
            // offsets may silently produce a short string.
            var sections = new List<Section>();
            var descriptors = new List<Dictionary<string, object>>();
            AddAttribute(sections, descriptors, "mesh", "weight", "POINT", "FLOAT",
                Section.Floats("attr.0.values", new float[] { 1.5f }));

            AddStringDescriptor(sections, descriptors, "short", new uint[] { 4 },
                System.Text.Encoding.UTF8.GetBytes("abc"));
            AddStringDescriptor(sections, descriptors, "good", new uint[] { 2, 2 },
                System.Text.Encoding.UTF8.GetBytes("abcd"));

            var notes = new List<string>();
            var root = new ArchivableDictionary();
            AttributeStore.Attach(root, "mesh", descriptors, sections, null,
                delegate(string note) { notes.Add(note); });

            string type, domain;
            object values;
            Check("malformed string is skipped with a note",
                notes.Count == 1 && notes[0].IndexOf("runs past", StringComparison.Ordinal) >= 0,
                string.Join(" | ", notes));
            Check("malformed string did not attach",
                !AttributeStore.TryGet(root, "short", out type, out domain, out values), DescribeRoot(root));
            Check("valid neighbours still attach",
                AttributeStore.TryGet(root, "weight", out type, out domain, out values)
                && AttributeStore.TryGet(root, "good", out type, out domain, out values),
                DescribeRoot(root));

            // A uint length above int.MaxValue must be rejected, not cast negative.
            var hugeSections = new List<Section>();
            var hugeDescriptors = new List<Dictionary<string, object>>();
            AddStringDescriptor(hugeSections, hugeDescriptors, "huge",
                new uint[] { uint.MaxValue }, System.Text.Encoding.UTF8.GetBytes("abc"));
            notes.Clear();
            var hugeRoot = new ArchivableDictionary();
            AttributeStore.Attach(hugeRoot, "mesh", hugeDescriptors, hugeSections, null,
                delegate(string note) { notes.Add(note); });
            Check("a length above int.MaxValue is rejected",
                notes.Count == 1 && notes[0].IndexOf("int.MaxValue", StringComparison.Ordinal) >= 0,
                string.Join(" | ", notes));

            // The offsets must cover the value bytes exactly.
            var sumSections = new List<Section>();
            var sumDescriptors = new List<Dictionary<string, object>>();
            AddStringDescriptor(sumSections, sumDescriptors, "sum", new uint[] { 2 },
                System.Text.Encoding.UTF8.GetBytes("abcd"));
            notes.Clear();
            var sumRoot = new ArchivableDictionary();
            AttributeStore.Attach(sumRoot, "mesh", sumDescriptors, sumSections, null,
                delegate(string note) { notes.Add(note); });
            Check("offsets must cover every value byte",
                notes.Count == 1 && notes[0].IndexOf("offsets cover", StringComparison.Ordinal) >= 0,
                string.Join(" | ", notes));

            // Invalid UTF-8 must not become U+FFFD silently.
            var utfSections = new List<Section>();
            var utfDescriptors = new List<Dictionary<string, object>>();
            AddStringDescriptor(utfSections, utfDescriptors, "bad_utf", new uint[] { 2 },
                new byte[] { 0xC3, 0x28 });
            notes.Clear();
            var utfRoot = new ArchivableDictionary();
            AttributeStore.Attach(utfRoot, "mesh", utfDescriptors, utfSections, null,
                delegate(string note) { notes.Add(note); });
            Check("invalid UTF-8 is rejected",
                notes.Count == 1 && notes[0].IndexOf("UTF-8", StringComparison.Ordinal) >= 0,
                string.Join(" | ", notes));

            // Values without offsets are not zero strings.
            var noOffsetSections = new List<Section>();
            var noOffsetDescriptors = new List<Dictionary<string, object>>();
            var noOffsets = new Dictionary<string, object>();
            noOffsets["group"] = "mesh";
            noOffsets["name"] = "no_offsets";
            noOffsets["domain"] = "POINT";
            noOffsets["type"] = "STRING";
            noOffsets["components"] = 1L;
            noOffsets["count"] = 1L;
            noOffsets["section"] = "noff.values";
            noOffsetSections.Add(Section.Bytes("noff.values",
                System.Text.Encoding.UTF8.GetBytes("abc")));
            noOffsetDescriptors.Add(noOffsets);
            notes.Clear();
            var noOffsetRoot = new ArchivableDictionary();
            AttributeStore.Attach(noOffsetRoot, "mesh", noOffsetDescriptors, noOffsetSections, null,
                delegate(string note) { notes.Add(note); });
            Check("string without offsets is rejected",
                notes.Count == 1 && notes[0].IndexOf("no offsets", StringComparison.Ordinal) >= 0,
                string.Join(" | ", notes));
        }

        private static void CheckDescriptorValidation()
        {
            List<Section> sections;
            Dictionary<string, object> good = BaseFloatDescriptor(out sections);

            Dictionary<string, object> d = CopyDescriptor(good);
            d["group"] = 5L;
            ExpectSkipped("descriptor: numeric group is skipped", d, sections, "group");
            d = CopyDescriptor(good);
            d["group"] = new List<object> { "mesh" };
            ExpectSkipped("descriptor: array group is skipped", d, sections, "group");
            d = CopyDescriptor(good);
            d["group"] = null;
            ExpectSkipped("descriptor: null group is skipped", d, sections, "group");

            d = CopyDescriptor(good);
            d["type"] = 5L;
            ExpectSkipped("descriptor: numeric type is skipped", d, sections, "type");
            d = CopyDescriptor(good);
            d["type"] = new List<object> { "FLOAT" };
            ExpectSkipped("descriptor: array type is skipped", d, sections, "type");
            d = CopyDescriptor(good);
            d["type"] = null;
            ExpectSkipped("descriptor: null type is skipped", d, sections, "type");
            d = CopyDescriptor(good);
            d["type"] = "NOPE";
            ExpectSkipped("descriptor: unknown type is skipped", d, sections, "unknown attribute type");
            d = CopyDescriptor(good);
            d["name"] = 5L;
            ExpectSkipped("descriptor: numeric name is skipped", d, sections, "name");
            d = CopyDescriptor(good);
            d["name"] = new List<object>();
            ExpectSkipped("descriptor: array name is skipped", d, sections, "name");
            d = CopyDescriptor(good);
            d["name"] = null;
            ExpectSkipped("descriptor: null name is skipped", d, sections, "name");
            d = CopyDescriptor(good);
            d["name"] = "";
            ExpectSkipped("descriptor: empty name is skipped", d, sections, "name");

            d = CopyDescriptor(good);
            d["domain"] = 5L;
            ExpectSkipped("descriptor: numeric domain is skipped", d, sections, "domain");
            d = CopyDescriptor(good);
            d["domain"] = new List<object> { "POINT" };
            ExpectSkipped("descriptor: array domain is skipped", d, sections, "domain");
            d = CopyDescriptor(good);
            d["domain"] = null;
            ExpectSkipped("descriptor: null domain is skipped", d, sections, "domain");
            d = CopyDescriptor(good);
            d["domain"] = "INSTANCE";
            ExpectSkipped("descriptor: unknown domain is skipped", d, sections, "domain");

            d = CopyDescriptor(good);
            d["section"] = 5L;
            ExpectSkipped("descriptor: numeric section is skipped", d, sections, "section");
            d = CopyDescriptor(good);
            d["section"] = new List<object>();
            ExpectSkipped("descriptor: array section is skipped", d, sections, "section");
            d = CopyDescriptor(good);
            d["section"] = null;
            ExpectSkipped("descriptor: null section is skipped", d, sections, "section");
            d = CopyDescriptor(good);
            d["section"] = "missing.values";
            ExpectSkipped("descriptor: missing section is skipped", d, sections, "is missing");

            d = CopyDescriptor(good);
            d["definition"] = "0";
            ExpectSkipped("descriptor: string definition is skipped", d, sections, "definition");
            d = CopyDescriptor(good);
            d["definition"] = 0.5;
            ExpectSkipped("descriptor: float definition is skipped", d, sections, "definition");
            d = CopyDescriptor(good);
            d["definition"] = new List<object>();
            ExpectSkipped("descriptor: array definition is skipped", d, sections, "definition");
            d = CopyDescriptor(good);
            d["definition"] = -1L;
            ExpectSkipped("descriptor: negative definition is skipped", d, sections, "definition");

            d = CopyDescriptor(good);
            d["count"] = "2";
            ExpectSkipped("descriptor: string count is skipped", d, sections, "count");
            d = CopyDescriptor(good);
            d["count"] = 2.0;
            ExpectSkipped("descriptor: float count is skipped", d, sections, "count");
            d = CopyDescriptor(good);
            d["count"] = new List<object>();
            ExpectSkipped("descriptor: array count is skipped", d, sections, "count");
            d = CopyDescriptor(good);
            d["count"] = null;
            ExpectSkipped("descriptor: null count is skipped", d, sections, "count");
            d = CopyDescriptor(good);
            d["count"] = -1L;
            ExpectSkipped("descriptor: negative count is skipped", d, sections, "count");
            d = CopyDescriptor(good);
            d.Remove("count");
            ExpectSkipped("descriptor: missing count is skipped", d, sections, "count is missing");
            d = CopyDescriptor(good);
            d["count"] = 3L;
            ExpectSkipped("descriptor: count mismatch is skipped", d, sections, "does not match");

            d = CopyDescriptor(good);
            d["components"] = "1";
            ExpectSkipped("descriptor: string components is skipped", d, sections, "components");
            d = CopyDescriptor(good);
            d["components"] = new List<object>();
            ExpectSkipped("descriptor: array components is skipped", d, sections, "components");
            d = CopyDescriptor(good);
            d["components"] = 3L;
            ExpectSkipped("descriptor: components mismatch is skipped", d, sections, "components");
            d = CopyDescriptor(good);
            d["components"] = 0L;
            ExpectSkipped("descriptor: zero components is skipped", d, sections, "components");

            var stringSections = new List<Section>();
            var stringDescriptors = new List<Dictionary<string, object>>();
            AddStringDescriptor(stringSections, stringDescriptors, "text",
                new uint[] { 2 }, System.Text.Encoding.UTF8.GetBytes("hi"));
            d = CopyDescriptor(stringDescriptors[0]);
            d["offsets"] = 5L;
            ExpectSkipped("descriptor: numeric offsets is skipped", d, stringSections, "offsets");

            // An absent components field is legal; the type determines the
            // shape. The decoded values still have to match count.
            d = CopyDescriptor(good);
            d.Remove("components");
            var notes = new List<string>();
            var root = new ArchivableDictionary();
            AttributeStore.Attach(root, "mesh",
                new List<Dictionary<string, object>> { d }, sections, null,
                delegate(string text) { notes.Add(text); });
            string type, domain;
            object values;
            Check("descriptor: absent components uses the type's shape",
                notes.Count == 0
                && AttributeStore.TryGet(root, "value", out type, out domain, out values),
                "notes: " + string.Join(" | ", notes.ToArray()));

            // A malformed descriptor must not stop its valid neighbour.
            var bad = CopyDescriptor(good);
            bad["type"] = "NOPE";
            var neighbour = CopyDescriptor(good);
            neighbour["name"] = "neighbour";
            notes.Clear();
            root = new ArchivableDictionary();
            AttributeStore.Attach(root, "mesh",
                new List<Dictionary<string, object>> { bad, neighbour }, sections, null,
                delegate(string text) { notes.Add(text); });
            Check("descriptor: a valid neighbour still attaches",
                notes.Count == 1
                && AttributeStore.TryGet(root, "neighbour", out type, out domain, out values)
                && !AttributeStore.TryGet(root, "value", out type, out domain, out values),
                "notes: " + string.Join(" | ", notes.ToArray())
                + "; attached: " + DescribeRoot(root));
        }

        private static void CheckDtypeValidation()
        {
            ExpectDtypeMismatch("FLOAT", 1, "i32", "must be f32");
            ExpectDtypeMismatch("FLOAT_VECTOR", 3, "i32", "must be f32");
            ExpectDtypeMismatch("FLOAT2", 2, "i32", "must be f32");
            ExpectDtypeMismatch("FLOAT4", 4, "i32", "must be f32");
            ExpectDtypeMismatch("FLOAT_COLOR", 4, "i32", "must be f32");
            ExpectDtypeMismatch("QUATERNION", 4, "i32", "must be f32");
            ExpectDtypeMismatch("FLOAT4X4", 16, "i32", "must be f32");
            ExpectDtypeMismatch("INT", 1, "f32", "must be i32");
            ExpectDtypeMismatch("INT8", 1, "f32", "must be i32");
            ExpectDtypeMismatch("INT16_2D", 2, "f32", "must be i32");
            ExpectDtypeMismatch("INT32_2D", 2, "f32", "must be i32");
            ExpectDtypeMismatch("BOOLEAN", 1, "f32", "must be u8");
            ExpectDtypeMismatch("BYTE_COLOR", 4, "f32", "must be u8");

            var sections = new List<Section>();
            sections.Add(Section.Floats("attr.0.values", new float[] { 1f }));
            sections.Add(Section.U32("attr.0.offsets", new uint[] { 1 }));
            ExpectSkipped("dtype: STRING values reject f32",
                StringEntry(1), sections, "must be u8");

            sections = new List<Section>();
            sections.Add(Section.Bytes("attr.0.values",
                System.Text.Encoding.UTF8.GetBytes("hi")));
            sections.Add(Section.Ints("attr.0.offsets", new int[] { 2 }));
            Dictionary<string, object> entry = StringEntry(1);
            entry["offsets"] = "attr.0.offsets";
            ExpectSkipped("dtype: STRING offsets reject i32", entry, sections, "must be u32");

            sections = new List<Section>();
            sections.Add(Section.Bytes("attr.0.values",
                System.Text.Encoding.UTF8.GetBytes("hi")));
            sections.Add(new Section("attr.0.offsets", "u32", new byte[] { 2, 0, 0 }));
            entry = StringEntry(1);
            entry["offsets"] = "attr.0.offsets";
            ExpectSkipped("dtype: truncated STRING offsets are skipped",
                entry, sections, "multiple of 4");

            sections = new List<Section>();
            sections.Add(Section.Bytes("attr.0.values",
                System.Text.Encoding.UTF8.GetBytes("hi")));
            ExpectSkipped("dtype: STRING without offsets is skipped",
                StringEntry(1), sections, "no offsets");

            entry = StringEntry(1);
            entry["offsets"] = "missing.offsets";
            ExpectSkipped("dtype: STRING with a missing offsets section is skipped",
                entry, sections, "offsets section");
        }

        private static void CheckShortValues()
        {
            List<Section> sections;
            Dictionary<string, object> d;

            d = FloatDescriptor(new float[] { 1.5f }, 2, out sections);
            ExpectSkipped("short: count above a short section is skipped",
                d, sections, "does not match");
            d = FloatDescriptor(new float[] { 1.5f, 2.5f }, 3, out sections);
            ExpectSkipped("short: count above the section is skipped",
                d, sections, "does not match");
            d = FloatDescriptor(new float[] { 1.5f, 2.5f, 3.5f }, 2, out sections);
            ExpectSkipped("short: count below the section is skipped",
                d, sections, "does not match");
            d = FloatDescriptor(new float[] { 1.5f, 2.5f }, 1, out sections);
            ExpectSkipped("short: count below a long section is skipped",
                d, sections, "does not match");

            var broken = new List<Section>();
            broken.Add(new Section("attr.0.values", "f32", new byte[] { 0, 0, 0 }));
            ExpectSkipped("short: a partial f32 element is skipped",
                MeshDescriptor("broken", "FLOAT", 1, 1, "attr.0.values"),
                broken, "multiple of 4");

            var vectorSections = new List<Section>();
            vectorSections.Add(Section.Floats("attr.0.values", new float[] { 1, 2, 3, 4 }));
            ExpectSkipped("short: a partial vector is skipped",
                MeshDescriptor("vector", "FLOAT_VECTOR", 3, 1, "attr.0.values"),
                vectorSections, "does not match");
        }

        private static void CheckCurveValidation()
        {
            uint[] counts = new uint[] { 3, 2 };

            List<Section> sections;
            Dictionary<string, object> curve = CurveDescriptor("CURVE", "BOOLEAN",
                Section.Bytes("unused", new byte[] { 1, 0 }), out sections);
            Dictionary<string, object> d = CopyDescriptor(curve);
            d["count"] = "2";
            ExpectCurveSkipped("curve: string count is skipped", d, sections, counts, "count");
            d = CopyDescriptor(curve);
            d.Remove("count");
            ExpectCurveSkipped("curve: missing count is skipped", d, sections, counts, "count");
            d = CopyDescriptor(curve);
            d["count"] = 3L;
            var threeCurves = new List<Section>();
            threeCurves.Add(Section.Bytes("attr.0.values", new byte[] { 1, 0, 1 }));
            ExpectCurveSkipped("curve: count mismatch is skipped",
                d, threeCurves, counts, "curves");
            d = CopyDescriptor(curve);
            d["domain"] = "BOGUS";
            ExpectCurveSkipped("curve: unknown domain is skipped", d, sections, counts, "domain");

            var badBoolean = new List<Section>();
            badBoolean.Add(Section.Floats("attr.0.values", new float[] { 1, 0 }));
            d = MeshDescriptor("curve", "BOOLEAN", 1, 2, "attr.0.values");
            d["group"] = "crv";
            d["domain"] = "CURVE";
            ExpectCurveSkipped("curve: dtype mismatch is skipped", d, badBoolean, counts, "must be u8");

            Dictionary<string, object> point = CurveDescriptor("POINT", "INT",
                Section.Ints("unused", new int[] { 10, 11, 12, 20, 21 }), out sections);
            d = CopyDescriptor(point);
            d["count"] = 4L;
            var fourPoints = new List<Section>();
            fourPoints.Add(Section.Ints("attr.0.values", new int[] { 10, 11, 12, 20 }));
            ExpectCurveSkipped("curve: point count mismatch is skipped",
                d, fourPoints, counts, "curve points");
            ExpectCurveSkipped("curve: missing point counts are skipped",
                point, sections, null, "counts are missing");
            ExpectCurveSkipped("curve: wrong count-section length is skipped",
                point, sections, new uint[] { 5 }, "curve counts");
            d = CopyDescriptor(point);
            var shortPoints = new List<Section>();
            shortPoints.Add(Section.Ints("attr.0.values", new int[] { 10, 11, 12, 20 }));
            ExpectCurveSkipped("curve: short point values are skipped",
                d, shortPoints, counts, "does not match");

            var stringSections = new List<Section>();
            var stringDescriptors = new List<Dictionary<string, object>>();
            AddStringDescriptor(stringSections, stringDescriptors, "tag",
                new uint[] { 4 }, System.Text.Encoding.UTF8.GetBytes("abc"));
            stringDescriptors[0]["group"] = "crv";
            stringDescriptors[0]["domain"] = "CURVE";
            ExpectCurveSkipped("curve: malformed STRING offsets are skipped",
                stringDescriptors[0], stringSections, counts, "runs past");

            // A malformed curve attribute must not stop a valid one, and a
            // valid STRING per-curve attribute keeps its two offsets.
            var mixedSections = new List<Section>();
            var mixedDescriptors = new List<Dictionary<string, object>>();
            AddAttribute(mixedSections, mixedDescriptors, "crv", "bad", "CURVE", "BOOLEAN",
                Section.Bytes("unused", new byte[] { 1, 0 }));
            AddAttribute(mixedSections, mixedDescriptors, "crv", "pidx", "POINT", "INT",
                Section.Ints("unused", new int[] { 10, 11, 12, 20, 21 }));
            var bad = CopyDescriptor(mixedDescriptors[0]);
            bad["count"] = "2";
            var roots = new List<ArchivableDictionary>
            {
                new ArchivableDictionary(),
                new ArchivableDictionary(),
            };
            var notes = new List<string>();
            AttributeStore.AttachCurves(roots, counts,
                new List<Dictionary<string, object>> { bad, mixedDescriptors[1] },
                mixedSections, null, delegate(string text) { notes.Add(text); });
            string type, domain;
            object values;
            Check("curve: a valid neighbour still attaches to every curve",
                notes.Count == 1
                && !AttributeStore.TryGet(roots[0], "bad", out type, out domain, out values)
                && AttributeStore.TryGet(roots[0], "pidx", out type, out domain, out values)
                && AttributeStore.TryGet(roots[1], "pidx", out type, out domain, out values),
                "notes: " + string.Join(" | ", notes.ToArray()));

            var stringRoots = new List<ArchivableDictionary>
            {
                new ArchivableDictionary(),
                new ArchivableDictionary(),
            };
            var stringCurveSections = new List<Section>();
            var stringCurveDescriptors = new List<Dictionary<string, object>>();
            AddStringDescriptor(stringCurveSections, stringCurveDescriptors, "tag",
                new uint[] { 1, 1 }, System.Text.Encoding.UTF8.GetBytes("ab"));
            stringCurveDescriptors[0]["group"] = "crv";
            stringCurveDescriptors[0]["domain"] = "CURVE";
            AttributeStore.AttachCurves(stringRoots, new uint[] { 1, 1 },
                stringCurveDescriptors, stringCurveSections);
            bool gotTag0 = AttributeStore.TryGet(stringRoots[0], "tag", out type, out domain, out values);
            bool gotTag1 = AttributeStore.TryGet(stringRoots[1], "tag", out type, out domain, out values);
            Check("curve: valid STRING values attach per curve",
                gotTag0 && domain == "CURVE" && values is string[]
                && ((string[])values).Length == 1
                && gotTag1 && AttributeStore.TryGet(stringRoots[1], "tag", out type, out domain, out values)
                && ((string[])values).Length == 1,
                gotTag0 ? "first " + ValuesName(values) : "missing");
        }

        private static void CheckCollectValidation()
        {
            var root = new ArchivableDictionary();
            root.Set("junk", new double[] { 1.0 });
            ExpectBadAttribute("collect: a non-dictionary entry is bad_attribute",
                root, "not a dictionary");

            var record = new ArchivableDictionary();
            record.Set("values", new double[] { 1.0 });
            ExpectBadAttribute("collect: a missing type is bad_attribute",
                RootOf(record), "type");

            record = new ArchivableDictionary();
            record.Set("type", "NOPE");
            record.Set("domain", "POINT");
            record.Set("values", new double[] { 1.0 });
            ExpectBadAttribute("collect: an unknown type is bad_attribute",
                RootOf(record), "unknown attribute type");

            record = new ArchivableDictionary();
            record.Set("type", "FLOAT");
            record.Set("domain", "POINT");
            record.Set("values", new int[] { 1 });
            ExpectBadAttribute("collect: a wrong array kind is bad_attribute",
                RootOf(record), "double[]");

            record = new ArchivableDictionary();
            record.Set("type", "FLOAT_VECTOR");
            record.Set("domain", "POINT");
            record.Set("values", new double[] { 1, 2, 3, 4, 5 });
            ExpectBadAttribute("collect: a partial element is bad_attribute",
                RootOf(record), "multiple of 3");

            record = new ArchivableDictionary();
            record.Set("type", "FLOAT");
            record.Set("domain", 5);
            record.Set("values", new double[] { 1.0 });
            ExpectBadAttribute("collect: a non-string domain is bad_attribute",
                RootOf(record), "domain");

            record = new ArchivableDictionary();
            record.Set("type", "STRING");
            record.Set("domain", "POINT");
            record.Set("values", new string[] { null });
            ExpectBadAttribute("collect: a null STRING value is bad_attribute",
                RootOf(record), "null");

            record = new ArchivableDictionary();
            record.Set("type", "FLOAT");
            record.Set("domain", "POINT");
            ExpectBadAttribute("collect: missing values are bad_attribute",
                RootOf(record), "values");

            record = new ArchivableDictionary();
            record.Set("type", "FLOAT");
            record.Set("domain", "");
            record.Set("values", new double[] { 1.0 });
            ExpectBadAttribute("collect: an empty domain is bad_attribute",
                RootOf(record), "domain");

            record = new ArchivableDictionary();
            record.Set("type", "FLOAT");
            record.Set("domain", "BOGUS");
            record.Set("values", new double[] { 1.0 });
            ExpectBadAttribute("collect: an unknown domain is bad_attribute",
                RootOf(record), "domain");

            root = new ArchivableDictionary();
            var setRoot = root;
            ExpectBadAttribute("set: a wrong array kind is bad_attribute", delegate
            {
                AttributeStore.Set(setRoot, "x", "FLOAT", "POINT", new int[] { 1 });
            }, "double[]");
            ExpectBadAttribute("set: a partial element is bad_attribute", delegate
            {
                AttributeStore.Set(setRoot, "x", "FLOAT_VECTOR", "POINT", new double[] { 1, 2 });
            }, "multiple of 3");
            ExpectBadAttribute("set: an unknown type is bad_attribute", delegate
            {
                AttributeStore.Set(setRoot, "x", "NOPE", "POINT", new double[] { 1.0 });
            }, "unknown attribute type");
            ExpectBadAttribute("set: an empty domain is bad_attribute", delegate
            {
                AttributeStore.Set(setRoot, "x", "FLOAT", "", new double[] { 1.0 });
            }, "domain");
            ExpectBadAttribute("set: an empty name is bad_attribute", delegate
            {
                AttributeStore.Set(setRoot, "", "FLOAT", "POINT", new double[] { 1.0 });
            }, "name");
        }

        private static void CheckHasIgnoresNonAttributes()
        {
            var root = new ArchivableDictionary();
            root.Set("junk", new double[] { 1.0 });
            Check("has: a non-dictionary entry does not count",
                !AttributeStore.Has(root), DescribeRoot(root));

            var record = new ArchivableDictionary();
            record.Set("type", "NOPE");
            record.Set("values", new double[] { 1.0 });
            root.Set("unknown", record);
            Check("has: an unknown type does not count",
                !AttributeStore.Has(root), DescribeRoot(root));

            AttributeStore.Set(root, "good", "FLOAT", "POINT", new double[] { 1.0 });
            Check("has: a valid attribute counts",
                AttributeStore.Has(root), DescribeRoot(root));
            Check("has: an empty root has nothing",
                !AttributeStore.Has(new ArchivableDictionary()), "empty");

            var noDomain = new ArchivableDictionary();
            noDomain.Set("type", "FLOAT");
            noDomain.Set("values", new double[] { 1.0 });
            root = new ArchivableDictionary();
            root.Set("nodomain", noDomain);
            Check("has: a missing domain does not count",
                !AttributeStore.Has(root), DescribeRoot(root));

            var badValues = new ArchivableDictionary();
            badValues.Set("type", "FLOAT");
            badValues.Set("domain", "POINT");
            badValues.Set("values", new int[] { 1 });
            root = new ArchivableDictionary();
            root.Set("bad", badValues);
            string type, domain;
            object values;
            Check("tryget: a malformed value shape is not returned",
                !AttributeStore.TryGet(root, "bad", out type, out domain, out values),
                DescribeRoot(root));
        }

        private static Dictionary<string, object> BaseFloatDescriptor(out List<Section> sections)
        {
            return FloatDescriptor(new float[] { 1.5f, 2.5f }, null, out sections);
        }

        private static Dictionary<string, object> FloatDescriptor(float[] values, long? count,
                                                                  out List<Section> sections)
        {
            var descriptors = new List<Dictionary<string, object>>();
            sections = new List<Section>();
            AddAttribute(sections, descriptors, "mesh", "value", "POINT", "FLOAT",
                Section.Floats("unused", values));
            if (count.HasValue) descriptors[0]["count"] = count.Value;
            return descriptors[0];
        }

        private static Dictionary<string, object> CurveDescriptor(string domain, string type,
                                                                  Section values,
                                                                  out List<Section> sections)
        {
            var descriptors = new List<Dictionary<string, object>>();
            sections = new List<Section>();
            AddAttribute(sections, descriptors, "crv", "attr", domain, type, values);
            return descriptors[0];
        }

        private static Dictionary<string, object> MeshDescriptor(string name, string type,
                                                                 int components, long count,
                                                                 string section)
        {
            var entry = new Dictionary<string, object>();
            entry["group"] = "mesh";
            entry["name"] = name;
            entry["domain"] = "POINT";
            entry["type"] = type;
            entry["components"] = (long)components;
            entry["count"] = count;
            entry["section"] = section;
            return entry;
        }

        private static Dictionary<string, object> StringEntry(long count)
        {
            return MeshDescriptor("text", "STRING", 1, count, "attr.0.values");
        }

        private static Dictionary<string, object> CopyDescriptor(Dictionary<string, object> source)
        {
            return new Dictionary<string, object>(source);
        }

        private static ArchivableDictionary RootOf(ArchivableDictionary record)
        {
            var root = new ArchivableDictionary();
            root.Set("bad", record);
            return root;
        }

        private static void ExpectSkipped(string what, Dictionary<string, object> descriptor,
                                          List<Section> sections, string expectedNote)
        {
            var notes = new List<string>();
            var root = new ArchivableDictionary();
            Exception failure = null;
            try
            {
                AttributeStore.Attach(root, "mesh",
                    new List<Dictionary<string, object>> { descriptor }, sections, null,
                    delegate(string text) { notes.Add(text); });
            }
            catch (Exception ex) { failure = ex; }
            Check(what, failure == null && AttributeStore.Names(root).Length == 0
                && notes.Count == 1
                && notes[0].IndexOf(expectedNote, StringComparison.Ordinal) >= 0,
                (failure != null ? "threw " + failure.GetType().Name + ": "
                    + failure.Message + "; " : "")
                + "attached [" + DescribeRoot(root) + "]; notes: "
                + string.Join(" | ", notes.ToArray()));
        }

        private static void ExpectCurveSkipped(string what, Dictionary<string, object> descriptor,
                                               List<Section> sections, uint[] counts,
                                               string expectedNote)
        {
            var roots = new List<ArchivableDictionary>
            {
                new ArchivableDictionary(),
                new ArchivableDictionary(),
            };
            var notes = new List<string>();
            Exception failure = null;
            try
            {
                AttributeStore.AttachCurves(roots, counts,
                    new List<Dictionary<string, object>> { descriptor }, sections, null,
                    delegate(string text) { notes.Add(text); });
            }
            catch (Exception ex) { failure = ex; }
            Check(what, failure == null
                && AttributeStore.Names(roots[0]).Length == 0
                && AttributeStore.Names(roots[1]).Length == 0
                && notes.Count == 1
                && notes[0].IndexOf(expectedNote, StringComparison.Ordinal) >= 0,
                (failure != null ? "threw " + failure.GetType().Name + ": "
                    + failure.Message + "; " : "")
                + "attached [" + DescribeRoot(roots[0]) + "]/["
                + DescribeRoot(roots[1]) + "]; notes: " + string.Join(" | ", notes.ToArray()));
        }

        private static void ExpectDtypeMismatch(string type, int components, string wrongDtype,
                                                string expectedNote)
        {
            var sections = new List<Section>();
            Section wrong;
            if (wrongDtype == "i32")
                wrong = Section.Ints("wrong", new int[components]);
            else
                wrong = Section.Floats("wrong", new float[components]);
            sections.Add(new Section("attr.0.values", wrong.Dtype, wrong.Raw));
            ExpectSkipped("dtype: " + type + " rejects " + wrongDtype,
                MeshDescriptor("dtype", type, components, 1, "attr.0.values"),
                sections, expectedNote);
        }

        private static void ExpectBadAttribute(string what, ArchivableDictionary root,
                                               string expectedNote)
        {
            ExpectBadAttribute(what, delegate
            {
                AttributeStore.Collect(root, "mesh", new List<Section>(),
                    new List<Dictionary<string, object>>());
            }, expectedNote);
        }

        private static void ExpectBadAttribute(string what, Action action, string expectedNote)
        {
            try
            {
                action();
            }
            catch (FrameException ex)
            {
                Check(what, ex.Code == "bad_attribute"
                    && ex.Message.IndexOf(expectedNote, StringComparison.Ordinal) >= 0,
                    "code " + ex.Code + ": " + ex.Message);
                return;
            }
            catch (Exception ex)
            {
                Check(what, false,
                    "unclassified " + ex.GetType().Name + ": " + ex.Message);
                return;
            }
            Check(what, false, "no exception");
        }

        private static void AddStringDescriptor(List<Section> sections,
                                                List<Dictionary<string, object>> descriptors,
                                                string name, uint[] offsets, byte[] valueBytes)
        {
            int index = descriptors.Count;
            sections.Add(Section.Bytes("attr." + index + ".values", valueBytes));
            sections.Add(Section.U32("attr." + index + ".offsets", offsets));
            var entry = new Dictionary<string, object>();
            entry["group"] = "mesh";
            entry["name"] = name;
            entry["domain"] = "POINT";
            entry["type"] = "STRING";
            entry["components"] = 1L;
            entry["count"] = (long)offsets.Length;
            entry["section"] = "attr." + index + ".values";
            entry["offsets"] = "attr." + index + ".offsets";
            descriptors.Add(entry);
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
