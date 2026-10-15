// Protocol v2 frame codec: encode and decode the binary frames, plus the two
// hashes. The C# twin of blender/gh_stack/core/codec.py and hashing.py, checked
// against the same golden fixtures (protocol/fixtures, docs/protocol.md §9).
//
// No RhinoCommon and no Grasshopper references live here, so this file builds into
// console tests that run under wine without Rhino.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace GhStack.Core
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
        };

        // -------------------------------------------------------------- encode

        /// <summary>Encode one frame. The header gains "v" and the section table,
        /// exactly as the Python codec does, so headers stay in sync with bytes.</summary>
        public static byte[] Encode(Dictionary<string, object> header, IList<Section> sections)
        {
            var head = new Dictionary<string, object>(header);
            if (!head.ContainsKey("v")) head["v"] = (long)ProtocolVersion;
            if (!(head.ContainsKey("type") && head["type"] is string))
                throw new FrameException("bad_frame", "header needs a type");

            var table = new List<object>();
            long total = 0;
            if (sections != null && sections.Count > 0)
            {
                foreach (Section s in sections)
                {
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

            byte[] headBytes = Json.WriteCanonicalUtf8(head);
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
            long headLen = ReadU32(frame, 0);
            if (headLen > MaxHeaderBytes)
                throw new FrameException("bad_frame", "header length " + headLen + " over the cap");
            if (frame.Length < 4 + headLen)
                throw new FrameException("bad_frame", "header is truncated");

            string headText = Encoding.UTF8.GetString(frame, 4, (int)headLen);
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
            // A number compares like Python's `v != expect`, so 2.0 == 2 holds
            // and a string "2" does not (P5-2). A non-number must land here as a
            // proto_mismatch, not as a converter exception (P2-2): the caller
            // would otherwise tear down a healthy connection over one bad frame.
            bool versionOk = Json.IsNumber(v)
                             && Convert.ToDouble(v, CultureInfo.InvariantCulture) == ProtocolVersion;
            if (!versionOk)
                throw new FrameException("proto_mismatch",
                    "header v=" + (v == null ? "(missing)" : Convert.ToString(v, CultureInfo.InvariantCulture))
                    + ", expected " + ProtocolVersion);

            var sections = new List<Section>();
            long offset = 4 + headLen;
            object tableObj = Json.Get(header, "sections");
            var table = tableObj as List<object>;
            if (table != null)
            {
                foreach (object entryObj in table)
                {
                    var entry = entryObj as Dictionary<string, object>;
                    if (entry == null)
                        throw new FrameException("bad_frame", "section entry is not an object");
                    // Validate before the values are used: a missing name makes
                    // the section unmatchable (`Find` compares names), and an
                    // unknown dtype is a frame Python rejects (codec.py:88).
                    string name = Json.GetString(entry, "name");
                    if (string.IsNullOrEmpty(name))
                        throw new FrameException("bad_frame", "section entry without a name");
                    string dtype = Json.GetString(entry, "dtype");
                    if (dtype != "f32" && dtype != "i32" && dtype != "u32" && dtype != "u8")
                        throw new FrameException("bad_frame",
                            "section " + name + " has unknown dtype " + (dtype ?? "(missing)"));
                    long nbytes = Json.GetLong(entry, "bytes");
                    if (offset + nbytes > frame.Length)
                        throw new FrameException("bad_frame", "section " + name + " runs past the payload");
                    var raw = new byte[nbytes];
                    Buffer.BlockCopy(frame, (int)offset, raw, 0, (int)nbytes);
                    offset += nbytes;
                    if (dtype == "u8")
                    {
                        // one byte per element, nothing to check
                    }
                    else if (nbytes % 4 != 0)
                    {
                        throw new FrameException("bad_frame",
                            "section " + name + " length " + nbytes + " is not a multiple of 4");
                    }
                    sections.Add(new Section(name, dtype, raw));
                }
            }
            if (offset != frame.Length)
                throw new FrameException("bad_frame",
                    (frame.Length - offset) + " trailing bytes after the declared sections");
            return new DecodedFrame(header, sections);
        }

        // --------------------------------------------------------------- hash

        public static string Sha256Hex(byte[] data)
        {
            using (var sha = SHA256.Create())
            {
                return "sha256:" + ToHex(sha.ComputeHash(data));
            }
        }

        /// <summary>Geometry hash: sections in the fixed order above (protocol §6.1).</summary>
        public static string GeometryHash(IList<Section> sections)
        {
            var byName = new Dictionary<string, Section>();
            foreach (Section s in sections)
                if (!byName.ContainsKey(s.Name)) byName[s.Name] = s;

            var known = new List<Section>();
            foreach (string name in HashSectionOrder)
            {
                Section s;
                if (byName.TryGetValue(name, out s)) known.Add(s);
            }
            var extraNames = new List<string>();
            foreach (string name in byName.Keys)
                if (Array.IndexOf(HashSectionOrder, name) < 0) extraNames.Add(name);
            extraNames.Sort(StringComparer.Ordinal);

            using (var sha = SHA256.Create())
            {
                foreach (Section s in known) UpdateSection(sha, s);
                foreach (string name in extraNames) UpdateSection(sha, byName[name]);
                sha.TransformFinalBlock(new byte[0], 0, 0);
                return "sha256:" + ToHex(sha.Hash);
            }
        }

        /// <summary>Output hash: sections in the given (header) order (protocol
        /// §6.3). Attribute descriptors take part as one synthetic entry named
        /// `attr.descriptors` (canonical JSON): section names are index-based,
        /// so without it a renamed attribute would hash identically and Blender
        /// would skip the apply.</summary>
        public static string OutputHash(IList<Section> sections, string attributesJson = null)
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
                sha.TransformFinalBlock(new byte[0], 0, 0);
                return "sha256:" + ToHex(sha.Hash);
            }
        }

        private static void UpdateSection(HashAlgorithm sha, Section s)
        {
            byte[] name = Encoding.UTF8.GetBytes(s.Name);
            sha.TransformBlock(name, 0, name.Length, null, 0);
            sha.TransformBlock(new byte[] { 0 }, 0, 1, null, 0);
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
