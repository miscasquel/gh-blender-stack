// Layer C, instance-grammar half: the C# inbound half of protocol §5.2.
//
//     StageloomInstanceTests.exe <fixtures root>
//
// The definition table, the `idef.<i>.*` / `idef.<i>.<p>.*` namespaces, the
// part attribute descriptors and the row pairs are validated before Rhino
// geometry exists, so most of this suite runs under wine with no RhinoCore.
// When the process can construct native Rhino geometry, the last checks build
// the Q10a multi-part fixtures through Instances.Build and inspect the
// resulting blocks.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using Grasshopper.Kernel.Types;
using Grasshopper.Rhinoceros.Model;
using Rhino.Collections;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom.Tests.Console
{
    public static class InstanceTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            string root = args.Length > 0 ? args[0] : "protocol/fixtures";
            System.Console.WriteLine("instance grammar");

            CheckFixturePlans(root);
            CheckTableValidation();
            CheckNamespaces();
            CheckRows();
            CheckPreflight();
            CheckContentHash(root);
            CheckDescriptorGrammar();
            CheckAttributePartFilter();

            if (CanBuildRhinoGeometry())
            {
                CheckNativeBuild(root);
            }
            else
            {
                System.Console.WriteLine();
                System.Console.WriteLine("SKIP native instance build: RhinoCore is unavailable in this harness");
            }

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: instance grammar PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: instance grammar FAIL (" + _failures + ")");
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
            System.Console.WriteLine("FAIL " + what + (string.IsNullOrEmpty(detail) ? "" : ": " + detail));
        }

        // ------------------------------------------------------------ fixtures

        private static void CheckFixturePlans(string root)
        {
            var notes = new List<string>();
            DecodedFrame single = Load(root, "request-instances");
            var singleBuilder = Builder(single, notes, AttributesOf(single));
            Instances.DefinitionPlan panel = singleBuilder.Plan(0);
            Instances.DefinitionPlan cluster = singleBuilder.Plan(1);
            Check("single fixture: definition 0 keeps the single-part namespace",
                panel != null && panel.PartCount == 1 && panel.Parts.Count == 1
                && panel.Parts[0].Part == -1
                && panel.Parts[0].Prefix == "idef.0."
                && panel.NestedPrefix == "idef.0."
                && panel.Name == "Panel",
                DescribePlan(panel));
            Check("single fixture: definition 1 keeps the single-part namespace",
                cluster != null && cluster.PartCount == 1 && cluster.Parts.Count == 1
                && cluster.Parts[0].Prefix == "idef.1.",
                DescribePlan(cluster));
            Check("single fixture: no grammar notes", notes.Count == 0, Notes(notes));

            notes = new List<string>();
            DecodedFrame multi = Load(root, "response-instances-multipart");
            var multiBuilder = Builder(multi, notes, AttributesOf(multi));
            Instances.DefinitionPlan assembly = multiBuilder.Plan(0);
            Instances.DefinitionPlan bolt = multiBuilder.Plan(1);
            Check("multi fixture: definition 0 has two ordered part namespaces",
                assembly != null && assembly.PartCount == 2 && assembly.Parts.Count == 2
                && assembly.Parts[0].Part == 0 && assembly.Parts[0].Prefix == "idef.0.0."
                && assembly.Parts[1].Part == 1 && assembly.Parts[1].Prefix == "idef.0.1."
                && assembly.NestedPrefix == "idef.0."
                && assembly.Name == "Assembly",
                DescribePlan(assembly));
            Check("multi fixture: definition 1 stays single-part",
                bolt != null && bolt.PartCount == 1 && bolt.Parts[0].Prefix == "idef.1.",
                DescribePlan(bolt));
            Check("multi fixture: no grammar notes", notes.Count == 0, Notes(notes));
        }

        private static DecodedFrame Load(string root, string name)
        {
            string path = Path.Combine(root, name, "frame.bin");
            if (!File.Exists(path))
                throw new FileNotFoundException("fixture frame is missing", path);
            return FrameCodec.Decode(File.ReadAllBytes(path));
        }

        private static List<Dictionary<string, object>> AttributesOf(DecodedFrame frame)
        {
            var result = new List<Dictionary<string, object>>();
            var list = Json.Get(frame.Header, "attributes") as System.Collections.IList;
            if (list == null) return result;
            foreach (object item in list)
            {
                var descriptor = item as Dictionary<string, object>;
                if (descriptor != null) result.Add(descriptor);
            }
            return result;
        }

        // --------------------------------------------------------------- table

        private static void CheckTableValidation()
        {
            var notes = new List<string>();
            Instances.DefinitionBuilder builder = Builder(Frame(new List<object> { 7L }, null), notes);
            Check("table: a non-object entry is rejected",
                builder.Plan(0) == null && HasNote(notes, "not an object"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefName(5L) }, null), notes);
            Check("table: a non-string name is rejected",
                builder.Plan(0) == null && HasNote(notes, "name is not a string"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefName(null) }, null), notes);
            Check("table: a null name is rejected",
                builder.Plan(0) == null && HasNote(notes, "name is not a string"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefParts("bad", true) }, null), notes);
            Check("table: a boolean parts is rejected",
                builder.Plan(0) == null && HasNote(notes, "not a positive integer"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefParts("bad", null) }, null), notes);
            Check("table: a null parts is rejected",
                builder.Plan(0) == null && HasNote(notes, "not a positive integer"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefParts("zero", 0L) }, null), notes);
            Check("table: zero parts is rejected",
                builder.Plan(0) == null && HasNote(notes, "not a positive integer"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefParts("float", 2.0) }, null), notes);
            Check("table: a fractional parts is rejected",
                builder.Plan(0) == null && HasNote(notes, "not a positive integer"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefParts("string", "2") }, null), notes);
            Check("table: a string parts is rejected",
                builder.Plan(0) == null && HasNote(notes, "not a positive integer"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> {
                DefParts("huge", (long)Instances.MaxDefinitionParts + 1) }, null), notes);
            Check("table: parts over the defensive cap is rejected",
                builder.Plan(0) == null && HasNote(notes, "over the 4096-part cap"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { DefParts("pair", 2L) }, null), notes);
            Instances.DefinitionPlan pair = builder.Plan(0);
            Check("table: a valid parts count plans every part",
                pair != null && pair.PartCount == 2 && pair.Parts.Count == 2
                && pair.Parts[0].Prefix == "idef.0.0."
                && pair.Parts[1].Prefix == "idef.0.1." && notes.Count == 0,
                DescribePlan(pair) + " notes: " + Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { Def(null) }, null), notes);
            Check("table: an absent name is the legal unnamed definition",
                builder.Plan(0) != null && builder.Plan(0).Name == "" && notes.Count == 0,
                notes.Count == 0 ? "plan ok" : Notes(notes));
        }

        // ----------------------------------------------------------- namespace

        private static void CheckNamespaces()
        {
            ExpectPlanRejected("namespace: a part section on a single-part definition is rejected",
                new List<object> { Def("Single") },
                new List<Section> { AttrValue("idef.0.0.attr.0.values", 1f) },
                "multi-part namespace on single-part definition");

            ExpectPlanRejected("namespace: definition-level geometry in a multi-part definition is rejected",
                new List<object> { DefParts("Multi", 2L) },
                MeshSections("idef.0."),
                "definition-level in multi-part definition");

            ExpectPlanRejected("namespace: a part index outside 0..N-1 is rejected",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { AttrValue("idef.0.2.attr.0.values", 1f) },
                "outside 0..1");

            ExpectPlanRejected("namespace: nested rows under a part are rejected",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { Section.U32("idef.0.0.inst.ref_def", new uint[] { 0 }) },
                "does not parse as a part section");

            ExpectPlanRejected("namespace: an unknown single-part group is rejected",
                new List<object> { Def("Single") },
                new List<Section> { Section.U32("idef.0.extra.value", new uint[] { 1 }) },
                "does not parse as a single-part definition section");

            ExpectPlanRejected("namespace: an unknown part group is rejected",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { Section.U32("idef.0.0.extra.value", new uint[] { 1 }) },
                "does not parse as a part section");

            ExpectPlanRejected("namespace: a nested part namespace is rejected",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { Section.U32("idef.0.0.0.mesh.verts", new uint[] { 1 }) },
                "does not parse as a part section");

            ExpectPlanRejected("namespace: a partial mesh group skips the definition",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { Section.Floats("idef.0.0.mesh.verts",
                    new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 }) },
                "mesh group is incomplete");

            ExpectPlanRejected("namespace: a malformed point-cloud section skips the definition",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { Section.U32("idef.0.0.pts.positions", new uint[] { 1 }) },
                "pts.positions must be f32");

            ExpectPlanRejected("namespace: a part token spelled two ways is rejected",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section>
                {
                    AttrValue("idef.0.0.attr.0.values", 1f),
                    AttrValue("idef.0.00.attr.1.values", 2f),
                },
                "more than one namespace token");

            ExpectPlanRejected("namespace: a definition token spelled two ways is rejected",
                new List<object> { Def("Single") },
                new List<Section>
                {
                    AttrValue("idef.0.attr.0.values", 1f),
                    AttrValue("idef.00.attr.1.values", 2f),
                },
                "names its sections with both");

            ExpectPlanRejected("namespace: a non-canonical definition token is rejected",
                new List<object> { Def("Single") },
                new List<Section> { AttrValue("idef.00.attr.0.values", 1f) },
                "non-canonical namespace token");

            ExpectPlanRejected("namespace: a non-canonical part token is rejected",
                new List<object> { DefParts("Multi", 2L) },
                new List<Section> { AttrValue("idef.0.00.attr.0.values", 1f) },
                "non-canonical namespace token");

            var malformedNotes = new List<string>();
            var malformed = Builder(Frame(new List<object> { Def("Single") }, new List<Section>
            {
                new Section("idef.0", "u32", new byte[] { 1, 0, 0, 0 }),
                new Section("idef.x.mesh.verts", "f32", new byte[12]),
            }), malformedNotes);
            malformed.Plan(0);
            Check("namespace: an unparsed idef name is noted, not silently skipped",
                HasNote(malformedNotes, "does not parse as a definition section"),
                Notes(malformedNotes));

            var outsideNotes = new List<string>();
            var outside = Builder(Frame(new List<object> { Def("Single") },
                new List<Section> { AttrValue("idef.5.attr.0.values", 1f) }), outsideNotes);
            Check("namespace: a section outside the table is noted",
                outside.Plan(5) == null && HasNote(outsideNotes, "outside the definitions table"),
                Notes(outsideNotes));

            var emptyNotes = new List<string>();
            var empty = Builder(Frame(new List<object> { DefParts("Three", 3L) },
                MeshSections("idef.0.1.")), emptyNotes);
            Instances.DefinitionPlan three = empty.Plan(0);
            Check("namespace: empty parts are legal and keep their order",
                three != null && three.PartCount == 3 && three.Parts.Count == 3
                && three.Parts[0].Part == 0 && three.Parts[1].Part == 1
                && three.Parts[2].Part == 2 && emptyNotes.Count == 0,
                DescribePlan(three) + " notes: " + Notes(emptyNotes));

            ExpectPlanRejected("table: a definition index outside the table is rejected",
                new List<object> { Def("Only") }, null, "outside the definitions table", 4);
        }

        private static void ExpectPlanRejected(string what, List<object> definitions,
                                               List<Section> sections, string noteText,
                                               long index = 0)
        {
            var notes = new List<string>();
            Instances.DefinitionBuilder builder = Builder(
                Frame(definitions, sections == null ? new List<Section>() : sections), notes);
            Instances.DefinitionPlan plan = builder.Plan(index);
            Check(what, plan == null && HasNote(notes, noteText),
                (plan == null ? "plan null" : DescribePlan(plan)) + "; notes: " + Notes(notes));
        }

        // --------------------------------------------------------------- rows

        private static void CheckRows()
        {
            var notes = new List<string>();
            long[] refs;
            List<Transform> transforms;
            DecodedFrame valid = Frame(null, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[] { 0 }),
                Identity("inst.transforms"),
            });
            bool ok = Instances.ReadRows(valid, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: a matching pair reads exactly",
                ok && refs.Length == 1 && transforms.Count == 1 && notes.Count == 0,
                ok ? refs.Length + "/" + transforms.Count : Notes(notes));

            notes = new List<string>();
            DecodedFrame mismatch = Frame(null, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[] { 0, 1 }),
                Identity("inst.transforms"),
            });
            ok = Instances.ReadRows(mismatch, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: extra definition indices are a note, not a truncated pair",
                !ok && HasNote(notes, "definition indices"), Notes(notes));

            notes = new List<string>();
            DecodedFrame shortMats = Frame(null, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[] { 0 }),
                Section.Floats("inst.transforms", new float[15]),
            });
            ok = Instances.ReadRows(shortMats, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: a transform count that is not 16 floats is a note",
                !ok && HasNote(notes, "each row is 16 f32"), Notes(notes));

            notes = new List<string>();
            DecodedFrame wrongDtype = Frame(null, new List<Section>
            {
                Section.Floats("inst.ref_def", new float[] { 0 }),
                Identity("inst.transforms"),
            });
            ok = Instances.ReadRows(wrongDtype, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: a non-u32 ref_def is a note",
                !ok && HasNote(notes, "must be u32"), Notes(notes));

            notes = new List<string>();
            DecodedFrame half = Frame(null, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[] { 0 }),
            });
            ok = Instances.ReadRows(half, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: half a pair is a note",
                !ok && HasNote(notes, "inst.transforms is missing"), Notes(notes));

            notes = new List<string>();
            DecodedFrame absent = Frame(null, null);
            ok = Instances.ReadRows(absent, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: an absent pair is silent",
                !ok && notes.Count == 0, Notes(notes));

            notes = new List<string>();
            DecodedFrame emptyPair = Frame(null, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[0]),
                Section.Floats("inst.transforms", new float[0]),
            });
            ok = Instances.ReadRows(emptyPair, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: an empty pair reads no instances",
                !ok && notes.Count == 0, Notes(notes));

            notes = new List<string>();
            DecodedFrame shortBytes = Frame(null, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[] { 0 }),
                new Section("inst.transforms", "f32", new byte[] { 1, 2, 3 }),
            });
            ok = Instances.ReadRows(shortBytes, "", 1.0, out refs, out transforms,
                delegate(string text) { notes.Add(text); });
            Check("rows: a transform section that is not f32-aligned is a note",
                !ok && HasNote(notes, "not a multiple of 4"), Notes(notes));
        }

        // ----------------------------------------------------------- preflight

        private static void CheckPreflight()
        {
            var notes = new List<string>();
            var definitions = new List<object> { Def("A"), Def("B") };
            var sections = new List<Section>();
            Rows(sections, "idef.0.", 1);
            Rows(sections, "idef.1.", 0);
            Instances.DefinitionBuilder builder = Builder(Frame(definitions, sections), notes);
            bool ok = builder.Preflight(0);
            Check("preflight: a two-definition cycle is noted and cut",
                ok && HasNote(notes, "inside itself"), Notes(notes));
            Check("preflight: a cyclic graph still hashes without recursing forever",
                builder.ContentHash(0).Length == 12,
                builder.ContentHash(0));

            notes = new List<string>();
            sections = new List<Section>();
            Rows(sections, "idef.0.", 0);
            builder = Builder(Frame(new List<object> { Def("Self") }, sections), notes);
            ok = builder.Preflight(0);
            Check("preflight: a self-cycle is noted and cut",
                ok && HasNote(notes, "inside itself"), Notes(notes));

            notes = new List<string>();
            sections = new List<Section>();
            Rows(sections, "idef.0.", 5);
            builder = Builder(Frame(new List<object> { Def("Missing") }, sections), notes);
            ok = builder.Preflight(0);
            Check("preflight: an out-of-table nested row is noted and cut",
                ok && HasNote(notes, "which the frame does not carry"), Notes(notes));

            notes = new List<string>();
            builder = Builder(Frame(new List<object> { Def("Missing") }, new List<Section>()), notes);
            ok = builder.Preflight(3);
            Check("preflight: an out-of-table row target is noted",
                !ok && HasNote(notes, "outside the definitions table"), Notes(notes));
        }

        // --------------------------------------------------------------- hash

        private static void CheckContentHash(string root)
        {
            var header = new Dictionary<string, object>();
            header["definitions"] = new List<object> { Def("") };
            var sections = MeshSections("idef.0.");
            sections.Add(AttrValue("idef.0.attr.0.values", 1f));
            var frame = DecodedFrame.FromSections(header, sections);
            var notes = new List<string>();
            Instances.DefinitionBuilder builder = Builder(frame, notes);
            string single = builder.ContentHash(0);
            Check("hash: a single-part definition keeps the old content hash",
                single == LegacySingleHash(frame, 0, 1.0, new List<string>()),
                single + " vs " + LegacySingleHash(frame, 0, 1.0, new List<string>()));

            var nestedSections = MeshSections("idef.0.");
            nestedSections.AddRange(MeshSections("idef.1."));
            Rows(nestedSections, "idef.0.", 1);
            var nestedFrame = Frame(new List<object> { Def(""), Def("Child") }, nestedSections);
            builder = Builder(nestedFrame, new List<string>());
            Check("hash: nested rows keep the old single-part content hash",
                builder.ContentHash(0)
                    == LegacySingleHash(nestedFrame, 0, 1.0, new List<string>()),
                builder.ContentHash(0) + " vs "
                    + LegacySingleHash(nestedFrame, 0, 1.0, new List<string>()));

            DecodedFrame multi = Load(root, "response-instances-multipart");
            var attributes = AttributesOf(multi);
            builder = Builder(multi, new List<string>(), attributes);
            string assembly = builder.ContentHash(0);
            Check("hash: a multi-part definition hashes to 12 hex characters",
                assembly.Length == 12, assembly);

            var renamed = CopyDescriptors(attributes);
            renamed[0]["name"] = "panel_other";
            builder = Builder(WithDefinitions(multi, new object[]
            {
                new Dictionary<string, object> { { "name", "Assembly" }, { "parts", 2L } },
                new Dictionary<string, object> { { "name", "Bolt" } },
            }), new List<string>(), renamed);
            Check("hash: a multi-part attribute name changes the content hash",
                builder.ContentHash(0) != assembly,
                builder.ContentHash(0) + " vs " + assembly);

            builder = Builder(WithDefinitions(multi, new object[]
            {
                new Dictionary<string, object> { { "name", "Assembly" }, { "parts", 3L } },
                new Dictionary<string, object> { { "name", "Bolt" } },
            }), new List<string>(), attributes);
            Check("hash: a different part count changes the content hash",
                builder.ContentHash(0) != assembly,
                builder.ContentHash(0) + " vs " + assembly);

            List<Section> changed = CopySections(multi.Sections);
            Section partOne = Find(changed, "idef.0.1.mesh.verts");
            partOne.Raw[0] = (byte)(partOne.Raw[0] ^ 0xff);
            builder = Builder(WithDefinitions(multi, new object[]
            {
                new Dictionary<string, object> { { "name", "Assembly" }, { "parts", 2L } },
                new Dictionary<string, object> { { "name", "Bolt" } },
            }, changed), new List<string>(), attributes);
            Check("hash: a changed part section changes the content hash",
                builder.ContentHash(0) != assembly,
                builder.ContentHash(0) + " vs " + assembly);

            DecodedFrame request = Load(root, "request-instances");
            var requestAttributes = AttributesOf(request);
            builder = Builder(request, new List<string>(), requestAttributes);
            string panel = builder.ContentHash(0);
            var changedRequest = CopyDescriptors(requestAttributes);
            changedRequest[0]["name"] = "load_renamed";
            builder = Builder(WithDefinitions(request, new object[]
            {
                new Dictionary<string, object> { { "name", "Panel" } },
                new Dictionary<string, object> { { "name", "Cluster" } },
            }, request.Sections), new List<string>(), changedRequest);
            Check("hash: a single-part definition ignores descriptors as before",
                builder.ContentHash(0) == panel,
                builder.ContentHash(0) + " vs " + panel);

            var twoEmpty = Frame(new List<object>
            {
                DefParts("Two", 2L), DefParts("Three", 3L),
            }, new List<Section>());
            builder = Builder(twoEmpty, new List<string>());
            Check("hash: empty definitions with different part counts do not collide",
                builder.ContentHash(0) != builder.ContentHash(1),
                builder.ContentHash(0) + " vs " + builder.ContentHash(1));
        }

        // ---------------------------------------------------------- attributes

        private static void CheckDescriptorGrammar()
        {
            var notes = new List<string>();
            var sections = MeshSections("idef.0.");
            var descriptors = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "wrong_section", "FLOAT", "POINT", 1,
                    "idef.0.mesh.verts", 0, null),
            };
            Instances.DefinitionBuilder builder = Builder(
                Frame(new List<object> { Def("Single") }, sections), notes, descriptors);
            builder.Plan(0);
            Check("descriptors: a geometry section is not an attribute section",
                HasNote(notes, "section does not match definition 0"), Notes(notes));

            notes = new List<string>();
            descriptors = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "wrong_part", "FLOAT", "POINT", 1,
                    "idef.0.1.attr.0.values", 0, 0),
            };
            builder = Builder(Frame(new List<object> { DefParts("Multi", 2L) }, sections),
                notes, descriptors);
            builder.Plan(0);
            Check("descriptors: a part descriptor must name its own part section",
                HasNote(notes, "section does not match definition 0 part 0"), Notes(notes));

            notes = new List<string>();
            var stringSections = new List<Section>
            {
                Section.Bytes("idef.0.0.attr.0.values", new byte[] { 0x61 }),
                Section.U32("idef.0.1.attr.0.offsets", new uint[] { 1 }),
            };
            descriptors = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "wrong_offsets", "STRING", "POINT", 1,
                    "idef.0.0.attr.0.values", 0, 0),
            };
            descriptors[0]["offsets"] = "idef.0.1.attr.0.offsets";
            builder = Builder(Frame(new List<object> { DefParts("Multi", 2L) }, stringSections),
                notes, descriptors);
            builder.Plan(0);
            Check("descriptors: an offsets section must name its own part",
                HasNote(notes, "offsets section does not match definition 0 part 0"),
                Notes(notes));

            notes = new List<string>();
            descriptors = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "bool_part", "FLOAT", "POINT", 1,
                    "idef.0.0.attr.0.values", 0, null),
            };
            descriptors[0]["part"] = true;
            builder = Builder(Frame(new List<object> { DefParts("Multi", 2L) },
                new List<Section> { AttrValue("idef.0.0.attr.0.values", 1f) }), notes, descriptors);
            builder.Plan(0);
            Check("descriptors: a boolean part is rejected by definition validation",
                HasNote(notes, "part that is not a non-negative integer"), Notes(notes));

            notes = new List<string>();
            descriptors = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "orphan_section", "FLOAT", "POINT", 1,
                    "idef.0.attr.0.values", null, null),
            };
            builder = Builder(Frame(new List<object> { Def("Single") },
                new List<Section> { AttrValue("idef.0.attr.0.values", 1f) }), notes, descriptors);
            builder.Plan(0);
            Check("descriptors: a definition section without a definition index is noted",
                HasNote(notes, "without a definition index"), Notes(notes));
        }

        private static void CheckAttributePartFilter()
        {
            var sections = new List<Section>
            {
                AttrValue("idef.0.0.attr.0.values", 1f),
                AttrValue("idef.0.1.attr.1.values", 2f),
                AttrValue("idef.0.attr.2.values", 3f),
                AttrValue("attr.3.values", 4f),
            };
            var descriptors = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "part_zero", "FLOAT", "POINT", 1,
                    "idef.0.0.attr.0.values", 0, 0),
                Descriptor("mesh", "part_one", "FLOAT", "POINT", 1,
                    "idef.0.1.attr.1.values", 0, 1),
                Descriptor("mesh", "legacy", "FLOAT", "POINT", 1,
                    "idef.0.attr.2.values", 0, null),
                Descriptor("mesh", "base", "FLOAT", "POINT", 1,
                    "attr.3.values", null, null),
            };

            var partZero = new ArchivableDictionary();
            AttributeStore.Attach(partZero, "mesh", descriptors, sections, 0, null, 0);
            string type, domain;
            object values;
            Check("attributes: part descriptors attach only to their part",
                AttributeStore.TryGet(partZero, "part_zero", out type, out domain, out values)
                && !AttributeStore.TryGet(partZero, "part_one", out type, out domain, out values)
                && !AttributeStore.TryGet(partZero, "legacy", out type, out domain, out values),
                RootNames(partZero));

            var partOne = new ArchivableDictionary();
            AttributeStore.Attach(partOne, "mesh", descriptors, sections, 0, null, 1);
            Check("attributes: the other part gets only its own descriptor",
                AttributeStore.TryGet(partOne, "part_one", out type, out domain, out values)
                && !AttributeStore.TryGet(partOne, "part_zero", out type, out domain, out values)
                && !AttributeStore.TryGet(partOne, "legacy", out type, out domain, out values),
                RootNames(partOne));

            var notes = new List<string>();
            var legacy = new ArchivableDictionary();
            AttributeStore.Attach(legacy, "mesh", descriptors, sections, 0,
                delegate(string text) { notes.Add(text); }, null);
            Check("attributes: a definition without a part filter takes only legacy descriptors",
                AttributeStore.TryGet(legacy, "legacy", out type, out domain, out values)
                && !AttributeStore.TryGet(legacy, "part_zero", out type, out domain, out values)
                && HasNote(notes, "not valid on single-part definition 0"),
                RootNames(legacy) + " notes: " + Notes(notes));

            notes = new List<string>();
            var frameOwn = new ArchivableDictionary();
            AttributeStore.Attach(frameOwn, "mesh", descriptors, sections, null,
                delegate(string text) { notes.Add(text); }, null);
            Check("attributes: the frame's own geometry keeps its descriptor",
                AttributeStore.TryGet(frameOwn, "base", out type, out domain, out values)
                && !AttributeStore.TryGet(frameOwn, "part_zero", out type, out domain, out values),
                RootNames(frameOwn));

            notes = new List<string>();
            var missing = new ArchivableDictionary();
            AttributeStore.Attach(missing, "mesh", descriptors, sections, 0,
                delegate(string text) { notes.Add(text); }, 1);
            Check("attributes: a descriptor without a part is noted on a part filter",
                HasNote(notes, "has no part; definition 0 part 1"), Notes(notes));

            notes = new List<string>();
            var partOnly = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "orphan", "FLOAT", "POINT", 1,
                    "idef.0.0.attr.0.values", null, 0),
            };
            var orphan = new ArchivableDictionary();
            AttributeStore.Attach(orphan, "mesh", partOnly, sections, null,
                delegate(string text) { notes.Add(text); }, null);
            Check("attributes: a part without a definition is noted and skipped",
                AttributeStore.Names(orphan).Length == 0 && HasNote(notes, "has no definition"),
                Notes(notes));

            notes = new List<string>();
            var orphanPartRoot = new ArchivableDictionary();
            AttributeStore.Attach(orphanPartRoot, "mesh", partOnly, sections, null,
                delegate(string text) { notes.Add(text); }, 0);
            Check("attributes: a part filter without a definition cannot claim one",
                AttributeStore.Names(orphanPartRoot).Length == 0
                && HasNote(notes, "has no definition"), Notes(notes));

            // Curve attributes use the same part filter.
            var curveSections = new List<Section>
            {
                Section.Floats("idef.0.1.attr.0.values", new float[] { 2f, 3f }),
            };
            var curveDescriptors = new List<Dictionary<string, object>>
            {
                Descriptor("crv", "part_weight", "FLOAT", "CURVE", 2,
                    "idef.0.1.attr.0.values", 0, 1),
            };
            var roots = new List<ArchivableDictionary>
            {
                new ArchivableDictionary(), new ArchivableDictionary(),
            };
            AttributeStore.AttachCurves(roots, new uint[] { 1, 1 }, curveDescriptors,
                curveSections, 0, null, 1);
            bool curvePartOne = AttributeStore.TryGet(roots[0], "part_weight",
                out type, out domain, out values)
                && AttributeStore.TryGet(roots[1], "part_weight", out type, out domain, out values);
            var otherRoots = new List<ArchivableDictionary>
            {
                new ArchivableDictionary(), new ArchivableDictionary(),
            };
            AttributeStore.AttachCurves(otherRoots, new uint[] { 1, 1 }, curveDescriptors,
                curveSections, 0, null, 0);
            Check("attributes: curve descriptors follow the part filter",
                curvePartOne && AttributeStore.Names(otherRoots[0]).Length == 0
                && AttributeStore.Names(otherRoots[1]).Length == 0,
                "part 1: " + RootNames(roots[0]) + "; part 0: " + RootNames(otherRoots[0]));

            notes = new List<string>();
            var boolPart = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "bool_part", "FLOAT", "POINT", 1,
                    "idef.0.0.attr.0.values", 0, null),
            };
            boolPart[0]["part"] = true;
            var boolRoot = new ArchivableDictionary();
            AttributeStore.Attach(boolRoot, "mesh", boolPart, sections, 0,
                delegate(string text) { notes.Add(text); }, 0);
            Check("attributes: a boolean part is noted and skipped",
                AttributeStore.Names(boolRoot).Length == 0
                && HasNote(notes, "part must be a non-negative integer"), Notes(notes));

            notes = new List<string>();
            var nullPart = new List<Dictionary<string, object>>
            {
                Descriptor("mesh", "null_part", "FLOAT", "POINT", 1,
                    "idef.0.0.attr.0.values", 0, null),
            };
            nullPart[0]["part"] = null;
            var nullRoot = new ArchivableDictionary();
            AttributeStore.Attach(nullRoot, "mesh", nullPart, sections, 0,
                delegate(string text) { notes.Add(text); }, 0);
            Check("attributes: a null part is noted and skipped",
                AttributeStore.Names(nullRoot).Length == 0
                && HasNote(notes, "part must be a non-negative integer"), Notes(notes));
        }

        // -------------------------------------------------------------- native

        private static void CheckNativeBuild(string root)
        {
            System.Console.WriteLine();
            System.Console.WriteLine("native instance build");

            var notes = new List<string>();
            DecodedFrame single = Load(root, "request-instances");
            var singleAttributes = AttributesOf(single);
            var singleGeometry = new List<IGH_Goo>();
            Instances.Build(single, 1.0, singleAttributes, singleGeometry,
                delegate(string text) { notes.Add(text); });
            var panelRef = singleGeometry.Count == 2 ? singleGeometry[0] as GH_InstanceReference : null;
            var clusterRef = singleGeometry.Count == 2 ? singleGeometry[1] as GH_InstanceReference : null;
            Mesh panelMesh = null;
            if (panelRef != null && panelRef.InstanceDefinition != null
                && panelRef.InstanceDefinition.Objects != null
                && panelRef.InstanceDefinition.Objects.Count == 1)
                panelRef.InstanceDefinition.Objects[0].CastTo<Mesh>(out panelMesh);
            string loadType, loadDomain;
            object loadValues;
            bool panelLoad = panelMesh != null
                && AttributeStore.TryGet(AttributeRoot(panelMesh), "load",
                    out loadType, out loadDomain, out loadValues)
                && loadValues is double[] && ((double[])loadValues).Length == 4
                && ((double[])loadValues)[3] == 2.0;
            Check("native: the single-part fixture is unchanged",
                singleGeometry.Count == 2 && notes.Count == 0
                && panelRef != null && panelRef.InstanceDefinition != null
                && panelRef.InstanceDefinition.DisplayName == "Panel"
                && panelRef.InstanceDefinition.Objects != null
                && panelRef.InstanceDefinition.Objects.Count == 1
                && clusterRef != null && clusterRef.InstanceDefinition != null
                && clusterRef.InstanceDefinition.DisplayName == "Cluster"
                && clusterRef.InstanceDefinition.Objects != null
                && clusterRef.InstanceDefinition.Objects.Count == 2
                && panelLoad,
                "geometry " + singleGeometry.Count + "; notes: " + Notes(notes));

            notes = new List<string>();
            DecodedFrame frame = Load(root, "response-instances-multipart");
            var attributes = AttributesOf(frame);
            var geometry = new List<IGH_Goo>();
            Instances.Build(frame, 1.0, attributes, geometry,
                delegate(string text) { notes.Add(text); });
            Check("native: the multi-part fixture builds one top-level reference",
                geometry.Count == 1 && notes.Count == 0,
                "geometry " + geometry.Count + "; notes: " + Notes(notes));

            var top = geometry.Count == 1 ? geometry[0] as GH_InstanceReference : null;
            ModelInstanceDefinition assembly = top == null ? null : top.InstanceDefinition;
            Check("native: definition 0 carries one object per part and the nested row",
                assembly != null && assembly.Objects != null && assembly.Objects.Count == 3,
                assembly == null || assembly.Objects == null
                    ? "no definition" : assembly.Objects.Count + " objects");

            Mesh partZero = null, partOne = null;
            GH_InstanceReference nested = null;
            if (assembly != null && assembly.Objects != null && assembly.Objects.Count == 3)
            {
                assembly.Objects[0].CastTo<Mesh>(out partZero);
                assembly.Objects[1].CastTo<Mesh>(out partOne);
                assembly.Objects[2].CastTo<GH_InstanceReference>(out nested);
            }
            string type, domain;
            object values;
            bool panel = partZero != null
                && AttributeStore.TryGet(AttributeRoot(partZero), "panel_id",
                    out type, out domain, out values)
                && values is int[] && ((int[])values).Length == 1 && ((int[])values)[0] == 7;
            bool partOneClean = partOne != null
                && !AttributeStore.TryGet(AttributeRoot(partOne), "panel_id",
                    out type, out domain, out values);
            Check("native: part 0 keeps its attribute and part 1 does not inherit it",
                panel && partOneClean,
                "part 0 " + (partZero == null ? "missing" : "read")
                + ", part 1 " + (partOne == null ? "missing" : "read"));

            Check("native: the nested row resolves to the child definition",
                nested != null && nested.Value != null
                && nested.InstanceDefinition != null
                && nested.InstanceDefinition.DisplayName == "Bolt"
                && nested.InstanceDefinition.Objects != null
                && nested.InstanceDefinition.Objects.Count == 1,
                nested == null || nested.InstanceDefinition == null
                    ? "no nested reference"
                    : nested.InstanceDefinition.DisplayName + "/"
                        + (nested.InstanceDefinition.Objects == null
                            ? "null" : nested.InstanceDefinition.Objects.Count.ToString()));

            // An empty part inside a multi-part definition is legal, and a
            // fully empty multi-part definition is still a definition.
            var emptyPartSections = MeshSections("idef.0.1.");
            Rows(emptyPartSections, "", 0);
            var emptyPartFrame = Frame(new List<object> { DefParts("EmptyPart", 3L) },
                emptyPartSections);
            geometry = new List<IGH_Goo>();
            notes = new List<string>();
            Instances.Build(emptyPartFrame, 1.0, null, geometry,
                delegate(string text) { notes.Add(text); });
            var emptyPart = geometry.Count == 1 ? geometry[0] as GH_InstanceReference : null;
            Check("native: an empty part does not erase the other parts",
                emptyPart != null && emptyPart.InstanceDefinition != null
                && emptyPart.InstanceDefinition.Objects != null
                && emptyPart.InstanceDefinition.Objects.Count == 1 && notes.Count == 0,
                (emptyPart == null || emptyPart.InstanceDefinition == null
                    ? "no definition"
                    : emptyPart.InstanceDefinition.Objects == null
                        ? "null objects"
                        : emptyPart.InstanceDefinition.Objects.Count + " objects")
                + "; notes: " + Notes(notes));

            var emptySections = new List<Section>();
            Rows(emptySections, "", 0);
            var emptyFrame = Frame(new List<object> { DefParts("Empty", 2L) }, emptySections);
            geometry = new List<IGH_Goo>();
            notes = new List<string>();
            Instances.Build(emptyFrame, 1.0, null, geometry,
                delegate(string text) { notes.Add(text); });
            var empty = geometry.Count == 1 ? geometry[0] as GH_InstanceReference : null;
            Check("native: a multi-part definition with no geometry is still a definition",
                empty != null && empty.InstanceDefinition != null
                && empty.InstanceDefinition.Objects != null
                && empty.InstanceDefinition.Objects.Count == 0 && notes.Count == 0,
                (empty == null || empty.InstanceDefinition == null
                    ? "no definition"
                    : empty.InstanceDefinition.Objects == null
                        ? "null objects"
                        : empty.InstanceDefinition.Objects.Count + " objects")
                + "; notes: " + Notes(notes));
        }

        private static ArchivableDictionary AttributeRoot(GeometryBase geometry)
        {
            ArchivableDictionary root;
            geometry.UserDictionary.TryGetDictionary(AttributeStore.RootKey, out root);
            return root;
        }

        private static bool CanBuildRhinoGeometry()
        {
            try
            {
                new Mesh();
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        // ------------------------------------------------------------- helpers

        private static Instances.DefinitionBuilder Builder(DecodedFrame frame,
                                                           List<string> notes,
                                                           List<Dictionary<string, object>> attributes = null)
        {
            return new Instances.DefinitionBuilder(frame, 1.0, attributes,
                delegate(string text) { notes.Add(text); });
        }

        private static DecodedFrame Frame(List<object> definitions, List<Section> sections)
        {
            var header = new Dictionary<string, object>();
            if (definitions != null) header["definitions"] = definitions;
            return DecodedFrame.FromSections(header,
                sections == null ? new List<Section>() : sections);
        }

        private static DecodedFrame WithDefinitions(DecodedFrame frame, object[] definitions)
        {
            var header = new Dictionary<string, object>(frame.Header);
            header["definitions"] = new List<object>(definitions);
            return DecodedFrame.FromSections(header, frame.Sections);
        }

        private static DecodedFrame WithDefinitions(DecodedFrame frame, object[] definitions,
                                                    List<Section> sections)
        {
            var header = new Dictionary<string, object>(frame.Header);
            header["definitions"] = new List<object>(definitions);
            return DecodedFrame.FromSections(header, sections);
        }

        private static Dictionary<string, object> Def(string name)
        {
            var entry = new Dictionary<string, object>();
            if (name != null) entry["name"] = name;
            return entry;
        }

        private static Dictionary<string, object> DefName(object name)
        {
            var entry = new Dictionary<string, object>();
            entry["name"] = name;
            return entry;
        }

        private static Dictionary<string, object> DefParts(string name, object parts)
        {
            var entry = new Dictionary<string, object>();
            entry["name"] = name;
            entry["parts"] = parts;
            return entry;
        }

        private static List<Section> MeshSections(string prefix)
        {
            return new List<Section>
            {
                Section.Floats(prefix + "mesh.verts", new float[]
                {
                    0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                }),
                Section.U32(prefix + "mesh.face_counts", new uint[] { 4 }),
                Section.U32(prefix + "mesh.face_indices", new uint[] { 0, 1, 2, 3 }),
            };
        }

        private static Section AttrValue(string name, float value)
        {
            return Section.Floats(name, new float[] { value });
        }

        private static Section Identity(string name)
        {
            return Section.Floats(name, new float[]
            {
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            });
        }

        private static void Rows(List<Section> sections, string prefix, uint definition)
        {
            sections.Add(Section.U32(prefix + "inst.ref_def", new uint[] { definition }));
            sections.Add(Identity(prefix + "inst.transforms"));
        }

        private static Dictionary<string, object> Descriptor(string group, string name,
            string type, string domain, long count, string section,
            long? definition, long? part)
        {
            var entry = new Dictionary<string, object>();
            entry["group"] = group;
            entry["name"] = name;
            entry["type"] = type;
            entry["domain"] = domain;
            entry["components"] = 1L;
            entry["count"] = count;
            entry["section"] = section;
            if (definition.HasValue) entry["definition"] = definition.Value;
            if (part.HasValue) entry["part"] = part.Value;
            return entry;
        }

        private static List<Dictionary<string, object>> CopyDescriptors(
            List<Dictionary<string, object>> source)
        {
            var result = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> descriptor in source)
                result.Add(new Dictionary<string, object>(descriptor));
            return result;
        }

        private static List<Section> CopySections(List<Section> source)
        {
            var result = new List<Section>();
            foreach (Section section in source)
                result.Add(new Section(section.Name, section.Dtype, (byte[])section.Raw.Clone()));
            return result;
        }

        private static Section Find(List<Section> sections, string name)
        {
            foreach (Section section in sections)
                if (section.Name == name) return section;
            return null;
        }

        private static string RootNames(ArchivableDictionary root)
        {
            return "[" + string.Join(",", AttributeStore.Names(root)) + "]";
        }

        private static bool HasNote(List<string> notes, string fragment)
        {
            foreach (string note in notes)
                if (note.IndexOf(fragment, StringComparison.Ordinal) >= 0) return true;
            return false;
        }

        private static string Notes(List<string> notes)
        {
            return notes.Count == 0 ? "(none)" : string.Join(" | ", notes.ToArray());
        }

        private static string DescribePlan(Instances.DefinitionPlan plan)
        {
            if (plan == null) return "no plan";
            string parts = "";
            foreach (Instances.PartPlan part in plan.Parts)
                parts += (parts.Length > 0 ? "," : "") + part.Part + "=" + part.Prefix;
            return "name=" + plan.Name + " parts=" + plan.PartCount
                + " [" + parts + "] nested=" + plan.NestedPrefix;
        }

        // The pre-Q10b2a single-part hash algorithm, kept here so the test
        // proves that a valid single-part frame still produces the same
        // fallback name. Multi-part definitions deliberately add the part
        // count, part sections and descriptors.
        private static string LegacySingleHash(DecodedFrame frame, long index,
                                               double scale, List<string> notes)
        {
            string prefix = "idef." + index + ".";
            var byName = new Dictionary<string, Section>();
            foreach (Section section in frame.Sections)
                if (section.Name.StartsWith(prefix, StringComparison.Ordinal))
                    byName[section.Name.Substring(prefix.Length)] = section;

            long[] nested;
            List<Transform> nestedTransforms;
            Instances.ReadRows(frame, prefix, scale, out nested, out nestedTransforms,
                delegate(string text) { notes.Add(text); });

            using (var sha = SHA256.Create())
            {
                foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                    LegacyUpdate(sha, name, byName[name].Raw);
                if (nested != null)
                {
                    for (int i = 0; i < nested.Length; i++)
                    {
                        LegacyUpdate(sha, "inst.ref_def",
                            BitConverter.GetBytes((uint)nested[i]));
                        var m = Conversion.TransformToWire(nestedTransforms[i], scale);
                        var raw = new byte[m.Length * 4];
                        Buffer.BlockCopy(m, 0, raw, 0, raw.Length);
                        LegacyUpdate(sha, "inst.transforms", raw);
                    }
                    for (int i = 0; i < nested.Length; i++)
                        LegacyUpdate(sha, "inst.name",
                            System.Text.Encoding.UTF8.GetBytes(LegacyName(frame, nested[i])));
                    for (int i = 0; i < nested.Length; i++)
                        LegacyUpdate(sha, "inst.content",
                            System.Text.Encoding.UTF8.GetBytes(
                                LegacySingleHash(frame, nested[i], scale, notes)));
                }
                sha.TransformFinalBlock(new byte[0], 0, 0);
                var text = new System.Text.StringBuilder();
                for (int i = 0; i < sha.Hash.Length && text.Length < 12; i++)
                    text.Append(sha.Hash[i].ToString("x2",
                        System.Globalization.CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        private static string LegacyName(DecodedFrame frame, long index)
        {
            var table = Json.Get(frame.Header, "definitions") as System.Collections.IList;
            if (table == null || index < 0 || index >= table.Count) return "";
            var entry = table[(int)index] as Dictionary<string, object>;
            return entry == null ? "" : (Json.GetString(entry, "name") ?? "");
        }

        private static void LegacyUpdate(HashAlgorithm sha, string name, byte[] raw)
        {
            byte[] encoded = System.Text.Encoding.UTF8.GetBytes(name);
            sha.TransformBlock(encoded, 0, encoded.Length, null, 0);
            sha.TransformBlock(new byte[] { 0 }, 0, 1, null, 0);
            sha.TransformBlock(raw, 0, raw.Length, null, 0);
        }
    }
}
