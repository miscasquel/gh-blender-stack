// Inbound definition plan: table entry, namespace and part validation.

using System;
using System.Collections.Generic;
using System.Globalization;
using Stageloom.Core;

namespace Stageloom
{
    /// <summary>One part: single-part plans use Part -1.</summary>
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

    /// <summary>The validated parts and nested-row prefix of one definition.</summary>
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
            NestedPrefix = DefinitionTable.Prefix(index, -1);
        }
    }

    /// <summary>The section-group read dispatch: mesh/SubD, curves, points.</summary>
    internal static class DefinitionGroupValidation
    {
        private delegate void Validator(DecodedFrame frame, string prefix);

        private static readonly Validator[] Validators =
        {
            ValidateMesh, ValidateCurve, ValidatePoint,
        };

        public static void Validate(DecodedFrame frame, string prefix)
        {
            foreach (Validator validate in Validators)
                validate(frame, prefix);
        }

        private static void ValidateMesh(DecodedFrame frame, string prefix)
        {
            Conversion.MeshGroup group = Conversion.SelectMeshGroup(frame, prefix);
            if (group != null) Conversion.ReadMeshData(group);
        }

        private static void ValidateCurve(DecodedFrame frame, string prefix)
        {
            Conversion.ReadCurveData(frame, prefix);
        }

        /// <summary>The point-cloud group has no decoder owner; validate it here.</summary>
        private static void ValidatePoint(DecodedFrame frame, string prefix)
        {
            Section positions = frame.Find(prefix + "pts.positions");
            Section radii = frame.Find(prefix + "pts.radii");
            Section colors = frame.Find(prefix + "pts.colors");
            if (positions == null && radii == null && colors == null) return;
            if (positions == null)
                throw new FrameException("bad_frame",
                    "point group is incomplete: " + prefix + "pts.positions is missing");
            if (positions.Dtype != "f32")
                throw new FrameException("bad_frame", positions.Name + " must be f32, not "
                    + (positions.Dtype ?? "(missing)"));
            if (!positions.HasBytes)
                throw new FrameException("bad_frame", positions.Name + " has no bytes");
            if (positions.ByteLength % 12 != 0)
                throw new FrameException("bad_frame", positions.Name + " has "
                    + positions.ByteLength + " bytes; positions are xyz triples");
            int points = positions.ByteLength / 12;

            if (radii != null)
            {
                if (radii.Dtype != "f32")
                    throw new FrameException("bad_frame", radii.Name + " must be f32, not "
                        + (radii.Dtype ?? "(missing)"));
                if (!radii.HasBytes || radii.ByteLength != points * 4)
                    throw new FrameException("bad_frame", radii.Name + " has "
                        + (radii.HasBytes ? radii.ByteLength : 0) + " bytes for "
                        + points + " point(s)");
            }
            if (colors != null)
            {
                if (colors.Dtype != "u8")
                    throw new FrameException("bad_frame", colors.Name + " must be u8, not "
                        + (colors.Dtype ?? "(missing)"));
                if (!colors.HasBytes || colors.ByteLength != points * 4)
                    throw new FrameException("bad_frame", colors.Name + " has "
                        + (colors.HasBytes ? colors.ByteLength : 0) + " bytes for "
                        + points + " point(s); colours are RGBA");
            }
        }
    }

    /// <summary>Validates the definitions table and every `idef.*` section into
    /// a cached per-definition plan.</summary>
    internal sealed class DefinitionPlanner
    {
        private readonly DecodedFrame _frame;
        private readonly Action<string> _note;
        private readonly Dictionary<long, DefinitionTable.Entry> _entries =
            new Dictionary<long, DefinitionTable.Entry>();
        private readonly HashSet<long> _invalidEntries = new HashSet<long>();
        private readonly Dictionary<long, List<DefinitionTable.SectionRef>> _sections =
            new Dictionary<long, List<DefinitionTable.SectionRef>>();
        private readonly HashSet<long> _outsideDefinitions = new HashSet<long>();
        private readonly HashSet<long> _duplicateDefinitions = new HashSet<long>();
        private readonly HashSet<string> _duplicateParts = new HashSet<string>();
        private readonly Dictionary<long, string> _indexTokens = new Dictionary<long, string>();
        private readonly Dictionary<string, string> _partTokens = new Dictionary<string, string>();
        private readonly Dictionary<long, DefinitionPlan> _plans = new Dictionary<long, DefinitionPlan>();
        private readonly HashSet<long> _planFailed = new HashSet<long>();

        public DefinitionPlanner(DecodedFrame frame, Action<string> note)
        {
            _frame = frame;
            _note = note;
            ValidateTable();
            ScanSections();
        }

        public bool Knows(long index)
        {
            return _entries.ContainsKey(index);
        }

        public bool TryGetEntry(long index, out DefinitionTable.Entry entry)
        {
            return _entries.TryGetValue(index, out entry);
        }

        /// <summary>The validated, cached plan of a definition, or null with notes.</summary>
        public DefinitionPlan Plan(long index)
        {
            DefinitionPlan cached;
            if (_plans.TryGetValue(index, out cached)) return cached;
            if (_planFailed.Contains(index)) return null;

            DefinitionTable.Entry entry;
            string problem;
            if (!_entries.TryGetValue(index, out entry))
            {
                if (!_invalidEntries.Contains(index))
                {
                    if (DefinitionTable.TryEntry(_frame, index, out entry, out problem))
                        _entries[index] = entry;
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
            List<DefinitionTable.SectionRef> sections;
            if (!_sections.TryGetValue(index, out sections))
                sections = new List<DefinitionTable.SectionRef>();

            for (int i = 0; i < sections.Count; i++)
            {
                DefinitionTable.SectionRef section = sections[i];
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
                    string head = DefinitionTable.HeadOf(section.Inner);
                    if (!DefinitionTable.AllowedSingle(head))
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
                        string head = DefinitionTable.HeadOf(section.Inner);
                        if (!DefinitionTable.AllowedPart(head))
                        {
                            Note("section " + where
                                + " does not parse as a part section of definition " + index
                                + "; definition skipped");
                            return FailPlan(index);
                        }
                        string partKey = DefinitionTable.PartKey(index, section.Part);
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
                    plan.Parts.Add(new PartPlan(part, DefinitionTable.Prefix(index, part)));
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
                    DefinitionGroupValidation.Validate(_frame, part.Prefix);
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

        private void ValidateTable()
        {
            System.Collections.IList table = DefinitionTable.Raw(_frame);
            if (table == null)
            {
                if (Json.Get(_frame.Header, "definitions") != null)
                    Note("definitions is not an array; no instance definitions are read");
                return;
            }
            for (int i = 0; i < table.Count; i++)
            {
                DefinitionTable.Entry entry;
                string problem;
                if (DefinitionTable.TryEntry(_frame, i, out entry, out problem)) _entries[i] = entry;
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

                DefinitionTable.SectionRef parsed;
                string problem;
                if (!DefinitionTable.TryParseSection(section, section.Name, out parsed, out problem))
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
                    string key = DefinitionTable.PartKey(parsed.Index, parsed.Part);
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

                List<DefinitionTable.SectionRef> list;
                if (!_sections.TryGetValue(parsed.Index, out list))
                {
                    list = new List<DefinitionTable.SectionRef>();
                    _sections[parsed.Index] = list;
                }
                list.Add(parsed);
            }
        }

        private void Note(string message)
        {
            if (_note != null) _note(message);
        }
    }
}
