// Definition table primitives: the decoded table and section names.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using Stageloom.Core;

namespace Stageloom
{
    internal static class DefinitionTable
    {
        /// <summary>The protocol's defensive definition-part cap (4096).</summary>
        public const int MaxParts = 4096;

        /// <summary>One decoded table entry; a malformed one stays a problem.</summary>
        internal sealed class Entry
        {
            public readonly string Name;
            public readonly int Parts;

            public Entry(string name, int parts)
            {
                Name = name;
                Parts = parts;
            }
        }

        /// <summary>A section name inside a definition group; Part is -1 for a
        /// definition-level section, otherwise the parsed part index.</summary>
        internal sealed class SectionRef
        {
            public readonly Section Section;
            public readonly string Name;
            public readonly long Index;
            public readonly int Part;
            public readonly string IndexToken;
            public readonly string PartToken;
            public readonly string Inner;

            public SectionRef(Section section, string name, long index, int part,
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

        /// <summary>The definitions table, read through non-generic IList: a
        /// decoded frame and an in-memory slot carry different list shapes.</summary>
        public static IList Raw(DecodedFrame frame)
        {
            return Json.Get(frame.Header, "definitions") as IList;
        }

        /// <summary>A JSON integer that is not a boolean (protocol §5.2).</summary>
        public static bool TryInteger(object raw, out long value)
        {
            if (raw is long) { value = (long)raw; return true; }
            if (raw is int) { value = (int)raw; return true; }
            value = 0;
            return false;
        }

        /// <summary>One validated table entry (name and positive part count) or
        /// false with the reason; never a cast into defaults.</summary>
        public static bool TryEntry(DecodedFrame frame, long index,
                                    out Entry entry, out string problem)
        {
            entry = null;
            problem = null;
            IList table = Raw(frame);
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
                if (value > MaxParts)
                {
                    problem = "definition " + index + " parts is " + value
                        + ", over the " + MaxParts + "-part cap";
                    return false;
                }
                parts = (int)value;
            }
            entry = new Entry(name, parts);
            return true;
        }

        /// <summary>The display name of a definition table entry, or "".</summary>
        public static string NameOf(DecodedFrame frame, long index)
        {
            Entry entry;
            string problem;
            return TryEntry(frame, index, out entry, out problem) ? entry.Name : "";
        }

        /// <summary>The namespace prefix of one definition or part.</summary>
        public static string Prefix(long index, int part)
        {
            string prefix = "idef." + index.ToString(CultureInfo.InvariantCulture) + ".";
            return part < 0 ? prefix : prefix + part.ToString(CultureInfo.InvariantCulture) + ".";
        }

        /// <summary>Parse `idef.&lt;i&gt;.&lt;rest&gt;`; an ASCII-digit head is a part.</summary>
        public static bool TryParseSection(Section section, string name,
                                           out SectionRef parsed, out string problem)
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
            parsed = new SectionRef(section, name, index, part,
                                    indexToken, partToken, inner);
            return true;
        }

        public static bool TryIndexToken(string token, out int value)
        {
            value = 0;
            if (string.IsNullOrEmpty(token)) return false;
            for (int i = 0; i < token.Length; i++)
                if (token[i] < '0' || token[i] > '9') return false;
            return int.TryParse(token, NumberStyles.None,
                                CultureInfo.InvariantCulture, out value);
        }

        public static string HeadOf(string inner)
        {
            if (string.IsNullOrEmpty(inner)) return "";
            int dot = inner.IndexOf('.');
            return dot < 0 ? "" : inner.Substring(0, dot);
        }

        public static bool AllowedSingle(string head)
        {
            return head == "mesh" || head == "subd" || head == "crv"
                || head == "pts" || head == "attr" || head == "inst";
        }

        public static bool AllowedPart(string head)
        {
            return head == "mesh" || head == "subd" || head == "crv"
                || head == "pts" || head == "attr";
        }

        public static string PartKey(long index, long part)
        {
            return index.ToString(CultureInfo.InvariantCulture) + ":"
                + part.ToString(CultureInfo.InvariantCulture);
        }

        /// <summary>A section name belongs to definition index/part.</summary>
        public static bool SectionMatches(string name, long index, long part)
        {
            SectionRef parsed;
            string problem;
            if (!TryParseSection(null, name, out parsed, out problem)) return false;
            return parsed.Index == index && parsed.Part == part
                && HeadOf(parsed.Inner) == "attr";
        }
    }
}
