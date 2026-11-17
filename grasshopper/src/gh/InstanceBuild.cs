// Inbound instances (Stage In): definitions table and idef.* groups become
// graph-side Rhino definitions and GH_InstanceReference items.

using System;
using System.Collections.Generic;
using System.Globalization;
using Grasshopper.Kernel.Types;
using Grasshopper.Rhinoceros.Model;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom
{
    internal static class InstanceBuild
    {
        /// <summary>Definition rows and transforms, decoded; both halves are
        /// all-or-nothing and the wire translation is scaled to document units.</summary>
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
            if (mats.ByteLength % 64 != 0)
            {
                RowNote(note, where, "inst.transforms holds " + mats.ByteLength
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
            if (!section.HasBytes) return section.Name + " has no bytes";
            if (section.ByteLength % 4 != 0)
                return section.Name + " length " + section.ByteLength
                    + " is not a multiple of 4";
            return null;
        }

        private static void RowNote(Action<string> note, string where, string problem)
        {
            if (note != null) note("instance rows " + where + ": " + problem);
        }

        /// <summary>Append the frame's instances as GH_InstanceReference items;
        /// a definitions table without readable rows is a note, not silence.</summary>
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
    }

    /// <summary>Builds the frame's definitions graph-side, children first.</summary>
    internal class DefinitionBuilder
    {
        private readonly DecodedFrame _frame;
        private readonly double _scale;
        private readonly List<Dictionary<string, object>> _attributes;
        private readonly Action<string> _note;
        private readonly DefinitionPlanner _planner;
        private readonly Dictionary<long, ModelInstanceDefinition> _done =
            new Dictionary<long, ModelInstanceDefinition>();
        private readonly Dictionary<long, Guid> _referenceIds = new Dictionary<long, Guid>();
        private readonly HashSet<long> _visiting = new HashSet<long>();
        private readonly HashSet<Dictionary<string, object>> _badDescriptors =
            new HashSet<Dictionary<string, object>>();
        private readonly Dictionary<long, bool> _preflighted = new Dictionary<long, bool>();
        private readonly HashSet<long> _preflightVisiting = new HashSet<long>();
        private readonly HashSet<string> _blockedEdges = new HashSet<string>();
        private readonly HashSet<string> _reported = new HashSet<string>(StringComparer.Ordinal);
        private readonly Dictionary<long, string> _contentHashes = new Dictionary<long, string>();
        private readonly HashSet<long> _hashing = new HashSet<long>();

        public DefinitionBuilder(DecodedFrame frame, double scale,
                                 List<Dictionary<string, object>> attributes,
                                 Action<string> note)
        {
            _frame = frame;
            _scale = scale;
            _attributes = attributes;
            _note = note;
            _planner = new DefinitionPlanner(frame, Note);
            ValidateDescriptors();
        }

        /// <summary>The id shared by every reference to one definition.</summary>
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

        /// <summary>The validated, cached plan of a definition, or null with notes.</summary>
        internal DefinitionPlan Plan(long index)
        {
            return _planner.Plan(index);
        }

        /// <summary>Build one definition, children first. A multi-part definition
        /// with no geometry is still a definition; a single-part empty one is skipped.</summary>
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
                if (InstanceBuild.ReadRows(_frame, plan.NestedPrefix, _scale,
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
                    ? DefinitionIdentity.Prefix + ContentHash(index)
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

        /// <summary>Block out-of-table references and cycles before building.</summary>
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
                if (InstanceBuild.ReadRows(_frame, plan.NestedPrefix, _scale,
                                           out nested, out nestedTransforms, _note))
                {
                    for (int row = 0; row < nested.Length; row++)
                    {
                        long child = nested[row];
                        if (!_planner.Knows(child))
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
                if (!DefinitionIdentity.DescriptorDefinition(descriptor, out definition)
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

        /// <summary>Reject a descriptor that names a definition or bad section
        /// once; the definition still builds without it.</summary>
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
                if (!DefinitionTable.TryInteger(rawDefinition, out definition) || definition < 0)
                {
                    Note(label + " has a definition that is not a non-negative integer; skipped");
                    _badDescriptors.Add(descriptor);
                    continue;
                }
                DefinitionTable.Entry entry;
                if (!_planner.TryGetEntry(definition, out entry))
                {
                    Note(label + " names definition " + definition
                        + " outside the definitions table; skipped");
                    _badDescriptors.Add(descriptor);
                    continue;
                }

                if (hasPart)
                {
                    if (!DefinitionTable.TryInteger(rawPart, out part) || part < 0)
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
                if (!DefinitionTable.SectionMatches(sectionName, definition, expectedPart))
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
                        || !DefinitionTable.SectionMatches(offsets, definition, expectedPart))
                    {
                        Note(label + " offsets section does not match definition " + definition
                            + (expectedPart >= 0 ? " part " + expectedPart : "")
                            + "; skipped");
                        _badDescriptors.Add(descriptor);
                    }
                }
            }
        }

        private static bool DescriptorPart(Dictionary<string, object> descriptor,
                                           out long part, out bool hasPart)
        {
            part = -1;
            hasPart = false;
            object raw;
            if (!descriptor.TryGetValue("part", out raw) || raw == null) return true;
            hasPart = true;
            return DefinitionTable.TryInteger(raw, out part) && part >= 0;
        }

        /// <summary>The cached content hash; DefinitionIdentity owns the hash body.</summary>
        internal string ContentHash(long index)
        {
            string cached;
            if (_contentHashes.TryGetValue(index, out cached)) return cached;
            if (!_hashing.Add(index)) return "cycle";

            try
            {
                DefinitionTable.Entry entry;
                int parts = _planner.TryGetEntry(index, out entry) ? entry.Parts : 1;
                string hash = DefinitionIdentity.ContentHash(
                    _frame, index, parts, _attributes, _scale, ContentHash);
                _contentHashes[index] = hash;
                return hash;
            }
            finally
            {
                _hashing.Remove(index);
            }
        }

        private void Note(string message)
        {
            if (_note == null || string.IsNullOrEmpty(message)) return;
            if (_reported.Add(message)) _note(message);
        }
    }
}
