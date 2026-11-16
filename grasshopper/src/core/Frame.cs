// Protocol v2 frame codec: encode and decode the binary frames, plus the two
// hashes. The C# twin of blender/stageloom/core/codec.py and hashing.py, checked
// against the same golden fixtures (protocol/fixtures, docs/protocol.md §9).
//
// No RhinoCommon and no Grasshopper references live here, so this file builds into
// console tests that run under wine without Rhino.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace Stageloom.Core
{
    public class FrameException : Exception
    {
        /// <summary>Protocol error code, e.g. "bad_frame" or "proto_mismatch".</summary>
        public string Code { get; private set; }

        public FrameException(string code, string message) : base(message)
        {
            Code = code;
        }
    }

    /// <summary>One binary section: a name, a dtype, and raw little-endian bytes.</summary>
    public class Section
    {
        public readonly string Name;
        public readonly string Dtype;
        public readonly byte[] Raw;

        public Section(string name, string dtype, byte[] raw)
        {
            Name = name;
            Dtype = dtype;
            Raw = raw;
        }

        public static Section Floats(string name, float[] values)
        {
            var raw = new byte[values.Length * 4];
            Buffer.BlockCopy(values, 0, raw, 0, raw.Length);
            return new Section(name, "f32", raw);
        }

        public static Section U32(string name, uint[] values)
        {
            var raw = new byte[values.Length * 4];
            Buffer.BlockCopy(values, 0, raw, 0, raw.Length);
            return new Section(name, "u32", raw);
        }

        public static Section Bytes(string name, byte[] values)
        {
            return new Section(name, "u8", values);
        }

        public static Section Ints(string name, int[] values)
        {
            var raw = new byte[values.Length * 4];
            Buffer.BlockCopy(values, 0, raw, 0, raw.Length);
            return new Section(name, "i32", raw);
        }

        public float[] ToFloats()
        {
            var values = new float[Raw.Length / 4];
            Buffer.BlockCopy(Raw, 0, values, 0, Raw.Length);
            return values;
        }

        public uint[] ToU32()
        {
            var values = new uint[Raw.Length / 4];
            Buffer.BlockCopy(Raw, 0, values, 0, Raw.Length);
            return values;
        }

        public int[] ToI32()
        {
            var values = new int[Raw.Length / 4];
            Buffer.BlockCopy(Raw, 0, values, 0, Raw.Length);
            return values;
        }

        public int ElementCount { get { return Dtype == "u8" ? Raw.Length : Raw.Length / 4; } }
    }

    /// <summary>A decoded frame: the header object and the sections, header order.</summary>
    public class DecodedFrame
    {
        public readonly Dictionary<string, object> Header;
        public readonly List<Section> Sections;

        public DecodedFrame(Dictionary<string, object> header, List<Section> sections)
        {
            Header = header;
            Sections = sections;
        }

        /// <summary>An in-memory frame (a StageSlot's held state) shaped like a
        /// decoded one, so the section converters can be reused without going
        /// back through the wire. The header may be partial: converters look up
        /// only the keys they need.</summary>
        public static DecodedFrame FromSections(Dictionary<string, object> header, List<Section> sections)
        {
            return new DecodedFrame(header ?? new Dictionary<string, object>(), sections);
        }

        public Section Find(string name)
        {
            foreach (Section s in Sections)
                if (s.Name == name) return s;
            return null;
        }
    }

    public static class FrameCodec
    {
        public const int ProtocolVersion = 2;
        public const int MaxHeaderBytes = 1024 * 1024;
        public const long MaxMessageBytes = 512L * 1024 * 1024;

        // The default UTF-8 decoder replaces an invalid byte with U+FFFD, which
        // would turn a malformed peer frame into a valid-looking header. Python
        // decodes the header strictly (codec.py), so this side must too.
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        /// <summary>The fixed geometry-hash order (protocol §5 table order).</summary>
        public static readonly string[] HashSectionOrder = new string[]
        {
            "mesh.verts", "mesh.face_counts", "mesh.face_indices", "mesh.colors",
            "mesh.crease_edges", "mesh.crease_values",
            "subd.verts", "subd.face_counts", "subd.face_indices",
            "subd.crease_edges", "subd.crease_values",
            "crv.points", "crv.counts", "crv.types", "crv.orders",
            "crv.knots_mode", "crv.cyclic",
            "pts.positions", "pts.radii", "pts.colors",
            "inst.ref_def", "inst.transforms",
        };

        // -------------------------------------------------------------- encode

        /// <summary>Encode one frame. The header gains "v" and the section table,
        /// exactly as the Python codec does, so headers stay in sync with bytes.
        /// The encoder refuses every shape its decoder refuses; a caller cannot
        /// emit a frame the peer will reject.</summary>
        public static byte[] Encode(Dictionary<string, object> header, IList<Section> sections)
        {
            if (header == null)
                throw new FrameException("bad_frame", "header must be an object");
            var head = new Dictionary<string, object>(header);
            if (!head.ContainsKey("v")) head["v"] = (long)ProtocolVersion;

            // Python's encode_frame checks type, then the whole value tree, then
            // the version. The order decides the code when two fields are bad at
            // once (for example v=3 plus NaN is bad_frame, not proto_mismatch).
            object typeObj;
            if (!head.TryGetValue("type", out typeObj)
                || !(typeObj is string) || ((string)typeObj).Length == 0)
                throw new FrameException("bad_frame", "header type must be a non-empty string");
            try
            {
                Json.ValidateTree(head);
            }
            catch (JsonException ex)
            {
                throw new FrameException("bad_frame", "header is not JSON: " + ex.Message);
            }

            object v = head["v"];
            // Python's check_version wants an exact JSON integer: 2.0 and true
            // are different values even though both compare equal to 2.
            bool versionOk = (v is long && (long)v == ProtocolVersion)
                             || (v is int && (int)v == ProtocolVersion);
            if (!versionOk)
                throw new FrameException("proto_mismatch",
                    "header v must be the integer " + ProtocolVersion);

            var table = new List<object>();
            long total = 0;
            if (sections != null && sections.Count > 0)
            {
                var seen = new HashSet<string>(StringComparer.Ordinal);
                foreach (Section s in sections)
                {
                    if (s == null)
                        throw new FrameException("bad_frame", "null section");
                    if (string.IsNullOrEmpty(s.Name))
                        throw new FrameException("bad_frame", "section name must be a non-empty string");
                    if (!IsKnownDtype(s.Dtype))
                        throw new FrameException("bad_frame",
                            "section " + s.Name + " has unknown dtype " + (s.Dtype ?? "(missing)"));
                    if (s.Raw == null)
                        throw new FrameException("bad_frame",
                            "section " + s.Name + " has no bytes");
                    if (s.Raw.Length % ItemSize(s.Dtype) != 0)
                        throw new FrameException("bad_frame",
                            "section " + s.Name + " length " + s.Raw.Length
                            + " is not a multiple of " + ItemSize(s.Dtype));
                    if (!seen.Add(s.Name))
                        throw new FrameException("bad_frame",
                            "duplicate section name " + s.Name);

                    var entry = new Dictionary<string, object>();
                    entry["name"] = s.Name;
                    entry["dtype"] = s.Dtype;
                    entry["shape"] = new List<object> { (long)s.ElementCount };
                    entry["bytes"] = (long)s.Raw.Length;
                    table.Add(entry);
                    total += s.Raw.Length;
                }
                head["sections"] = table;
            }
            else
            {
                head.Remove("sections");
            }

            byte[] headBytes;
            try
            {
                // The writer refuses non-finite numbers and lone surrogates,
                // the same values Python's _validate_json_tree refuses.
                headBytes = Json.WriteCanonicalUtf8(head);
            }
            catch (JsonException ex)
            {
                throw new FrameException("bad_frame", "header is not canonical JSON: " + ex.Message);
            }
            if (headBytes.Length > MaxHeaderBytes)
                throw new FrameException("too_large", "header over the cap");
            if (4 + (long)headBytes.Length + total > MaxMessageBytes)
                throw new FrameException("too_large", "frame over the cap");

            var frame = new byte[4 + headBytes.Length + total];
            WriteU32(frame, 0, (uint)headBytes.Length);
            Buffer.BlockCopy(headBytes, 0, frame, 4, headBytes.Length);
            long offset = 4 + headBytes.Length;
            if (sections != null)
            {
                foreach (Section s in sections)
                {
                    Buffer.BlockCopy(s.Raw, 0, frame, (int)offset, s.Raw.Length);
                    offset += s.Raw.Length;
                }
            }
            return frame;
        }

        // -------------------------------------------------------------- decode

        public static DecodedFrame Decode(byte[] frame)
        {
            if (frame == null) throw new FrameException("bad_frame", "null frame");
            if (frame.Length < 4) throw new FrameException("bad_frame", "no header length");
            if (frame.Length > MaxMessageBytes)
                throw new FrameException("too_large",
                    "frame is " + frame.Length + " bytes, over the " + MaxMessageBytes + " cap");
            long headLen = ReadU32(frame, 0);
            if (headLen > MaxHeaderBytes)
                throw new FrameException("too_large", "header length " + headLen + " over the cap");
            if (frame.Length < 4 + headLen)
                throw new FrameException("bad_frame", "header is truncated");

            string headText;
            try
            {
                headText = StrictUtf8.GetString(frame, 4, (int)headLen);
            }
            catch (DecoderFallbackException ex)
            {
                throw new FrameException("bad_frame", "header is not valid UTF-8: " + ex.Message);
            }
            object parsed;
            try
            {
                parsed = Json.Parse(headText);
            }
            catch (JsonException ex)
            {
                throw new FrameException("bad_frame", "header is not valid JSON: " + ex.Message);
            }
            var header = parsed as Dictionary<string, object>;
            if (header == null)
                throw new FrameException("bad_frame", "header is not a JSON object");

            object v = Json.Get(header, "v");
            // An exact JSON integer, as Python's check_version requires: 2.0,
            // true, "2", null and a missing key are all proto_mismatch. A wrong
            // type must land here as a classified failure, not as a converter
            // exception, or one bad frame tears down a healthy connection.
            bool versionOk = (v is long && (long)v == ProtocolVersion)
                             || (v is int && (int)v == ProtocolVersion);
            if (!versionOk)
                throw new FrameException("proto_mismatch",
                    "header v=" + (v == null ? "(missing)" : Convert.ToString(v, CultureInfo.InvariantCulture))
                    + ", expected " + ProtocolVersion);

            object typeObj = Json.Get(header, "type");
            string type = typeObj as string;
            if (string.IsNullOrEmpty(type))
                throw new FrameException("bad_frame", "header type must be a non-empty string");

            return new DecodedFrame(header, DecodeSections(frame, 4 + headLen, header));
        }

        /// <summary>Validate the section table and decode it in order. This is
        /// the one place a section's name, dtype, byte count, duplicate name,
        /// shape and payload extent are checked before the bytes are indexed or
        /// interpreted; every failure is a bad_frame, matching codec.py's
        /// _decode_sections.</summary>
        private static List<Section> DecodeSections(byte[] frame, long offset,
                                                    Dictionary<string, object> header)
        {
            var sections = new List<Section>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            object tableObj;
            if (header.TryGetValue("sections", out tableObj))
            {
                var table = tableObj as List<object>;
                if (table == null)
                    throw new FrameException("bad_frame", "header sections must be an array");
                for (int index = 0; index < table.Count; index++)
                {
                    var entry = table[index] as Dictionary<string, object>;
                    if (entry == null)
                        throw new FrameException("bad_frame",
                            "section " + index + " is not a JSON object");

                    object nameObj;
                    entry.TryGetValue("name", out nameObj);
                    string name = nameObj as string;
                    if (string.IsNullOrEmpty(name))
                        throw new FrameException("bad_frame",
                            "section " + index + " name must be a non-empty string");

                    object dtypeObj;
                    entry.TryGetValue("dtype", out dtypeObj);
                    string dtype = dtypeObj as string;
                    if (!IsKnownDtype(dtype))
                        throw new FrameException("bad_frame",
                            "section " + name + " has unknown dtype " + (dtype ?? "(missing)"));

                    object bytesObj;
                    entry.TryGetValue("bytes", out bytesObj);
                    long nbytes;
                    if (bytesObj is long) nbytes = (long)bytesObj;
                    else if (bytesObj is int) nbytes = (int)bytesObj;
                    else throw new FrameException("bad_frame",
                        "section " + name + " bytes must be a non-negative integer");
                    if (nbytes < 0)
                        throw new FrameException("bad_frame",
                            "section " + name + " bytes must be a non-negative integer");

                    if (!seen.Add(name))
                        throw new FrameException("bad_frame", "duplicate section name " + name);

                    int itemSize = ItemSize(dtype);
                    object shapeObj;
                    if (entry.TryGetValue("shape", out shapeObj))
                    {
                        var shape = shapeObj as List<object>;
                        if (shape == null)
                            throw new FrameException("bad_frame",
                                "section " + name + " shape must be an array");
                        bool anyZero = false;
                        for (int i = 0; i < shape.Count; i++)
                        {
                            object dimObj = shape[i];
                            long dim;
                            if (!(dimObj is long))
                                throw new FrameException("bad_frame",
                                    "section " + name + " shape must hold non-negative integers");
                            dim = (long)dimObj;
                            if (dim < 0)
                                throw new FrameException("bad_frame",
                                    "section " + name + " shape must hold non-negative integers");
                            if (dim == 0) anyZero = true;
                        }
                        long count;
                        if (anyZero)
                        {
                            // Python computes an arbitrary-precision product, so
                            // a zero dimension wins over any other dimension.
                            count = 0;
                        }
                        else
                        {
                            count = 1;
                            try
                            {
                                for (int i = 0; i < shape.Count; i++)
                                    count = checked(count * (long)shape[i]);
                            }
                            catch (OverflowException)
                            {
                                // The target is at most the payload size, so an
                                // overflowing product cannot match it; classify
                                // instead of wrapping.
                                throw new FrameException("bad_frame",
                                    "section " + name + " shape product does not match "
                                    + nbytes + " bytes");
                            }
                        }
                        if (nbytes % itemSize != 0 || count != nbytes / itemSize)
                            throw new FrameException("bad_frame",
                                "section " + name + " shape product does not match "
                                + nbytes + " bytes");
                    }
                    if (nbytes % itemSize != 0)
                        throw new FrameException("bad_frame",
                            "section " + name + " length " + nbytes
                            + " is not a multiple of " + itemSize);

                    // Compare against the remainder, not offset + nbytes: a
                    // huge declared length would overflow the sum and slip
                    // through to an array allocation. A negative length never
                    // reaches here.
                    if (nbytes > frame.Length - offset)
                        throw new FrameException("bad_frame",
                            "section " + name + " runs past the payload");
                    var raw = new byte[nbytes];
                    Buffer.BlockCopy(frame, (int)offset, raw, 0, (int)nbytes);
                    offset += nbytes;
                    sections.Add(new Section(name, dtype, raw));
                }
            }
            if (offset != frame.Length)
                throw new FrameException("bad_frame",
                    (frame.Length - offset) + " trailing bytes after the declared sections");
            return sections;
        }

        private static bool IsKnownDtype(string dtype)
        {
            return dtype == "f32" || dtype == "i32" || dtype == "u32" || dtype == "u8";
        }

        private static int ItemSize(string dtype)
        {
            return dtype == "u8" ? 1 : 4;
        }

        // --------------------------------------------------------------- hash

        public static string Sha256Hex(byte[] data)
        {
            using (var sha = SHA256.Create())
            {
                return "sha256:" + ToHex(sha.ComputeHash(data));
            }
        }

        /// <summary>Geometry hash: sections in canonical order (protocol §6.1) —
        /// the §5 table order, then definitions in index order, then anything
        /// unknown, sorted.</summary>
        public static string GeometryHash(IList<Section> sections)
        {
            var byName = new Dictionary<string, Section>();
            foreach (Section s in sections)
                if (!byName.ContainsKey(s.Name)) byName[s.Name] = s;

            using (var sha = SHA256.Create())
            {
                foreach (string name in OrderedNames(byName.Keys))
                    UpdateSection(sha, byName[name]);
                sha.TransformFinalBlock(new byte[0], 0, 0);
                return "sha256:" + ToHex(sha.Hash);
            }
        }

        /// <summary>One definition's still-unprefixed section names, split into
        /// the multi-part namespaces and the definition-level ones.</summary>
        private sealed class DefinitionGroup
        {
            public readonly List<string> Base = new List<string>();
            public readonly SortedDictionary<int, List<string>> Parts =
                new SortedDictionary<int, List<string>>();
        }

        private static bool TryIndex(string token, out int value)
        {
            value = 0;
            if (string.IsNullOrEmpty(token)) return false;
            // ASCII digits only; char.IsDigit also accepts digits the Python
            // side does not treat as an index, which would split the hashers.
            for (int i = 0; i < token.Length; i++)
                if (token[i] < '0' || token[i] > '9') return false;
            return int.TryParse(token, NumberStyles.None, CultureInfo.InvariantCulture, out value);
        }

        private static bool TryPart(string rest, out int part, out string inner)
        {
            part = 0;
            inner = null;
            int dot = rest.IndexOf('.');
            if (dot <= 0 || dot == rest.Length - 1) return false;
            if (!TryIndex(rest.Substring(0, dot), out part)) return false;
            inner = rest.Substring(dot + 1);
            return true;
        }

        /// <summary>The canonical section-name order of protocol §6.1. Base
        /// sections follow the §5 table order. `idef.&lt;i&gt;.*` groups follow,
        /// one definition after the other in numeric index order. Within a
        /// definition, multi-part sections come first in numeric part order,
        /// each part ordered the same way; definition-level sections (nested
        /// `inst.*` rows) follow after all parts. Anything else is last, sorted.
        /// Single-part definitions have no numeric part fragment, so their
        /// order is unchanged.</summary>
        public static List<string> OrderedNames(IEnumerable<string> names)
        {
            var baseNames = new List<string>();
            var groups = new SortedDictionary<int, DefinitionGroup>();
            var extra = new List<string>();
            foreach (string name in names)
            {
                string[] parts = name.Split(new char[] { '.' }, 3);
                if (parts.Length == 3 && parts[0] == "idef")
                {
                    int index;
                    if (TryIndex(parts[1], out index))
                    {
                        DefinitionGroup group;
                        if (!groups.TryGetValue(index, out group))
                        {
                            group = new DefinitionGroup();
                            groups[index] = group;
                        }
                        int part;
                        string inner;
                        if (TryPart(parts[2], out part, out inner))
                        {
                            List<string> list;
                            if (!group.Parts.TryGetValue(part, out list))
                            {
                                list = new List<string>();
                                group.Parts[part] = list;
                            }
                            list.Add(inner);
                        }
                        else
                        {
                            group.Base.Add(parts[2]);
                        }
                        continue;
                    }
                }
                if (Array.IndexOf(HashSectionOrder, name) >= 0) baseNames.Add(name);
                else extra.Add(name);
            }

            var ordered = new List<string>();
            foreach (string name in HashSectionOrder)
                if (baseNames.Contains(name)) ordered.Add(name);
            foreach (KeyValuePair<int, DefinitionGroup> entry in groups)
            {
                string prefix = "idef." + entry.Key.ToString(CultureInfo.InvariantCulture) + ".";
                foreach (KeyValuePair<int, List<string>> part in entry.Value.Parts)
                {
                    string partPrefix = prefix + part.Key.ToString(CultureInfo.InvariantCulture) + ".";
                    foreach (string inner in OrderedNames(part.Value))
                        ordered.Add(partPrefix + inner);
                }
                foreach (string inner in OrderedNames(entry.Value.Base))
                    ordered.Add(prefix + inner);
            }
            extra.Sort(StringComparer.Ordinal);
            ordered.AddRange(extra);
            return ordered;
        }

        /// <summary>Topology token for every mesh/SubD helper a response writes
        /// (protocol §4.4): the base group plus each definition geometry group.
        /// It identifies what the Blender in-place fast path reuses: the
        /// selected group name, the vertex section's element count, and the raw
        /// face_counts/face_indices bytes, under the base or definition
        /// index/part it belongs to. Positions, colours, creases, attributes,
        /// curve and point sections, definition names and the response order are
        /// deliberately absent, so an attribute-only or position-only change
        /// keeps the token. A frame with no mesh/SubD section returns null and
        /// Blender keeps its verified topology path.</summary>
        public static string TopologyHash(IList<Section> sections)
        {
            if (sections == null) return null;

            using (var sha = SHA256.Create())
            {
                bool any = false;

                // The same precedence the Blender selector and Conversion use:
                // mesh.verts wins over subd.verts when both are present.
                Section verts = FindSection(sections, "mesh.verts");
                string group = "mesh";
                if (verts == null)
                {
                    verts = FindSection(sections, "subd.verts");
                    group = "subd";
                }
                if (verts != null)
                {
                    UpdateHashString(sha, "base");
                    UpdateHashString(sha, group);
                    UpdateHashU64(sha, (ulong)verts.ElementCount);
                    UpdateOptionalSection(sha, FindSection(sections, group + ".face_counts"));
                    UpdateOptionalSection(sha, FindSection(sections, group + ".face_indices"));
                    any = true;
                }

                List<DefinitionTopology> definitions = DefinitionTopologies(sections);
                definitions.Sort(delegate(DefinitionTopology left, DefinitionTopology right)
                {
                    int byIndex = left.Index.CompareTo(right.Index);
                    if (byIndex != 0) return byIndex;
                    return left.Part.CompareTo(right.Part);
                });
                foreach (DefinitionTopology definition in definitions)
                {
                    UpdateHashString(sha, "definition");
                    UpdateHashU64(sha, (ulong)definition.Index);
                    // -1 is the single-part namespace; part 0 is its first
                    // multi-part sibling. The +1 keeps both distinct.
                    UpdateHashU64(sha, (ulong)(definition.Part + 1));
                    UpdateHashString(sha, definition.Group);
                    UpdateHashU64(sha, (ulong)definition.Verts.ElementCount);
                    UpdateOptionalSection(sha, definition.Counts);
                    UpdateOptionalSection(sha, definition.Indices);
                    any = true;
                }

                if (!any) return null;
                sha.TransformFinalBlock(new byte[0], 0, 0);
                return "sha256:" + ToHex(sha.Hash);
            }
        }

        private sealed class DefinitionTopology
        {
            public int Index;
            public int Part;  // -1 for the single-part namespace (`idef.<i>.`).
            public string Group;
            public Section Verts;
            public Section Counts;
            public Section Indices;
        }

        private static List<DefinitionTopology> DefinitionTopologies(IList<Section> sections)
        {
            var prefixes = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (Section section in sections)
            {
                if (section == null || !section.Name.StartsWith("idef.", StringComparison.Ordinal))
                    continue;
                string prefix;
                if (TryDefinitionPrefix(section.Name, "mesh.verts", out prefix)
                    || TryDefinitionPrefix(section.Name, "subd.verts", out prefix))
                {
                    if (seen.Add(prefix)) prefixes.Add(prefix);
                }
            }

            var definitions = new List<DefinitionTopology>();
            foreach (string prefix in prefixes)
            {
                string group = FindSection(sections, prefix + "mesh.verts") != null
                    ? "mesh" : "subd";
                Section verts = FindSection(sections, prefix + group + ".verts");
                if (verts == null) continue;
                int index;
                int part;
                if (!ParseDefinitionPrefix(prefix, out index, out part)) continue;
                var definition = new DefinitionTopology();
                definition.Index = index;
                definition.Part = part;
                definition.Group = group;
                definition.Verts = verts;
                definition.Counts = FindSection(sections, prefix + group + ".face_counts");
                definition.Indices = FindSection(sections, prefix + group + ".face_indices");
                definitions.Add(definition);
            }
            return definitions;
        }

        private static bool TryDefinitionPrefix(string name, string groupSuffix, out string prefix)
        {
            prefix = null;
            string suffix = "." + groupSuffix;
            if (!name.EndsWith(suffix, StringComparison.Ordinal)) return false;
            prefix = name.Substring(0, name.Length - suffix.Length) + ".";
            return prefix.StartsWith("idef.", StringComparison.Ordinal);
        }

        /// <summary>Parse ``idef.&lt;i&gt;.`` or ``idef.&lt;i&gt;.&lt;p&gt;.`` into
        /// its numeric index and part (-1 for the single-part namespace). A
        /// malformed name is ignored here; the converters reject it as a frame
        /// error later.</summary>
        private static bool ParseDefinitionPrefix(string prefix, out int index, out int part)
        {
            index = 0;
            part = -1;
            string trimmed = prefix.Substring(0, prefix.Length - 1);
            string[] pieces = trimmed.Split('.');
            if (pieces.Length < 2 || pieces.Length > 3 || pieces[0] != "idef") return false;
            if (!TryIndex(pieces[1], out index)) return false;
            if (pieces.Length == 3 && !TryIndex(pieces[2], out part)) return false;
            return true;
        }

        private static Section FindSection(IList<Section> sections, string name)
        {
            foreach (Section section in sections)
                if (section != null && section.Name == name) return section;
            return null;
        }

        private static void UpdateHashString(HashAlgorithm sha, string value)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(value);
            UpdateHashU64(sha, (ulong)bytes.Length);
            sha.TransformBlock(bytes, 0, bytes.Length, null, 0);
        }

        private static void UpdateHashU64(HashAlgorithm sha, ulong value)
        {
            byte[] bytes = new byte[8];
            for (int i = 0; i < 8; i++)
            {
                bytes[i] = (byte)(value & 0xFF);
                value >>= 8;
            }
            sha.TransformBlock(bytes, 0, bytes.Length, null, 0);
        }

        private static void UpdateOptionalSection(HashAlgorithm sha, Section section)
        {
            byte[] present = new byte[] { section == null ? (byte)0 : (byte)1 };
            sha.TransformBlock(present, 0, 1, null, 0);
            if (section == null) return;
            UpdateHashU64(sha, (ulong)section.Raw.Length);
            sha.TransformBlock(section.Raw, 0, section.Raw.Length, null, 0);
        }

        /// <summary>Output hash: sections in the given (header) order (protocol
        /// §6.3). Attribute descriptors take part as one synthetic entry named
        /// `attr.descriptors` (canonical JSON): section names are index-based,
        /// so without it a renamed attribute would hash identically and Blender
        /// would skip the apply. The definitions list takes part the same way as
        /// the synthetic entry `definitions`.</summary>
        public static string OutputHash(IList<Section> sections, string attributesJson = null,
                                        string definitionsJson = null)
        {
            using (var sha = SHA256.Create())
            {
                foreach (Section s in sections) UpdateSection(sha, s);
                if (!string.IsNullOrEmpty(attributesJson))
                {
                    byte[] json = Encoding.UTF8.GetBytes(attributesJson);
                    var synthetic = new Section("attr.descriptors", "u8", json);
                    UpdateSection(sha, synthetic);
                }
                if (!string.IsNullOrEmpty(definitionsJson))
                {
                    byte[] json = Encoding.UTF8.GetBytes(definitionsJson);
                    var synthetic = new Section("definitions", "u8", json);
                    UpdateSection(sha, synthetic);
                }
                sha.TransformFinalBlock(new byte[0], 0, 0);
                return "sha256:" + ToHex(sha.Hash);
            }
        }

        private static readonly byte[] Zero = new byte[] { 0 };

        private static void UpdateSection(HashAlgorithm sha, Section s)
        {
            byte[] name = Encoding.UTF8.GetBytes(s.Name);
            sha.TransformBlock(name, 0, name.Length, null, 0);
            sha.TransformBlock(Zero, 0, 1, null, 0);
            sha.TransformBlock(s.Raw, 0, s.Raw.Length, null, 0);
        }

        // ------------------------------------------------------------ helpers

        public static string ToHex(byte[] data)
        {
            var sb = new StringBuilder(data.Length * 2);
            foreach (byte b in data) sb.Append(b.ToString("x2"));
            return sb.ToString();
        }

        private static void WriteU32(byte[] target, int offset, uint value)
        {
            target[offset] = (byte)(value & 0xFF);
            target[offset + 1] = (byte)((value >> 8) & 0xFF);
            target[offset + 2] = (byte)((value >> 16) & 0xFF);
            target[offset + 3] = (byte)((value >> 24) & 0xFF);
        }

        private static uint ReadU32(byte[] source, int offset)
        {
            return (uint)(source[offset]
                | (source[offset + 1] << 8)
                | (source[offset + 2] << 16)
                | (source[offset + 3] << 24));
        }
    }
}
