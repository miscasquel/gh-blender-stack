// Instances: Blender's instance rows <-> Rhino block instances (protocol §5.2).
//
// Both directions work graph-side, the way Grasshopper's own Model Block
// Definition/Instance components do (probed live, 2026-10-16):
//
// - A definition is a `ModelInstanceDefinition` built from Attributes with a
//   Name set. Anonymous attributes-built definitions are invalid; a named one
//   is valid, previews, and bakes. No document write happens at solve time:
//   Rhino materializes the blocks on first bake, and later bakes reuse them.
// - A nested placement is a `GH_InstanceReference` (paired with the child
//   definition) wrapped by `ModelObject.Cast`, which is exactly what the
//   component workflow does when a Block Instance feeds a Block Definition's
//   Objects input.
// - References to one definition share a stable id for the solve, so Stage Out
//   collapses the rows back into one definition table entry.
//
// Stage In: a frame's `definitions` table and `idef.<i>.*` groups become those
// definitions; the `inst.*` rows become GH_InstanceReference items.
//
// Stage Out: GH_InstanceReference items become the same table and rows. A
// definition's geometry comes from its paired ModelInstanceDefinition.Objects;
// a reference with no pairing (a document block by id) falls back to the
// document definition table.
//
// Definition geometry is read and written through the same Conversion and
// AttributeStore code as the frame's own geometry, under the `idef.<i>.`
// prefix. A multi-part definition (`definitions[i].parts > 1`, protocol §5.2)
// reads each part from its own `idef.<i>.<p>.` namespace and keeps nested rows
// at the definition level (`idef.<i>.inst.*`); single-part frames still read
// the original namespace unchanged.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using Grasshopper.Kernel.Types;
using Grasshopper.Rhinoceros.Model;
using Rhino;
using Rhino.DocObjects;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom
{
    public static class Instances
    {
        /// <summary>The name given to definitions Blender did not name.</summary>
        public const string DefinitionPrefix = "Stageloom ";

        /// <summary>The protocol's defensive definition-part cap, the same
        /// bound Blender's apply path enforces (4096).</summary>
        internal const int MaxDefinitionParts = 4096;

        /// <summary>One decoded table entry. A malformed entry stays visible
        /// as a problem instead of becoming a silent default (protocol §5.2).</summary>
        internal sealed class TableEntry
        {
            public readonly string Name;
            public readonly int Parts;

            public TableEntry(string name, int parts)
            {
                Name = name;
                Parts = parts;
            }
        }

        /// <summary>Definition rows and transforms, decoded (protocol §5.2).
        /// Returns false when the pair is absent or malformed; both halves are
        /// all-or-nothing and the lengths must agree exactly. The wire is
        /// metres, so every placement translation is converted to document
        /// units with <paramref name="scale"/>.</summary>
        public static bool ReadRows(DecodedFrame frame, string prefix, double scale,
                                    out long[] refDef, out List<Transform> transforms,
                                    Action<string> note = null)
        {
            refDef = null;
            transforms = null;
            Section refs = frame.Find(prefix + "inst.ref_def");
            Section mats = frame.Find(prefix + "inst.transforms");
            string where = string.IsNullOrEmpty(prefix) ? "inst" : prefix + "inst";
            if (refs == null && mats == null) return false;
            if (refs == null || mats == null)
            {
                RowNote(note, where, mats == null ? "inst.transforms is missing"
                                                  : "inst.ref_def is missing");
                return false;
            }

            string problem = RowSectionProblem(refs, "u32");
            if (problem == null) problem = RowSectionProblem(mats, "f32");
            if (problem != null)
            {
                RowNote(note, where, problem);
                return false;
            }
            if (mats.Raw.Length % 64 != 0)
            {
                RowNote(note, where, "inst.transforms holds " + mats.Raw.Length
                    + " bytes; each row is 16 f32 values");
                return false;
            }

            uint[] defs = refs.ToU32();
            float[] raw = mats.ToFloats();
            int rows = raw.Length / 16;
            if (defs.Length != rows)
            {
                RowNote(note, where, defs.Length + " definition indices for "
                    + rows + " transform row(s)");
                return false;
            }
            if (rows == 0) return false;

            refDef = new long[rows];
            transforms = new List<Transform>(rows);
            for (int i = 0; i < rows; i++)
            {
                refDef[i] = defs[i];
                var m = new float[16];
                Array.Copy(raw, i * 16, m, 0, 16);
                transforms.Add(Conversion.TransformFromWire(m, scale));
            }
            return true;
        }

        private static string RowSectionProblem(Section section, string dtype)
        {
            if (section.Dtype != dtype)
                return section.Name + " must be " + dtype + ", not "
                    + (section.Dtype ?? "(missing)");
            if (section.Raw == null) return section.Name + " has no bytes";
            if (section.Raw.Length % 4 != 0)
                return section.Name + " length " + section.Raw.Length
                    + " is not a multiple of 4";
            return null;
        }

        private static void RowNote(Action<string> note, string where, string problem)
        {
            if (note != null) note("instance rows " + where + ": " + problem);
        }

        /// <summary>The definitions table of a frame header, or null. Read
        /// through non-generic IList: a frame decoded from bytes carries
        /// List&lt;object&gt;, one rebuilt in memory from a StageSlot carries
        /// List&lt;Dictionary&lt;string,object&gt;&gt;, and a C# List&lt;T&gt;
        /// never casts across (invariance) — a plain `as List&lt;object&gt;`
        /// silently drops the whole table for one of the two shapes.</summary>
        private static System.Collections.IList DefinitionTable(DecodedFrame frame)
        {
            return Json.Get(frame.Header, "definitions") as System.Collections.IList;
        }

        /// <summary>A JSON integer that is not a boolean. Header numbers are
        /// long; int is accepted for in-memory callers. A double, a bool or a
        /// string is not an integer on the wire (protocol §5.2).</summary>
        internal static bool TryInteger(object raw, out long value)
        {
            if (raw is long) { value = (long)raw; return true; }
            if (raw is int) { value = (int)raw; return true; }
            value = 0;
            return false;
        }

        /// <summary>One table entry, validated (name string, parts a positive
        /// integer within the cap), or false with the reason. The caller
        /// decides whether a bad entry is a note or a skip; it is never an
        /// invalid cast into default values.</summary>
        internal static bool TryEntry(DecodedFrame frame, long index,
                                      out TableEntry entry, out string problem)
        {
            entry = null;
            problem = null;
            System.Collections.IList table = DefinitionTable(frame);
            if (table == null)
            {
                problem = "definitions is not an array";
                return false;
            }
            if (index < 0 || index >= table.Count)
            {
                problem = "definition " + index + " is outside the definitions table";
                return false;
            }
            var raw = table[(int)index] as Dictionary<string, object>;
            if (raw == null)
            {
                problem = "definition " + index + " entry is not an object";
                return false;
            }

            string name = "";
            object rawName;
            if (raw.TryGetValue("name", out rawName))
            {
                name = rawName as string;
                if (name == null)
                {
                    problem = "definition " + index + " name is not a string";
                    return false;
                }
            }

            int parts = 1;
            object rawParts;
            if (raw.TryGetValue("parts", out rawParts))
            {
                long value;
                if (!TryInteger(rawParts, out value) || value < 1)
                {
                    problem = "definition " + index + " parts is not a positive integer";
                    return false;
                }
                if (value > MaxDefinitionParts)
                {
                    problem = "definition " + index + " parts is " + value
                        + ", over the " + MaxDefinitionParts + "-part cap";
                    return false;
                }
                parts = (int)value;
            }
            entry = new TableEntry(name, parts);
            return true;
        }

        /// <summary>The display name of a definition table entry, or "".</summary>
        private static string NameOf(DecodedFrame frame, long index)
        {
            TableEntry entry;
            string problem;
            return TryEntry(frame, index, out entry, out problem) ? entry.Name : "";
        }

        // ==================================================================
        //  Stage In: frame -> graph-side named definitions + references
        // ==================================================================

        /// <summary>Append the frame's instances to the stage geometry as
        /// GH_InstanceReference items. Does nothing when the frame carries no
        /// definitions table; a definitions table without readable rows is a
        /// note, not a silent empty stage.</summary>
        public static void Build(DecodedFrame frame, double scale,
                                 List<Dictionary<string, object>> attributes,
                                 List<IGH_Goo> geometry, Action<string> note)
        {
            if (Json.Get(frame.Header, "definitions") == null) return;

            var builder = new DefinitionBuilder(frame, scale, attributes, note);
            long[] refDef;
            List<Transform> transforms;
            if (!ReadRows(frame, "", scale, out refDef, out transforms, note))
            {
                if (frame.Find("inst.ref_def") == null
                    && frame.Find("inst.transforms") == null && note != null)
                    note("definitions arrived without instance rows");
                return;
            }

            for (int row = 0; row < refDef.Length; row++)
            {
                ModelInstanceDefinition definition = builder.Definition(refDef[row]);
                if (definition == null) continue;
                var reference = new InstanceReferenceGeometry(
                    builder.ReferenceId(refDef[row]), transforms[row]);
                geometry.Add(new GH_InstanceReference(reference, definition));
            }
        }

        /// <summary>One part of a definition. A single-part definition has one
        /// plan with Part -1; a multi-part definition has one per index in
        /// 0..N-1, in that order.</summary>
        internal sealed class PartPlan
        {
            public readonly int Part;
            public readonly string Prefix;

            public PartPlan(int part, string prefix)
            {
                Part = part;
                Prefix = prefix;
            }
        }

        /// <summary>The validated namespace plan of one definition: the parts
        /// to build in order and the definition-level nested-row prefix.
        /// Validation is separate from geometry construction so a malformed
        /// frame can be reported without touching Rhino types.</summary>
        internal sealed class DefinitionPlan
        {
            public readonly long Index;
            public readonly string Name;
            public readonly int PartCount;
            public readonly string NestedPrefix;
            public readonly List<PartPlan> Parts = new List<PartPlan>();

            public DefinitionPlan(long index, string name, int partCount)
            {
                Index = index;
                Name = name ?? "";
                PartCount = partCount;
                NestedPrefix = "idef." + index.ToString(CultureInfo.InvariantCulture) + ".";
            }
        }

        /// <summary>A section name inside a definition group. Part is -1 for a
        /// definition-level section (`idef.<i>.inst.*`, or any single-part
        /// section), otherwise the parsed part index.</summary>
        internal sealed class DefinitionSection
        {
            public readonly Section Section;
            public readonly string Name;
            public readonly long Index;
            public readonly int Part;
            public readonly string IndexToken;
            public readonly string PartToken;
            public readonly string Inner;

            public DefinitionSection(Section section, string name, long index, int part,
                                     string indexToken, string partToken, string inner)
            {
                Section = section;
                Name = name;
                Index = index;
                Part = part;
                IndexToken = indexToken;
                PartToken = partToken;
                Inner = inner;
            }
        }

        /// <summary>Builds the frame's definitions graph-side, children first
        /// (a parent's Objects hold the child references). No document is
        /// touched: Rhino materializes the blocks when the user bakes.</summary>
        internal class DefinitionBuilder
        {
            private readonly DecodedFrame _frame;
            private readonly double _scale;
            private readonly List<Dictionary<string, object>> _attributes;
            private readonly Action<string> _note;
            private readonly Dictionary<long, ModelInstanceDefinition> _done = new Dictionary<long, ModelInstanceDefinition>();
            private readonly Dictionary<long, Guid> _referenceIds = new Dictionary<long, Guid>();
            private readonly HashSet<long> _visiting = new HashSet<long>();

            private readonly Dictionary<long, TableEntry> _entries = new Dictionary<long, TableEntry>();
            private readonly HashSet<long> _invalidEntries = new HashSet<long>();
            private readonly Dictionary<long, List<DefinitionSection>> _sections =
                new Dictionary<long, List<DefinitionSection>>();
            private readonly HashSet<long> _outsideDefinitions = new HashSet<long>();
            private readonly HashSet<long> _duplicateDefinitions = new HashSet<long>();
            private readonly HashSet<string> _duplicateParts = new HashSet<string>();
            private readonly Dictionary<long, string> _indexTokens = new Dictionary<long, string>();
            private readonly Dictionary<string, string> _partTokens = new Dictionary<string, string>();
            private readonly HashSet<Dictionary<string, object>> _badDescriptors =
                new HashSet<Dictionary<string, object>>();
            private readonly Dictionary<long, DefinitionPlan> _plans = new Dictionary<long, DefinitionPlan>();
            private readonly HashSet<long> _planFailed = new HashSet<long>();
            private readonly Dictionary<long, bool> _preflighted = new Dictionary<long, bool>();
            private readonly HashSet<long> _preflightVisiting = new HashSet<long>();
            private readonly HashSet<string> _blockedEdges = new HashSet<string>();
            private readonly HashSet<string> _reported = new HashSet<string>(StringComparer.Ordinal);

            public DefinitionBuilder(DecodedFrame frame, double scale,
                                     List<Dictionary<string, object>> attributes,
                                     Action<string> note)
            {
                _frame = frame;
                _scale = scale;
                _attributes = attributes;
                _note = note;

                ValidateTable();
                ScanSections();
                ValidateDescriptors();
            }

            /// <summary>The id every reference to one definition shares for this
            /// solve, so Stage Out can collapse the rows back to one entry.</summary>
            public Guid ReferenceId(long index)
            {
                Guid id;
                if (!_referenceIds.TryGetValue(index, out id))
                {
                    id = Guid.NewGuid();
                    _referenceIds[index] = id;
                }
                return id;
            }

            /// <summary>The validated plan of definition <paramref name="index"/>,
            /// or null with notes. The plan is cached: validation happens once,
            /// even when a definition is reached from several rows.</summary>
            internal DefinitionPlan Plan(long index)
            {
                DefinitionPlan cached;
                if (_plans.TryGetValue(index, out cached)) return cached;
                if (_planFailed.Contains(index)) return null;

                TableEntry entry;
                string problem;
                if (!_entries.TryGetValue(index, out entry))
                {
                    if (!_invalidEntries.Contains(index))
                    {
                        if (TryEntry(_frame, index, out entry, out problem)) _entries[index] = entry;
                        else
                        {
                            _invalidEntries.Add(index);
                            Note(problem);
                        }
                    }
                    if (!_entries.TryGetValue(index, out entry))
                    {
                        _planFailed.Add(index);
                        return null;
                    }
                }
                if (_duplicateDefinitions.Contains(index))
                {
                    _planFailed.Add(index);
                    return null;
                }

                var plan = new DefinitionPlan(index, entry.Name, entry.Parts);
                List<DefinitionSection> sections;
                if (!_sections.TryGetValue(index, out sections))
                    sections = new List<DefinitionSection>();

                for (int i = 0; i < sections.Count; i++)
                {
                    DefinitionSection section = sections[i];
                    string where = section.Name;
                    if (entry.Parts == 1)
                    {
                        if (section.Part >= 0)
                        {
                            Note("section " + where
                                + " uses the multi-part namespace on single-part definition "
                                + index + "; definition skipped");
                            return FailPlan(index);
                        }
                        string head = HeadOf(section.Inner);
                        if (!AllowedSingle(head))
                        {
                            Note("section " + where
                                + " does not parse as a single-part definition section; definition "
                                + index + " skipped");
                            return FailPlan(index);
                        }
                    }
                    else
                    {
                        if (section.Part < 0)
                        {
                            if (!section.Inner.StartsWith("inst.", StringComparison.Ordinal))
                            {
                                Note("section " + where
                                    + " is definition-level in multi-part definition " + index
                                    + "; definition skipped");
                                return FailPlan(index);
                            }
                        }
                        else
                        {
                            if (section.Part >= entry.Parts)
                            {
                                Note("section " + where + " names part " + section.Part
                                    + " outside 0.." + (entry.Parts - 1)
                                    + " of definition " + index + "; definition skipped");
                                return FailPlan(index);
                            }
                            string head = HeadOf(section.Inner);
                            if (!AllowedPart(head))
                            {
                                Note("section " + where
                                    + " does not parse as a part section of definition " + index
                                    + "; definition skipped");
                                return FailPlan(index);
                            }
                            string partKey = PartKey(index, section.Part);
                            if (_duplicateParts.Contains(partKey))
                            {
                                Note("definition " + index + " names part " + section.Part
                                    + " under more than one namespace token; definition skipped");
                                return FailPlan(index);
                            }
                        }
                    }
                }

                if (entry.Parts == 1)
                {
                    plan.Parts.Add(new PartPlan(-1, plan.NestedPrefix));
                }
                else
                {
                    for (int part = 0; part < entry.Parts; part++)
                        plan.Parts.Add(new PartPlan(part, plan.NestedPrefix
                            + part.ToString(CultureInfo.InvariantCulture) + "."));
                }

                // Completeness and dtype validation of each part's groups is
                // pure managed code; catching it here means a partial group
                // skips the definition with a note instead of failing halfway
                // through geometry construction.
                for (int i = 0; i < plan.Parts.Count; i++)
                {
                    PartPlan part = plan.Parts[i];
                    try
                    {
                        Conversion.MeshGroup group = Conversion.SelectMeshGroup(_frame, part.Prefix);
                        if (group != null) Conversion.ReadMeshData(group);
                        Conversion.ReadCurveData(_frame, part.Prefix);
                        ValidatePointSections(part.Prefix);
                    }
                    catch (FrameException ex)
                    {
                        Note("definition " + index
                            + (part.Part >= 0 ? " part " + part.Part : "") + ": "
                            + ex.Message + "; definition skipped");
                        return FailPlan(index);
                    }
                }

                _plans[index] = plan;
                return plan;
            }

            private DefinitionPlan FailPlan(long index)
            {
                _planFailed.Add(index);
                return null;
            }

            /// <summary>The point-cloud group has no Curves/Mesh-style decoder
            /// owner, so a wrong dtype or a partial group would be silently
            /// reinterpreted by the point-cloud builder. Validate the whole
            /// group here, before any Rhino geometry exists.</summary>
            private void ValidatePointSections(string prefix)
            {
                Section positions = _frame.Find(prefix + "pts.positions");
                Section radii = _frame.Find(prefix + "pts.radii");
                Section colors = _frame.Find(prefix + "pts.colors");
                if (positions == null && radii == null && colors == null) return;
                if (positions == null)
                    throw new FrameException("bad_frame",
                        "point group is incomplete: " + prefix + "pts.positions is missing");
                if (positions.Dtype != "f32")
                    throw new FrameException("bad_frame", positions.Name + " must be f32, not "
                        + (positions.Dtype ?? "(missing)"));
                if (positions.Raw == null)
                    throw new FrameException("bad_frame", positions.Name + " has no bytes");
                if (positions.Raw.Length % 12 != 0)
                    throw new FrameException("bad_frame", positions.Name + " has "
                        + positions.Raw.Length + " bytes; positions are xyz triples");
                int points = positions.Raw.Length / 12;

                if (radii != null)
                {
                    if (radii.Dtype != "f32")
                        throw new FrameException("bad_frame", radii.Name + " must be f32, not "
                            + (radii.Dtype ?? "(missing)"));
                    if (radii.Raw == null || radii.Raw.Length != points * 4)
                        throw new FrameException("bad_frame", radii.Name + " has "
                            + (radii.Raw == null ? 0 : radii.Raw.Length) + " bytes for "
                            + points + " point(s)");
                }
                if (colors != null)
                {
                    if (colors.Dtype != "u8")
                        throw new FrameException("bad_frame", colors.Name + " must be u8, not "
                            + (colors.Dtype ?? "(missing)"));
                    if (colors.Raw == null || colors.Raw.Length != points * 4)
                        throw new FrameException("bad_frame", colors.Name + " has "
                            + (colors.Raw == null ? 0 : colors.Raw.Length) + " bytes for "
                            + points + " point(s); colours are RGBA");
                }
            }

            /// <summary>Build one definition. Children are built first, so a
            /// parent's Objects carry their already-resolved references. A
            /// multi-part definition with no geometry and no nested rows is
            /// still a definition (protocol §5.2); a single-part empty one is
            /// skipped as before.</summary>
            public ModelInstanceDefinition Definition(long index)
            {
                ModelInstanceDefinition cached;
                if (_done.TryGetValue(index, out cached)) return cached;
                if (!Preflight(index)) return null;

                DefinitionPlan plan = Plan(index);
                if (plan == null) return null;
                if (!_visiting.Add(index))
                {
                    Note("definition " + index + " is nested inside itself; the row is skipped");
                    return null;
                }

                try
                {
                    var objects = new List<ModelObject>();
                    for (int i = 0; i < plan.Parts.Count; i++)
                        BuildPart(objects, plan, plan.Parts[i]);

                    long[] nested;
                    List<Transform> nestedTransforms;
                    if (ReadRows(_frame, plan.NestedPrefix, _scale,
                                 out nested, out nestedTransforms, _note))
                    {
                        for (int row = 0; row < nested.Length; row++)
                        {
                            if (IsBlocked(index, row)) continue;
                            ModelInstanceDefinition child = Definition(nested[row]);
                            if (child == null) continue;
                            var nestedGoo = new GH_InstanceReference(new InstanceReferenceGeometry(
                                ReferenceId(nested[row]), nestedTransforms[row]));
                            nestedGoo.InstanceDefinition = child;
                            AddObject(objects, nestedGoo, "definition " + index + " nested block");
                        }
                    }

                    if (objects.Count == 0 && plan.PartCount <= 1)
                    {
                        Note("definition " + index + " is empty; skipped");
                        return null;
                    }

                    string from = plan.Name;
                    var definitionAttributes = new ModelInstanceDefinition.Attributes();
                    definitionAttributes.Objects = objects;
                    definitionAttributes.Name = string.IsNullOrEmpty(from)
                        ? DefinitionPrefix + ContentHash(index)
                        : from;
                    var definition = new ModelInstanceDefinition(definitionAttributes);
                    if (!definition.IsValid)
                    {
                        Note("definition " + index + " (" + definitionAttributes.Name + ") came back invalid");
                        return null;
                    }

                    _done[index] = definition;
                    return definition;
                }
                catch (FrameException ex)
                {
                    Note("definition " + index + " could not be built: " + ex.Message);
                    return null;
                }
                finally
                {
                    _visiting.Remove(index);
                }
            }

            private void BuildPart(List<ModelObject> objects, DefinitionPlan plan, PartPlan part)
            {
                string prefix = part.Prefix;
                string label = plan.PartCount > 1
                    ? "definition " + plan.Index + " part " + part.Part
                    : "definition " + plan.Index;
                long? partFilter = part.Part >= 0 ? (long?)part.Part : null;

                Conversion.MeshGroup group = Conversion.SelectMeshGroup(_frame, prefix);
                Mesh mesh = Conversion.MeshFromSections(_frame, _scale, prefix);
                if (mesh != null && mesh.Vertices.Count > 0)
                {
                    string attributeGroup = group != null && group.Kind == "subd" ? "subd" : "mesh";
                    AttributeStore.Attach(mesh, attributeGroup, PartDescriptors(plan, part),
                                          _frame.Sections, plan.Index, _note, partFilter);
                    AddObject(objects, new GH_Mesh(mesh), label + " mesh");
                }

                var curves = Conversion.CurvesFromSections(_frame, _scale, prefix);
                if (curves.Count > 0)
                {
                    Section countsSection = _frame.Find(prefix + "crv.counts");
                    uint[] counts = countsSection != null ? countsSection.ToU32() : null;
                    AttributeStore.AttachCurves(curves, counts, PartDescriptors(plan, part),
                                                _frame.Sections, plan.Index, _note, partFilter);
                    foreach (Curve curve in curves)
                        AddObject(objects, new GH_Curve(curve), label + " curve");
                }

                PointCloud cloud = Conversion.PointCloudFromSections(_frame, _scale, prefix);
                if (cloud != null)
                {
                    AttributeStore.Attach(cloud, "pts", PartDescriptors(plan, part),
                                          _frame.Sections, plan.Index, _note, partFilter);
                    AddObject(objects, new GH_PointCloud(cloud), label + " point cloud");
                }
            }

            private bool AddObject(List<ModelObject> objects, IGH_Goo goo, string label)
            {
                ModelObject modelObject = ModelObject.Cast(goo);
                if (modelObject == null || !modelObject.IsValid)
                {
                    Note("a " + label + " object has no model form and was skipped");
                    return false;
                }
                objects.Add(modelObject);
                return true;
            }

            /// <summary>Walk a definition and its nested rows before building
            /// anything. Out-of-table references and cycles become notes and
            /// blocked edges, so construction never recurses forever and the
            /// cyclic row is skipped rather than silently kept.</summary>
            internal bool Preflight(long index)
            {
                bool known;
                if (_preflighted.TryGetValue(index, out known)) return known;
                if (!_preflightVisiting.Add(index)) return false;

                bool ok = false;
                try
                {
                    DefinitionPlan plan = Plan(index);
                    if (plan == null) return false;

                    long[] nested;
                    List<Transform> nestedTransforms;
                    if (ReadRows(_frame, plan.NestedPrefix, _scale,
                                 out nested, out nestedTransforms, _note))
                    {
                        for (int row = 0; row < nested.Length; row++)
                        {
                            long child = nested[row];
                            if (!_entries.ContainsKey(child))
                            {
                                Note("definition " + index + " row " + row
                                    + " references definition " + child
                                    + ", which the frame does not carry; the row is skipped");
                                Block(index, row);
                                continue;
                            }
                            if (_preflightVisiting.Contains(child))
                            {
                                Note("definition " + index + " nests definition " + child
                                    + " inside itself; the row is skipped");
                                Block(index, row);
                                continue;
                            }
                            if (!Preflight(child)) Block(index, row);
                        }
                    }
                    ok = true;
                    return true;
                }
                finally
                {
                    _preflightVisiting.Remove(index);
                    _preflighted[index] = ok;
                }
            }

            private static string EdgeKey(long index, int row)
            {
                return index.ToString(CultureInfo.InvariantCulture) + ":"
                    + row.ToString(CultureInfo.InvariantCulture);
            }

            private void Block(long index, int row)
            {
                _blockedEdges.Add(EdgeKey(index, row));
            }

            private bool IsBlocked(long index, int row)
            {
                return _blockedEdges.Contains(EdgeKey(index, row));
            }

            private List<Dictionary<string, object>> PartDescriptors(DefinitionPlan plan, PartPlan part)
            {
                var result = new List<Dictionary<string, object>>();
                if (_attributes == null) return result;
                foreach (Dictionary<string, object> descriptor in _attributes)
                {
                    if (descriptor == null || _badDescriptors.Contains(descriptor)) continue;
                    long definition;
                    if (!DescriptorDefinition(descriptor, out definition)
                        || definition != plan.Index) continue;
                    long descriptorPart;
                    bool hasPart;
                    if (!DescriptorPart(descriptor, out descriptorPart, out hasPart)) continue;
                    if (plan.PartCount == 1)
                    {
                        if (hasPart) continue;
                    }
                    else if (!hasPart || descriptorPart != part.Part) continue;
                    result.Add(descriptor);
                }
                return result;
            }

            // -------------------------------------------------------- table

            private void ValidateTable()
            {
                System.Collections.IList table = DefinitionTable(_frame);
                if (table == null)
                {
                    if (Json.Get(_frame.Header, "definitions") != null)
                        Note("definitions is not an array; no instance definitions are read");
                    return;
                }
                for (int i = 0; i < table.Count; i++)
                {
                    TableEntry entry;
                    string problem;
                    if (TryEntry(_frame, i, out entry, out problem)) _entries[i] = entry;
                    else
                    {
                        _invalidEntries.Add(i);
                        Note(problem);
                    }
                }
            }

            private void ScanSections()
            {
                foreach (Section section in _frame.Sections)
                {
                    if (section == null || section.Name == null
                        || !section.Name.StartsWith("idef.", StringComparison.Ordinal)) continue;

                    DefinitionSection parsed;
                    string problem;
                    if (!TryParseSection(section, section.Name, out parsed, out problem))
                    {
                        Note("section " + section.Name + " " + problem + "; ignored");
                        continue;
                    }
                    if (!_entries.ContainsKey(parsed.Index))
                    {
                        if (_invalidEntries.Contains(parsed.Index)) continue;
                        if (_outsideDefinitions.Add(parsed.Index))
                            Note("section " + section.Name + " names definition " + parsed.Index
                                + " outside the definitions table; ignored");
                        continue;
                    }

                    string knownIndexToken;
                    if (_indexTokens.TryGetValue(parsed.Index, out knownIndexToken)
                        && knownIndexToken != parsed.IndexToken)
                    {
                        if (_duplicateDefinitions.Add(parsed.Index))
                            Note("definition " + parsed.Index + " names its sections with both "
                                + "\"idef." + knownIndexToken + ".\" and \"idef."
                                + parsed.IndexToken + ".\"; definition skipped");
                    }
                    else
                    {
                        _indexTokens[parsed.Index] = parsed.IndexToken;
                    }
                    if (parsed.IndexToken
                        != parsed.Index.ToString(CultureInfo.InvariantCulture)
                        && _duplicateDefinitions.Add(parsed.Index))
                    {
                        Note("definition " + parsed.Index + " uses the non-canonical namespace token \""
                            + parsed.IndexToken + "\"; definition skipped");
                    }

                    if (parsed.Part >= 0)
                    {
                        string key = PartKey(parsed.Index, parsed.Part);
                        string knownPartToken;
                        if (_partTokens.TryGetValue(key, out knownPartToken)
                            && knownPartToken != parsed.PartToken)
                        {
                            if (_duplicateParts.Add(key))
                                Note("definition " + parsed.Index + " part " + parsed.Part
                                    + " names its sections with both \"" + knownPartToken
                                    + ".\" and \"" + parsed.PartToken + ".\"; definition skipped");
                        }
                        else
                        {
                            _partTokens[key] = parsed.PartToken;
                        }
                        if (parsed.PartToken
                            != parsed.Part.ToString(CultureInfo.InvariantCulture)
                            && _duplicateParts.Add(key))
                        {
                            Note("definition " + parsed.Index + " part " + parsed.Part
                                + " uses the non-canonical namespace token \""
                                + parsed.PartToken + "\"; definition skipped");
                        }
                    }

                    List<DefinitionSection> list;
                    if (!_sections.TryGetValue(parsed.Index, out list))
                    {
                        list = new List<DefinitionSection>();
                        _sections[parsed.Index] = list;
                    }
                    list.Add(parsed);
                }
            }

            /// <summary>Parse `idef.&lt;i&gt;.&lt;rest&gt;`; a leading numeric
            /// fragment in rest is a part namespace. ASCII digits only, the
            /// same split the Python apply path and the hash order use.</summary>
            private static bool TryParseSection(Section section, string name,
                                                out DefinitionSection parsed, out string problem)
            {
                parsed = null;
                problem = null;
                if (string.IsNullOrEmpty(name)
                    || !name.StartsWith("idef.", StringComparison.Ordinal))
                {
                    problem = "is not a definition section";
                    return false;
                }
                string rest = name.Substring("idef.".Length);
                int dot = rest.IndexOf('.');
                if (dot <= 0 || dot == rest.Length - 1)
                {
                    problem = "does not parse as a definition section";
                    return false;
                }
                string indexToken = rest.Substring(0, dot);
                int index;
                if (!TryIndexToken(indexToken, out index))
                {
                    problem = "does not parse as a definition section";
                    return false;
                }

                string tail = rest.Substring(dot + 1);
                int part = -1;
                string partToken = null;
                string inner = tail;
                int partDot = tail.IndexOf('.');
                if (partDot > 0 && partDot < tail.Length - 1)
                {
                    string head = tail.Substring(0, partDot);
                    int value;
                    if (TryIndexToken(head, out value))
                    {
                        part = value;
                        partToken = head;
                        inner = tail.Substring(partDot + 1);
                    }
                }
                parsed = new DefinitionSection(section, name, index, part,
                                               indexToken, partToken, inner);
                return true;
            }

            private static bool TryIndexToken(string token, out int value)
            {
                value = 0;
                if (string.IsNullOrEmpty(token)) return false;
                for (int i = 0; i < token.Length; i++)
                    if (token[i] < '0' || token[i] > '9') return false;
                return int.TryParse(token, NumberStyles.None,
                                    CultureInfo.InvariantCulture, out value);
            }

            private static string HeadOf(string inner)
            {
                if (string.IsNullOrEmpty(inner)) return "";
                int dot = inner.IndexOf('.');
                return dot < 0 ? "" : inner.Substring(0, dot);
            }

            private static bool AllowedSingle(string head)
            {
                return head == "mesh" || head == "subd" || head == "crv"
                    || head == "pts" || head == "attr" || head == "inst";
            }

            private static bool AllowedPart(string head)
            {
                return head == "mesh" || head == "subd" || head == "crv"
                    || head == "pts" || head == "attr";
            }

            private static string PartKey(long index, long part)
            {
                return index.ToString(CultureInfo.InvariantCulture) + ":"
                    + part.ToString(CultureInfo.InvariantCulture);
            }

            // ---------------------------------------------------- attributes

            /// <summary>Check every descriptor that names a definition once,
            /// against the table and the section prefix it carries. A bad
            /// descriptor is skipped with a note; the definition still builds
            /// so one malformed attribute cannot erase its geometry.</summary>
            private void ValidateDescriptors()
            {
                if (_attributes == null) return;
                foreach (Dictionary<string, object> descriptor in _attributes)
                {
                    if (descriptor == null)
                    {
                        Note("attribute descriptor is null; skipped");
                        continue;
                    }
                    string label = "attribute " + (Json.GetString(descriptor, "name") ?? "(unnamed)");

                    bool hasDefinition;
                    object rawDefinition;
                    hasDefinition = descriptor.TryGetValue("definition", out rawDefinition)
                        && rawDefinition != null;
                    long definition = -1;
                    long part = -1;
                    bool hasPart;
                    object rawPart;
                    hasPart = descriptor.TryGetValue("part", out rawPart) && rawPart != null;

                    if (!hasDefinition)
                    {
                        if (hasPart)
                        {
                            Note(label + " has a part but no definition; skipped");
                            _badDescriptors.Add(descriptor);
                            continue;
                        }
                        string baseSection = Json.GetString(descriptor, "section");
                        object baseOffsets;
                        string offsetsName = descriptor.TryGetValue("offsets", out baseOffsets)
                            && baseOffsets != null ? baseOffsets as string : null;
                        if ((baseSection != null
                                && baseSection.StartsWith("idef.", StringComparison.Ordinal))
                            || (offsetsName != null
                                && offsetsName.StartsWith("idef.", StringComparison.Ordinal)))
                        {
                            Note(label + " names a definition section without a definition index; skipped");
                            _badDescriptors.Add(descriptor);
                        }
                        continue;
                    }
                    if (!TryInteger(rawDefinition, out definition) || definition < 0)
                    {
                        Note(label + " has a definition that is not a non-negative integer; skipped");
                        _badDescriptors.Add(descriptor);
                        continue;
                    }
                    TableEntry entry;
                    if (!_entries.TryGetValue(definition, out entry))
                    {
                        Note(label + " names definition " + definition
                            + " outside the definitions table; skipped");
                        _badDescriptors.Add(descriptor);
                        continue;
                    }

                    if (hasPart)
                    {
                        if (!TryInteger(rawPart, out part) || part < 0)
                        {
                            Note(label + " has a part that is not a non-negative integer; skipped");
                            _badDescriptors.Add(descriptor);
                            continue;
                        }
                    }
                    if (entry.Parts == 1)
                    {
                        if (hasPart)
                        {
                            Note(label + " names part " + part
                                + " on single-part definition " + definition + "; skipped");
                            _badDescriptors.Add(descriptor);
                            continue;
                        }
                    }
                    else
                    {
                        if (!hasPart)
                        {
                            Note(label + " on multi-part definition " + definition
                                + " has no part; skipped");
                            _badDescriptors.Add(descriptor);
                            continue;
                        }
                        if (part >= entry.Parts)
                        {
                            Note(label + " names part " + part + " outside 0.."
                                + (entry.Parts - 1) + " of definition " + definition
                                + "; skipped");
                            _badDescriptors.Add(descriptor);
                            continue;
                        }
                    }

                    long expectedPart = hasPart ? part : -1;
                    string sectionName = Json.GetString(descriptor, "section");
                    if (!SectionMatches(sectionName, definition, expectedPart))
                    {
                        Note(label + " section does not match definition " + definition
                            + (expectedPart >= 0 ? " part " + expectedPart : "")
                            + "; skipped");
                        _badDescriptors.Add(descriptor);
                        continue;
                    }
                    object rawOffsets;
                    if (descriptor.TryGetValue("offsets", out rawOffsets) && rawOffsets != null)
                    {
                        string offsets = rawOffsets as string;
                        if (offsets == null
                            || !SectionMatches(offsets, definition, expectedPart))
                        {
                            Note(label + " offsets section does not match definition " + definition
                                + (expectedPart >= 0 ? " part " + expectedPart : "")
                                + "; skipped");
                            _badDescriptors.Add(descriptor);
                        }
                    }
                }
            }

            /// <summary>A section name belongs to definition `index` part
            /// `part` (-1 for the definition-level/single namespace).</summary>
            private static bool SectionMatches(string name, long index, long part)
            {
                DefinitionSection parsed;
                string problem;
                if (!TryParseSection(null, name, out parsed, out problem)) return false;
                return parsed.Index == index && parsed.Part == part
                    && HeadOf(parsed.Inner) == "attr";
            }

            private static bool DescriptorDefinition(Dictionary<string, object> descriptor,
                                                     out long definition)
            {
                definition = -1;
                object raw;
                if (!descriptor.TryGetValue("definition", out raw) || raw == null) return false;
                return TryInteger(raw, out definition) && definition >= 0;
            }

            private static bool DescriptorPart(Dictionary<string, object> descriptor,
                                               out long part, out bool hasPart)
            {
                part = -1;
                hasPart = false;
                object raw;
                if (!descriptor.TryGetValue("part", out raw) || raw == null) return true;
                hasPart = true;
                return TryInteger(raw, out part) && part >= 0;
            }

            // --------------------------------------------------------- hash

            /// <summary>A stable fallback name for an unnamed definition. A
            /// single-part definition hashes exactly as before: its sections in
            /// protocol order, then one entry per nested row. A multi-part
            /// definition additionally hashes its part count, the full part
            /// section names, and its part attribute descriptors, so two
            /// different partitions cannot share `Stageloom &lt;hash&gt;`.
            /// A rename, a moved row or a child's geometry still changes it.</summary>
            internal string ContentHash(long index)
            {
                string cached;
                if (_contentHashes.TryGetValue(index, out cached)) return cached;
                // A malformed frame can nest a definition inside itself; the
                // recursion is cut rather than allowed to run out the stack.
                if (!_hashing.Add(index)) return "cycle";

                try
                {
                    TableEntry entry;
                    string problem;
                    int parts = TryEntry(_frame, index, out entry, out problem) ? entry.Parts : 1;
                    string hash = parts > 1
                        ? MultiPartHash(index, parts)
                        : SinglePartHash(index);
                    _contentHashes[index] = hash;
                    return hash;
                }
                finally
                {
                    _hashing.Remove(index);
                }
            }

            private string SinglePartHash(long index)
            {
                string prefix = "idef." + index + ".";
                var byName = new Dictionary<string, Section>();
                foreach (Section section in _frame.Sections)
                    if (section.Name.StartsWith(prefix, StringComparison.Ordinal))
                        byName[section.Name.Substring(prefix.Length)] = section;

                long[] nested;
                List<Transform> nestedTransforms;
                ReadRows(_frame, prefix, _scale, out nested, out nestedTransforms);

                using (var sha = SHA256.Create())
                {
                    foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                        Update(sha, name, byName[name].Raw);
                    UpdateNested(sha, nested, nestedTransforms);
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    return Hex(sha.Hash, 12);
                }
            }

            private string MultiPartHash(long index, int parts)
            {
                string prefix = "idef." + index + ".";
                var byName = new Dictionary<string, Section>();
                foreach (Section section in _frame.Sections)
                    if (section.Name.StartsWith(prefix, StringComparison.Ordinal))
                        byName[section.Name] = section;

                long[] nested;
                List<Transform> nestedTransforms;
                ReadRows(_frame, prefix, _scale, out nested, out nestedTransforms);

                var descriptors = new List<Dictionary<string, object>>();
                if (_attributes != null)
                {
                    foreach (Dictionary<string, object> descriptor in _attributes)
                    {
                        long definition;
                        if (descriptor != null && DescriptorDefinition(descriptor, out definition)
                            && definition == index)
                            descriptors.Add(descriptor);
                    }
                }

                using (var sha = SHA256.Create())
                {
                    Update(sha, "definition.parts", BitConverter.GetBytes((long)parts));
                    foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                        Update(sha, name, byName[name].Raw);
                    byte[] descriptorBytes;
                    try
                    {
                        descriptorBytes = Json.WriteCanonicalUtf8(descriptors);
                    }
                    catch (JsonException)
                    {
                        descriptorBytes = System.Text.Encoding.UTF8.GetBytes("unhashable");
                    }
                    Update(sha, "attr.descriptors", descriptorBytes);
                    UpdateNested(sha, nested, nestedTransforms);
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    return Hex(sha.Hash, 12);
                }
            }

            private void UpdateNested(HashAlgorithm sha, long[] nested,
                                      List<Transform> nestedTransforms)
            {
                if (nested == null) return;
                for (int i = 0; i < nested.Length; i++)
                {
                    Update(sha, "inst.ref_def", BitConverter.GetBytes((uint)nested[i]));
                    var m = Conversion.TransformToWire(nestedTransforms[i], _scale);
                    var raw = new byte[m.Length * 4];
                    Buffer.BlockCopy(m, 0, raw, 0, raw.Length);
                    Update(sha, "inst.transforms", raw);
                }
                for (int i = 0; i < nested.Length; i++)
                    Update(sha, "inst.name",
                        System.Text.Encoding.UTF8.GetBytes(NameOf(_frame, nested[i])));
                for (int i = 0; i < nested.Length; i++)
                    Update(sha, "inst.content",
                        System.Text.Encoding.UTF8.GetBytes(ContentHash(nested[i])));
            }

            private readonly Dictionary<long, string> _contentHashes = new Dictionary<long, string>();
            private readonly HashSet<long> _hashing = new HashSet<long>();

            private void Note(string message)
            {
                if (_note == null || string.IsNullOrEmpty(message)) return;
                if (_reported.Add(message)) _note(message);
            }
        }

        // ==================================================================
        //  Stage Out: GH_InstanceReference -> definition table + rows
        // ==================================================================

        /// <summary>Write GH_InstanceReference items into a response: definitions
        /// the rows reference go into <paramref name="definitions"/> (header) and
        /// their geometry into <paramref name="sections"/> as `idef.<i>.*`; the
        /// rows themselves land in `inst.ref_def` / `inst.transforms`. Does
        /// nothing when the list is empty. <paramref name="counts"/>, when
        /// given, gains `definitions` and `instances` so a reader can tell an
        /// instanced response from a plain one without scanning sections.</summary>
        public static void Emit(IList<GH_InstanceReference> instances, double scale,
                                List<Section> sections,
                                List<Dictionary<string, object>> attributes,
                                List<Dictionary<string, object>> definitions,
                                List<WireWarning> warnings,
                                Dictionary<string, object> counts = null)
        {
            if (instances == null || instances.Count == 0) return;
            var writer = new DefinitionWriter(scale, sections, attributes, definitions, warnings);
            var refDef = new List<uint>();
            var transforms = new List<float>();
            foreach (GH_InstanceReference instance in instances)
            {
                if (instance == null || instance.Value == null)
                {
                    warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "An instance reference without a value was ignored"));
                    continue;
                }
                int index = writer.Definition(instance.InstanceDefinition, instance.Value.ParentIdefId);
                if (index < 0) continue;
                refDef.Add((uint)index);
                transforms.AddRange(Conversion.TransformToWire(instance.Value.Xform, scale));
            }
            if (refDef.Count == 0) return;
            sections.Add(Section.U32("inst.ref_def", refDef.ToArray()));
            sections.Add(Section.Floats("inst.transforms", transforms.ToArray()));
            if (counts != null)
            {
                counts["definitions"] = (long)definitions.Count;
                counts["instances"] = (long)refDef.Count;
            }
        }

        /// <summary>Text-only view of Emit's warnings.</summary>
        public static void Emit(IList<GH_InstanceReference> instances, double scale,
                                List<Section> sections,
                                List<Dictionary<string, object>> attributes,
                                List<Dictionary<string, object>> definitions,
                                List<string> warnings)
        {
            var rich = new List<WireWarning>();
            Emit(instances, scale, sections, attributes, definitions, rich);
            if (warnings == null) return;
            foreach (WireWarning warning in rich)
                warnings.Add(warning.Message);
        }

        /// <summary>Whether a definition carries anything the wire can name.
        /// A definition whose objects were all null (or none had a wire form)
        /// would otherwise become an empty block that Blender draws as nothing;
        /// the writer skips its instance row instead.</summary>
        internal static bool HasDefinitionContent(
            List<Mesh> meshes, List<Curve> curves, List<Point3d> points,
            List<SubD> subds, List<Brep> breps, List<GH_InstanceReference> nested)
        {
            if (meshes.Count > 0 || curves.Count > 0 || points.Count > 0
                || subds.Count > 0 || breps.Count > 0)
                return true;
            foreach (GH_InstanceReference goo in nested)
                if (goo != null && goo.Value != null) return true;
            return false;
        }

        private class DefinitionWriter
        {
            private readonly double _scale;
            private readonly List<Section> _sections;
            private readonly List<Dictionary<string, object>> _attributes;
            private readonly List<Dictionary<string, object>> _definitions;
            private readonly List<WireWarning> _warnings;
            private readonly Dictionary<string, int> _byKey = new Dictionary<string, int>();
            private readonly HashSet<string> _visiting = new HashSet<string>();

            public DefinitionWriter(double scale, List<Section> sections,
                                    List<Dictionary<string, object>> attributes,
                                    List<Dictionary<string, object>> definitions,
                                    List<WireWarning> warnings)
            {
                _scale = scale;
                _sections = sections;
                _attributes = attributes;
                _definitions = definitions;
                _warnings = warnings;
            }

            /// <summary>The table index for one instance's definition, writing
            /// the definition on first use.
            ///
            /// The paired definition is preferred: graph-side definitions (the
            /// ones Stage In builds, and the Model components') carry their
            /// content and their nested references as ModelObjects. A reference
            /// with no pairing names a document definition and is read from the
            /// table by id. Both routes share one key, so an id never lands in
            /// the table twice.</summary>
            public int Definition(ModelInstanceDefinition paired, Guid referenceId)
            {
                if (paired != null && paired.Objects != null && paired.Objects.Count > 0)
                    return WriteDefinition(paired, Key(paired, referenceId));

                if (referenceId != Guid.Empty)
                {
                    RhinoDoc document = RhinoDoc.ActiveDoc;
                    if (document != null && document.InstanceDefinitions.Find(referenceId, true) != null)
                        return DefinitionFromDocument(referenceId);
                    // A document block the id names is gone (deleted, another
                    // document): an empty table entry would be a phantom block,
                    // so the row is skipped loudly instead.
                    _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "An instance reference to a document block (" + referenceId.ToString("N")
                        + ") could not be resolved; the row was skipped"));
                    return -1;
                }
                if (paired == null || paired.Objects == null || paired.Objects.Count == 0)
                {
                    _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "An instance reference without a definition was skipped"));
                    return -1;
                }
                return WriteDefinition(paired, Key(paired, referenceId));
            }

            private int WriteDefinition(ModelInstanceDefinition paired, string key)
            {
                int known;
                if (key != null && _byKey.TryGetValue(key, out known)) return known;

                var meshes = new List<Mesh>();
                var curves = new List<Curve>();
                var points = new List<Point3d>();
                var subds = new List<SubD>();
                var breps = new List<Brep>();
                var nested = new List<GH_InstanceReference>();
                var sources = new List<GeometryBase>();

                if (paired != null && paired.Objects != null)
                {
                    foreach (ModelObject obj in paired.Objects)
                        CollectObject(obj, meshes, curves, points, subds, breps, nested, sources);
                }

                string name = DisplayName(paired);
                if (!HasDefinitionContent(meshes, curves, points, subds, breps, nested))
                {
                    // All the definition's objects were null (or none survived):
                    // writing the table entry would give the row an empty block,
                    // which Blender would draw as nothing. Drop the row instead.
                    _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "Block definition \"" + (string.IsNullOrEmpty(name) ? "(unnamed)" : name)
                        + "\" has no objects and was skipped"));
                    return -1;
                }

                int index = _definitions.Count;
                var entry = new Dictionary<string, object>();
                entry["name"] = name;
                _definitions.Add(entry);
                if (key != null) _byKey[key] = index;

                string prefix = "idef." + index + ".";
                WriteGeometry(prefix, meshes, curves, points, subds, breps, nested, sources, index);
                return index;
            }

            /// <summary>A document definition by id, for a reference whose
            /// pairing is gone (a definition another document object owns).</summary>
            private int DefinitionFromDocument(Guid id)
            {
                string key = "id:" + id.ToString("N");
                int known;
                if (_byKey.TryGetValue(key, out known)) return known;

                RhinoDoc doc = RhinoDoc.ActiveDoc;
                InstanceDefinition docDef = doc == null ? null : doc.InstanceDefinitions.Find(id, true);
                if (docDef == null)
                {
                    _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "A nested block (" + key + ") is not in the document and was skipped"));
                    return -1;
                }
                if (!_visiting.Add(key))
                {
                    _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "A block definition nests inside itself and was skipped"));
                    return -1;
                }
                try
                {
                    var meshes = new List<Mesh>();
                    var curves = new List<Curve>();
                    var points = new List<Point3d>();
                    var subds = new List<SubD>();
                    var breps = new List<Brep>();
                    var nested = new List<GH_InstanceReference>();
                    var sources = new List<GeometryBase>();
                    foreach (RhinoObject obj in docDef.GetObjects())
                    {
                        if (obj == null)
                        {
                            _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                                "A null object in block definition (" + key + ") was skipped"));
                            continue;
                        }
                        CollectGeometry(obj.Geometry, meshes, curves, points, subds, breps, nested, sources);
                    }

                    if (!HasDefinitionContent(meshes, curves, points, subds, breps, nested))
                    {
                        _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                            "Block definition \"" + (docDef.Name ?? "(unnamed)")
                            + "\" has no objects and was skipped"));
                        return -1;
                    }

                    int index = _definitions.Count;
                    var entry = new Dictionary<string, object>();
                    entry["name"] = docDef.Name ?? "";
                    _definitions.Add(entry);
                    _byKey[key] = index;

                    string prefix = "idef." + index + ".";
                    WriteGeometry(prefix, meshes, curves, points, subds, breps, nested, sources, index);
                    return index;
                }
                finally
                {
                    _visiting.Remove(key);
                }
            }

            private void WriteGeometry(string prefix,
                                       List<Mesh> meshes, List<Curve> curves, List<Point3d> points,
                                       List<SubD> subds, List<Brep> breps,
                                       List<GH_InstanceReference> nested,
                                       List<GeometryBase> sources, int index)
            {
                if (meshes.Count > 0 || curves.Count > 0 || points.Count > 0
                    || subds.Count > 0 || breps.Count > 0)
                {
                    Dictionary<string, object> counts;
                    List<Section> geometry = Conversion.SectionsFromGeometry(
                        meshes, curves, points, subds, breps, _scale, _warnings, out counts);
                    _sections.AddRange(Conversion.PrefixSections(geometry, prefix));
                    foreach (GeometryBase source in sources)
                    {
                        string group = AttributeStore.GroupOf(source);
                        if (group != null)
                            AttributeStore.Collect(source, group, _sections, _attributes, prefix, index);
                    }
                }

                if (nested.Count > 0)
                {
                    var refDef = new List<uint>();
                    var transforms = new List<float>();
                    foreach (GH_InstanceReference goo in nested)
                    {
                        if (goo == null || goo.Value == null) continue;
                        int child = Definition(goo.InstanceDefinition, goo.Value.ParentIdefId);
                        if (child < 0) continue;
                        refDef.Add((uint)child);
                        transforms.AddRange(Conversion.TransformToWire(goo.Value.Xform, _scale));
                    }
                    if (refDef.Count > 0)
                    {
                        _sections.Add(Section.U32(prefix + "inst.ref_def", refDef.ToArray()));
                        _sections.Add(Section.Floats(prefix + "inst.transforms", transforms.ToArray()));
                    }
                }
            }

            private void CollectObject(ModelObject obj, List<Mesh> meshes, List<Curve> curves,
                                       List<Point3d> points, List<SubD> subds, List<Brep> breps,
                                       List<GH_InstanceReference> nested,
                                       List<GeometryBase> sources)
            {
                if (obj == null)
                {
                    _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "A null object in a block definition was skipped"));
                    return;
                }
                // A block instance ModelObject (graph-side or document-backed) is
                // the one definitive way to reach the child definition and its
                // transform: CastTo<InstanceReferenceGeometry> refuses it, the
                // goo cast does not.
                GH_InstanceReference goo;
                if (obj.CastTo<GH_InstanceReference>(out goo) && goo != null && goo.Value != null)
                {
                    nested.Add(goo);
                    return;
                }

                GeometryBase geometry = GeometryOf(obj, nested);
                if (geometry != null)
                    CollectGeometry(geometry, meshes, curves, points, subds, breps, nested, sources);
            }

            /// <summary>One geometry out of a ModelObject, or null.</summary>
            private GeometryBase GeometryOf(ModelObject obj, List<GH_InstanceReference> nested)
            {
                // ObjectType first: a cast chain would happily mesh a Brep or
                // edge-ify a mesh (GH_Convert Secondary), which is the lossy
                // direction for definitions.
                ObjectType type = obj.ObjectType;
                if (type == ObjectType.InstanceReference)
                {
                    InstanceReferenceGeometry reference;
                    if (obj.CastTo<InstanceReferenceGeometry>(out reference) && reference != null)
                    {
                        nested.Add(new GH_InstanceReference(reference));
                        return null;
                    }
                }
                if (type == ObjectType.Mesh)
                {
                    Mesh mesh;
                    if (obj.CastTo<Mesh>(out mesh) && mesh != null) return mesh;
                }
                if (type == ObjectType.Brep || type == ObjectType.Surface)
                {
                    Brep brep;
                    if (obj.CastTo<Brep>(out brep) && brep != null) return brep;
                }
                if (type == ObjectType.Curve)
                {
                    Curve curve;
                    if (obj.CastTo<Curve>(out curve) && curve != null) return curve;
                }
                if (type == ObjectType.PointSet || type == ObjectType.Point)
                {
                    PointCloud cloud;
                    if (obj.CastTo<PointCloud>(out cloud) && cloud != null) return cloud;
                }
                if (type == ObjectType.SubD)
                {
                    SubD subd;
                    if (obj.CastTo<SubD>(out subd) && subd != null) return subd;
                }
                // Last resort: whichever of the transportable kinds it becomes.
                Mesh fallbackMesh;
                if (obj.CastTo<Mesh>(out fallbackMesh) && fallbackMesh != null) return fallbackMesh;
                Curve fallbackCurve;
                if (obj.CastTo<Curve>(out fallbackCurve) && fallbackCurve != null) return fallbackCurve;
                _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                    "An object in a block definition (" + obj.TypeName + ") has no wire form and was skipped"));
                return null;
            }

            private void CollectGeometry(GeometryBase geometry, List<Mesh> meshes, List<Curve> curves,
                                         List<Point3d> points, List<SubD> subds, List<Brep> breps,
                                         List<GH_InstanceReference> nested,
                                         List<GeometryBase> sources)
            {
                if (geometry == null) return;
                Mesh mesh = geometry as Mesh;
                if (mesh != null) { meshes.Add(mesh); sources.Add(mesh); return; }
                SubD subd = geometry as SubD;
                if (subd != null) { subds.Add(subd); sources.Add(subd); return; }
                PointCloud cloud = geometry as PointCloud;
                if (cloud != null)
                {
                    points.AddRange(cloud.GetPoints());
                    sources.Add(cloud);
                    return;
                }
                Brep brep = geometry as Brep;
                if (brep != null) { breps.Add(brep); return; }
                Curve curve = geometry as Curve;
                if (curve != null) { curves.Add(curve); return; }
                InstanceReferenceGeometry reference = geometry as InstanceReferenceGeometry;
                if (reference != null) { nested.Add(new GH_InstanceReference(reference)); return; }
                if (geometry is Point) { points.Add(((Point)geometry).Location); return; }
                _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                    "A block definition object (" + geometry.ObjectType + ") has no wire form and was skipped"));
            }

            private static string Key(ModelInstanceDefinition paired, Guid referenceId)
            {
                if (paired != null && paired.Id.HasValue) return "id:" + paired.Id.Value.ToString("N");
                if (referenceId != Guid.Empty) return "id:" + referenceId.ToString("N");
                if (paired != null) return "obj:" + System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(paired);
                return null;
            }

            private static string DisplayName(ModelInstanceDefinition paired)
            {
                if (paired == null) return "";
                string display = paired.DisplayName;
                if (!string.IsNullOrEmpty(display)) return display;
                object rawName = paired.Name;
                if (rawName != null)
                {
                    string name = rawName.ToString();
                    if (!string.IsNullOrEmpty(name)) return name;
                }
                return "";
            }
        }

        // ----------------------------------------------------------------- utility

        private static void Update(HashAlgorithm sha, string name, byte[] raw)
        {
            byte[] encoded = System.Text.Encoding.UTF8.GetBytes(name);
            sha.TransformBlock(encoded, 0, encoded.Length, null, 0);
            sha.TransformBlock(new byte[] { 0 }, 0, 1, null, 0);
            sha.TransformBlock(raw, 0, raw.Length, null, 0);
        }

        private static string Hex(byte[] digest, int chars)
        {
            var text = new System.Text.StringBuilder();
            for (int i = 0; i < digest.Length && text.Length < chars; i++)
                text.Append(digest[i].ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
            return text.ToString();
        }
    }
}
