// Layer C, fuzz half: the frame codec against 10,000 deterministic mutations.
//
//     StageloomFuzzTests.exe
//
// No Rhino and no Grasshopper: this is the C# twin of
// blender/tests/core/test_codec_fuzz.py and runs under wine like the fixture
// suite. Every case starts from a valid encoded frame and applies one of ten
// bounded mutation strategies (length prefix, raw byte, truncation, appended
// junk, random bytes, header text, structured header, frame splice, oversized
// declared lengths and section/payload surgery), occasionally followed by a
// second mutation.
//
// The contract under test is the receive boundary: a mutated peer frame either
// decodes coherently or fails as FrameException with one of the protocol §4.6
// codes. A successful decode must agree with the frame bytes, and re-encoding
// the decoded values must reproduce the same sections. No unclassified
// exception may escape. A separate 1,000-case pass checks Json.Parse's own
// contract (value or JsonException).
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using System.Text;
using Stageloom.Core;

namespace Stageloom.Tests.Console
{
    public static class FuzzTests
    {
        private const int FrameCases = 10000;
        private const int JsonCases = 1000;
        // Fixed for reproducibility; changing it is a test-contract change.
        private const uint Seed = 20261118u;

        private static readonly string[] ValidCodes =
            new string[] { "proto_mismatch", "bad_frame", "too_large" };

        private static int _failures;

        public static int Main(string[] args)
        {
            var rng = new Rng(Seed);
            List<byte[]> pool = ValidFrames();
            var codes = new Dictionary<string, int>(StringComparer.Ordinal);
            int accepted = 0;

            for (int i = 0; i < FrameCases; i++)
            {
                int strategy = i % 10;
                byte[] frame = pool[rng.NextInt(pool.Count)];
                frame = Mutate(strategy, rng, frame, pool);
                if (rng.NextInt(100) < 35)
                    frame = Mutate(rng.NextInt(10), rng, frame, pool);

                try
                {
                    DecodedFrame decoded = FrameCodec.Decode(frame);
                    string problem = CheckDecoded(frame, decoded);
                    if (problem != null)
                    {
                        Report("case " + i + " (" + StrategyName(strategy)
                            + "): incoherent success: " + problem + "; frame=" + Hex(frame));
                        continue;
                    }
                    accepted++;
                }
                catch (FrameException ex)
                {
                    if (!IsValidCode(ex.Code))
                    {
                        Report("case " + i + " (" + StrategyName(strategy)
                            + "): unclassified code " + (ex.Code ?? "(null)"));
                        continue;
                    }
                    int count;
                    codes.TryGetValue(ex.Code, out count);
                    codes[ex.Code] = count + 1;
                }
                catch (Exception ex)
                {
                    Report("case " + i + " (" + StrategyName(strategy)
                        + "): unclassified " + ex.GetType().Name + ": " + ex.Message
                        + "; frame=" + Hex(frame));
                }
            }

            if (accepted == 0)
                Report("the frame campaign never accepted a valid mutation");
            int classified = 0;
            foreach (KeyValuePair<string, int> entry in codes)
            {
                if (!IsValidCode(entry.Key))
                    Report("campaign recorded unclassified code " + entry.Key);
                if (entry.Value <= 0)
                    Report("campaign recorded a zero count for " + entry.Key);
                classified += entry.Value;
            }
            if (accepted + classified != FrameCases)
                Report("the frame campaign did not account for every case");
            foreach (string code in ValidCodes)
                if (!codes.ContainsKey(code))
                    Report("the frame campaign never exercised code " + code);

            RunJsonFuzz(new Rng(Seed ^ 0x9E3779B9u), JsonCases);

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: frame-fuzz PASS ("
                    + FrameCases + " frame cases, seed " + Seed
                    + "; " + JsonCases + " json cases)");
                return 0;
            }
            System.Console.WriteLine("RESULT: frame-fuzz FAIL (" + _failures + ")");
            return 1;
        }

        private static bool IsValidCode(string code)
        {
            if (code == null) return false;
            for (int i = 0; i < ValidCodes.Length; i++)
                if (ValidCodes[i] == code) return true;
            return false;
        }

        private static string StrategyName(int index)
        {
            string[] names = new string[]
            {
                "length-prefix", "flip-byte", "truncate", "append-junk",
                "random-frame", "header-text", "structured-header", "splice",
                "oversized-declaration", "section-surgery",
            };
            return names[index];
        }

        private static void Report(string problem)
        {
            _failures++;
            System.Console.WriteLine("FAIL " + problem);
        }

        // ------------------------------------------------------------- frames

        private static List<byte[]> ValidFrames()
        {
            var frames = new List<byte[]>();
            var ping = new Dictionary<string, object>();
            ping["type"] = "ping";
            frames.Add(FrameCodec.Encode(ping, null));

            var response = new Dictionary<string, object>();
            response["type"] = "response";
            response["stage"] = "fuzz";
            var sections = new List<Section>();
            sections.Add(Section.Floats("mesh.verts",
                new float[] { 0.0f, 1.0f, 2.0f }));
            sections.Add(Section.U32("mesh.face_counts", new uint[] { 3 }));
            frames.Add(FrameCodec.Encode(response, sections));

            var mixed = new Dictionary<string, object>();
            mixed["type"] = "response";
            mixed["stage"] = "fuzz";
            mixed["rid"] = "sha256:" + new string('0', 64);
            mixed["out_hash"] = "sha256:" + new string('1', 64);
            var definitions = new List<object>();
            var definition = new Dictionary<string, object>();
            definition["name"] = "Block";
            definitions.Add(definition);
            mixed["definitions"] = definitions;
            var mixedSections = new List<Section>();
            mixedSections.Add(Section.Floats("mesh.verts", new float[]
            {
                0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f,
                1.0f, 1.0f, 0.0f, 0.0f, 1.0f, 0.0f,
            }));
            mixedSections.Add(Section.U32("mesh.face_counts", new uint[] { 4 }));
            mixedSections.Add(Section.U32("mesh.face_indices", new uint[] { 0, 1, 2, 3 }));
            mixedSections.Add(Section.Bytes("mesh.colors", new byte[]
            {
                255, 0, 0, 255, 0, 255, 0, 255,
                0, 0, 255, 255, 255, 255, 0, 255,
            }));
            mixedSections.Add(Section.Floats("attr.0.values",
                new float[] { 0.5f, 1.0f, 1.5f, 2.0f }));
            frames.Add(FrameCodec.Encode(mixed, mixedSections));

            var parts = new Dictionary<string, object>();
            parts["type"] = "request";
            parts["stage"] = "fuzz";
            var partDefinitions = new List<object>();
            var partDefinition = new Dictionary<string, object>();
            partDefinition["name"] = "Parts";
            partDefinition["parts"] = 2L;
            partDefinitions.Add(partDefinition);
            parts["definitions"] = partDefinitions;
            var partSections = new List<Section>();
            partSections.Add(Section.Floats("idef.0.0.mesh.verts", new float[]
            {
                0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f,
                1.0f, 1.0f, 0.0f, 0.0f, 1.0f, 0.0f,
            }));
            partSections.Add(Section.U32("idef.0.0.mesh.face_counts", new uint[] { 4 }));
            partSections.Add(Section.U32("idef.0.0.mesh.face_indices", new uint[] { 0, 1, 2, 3 }));
            partSections.Add(Section.Floats("idef.0.1.crv.points", new float[]
            {
                0.0f, 0.0f, 0.0f, 1.0f, 1.0f, 0.0f, 0.0f, 1.0f,
            }));
            partSections.Add(Section.U32("idef.0.1.crv.counts", new uint[] { 2 }));
            partSections.Add(Section.Bytes("idef.0.1.crv.types", new byte[] { 0 }));
            partSections.Add(Section.Bytes("idef.0.1.crv.orders", new byte[] { 0 }));
            partSections.Add(Section.Bytes("idef.0.1.crv.knots_mode", new byte[] { 0 }));
            partSections.Add(Section.Bytes("idef.0.1.crv.cyclic", new byte[] { 0 }));
            partSections.Add(Section.U32("inst.ref_def", new uint[] { 0, 0 }));
            partSections.Add(Section.Floats("inst.transforms", new float[]
            {
                1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
                1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 0, 0, 1,
            }));
            frames.Add(FrameCodec.Encode(parts, partSections));

            // Valid non-canonical tables: a multi-dimensional shape, an omitted
            // shape, and an explicit empty list.
            frames.Add(FrameWithBody(
                "{\"type\":\"response\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[2,2],\"bytes\":4}]}",
                new byte[] { 1, 2, 3, 4 }));
            frames.Add(FrameWithBody(
                "{\"type\":\"response\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"f32\",\"bytes\":4}]}",
                new byte[] { 0, 0, 0xC0, 0x3F }));
            frames.Add(FrameWithBody(
                "{\"type\":\"response\",\"v\":2,\"sections\":[]}", new byte[0]));
            return frames;
        }

        private static byte[] Mutate(int strategy, Rng rng, byte[] frame, List<byte[]> pool)
        {
            switch (strategy)
            {
                case 0: return MutateLengthPrefix(rng, frame);
                case 1: return FlipByte(rng, frame);
                case 2: return Truncate(rng, frame);
                case 3: return AppendJunk(rng, frame);
                case 4: return RandomFrame(rng);
                case 5: return HeaderText(rng, frame);
                case 6: return StructuredHeader(rng, frame);
                case 7: return Splice(rng, frame, pool);
                case 8: return OversizedDeclaration(rng, frame);
                default: return SectionSurgery(rng, frame);
            }
        }

        private static byte[] MutateLengthPrefix(Rng rng, byte[] frame)
        {
            uint[] choices = new uint[]
            {
                0u, 1u, 2u, 3u, (uint)frame.Length,
                (uint)Math.Max(0, frame.Length - 1),
                (uint)(frame.Length + 1),
                (uint)FrameCodec.MaxHeaderBytes + 1u,
                0xFFFFFFFFu, rng.Next(),
            };
            uint value = choices[rng.NextInt(choices.Length)];
            int payload = Math.Max(0, frame.Length - 4);
            var result = new byte[4 + payload];
            if (payload > 0)
                Buffer.BlockCopy(frame, 4, result, 4, payload);
            WriteU32(result, 0, value);
            return result;
        }

        private static byte[] FlipByte(Rng rng, byte[] frame)
        {
            if (frame.Length == 0) return frame;
            var result = new byte[frame.Length];
            Buffer.BlockCopy(frame, 0, result, 0, frame.Length);
            int index = rng.NextInt(result.Length);
            result[index] = (byte)(result[index] ^ (1 << rng.NextInt(8)));
            return result;
        }

        private static byte[] Truncate(Rng rng, byte[] frame)
        {
            int length = rng.NextInt(frame.Length + 1);
            var result = new byte[length];
            Buffer.BlockCopy(frame, 0, result, 0, length);
            return result;
        }

        private static byte[] AppendJunk(Rng rng, byte[] frame)
        {
            int extra = 1 + rng.NextInt(16);
            var result = new byte[frame.Length + extra];
            Buffer.BlockCopy(frame, 0, result, 0, frame.Length);
            for (int i = 0; i < extra; i++)
                result[frame.Length + i] = (byte)rng.Next();
            return result;
        }

        private static byte[] RandomFrame(Rng rng)
        {
            var result = new byte[rng.NextInt(65)];
            for (int i = 0; i < result.Length; i++)
                result[i] = (byte)rng.Next();
            return result;
        }

        private static byte[] HeaderText(Rng rng, byte[] frame)
        {
            int start;
            int end;
            if (!HeaderBounds(frame, out start, out end)) return frame;
            string text;
            try
            {
                text = Encoding.UTF8.GetString(frame, start, end - start);
            }
            catch (Exception)
            {
                return frame;
            }
            string[] patterns = new string[]
            {
                "\"f32\"", "\"dtype\":\"u32\"", "\"bytes\":4", "\"v\":2",
                "\"type\":\"response\"", "\"name\":\"mesh.verts\"",
                "\"name\":\"a\"", "\"shape\":[1]", ":", "\"sections\"",
            };
            string[] replacements = new string[]
            {
                "\"f64\"", "\"dtype\":\"u8\"", "\"bytes\":3", "\"v\":3",
                "\"type\":\"\"", "\"name\":\"mesh.other\"",
                "\"name\":\"a\"", "\"shape\":[2]", ",", "\"sections\":{},\"old_sections\"",
            };
            int index = rng.NextInt(patterns.Length);
            string changed = text;
            int at = text.IndexOf(patterns[index], StringComparison.Ordinal);
            if (at >= 0)
                changed = text.Substring(0, at) + replacements[index]
                    + text.Substring(at + patterns[index].Length);
            else if (rng.NextInt(4) == 0)
                changed = text + "{\"a\":1,\"a\":2}";
            else if (rng.NextInt(4) == 0)
                changed = text.Substring(0, text.Length / 2);
            else if (rng.NextInt(4) == 0)
                changed = text.Replace("\"v\":2", "\"v\":2,\"note\":\"\\ud800\"");
            else
                changed = text + " ";

            byte[] head = Encoding.UTF8.GetBytes(changed);
            if (rng.NextInt(5) == 0)
            {
                // Explicitly malformed UTF-8 in the header, not only JSON syntax.
                int cut = rng.NextInt(head.Length + 1);
                var bad = new byte[head.Length + 1];
                Buffer.BlockCopy(head, 0, bad, 0, cut);
                bad[cut] = 0xFF;
                Buffer.BlockCopy(head, cut, bad, cut + 1, head.Length - cut);
                head = bad;
            }
            return RebuildHeader(frame, start, end, head);
        }

        private static byte[] StructuredHeader(Rng rng, byte[] frame)
        {
            int start;
            int end;
            if (!HeaderBounds(frame, out start, out end)) return frame;
            object parsed;
            try
            {
                parsed = Json.Parse(Encoding.UTF8.GetString(frame, start, end - start));
            }
            catch (JsonException)
            {
                return frame;
            }
            var header = parsed as Dictionary<string, object>;
            if (header == null) return frame;

            int change = rng.NextInt(9);
            if (change == 0)
            {
                object[] values = new object[]
                {
                    1L, 3L, 2.0, true, null, "2", new List<object>(),
                };
                header["v"] = values[rng.NextInt(values.Length)];
            }
            else if (change == 1)
            {
                object[] values = new object[]
                {
                    "", null, 5L, new List<object>(), new Dictionary<string, object>(),
                };
                header["type"] = values[rng.NextInt(values.Length)];
            }
            else if (change == 2)
            {
                header.Remove("type");
            }
            else if (change == 3)
            {
                object[] values = new object[]
                {
                    null, new Dictionary<string, object>(), "sections", 7L, true,
                };
                header["sections"] = values[rng.NextInt(values.Length)];
            }
            else if (change == 4)
            {
                var table = Json.Get(header, "sections") as List<object>;
                if (table != null && table.Count > 0)
                {
                    var entry = table[rng.NextInt(table.Count)] as Dictionary<string, object>;
                    if (entry != null)
                    {
                        string[] fields = new string[] { "name", "dtype", "bytes", "shape" };
                        object[] values = new object[]
                        {
                            null, "", -1L, 0L, 4.0, true, "x",
                            new List<object>(), new Dictionary<string, object>(),
                            1000000000000000000L,
                        };
                        entry[fields[rng.NextInt(fields.Length)]]
                            = values[rng.NextInt(values.Length)];
                    }
                }
            }
            else if (change == 5)
            {
                var table = Json.Get(header, "sections") as List<object>;
                if (table != null && table.Count > 0)
                {
                    var copy = new Dictionary<string, object>();
                    var first = table[0] as Dictionary<string, object>;
                    if (first != null)
                        foreach (KeyValuePair<string, object> pair in first)
                            copy[pair.Key] = pair.Value;
                    table.Add(copy);
                }
            }
            else if (change == 6)
            {
                var table = Json.Get(header, "sections") as List<object>;
                if (table != null && table.Count > 0)
                {
                    var entry = table[0] as Dictionary<string, object>;
                    if (entry != null)
                    {
                        entry["bytes"] = new object[]
                        {
                            1000000000L, 1000000000000000000L, long.MaxValue, -1L,
                        }[rng.NextInt(4)];
                        entry["shape"] = new object[]
                        {
                            new List<object> { 0L },
                            new List<object> { long.MaxValue },
                            new List<object> { 2L, 2L },
                            new List<object>(),
                            null,
                        }[rng.NextInt(5)];
                    }
                }
            }
            else if (change == 7)
            {
                var entry = new Dictionary<string, object>();
                entry["name"] = new object[] { "a", "", null, 5L }[rng.NextInt(4)];
                entry["dtype"] = new object[] { "f32", "f64", null, 5L }[rng.NextInt(4)];
                entry["bytes"] = new object[]
                {
                    0L, 1L, 4L, -4L, 1000000000000000000L, "4",
                }[rng.NextInt(6)];
                entry["shape"] = new object[]
                {
                    null, new List<object>(), new List<object> { 2L }, "2",
                }[rng.NextInt(4)];
                header["sections"] = new List<object> { entry };
            }
            else
            {
                header["extra"] = new object[]
                {
                    null, true, 0L, -1L, 1e100, "x",
                    new List<object>(), new Dictionary<string, object>(),
                }[rng.NextInt(8)];
            }

            byte[] head;
            try
            {
                head = Json.WriteCanonicalUtf8(header);
            }
            catch (JsonException)
            {
                head = Encoding.UTF8.GetBytes("not json");
            }
            return RebuildHeader(frame, start, end, head);
        }

        private static byte[] Splice(Rng rng, byte[] frame, List<byte[]> pool)
        {
            byte[] other = pool[rng.NextInt(pool.Count)];
            int start;
            int end;
            int otherStart;
            int otherEnd;
            if (!HeaderBounds(frame, out start, out end)) return frame;
            if (!HeaderBounds(other, out otherStart, out otherEnd)) return frame;
            var result = new byte[4 + (end - start) + (other.Length - otherEnd)];
            WriteU32(result, 0, (uint)(end - start));
            Buffer.BlockCopy(frame, start, result, 4, end - start);
            Buffer.BlockCopy(other, otherEnd, result, 4 + (end - start),
                other.Length - otherEnd);
            return result;
        }

        private static byte[] OversizedDeclaration(Rng rng, byte[] frame)
        {
            int start;
            int end;
            if (!HeaderBounds(frame, out start, out end)) return frame;
            object parsed;
            try
            {
                parsed = Json.Parse(Encoding.UTF8.GetString(frame, start, end - start));
            }
            catch (JsonException)
            {
                return frame;
            }
            var header = parsed as Dictionary<string, object>;
            if (header == null) return frame;
            long[] sizes = new long[]
            {
                1000000000000000000L, long.MaxValue, 1000000000000L,
            };
            long size = sizes[rng.NextInt(sizes.Length)];
            var entry = new Dictionary<string, object>();
            entry["name"] = "mesh.verts";
            entry["dtype"] = "f32";
            entry["bytes"] = size;
            entry["shape"] = new List<object> { size / 4L };
            var second = new Dictionary<string, object>();
            second["name"] = "mesh.more";
            second["dtype"] = "u8";
            second["bytes"] = size;
            header["sections"] = new List<object> { entry, second };
            byte[] head = Json.WriteCanonicalUtf8(header);
            return RebuildHeader(frame, start, end, head);
        }

        private static byte[] SectionSurgery(Rng rng, byte[] frame)
        {
            int start;
            int end;
            if (!HeaderBounds(frame, out start, out end)) return frame;
            object parsed;
            try
            {
                parsed = Json.Parse(Encoding.UTF8.GetString(frame, start, end - start));
            }
            catch (JsonException)
            {
                return frame;
            }
            var header = parsed as Dictionary<string, object>;
            if (header == null) return frame;
            var table = Json.Get(header, "sections") as List<object>;
            if (table == null || table.Count == 0) return frame;
            int choice = rng.NextInt(5);
            var entry = table[rng.NextInt(table.Count)] as Dictionary<string, object>;
            if (choice == 0 && entry != null)
            {
                entry["bytes"] = 0L;
            }
            else if (choice == 1)
            {
                var copy = new Dictionary<string, object>();
                var first = table[0] as Dictionary<string, object>;
                if (first != null)
                    foreach (KeyValuePair<string, object> pair in first)
                        copy[pair.Key] = pair.Value;
                table.Add(copy);
            }
            else if (choice == 2 && entry != null)
            {
                entry["dtype"] = new string[] { "u8", "u32", "i32" }[rng.NextInt(3)];
            }
            else if (choice == 3)
            {
                table.RemoveAt(rng.NextInt(table.Count));
            }
            else
            {
                table.Reverse();
            }
            byte[] head = Json.WriteCanonicalUtf8(header);
            int payloadLength = frame.Length - end;
            int cut = rng.NextInt(payloadLength + 1);
            var result = new byte[4 + head.Length + cut];
            WriteU32(result, 0, (uint)head.Length);
            Buffer.BlockCopy(head, 0, result, 4, head.Length);
            Buffer.BlockCopy(frame, end, result, 4 + head.Length, cut);
            return result;
        }

        private static byte[] RebuildHeader(byte[] frame, int start, int end, byte[] head)
        {
            int payloadLength = frame.Length - end;
            var result = new byte[4 + head.Length + payloadLength];
            WriteU32(result, 0, (uint)head.Length);
            Buffer.BlockCopy(head, 0, result, 4, head.Length);
            Buffer.BlockCopy(frame, end, result, 4 + head.Length, payloadLength);
            return result;
        }

        private static bool HeaderBounds(byte[] frame, out int start, out int end)
        {
            start = 4;
            end = 4;
            if (frame.Length < 4) return false;
            long headLen = ReadU32(frame, 0);
            if (headLen > frame.Length - 4) return false;
            end = 4 + (int)headLen;
            return true;
        }

        private static byte[] FrameWithBody(string header, byte[] body)
        {
            byte[] head = Encoding.UTF8.GetBytes(header);
            var frame = new byte[4 + head.Length + body.Length];
            WriteU32(frame, 0, (uint)head.Length);
            Buffer.BlockCopy(head, 0, frame, 4, head.Length);
            Buffer.BlockCopy(body, 0, frame, 4 + head.Length, body.Length);
            return frame;
        }

        // ---------------------------------------------------------- coherence

        private static string CheckDecoded(byte[] frame, DecodedFrame decoded)
        {
            if (decoded == null) return "Decode returned null";
            object version = Json.Get(decoded.Header, "v");
            if (!(version is long) || (long)version != 2L)
                return "decoded v is not 2";
            object type = Json.Get(decoded.Header, "type");
            if (!(type is string) || ((string)type).Length == 0)
                return "decoded type is invalid";

            object tableObj;
            bool hasTable = decoded.Header.TryGetValue("sections", out tableObj);
            var table = hasTable ? tableObj as List<object> : null;
            if (hasTable && table == null)
                return "decoded sections table is not an array";
            int expected = table == null ? 0 : table.Count;
            if (decoded.Sections.Count != expected)
                return "decoded section count differs from the table";

            long headLen = ReadU32(frame, 0);
            int offset = 4 + (int)headLen;
            long total = 0;
            for (int i = 0; i < decoded.Sections.Count; i++)
            {
                var entry = table[i] as Dictionary<string, object>;
                if (entry == null) return "decoded table entry " + i + " is not an object";
                string name = Json.Get(entry, "name") as string;
                string dtype = Json.Get(entry, "dtype") as string;
                object bytesObj = Json.Get(entry, "bytes");
                long nbytes;
                if (bytesObj is long) nbytes = (long)bytesObj;
                else if (bytesObj is int) nbytes = (int)bytesObj;
                else return "decoded table entry " + i + " has no integer bytes";

                Section section = decoded.Sections[i];
                if (section.Name != name) return "section " + i + " name differs";
                if (section.Dtype != dtype) return "section " + i + " dtype differs";
                if (section.ByteLength != nbytes)
                    return "section " + i + " length " + section.ByteLength
                        + " != declared " + nbytes;
                if (section.Raw.Length != nbytes)
                    return "section " + i + " Raw length differs";
                int itemSize = ProtocolTables.ItemSize(dtype);
                if ((long)section.ElementCount * itemSize != nbytes)
                    return "section " + i + " element count disagrees with bytes";
                for (int b = 0; b < nbytes; b++)
                    if (section.Raw[b] != frame[offset + b])
                        return "section " + i + " byte " + b + " differs from the frame";
                offset += (int)nbytes;
                total += nbytes;
            }
            if (offset != frame.Length)
                return "decoded sections do not cover the payload";

            byte[] rebuilt;
            try
            {
                rebuilt = FrameCodec.Encode(
                    new Dictionary<string, object>(decoded.Header), decoded.Sections);
            }
            catch (Exception ex)
            {
                return "re-encode threw " + ex.GetType().Name + ": " + ex.Message;
            }
            DecodedFrame again;
            try
            {
                again = FrameCodec.Decode(rebuilt);
            }
            catch (Exception ex)
            {
                return "re-decode threw " + ex.GetType().Name + ": " + ex.Message;
            }
            if (again.Sections.Count != decoded.Sections.Count)
                return "re-encode changed the section count";
            for (int i = 0; i < again.Sections.Count; i++)
            {
                Section left = decoded.Sections[i];
                Section right = again.Sections[i];
                if (left.Name != right.Name || left.Dtype != right.Dtype)
                    return "re-encode changed section " + i;
                if (!BytesEqual(left.Raw, right.Raw))
                    return "re-encode changed the bytes of section " + i;
            }
            return null;
        }

        // -------------------------------------------------------------- json

        private static void RunJsonFuzz(Rng rng, int cases)
        {
            string[] seeds = new string[]
            {
                "{\"type\":\"ping\",\"v\":2}",
                "{\"type\":\"response\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"f32\",\"bytes\":4}]}",
                "[1,2,3]",
                "\"text\"",
                "null",
                "true",
            };
            int accepted = 0;
            int rejected = 0;
            for (int i = 0; i < cases; i++)
            {
                string text = seeds[rng.NextInt(seeds.Length)];
                int change = rng.NextInt(6);
                if (change == 0 && text.Length > 0)
                {
                    int at = rng.NextInt(text.Length);
                    text = text.Substring(0, at) + text.Substring(at + 1);
                }
                else if (change == 1)
                {
                    int at = rng.NextInt(text.Length + 1);
                    char[] junk = new char[]
                    {
                        '"', '\\', '{', '}', '[', ']', ':', ',', '\n', '\t',
                        '0', 'x', 'N', 'a',
                    };
                    text = text.Substring(0, at) + junk[rng.NextInt(junk.Length)]
                        + text.Substring(at);
                }
                else if (change == 2)
                {
                    text = text + new string(']', 1 + rng.NextInt(70));
                }
                else if (change == 3)
                {
                    text = text.Replace("\"v\":2", "\"v\":2,\"a\":1,\"a\":2");
                }
                else if (change == 4)
                {
                    text = text.Replace("2", "9223372036854775808");
                }
                else
                {
                    text = new string((char)('0' + rng.NextInt(10)),
                        1 + rng.NextInt(20));
                }
                try
                {
                    Json.Parse(text);
                    accepted++;
                }
                catch (JsonException)
                {
                    rejected++;
                }
                catch (Exception ex)
                {
                    Report("json case " + i + ": unclassified " + ex.GetType().Name
                        + ": " + ex.Message + "; text=" + Escape(text));
                }
            }
            if (accepted + rejected != cases)
                Report("the json campaign did not account for every case");
            if (accepted == 0)
                Report("the json campaign never accepted a value");
            if (rejected == 0)
                Report("the json campaign never rejected a value");
        }

        // ----------------------------------------------------------- helpers

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

        private static bool BytesEqual(byte[] left, byte[] right)
        {
            if (left.Length != right.Length) return false;
            for (int i = 0; i < left.Length; i++)
                if (left[i] != right[i]) return false;
            return true;
        }

        private static string Hex(byte[] data)
        {
            var sb = new StringBuilder(data.Length * 2);
            for (int i = 0; i < data.Length; i++)
                sb.Append(data[i].ToString("x2"));
            return sb.ToString();
        }

        private static string Escape(string text)
        {
            var sb = new StringBuilder();
            for (int i = 0; i < text.Length; i++)
            {
                char c = text[i];
                if (c >= 0x20 && c < 0x7F) sb.Append(c);
                else sb.Append("\\u").Append(((int)c).ToString("x4"));
            }
            return sb.ToString();
        }

        /// <summary>A deterministic xorshift32 stream (no framework Random
        /// version dependency), seeded once per campaign.</summary>
        private sealed class Rng
        {
            private uint _state;

            public Rng(uint seed)
            {
                _state = seed == 0u ? 0x9E3779B9u : seed;
            }

            public uint Next()
            {
                uint value = _state;
                value ^= value << 13;
                value ^= value >> 17;
                value ^= value << 5;
                _state = value;
                return value;
            }

            public int NextInt(int bound)
            {
                if (bound <= 0) return 0;
                return (int)(Next() % (uint)bound);
            }
        }
    }
}
