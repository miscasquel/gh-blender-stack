// Outbound definition table grammar: part grouping, deterministic content keys,
// deduplication, nested rows and the `blocks/2` fallback split. It operates on
// already-converted parts so the grammar is testable without native geometry.

using System;
using System.Collections.Generic;
using Stageloom.Core;

namespace Stageloom
{
    /// <summary>Owns the outbound definition table: part grouping, content keys,
    /// deduplication, nested rows and the `blocks/2` fallback split.</summary>
    internal sealed class DefinitionTableWriter
    {
        /// <summary>One part's unprefixed sections and neutral descriptors; the
        /// writer adds the namespace and real definition index.</summary>
        internal sealed class Part
        {
            public readonly List<Section> Sections = new List<Section>();
            public readonly List<Dictionary<string, object>> Descriptors =
                new List<Dictionary<string, object>>();

            public Part()
            {
            }

            public Part(List<Section> sections,
                        List<Dictionary<string, object>> descriptors)
            {
                if (sections != null) Sections.AddRange(sections);
                if (descriptors != null) Descriptors.AddRange(descriptors);
            }
        }

        /// <summary>One definition-level nested placement: wire transform,
        /// child identity and a deferred child resolver.</summary>
        internal sealed class NestedRow
        {
            public readonly float[] WireTransform;
            public readonly string Signature;
            private readonly Func<int[]> _resolve;

            public NestedRow(float[] wireTransform, string signature, Func<int[]> resolve)
            {
                if (wireTransform == null) wireTransform = new float[16];
                if (wireTransform.Length != 16)
                    throw new FrameException("bad_frame",
                        "a nested instance row needs 16 floats, not "
                        + wireTransform.Length);
                WireTransform = wireTransform;
                Signature = signature ?? "";
                _resolve = resolve;
            }

            public int[] Resolve()
            {
                return _resolve == null ? null : _resolve();
            }
        }

        private readonly List<Section> _sections;
        private readonly List<Dictionary<string, object>> _attributes;
        private readonly List<Dictionary<string, object>> _definitions;
        private readonly List<WireWarning> _warnings;
        private readonly bool _peerSupportsParts;
        private readonly Dictionary<string, int[]> _byKey =
            new Dictionary<string, int[]>(StringComparer.Ordinal);
        private readonly Dictionary<string, int[]> _byContent =
            new Dictionary<string, int[]>(StringComparer.Ordinal);
        private readonly HashSet<string> _visiting = new HashSet<string>(StringComparer.Ordinal);
        private readonly HashSet<string> _warned = new HashSet<string>(StringComparer.Ordinal);
        private readonly HashSet<string> _sectionNames = new HashSet<string>(StringComparer.Ordinal);

        public DefinitionTableWriter(List<Section> sections,
                                     List<Dictionary<string, object>> attributes,
                                     List<Dictionary<string, object>> definitions,
                                     List<WireWarning> warnings,
                                     bool peerSupportsParts)
        {
            _sections = sections;
            _attributes = attributes;
            _definitions = definitions;
            _warnings = warnings;
            _peerSupportsParts = peerSupportsParts;
            if (sections != null)
                foreach (Section section in sections)
                    if (section != null && section.Name != null) _sectionNames.Add(section.Name);
        }

        /// <summary>The committed descriptor count (legacy single-part indices).</summary>
        public int AttributeCount
        {
            get { return _attributes.Count; }
        }

        public bool PeerSupportsParts
        {
            get { return _peerSupportsParts; }
        }

        public bool TryGet(string key, out int[] indices)
        {
            if (key != null) return _byKey.TryGetValue(key, out indices);
            indices = null;
            return false;
        }

        /// <summary>Write one definition and return every index a referring row
        /// must use; null means a cycle and the row is skipped.</summary>
        public int[] Write(string key, string name, List<Part> parts,
                           List<NestedRow> nested)
        {
            if (parts == null) parts = new List<Part>();
            if (nested == null) nested = new List<NestedRow>();

            int[] known;
            if (key != null && _byKey.TryGetValue(key, out known)) return known;

            bool added = key != null && _visiting.Add(key);
            if (key != null && !added)
            {
                Warn(WarningCodes.InstanceSkipped,
                    "Block definition \"" + DefinitionIdentity.Label(name)
                    + "\" nests inside itself; the nested row was skipped");
                return null;
            }

            try
            {
                int[] indices;
                if (parts.Count <= 1)
                {
                    // Single-part output predates parts: it keeps the old
                    // identity key, bytes and name. Content dedupe here
                    // would change the table, so it applies only below.
                    indices = WriteSingle(name, parts, nested);
                }
                else
                {
                    string contentKey = ContentKey(parts, nested, !_peerSupportsParts);
                    if (_byContent.TryGetValue(contentKey, out known))
                    {
                        Cache(key, known);
                        return known;
                    }
                    indices = _peerSupportsParts
                        ? WriteMulti(name, contentKey, parts, nested)
                        : WriteSplit(name, contentKey, parts, nested);
                    _byContent[contentKey] = indices;
                }
                Cache(key, indices);
                return indices;
            }
            finally
            {
                if (key != null) _visiting.Remove(key);
            }
        }

        private int[] WriteSingle(string name, List<Part> parts, List<NestedRow> nested)
        {
            int index = _definitions.Count;
            var entry = new Dictionary<string, object>();
            entry["name"] = name ?? "";
            _definitions.Add(entry);

            string prefix = DefinitionTable.Prefix(index, -1);
            if (parts.Count == 1) AppendPart(parts[0], prefix, index, -1);
            AppendRows(nested, prefix);
            return new int[] { index };
        }

        private int[] WriteMulti(string name, string contentKey, List<Part> parts,
                                 List<NestedRow> nested)
        {
            int index = _definitions.Count;
            var entry = new Dictionary<string, object>();
            entry["name"] = DefinitionIdentity.NameOrFallback(name, contentKey);
            entry["parts"] = (long)parts.Count;
            _definitions.Add(entry);

            for (int part = 0; part < parts.Count; part++)
                AppendPart(parts[part], DefinitionTable.Prefix(index, part), index, part);
            AppendRows(nested, DefinitionTable.Prefix(index, -1));
            return new int[] { index };
        }

        private int[] WriteSplit(string name, string contentKey, List<Part> parts,
                                 List<NestedRow> nested)
        {
            Warn(WarningCodes.InstanceSplit,
                "Block definition \"" + DefinitionIdentity.Label(name) + "\" (" + contentKey
                + ") has " + parts.Count
                + " parts; the Blender peer does not advertise blocks/2, so it was split into "
                + parts.Count + " single-part definitions");

            var indices = new int[parts.Count];
            for (int part = 0; part < parts.Count; part++)
            {
                int index = _definitions.Count;
                indices[part] = index;

                var entry = new Dictionary<string, object>();
                entry["name"] = DefinitionIdentity.SplitName(name, contentKey, part, parts.Count);
                _definitions.Add(entry);

                string prefix = DefinitionTable.Prefix(index, -1);
                AppendPart(parts[part], prefix, index, -1);
                // The nested rows belong to the first part, exactly as
                // Blender's Q10b1 split does: putting them on every part
                // would draw each child once per part.
                if (part == 0) AppendRows(nested, prefix);
            }
            return indices;
        }

        private void AppendPart(Part part, string prefix, int definition, int partIndex)
        {
            if (part == null) return;
            foreach (Section section in part.Sections)
                AddSection(new Section(prefix + section.Name, section.Dtype, section.Raw));

            foreach (Dictionary<string, object> descriptor in part.Descriptors)
            {
                var copy = new Dictionary<string, object>(descriptor);
                copy["definition"] = (long)definition;
                if (partIndex >= 0) copy["part"] = (long)partIndex;
                else copy.Remove("part");

                object rawSection;
                string section = copy.TryGetValue("section", out rawSection)
                    ? rawSection as string : null;
                if (section != null) copy["section"] = prefix + section;
                object rawOffsets;
                if (copy.TryGetValue("offsets", out rawOffsets) && rawOffsets is string)
                    copy["offsets"] = prefix + (string)rawOffsets;
                _attributes.Add(copy);
            }
        }

        private void AppendRows(List<NestedRow> nested, string prefix)
        {
            var refDef = new List<uint>();
            var transforms = new List<float>();
            foreach (NestedRow row in nested)
            {
                if (row == null) continue;
                int[] children = row.Resolve();
                if (children == null) continue;
                foreach (int child in children)
                {
                    if (child < 0) continue;
                    refDef.Add((uint)child);
                    transforms.AddRange(row.WireTransform);
                }
            }
            if (refDef.Count == 0) return;
            AddSection(Section.U32(prefix + "inst.ref_def", refDef.ToArray()));
            AddSection(Section.Floats(prefix + "inst.transforms", transforms.ToArray()));
        }

        private void AddSection(Section section)
        {
            if (section == null) return;
            if (!_sectionNames.Add(section.Name))
                throw new FrameException("bad_frame",
                    "duplicate section name " + section.Name);
            _sections.Add(section);
        }

        /// <summary>A stable key over part count/form, each part's ordered
        /// sections and descriptors, and nested rows; it omits index and name.</summary>
        private string ContentKey(List<Part> parts, List<NestedRow> nested, bool split)
        {
            using (var hash = new DefinitionIdentity.DefinitionHash())
            {
                hash.Add("definition.parts", BitConverter.GetBytes((long)parts.Count));
                hash.Add("definition.form",
                    BitConverter.GetBytes((long)(split ? 2 : 1)));
                for (int part = 0; part < parts.Count; part++)
                {
                    hash.Add("part.index", BitConverter.GetBytes((long)part));
                    Part content = parts[part];
                    if (content == null) continue;

                    var byName = new Dictionary<string, Section>(StringComparer.Ordinal);
                    foreach (Section section in content.Sections)
                        if (section != null && section.Name != null)
                            byName[section.Name] = section;
                    foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                        hash.Add(name, byName[name].Raw);

                    foreach (Dictionary<string, object> descriptor in content.Descriptors)
                        hash.AddJson("attr.descriptor", descriptor);
                }

                for (int row = 0; row < nested.Count; row++)
                {
                    NestedRow placement = nested[row];
                    if (placement == null) continue;
                    hash.Add("inst.ref",
                        System.Text.Encoding.UTF8.GetBytes(placement.Signature));
                    var bytes = new byte[placement.WireTransform.Length * 4];
                    Buffer.BlockCopy(placement.WireTransform, 0, bytes, 0, bytes.Length);
                    hash.Add("inst.transforms", bytes);
                }

                return hash.Finish(12);
            }
        }

        private void Cache(string key, int[] indices)
        {
            if (key != null) _byKey[key] = indices;
        }

        private void Warn(string code, string message)
        {
            if (_warnings == null || string.IsNullOrEmpty(message)) return;
            if (!_warned.Add(message)) return;
            _warnings.Add(new WireWarning(code, message));
        }
    }
}
