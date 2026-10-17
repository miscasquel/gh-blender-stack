// Layer C, codec half: the C# codec against the golden fixtures.
//
//     GhStackCoreTests.exe <fixtures root>
//
// No Rhino and no Grasshopper: the codec is pure managed code, so this runs under
// the wine runtime directly (see build.sh). For every fixture it checks:
//   1. frame.bin decodes, and the header matches expect.json exactly
//   2. every section's dtype, shape, byte length, and sha256 match expect.json
//   3. re-encoding the decoded values reproduces frame.bin byte for byte
//      (this is what pins the canonical header encoding)
//   4. the section hash matches expect.json's sections_hash, where present
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using GhStack.Core;

namespace GhStack.Tests.Console
{
    public static class Program
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            if (args.Length < 1)
            {
                System.Console.WriteLine("usage: GhStackCoreTests.exe <fixtures root>");
                return 2;
            }
            string root = args[0];
            if (!Directory.Exists(root))
            {
                System.Console.WriteLine("no fixtures root at " + root);
                return 2;
            }

            var names = new List<string>(Directory.GetDirectories(root));
            names.Sort(StringComparer.Ordinal);
            int ran = 0;
            foreach (string dir in names)
            {
                string framePath = Path.Combine(dir, "frame.bin");
                string expectPath = Path.Combine(dir, "expect.json");
                if (!File.Exists(framePath) || !File.Exists(expectPath)) continue;
                ran++;
                CheckFixture(Path.GetFileName(dir), framePath, expectPath);
            }

            CheckParamValues();

            CheckDecoderBoundary();

            CheckFloatText();

            CheckJsonWriter();

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: " + ran + " fixtures PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: " + _failures + " check(s) FAILED across " + ran + " fixtures");
            return 1;
        }

        private static void CheckFixture(string name, string framePath, string expectPath)
        {
            byte[] frame = File.ReadAllBytes(framePath);
            var expect = (Dictionary<string, object>)Json.Parse(File.ReadAllText(expectPath, Encoding.UTF8));
            var problems = new List<string>();

            // 1. decode and compare the header
            DecodedFrame decoded = null;
            try
            {
                decoded = FrameCodec.Decode(frame);
            }
            catch (FrameException ex)
            {
                problems.Add("decode threw " + ex.Code + ": " + ex.Message);
            }
            if (decoded == null)
            {
                Report(name, problems);
                return;
            }
            if (!Json.DeepEquals(decoded.Header, Json.Get(expect, "header")))
                problems.Add("header does not match expect.json");

            // frame-level checks
            string frameSha = FrameCodec.Sha256Hex(frame);
            string expectedFrameSha = Json.GetString(expect, "frame_sha256");
            if (frameSha != expectedFrameSha)
                problems.Add("frame sha256 " + frameSha + " != " + expectedFrameSha);

            // 2. sections
            var expectedSections = Json.Get(expect, "sections") as Dictionary<string, object>;
            var seen = new List<string>();
            foreach (Section s in decoded.Sections)
            {
                seen.Add(s.Name);
                var meta = expectedSections != null && expectedSections.ContainsKey(s.Name)
                    ? expectedSections[s.Name] as Dictionary<string, object>
                    : null;
                if (meta == null)
                {
                    problems.Add("unexpected section " + s.Name);
                    continue;
                }
                if (Json.GetString(meta, "dtype") != s.Dtype)
                    problems.Add(s.Name + ": dtype " + s.Dtype + " != " + Json.GetString(meta, "dtype"));
                if (Json.GetLong(meta, "bytes") != s.Raw.Length)
                    problems.Add(s.Name + ": bytes " + s.Raw.Length + " != " + Json.GetLong(meta, "bytes"));
                var shape = Json.Get(meta, "shape") as List<object>;
                if (shape == null || shape.Count != 1 || Convert.ToInt64(shape[0]) != s.ElementCount)
                    problems.Add(s.Name + ": shape does not match");
                string sha = FrameCodec.Sha256Hex(s.Raw);
                if (sha != Json.GetString(meta, "sha256"))
                    problems.Add(s.Name + ": sha256 " + sha + " != " + Json.GetString(meta, "sha256"));
            }
            if (expectedSections != null)
                foreach (string expectedName in expectedSections.Keys)
                    if (!seen.Contains(expectedName))
                        problems.Add("missing section " + expectedName);

            // 3. re-encode to identical bytes
            byte[] reencoded = FrameCodec.Encode(new Dictionary<string, object>(decoded.Header), decoded.Sections);
            if (!BytesEqual(reencoded, frame))
                problems.Add("re-encoded frame differs (" + reencoded.Length + " vs " + frame.Length + " bytes)");

            // 4. section hash, where the fixture declares one
            string expectedHash = Json.GetString(expect, "sections_hash");
            if (expectedHash != null)
            {
                string got = FrameCodec.GeometryHash(decoded.Sections);
                if (got != expectedHash)
                    problems.Add("sections_hash " + got + " != " + expectedHash);
            }

            Report(name, problems);

            // Exercise the typed accessors on a couple of fixtures, so a broken
            // conversion path cannot hide behind the raw-byte checks. Guarded:
            // the fixture is regenerated from time to time and a missing
            // section must report, not dereference null (P6-18).
            if (name == "response-mixed")
            {
                Section verts = decoded.Find("mesh.verts");
                if (verts == null)
                {
                    problems.Add("typed access: mesh.verts is missing");
                }
                else
                {
                    float[] f = verts.ToFloats();
                    if (f.Length != 9 || f[3] != 1.0f)
                        problems.Add("typed access: mesh.verts looks wrong");
                }
                Section counts = decoded.Find("crv.counts");
                if (counts == null)
                {
                    problems.Add("typed access: crv.counts is missing");
                }
                else
                {
                    uint[] c = counts.ToU32();
                    if (c.Length != 2 || c[0] != 2 || c[1] != 3)
                        problems.Add("typed access: crv.counts looks wrong");
                }
            }
        }

        /// <summary>Parameter value conversion, the pure half of the Param
        /// component. A vector arrives from Json.Parse as List<object>, not
        /// double[]; treating only double[] silently produced (0,0,0) live.</summary>
        private static void CheckParamValues()
        {
            var problems = new List<string>();

            object parsed = Json.Parse("[2.0, 2.0, 2.0]");
            double[] doubles = ParamValues.AsDoubles(parsed);
            if (doubles == null || doubles.Length != 3 || doubles[0] != 2.0)
                problems.Add("AsDoubles did not accept the parsed JSON array");
            if (ParamValues.AsDoubles(1.04) != null)
                problems.Add("AsDoubles accepted a scalar");

            object cast = ParamValues.Cast("vector3", parsed);
            var castArray = cast as double[];
            if (castArray == null || castArray[2] != 2.0)
                problems.Add("Cast(vector3) lost the parsed array");
            object fallback = ParamValues.Cast("vector3", 1.04);
            var fallbackArray = fallback as double[];
            if (fallbackArray == null || fallbackArray[0] != 0.0)
                problems.Add("Cast(vector3) did not fall back to zeros");

            object def = ParamValues.ParseDefault("vector3", "1, 2.5, 3");
            var defArray = def as double[];
            if (defArray == null || defArray[1] != 2.5)
                problems.Add("ParseDefault(vector3) failed");
            if ((string)ParamValues.ParseDefault("text", null) != "")
                problems.Add("ParseDefault(text, null) failed");
            if (!(bool)ParamValues.ParseDefault("bool", "true"))
                problems.Add("ParseDefault(bool) failed");

            // Cast must be total: a value left from another type (or a racing
            // request) never throws into SolveInstance.
            if (!(ParamValues.Cast("float", "[0.0, 0.0, 0.0]") is double))
                problems.Add("Cast(float, vector string) did not fall back to a double");
            if ((double)ParamValues.Cast("float", "[0.0, 0.0, 0.0]") != 0.0)
                problems.Add("Cast(float, vector string) was not neutral");
            if ((string)ParamValues.Cast("text", parsed) != "")
                problems.Add("Cast(text, list) was not empty");
            var junk = ParamValues.Cast("vector3", "junk") as double[];
            if (junk == null || junk[0] != 0.0)
                problems.Add("Cast(vector3, junk) was not neutral");
            if ((int)ParamValues.Cast("value_list", "nonsense") != 0)
                problems.Add("Cast(value_list, nonsense) was not neutral");
            if ((bool)ParamValues.Cast("bool", "not a bool") != false)
                problems.Add("Cast(bool, string) was not neutral");

            // P5-1, the two sides' int rules read from params.py: `int` is
            // int(round(float(v))) — banker's rounding — and `value_list` is
            // int(v), truncation. A disagreement would compute one input as two
            // different request IDs, one per side.
            if ((int)ParamValues.Cast("int", 2.7) != 3)
                problems.Add("Cast(int, 2.7) did not round");
            if ((int)ParamValues.Cast("int", 2.5) != 2 || (int)ParamValues.Cast("int", 3.5) != 4)
                problems.Add("Cast(int) is not banker's rounding");
            if ((int)ParamValues.Cast("value_list", 2.7) != 2)
                problems.Add("Cast(value_list, 2.7) did not truncate");
            if ((int)ParamValues.Cast("value_list", -2.7) != -2)
                problems.Add("Cast(value_list, -2.7) did not truncate toward zero");
            if ((long)ParamValues.ParseDefault("int", "2.7") != 3L)
                problems.Add("ParseDefault(int, 2.7) did not round");

            object json = ParamValues.JsonValue(new double[] { 1.0, 2.0, 3.0 });
            var jsonList = json as List<object>;
            if (jsonList == null || jsonList.Count != 3)
                problems.Add("JsonValue(double[]) failed");

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS param-values");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL param-values");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        /// <summary>Decoder boundary: malformed input must fail as a classified
        /// exception, never as a raw one a caller cannot catch (P2-1/2/3).</summary>
        private static void CheckDecoderBoundary()
        {
            var problems = new List<string>();

            // P2-1: the parser's contract is JsonException.
            CheckThrows<JsonException>(problems, "\\uZZZZ escape",
                delegate { Json.Parse("\"\\uZZZZ\""); });
            CheckThrows<JsonException>(problems, "1.2.3",
                delegate { Json.Parse("1.2.3"); });
            CheckThrows<JsonException>(problems, "1e",
                delegate { Json.Parse("1e"); });
            // A megabyte of '[' would push the recursive parser into an
            // uncatchable StackOverflowException without the depth cap.
            CheckThrows<JsonException>(problems, "deep nesting",
                delegate { Json.Parse(new string('[', 1000) + new string(']', 1000)); });

            // P5-2: the number grammar is JSON's, not double.TryParse's. Python's
            // json rejects these tokens, so a peer must not be accepted here.
            CheckThrows<JsonException>(problems, "+2",
                delegate { Json.Parse("+2"); });
            CheckThrows<JsonException>(problems, "01",
                delegate { Json.Parse("01"); });
            CheckThrows<JsonException>(problems, "1.",
                delegate { Json.Parse("1."); });
            CheckThrows<JsonException>(problems, ".5",
                delegate { Json.Parse(".5"); });
            // A syntactically valid number beyond double is Python's float('inf')
            // (json.loads accepts it), not a syntax error.
            object huge = Json.Parse("1e400");
            if (!(huge is double) || !double.IsPositiveInfinity((double)huge))
                problems.Add("1e400 did not parse as an infinity");
            object tiny = Json.Parse("-1e400");
            if (!(tiny is double) || !double.IsNegativeInfinity((double)tiny))
                problems.Add("-1e400 did not parse as a negative infinity");

            // A header that is not valid JSON decodes as a bad_frame.
            CheckFrameProblem(problems, "truncated header json",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2"), "bad_frame");
            CheckFrameProblem(problems, "deeply nested header",
                FrameWithHeader(new string('[', 1000) + new string(']', 1000)), "bad_frame");

            // P2-3: unknown dtype and a missing name are bad_frames.
            CheckFrameProblem(problems, "unknown dtype",
                FrameCodec.Encode(PingHeader(),
                    new List<Section> { new Section("mesh.verts", "f64", new byte[4]) }),
                "bad_frame");
            CheckFrameProblem(problems, "missing name",
                FrameCodec.Encode(PingHeader(),
                    new List<Section> { new Section("", "f32", new byte[4]) }),
                "bad_frame");

            // The section table's field types are part of the contract: a
            // number where a name belongs used to throw InvalidCastException
            // out of Json.GetString, and a string where a length belongs a
            // FormatException out of Json.GetLong (P2-1's remaining inputs).
            CheckFrameProblem(problems, "numeric section name",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":7,\"dtype\":\"f32\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "numeric dtype",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":9,\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "string section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":\"4\"}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "negative section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":-4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "astronomical section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":9223372036854775807}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "sections is not an array",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":{\"name\":\"x\"}}"),
                "bad_frame");
            CheckFrameProblem(problems, "section entry is not an object",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[4]}"),
                "bad_frame");
            CheckFrameProblem(problems, "trailing bytes after a section",
                FrameWithPayload("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"u8\",\"bytes\":1}]}", "xy"),
                "bad_frame");

            // The size caps are too_large, the code protocol §4.6 reserves for
            // "message exceeds the size limit"; answerable frames are the ones
            // the receive path sends an error for (P5-4).
            byte[] overCap = new byte[4];
            long cap = FrameCodec.MaxHeaderBytes + 1;
            overCap[0] = (byte)(cap & 0xFF);
            overCap[1] = (byte)((cap >> 8) & 0xFF);
            overCap[2] = (byte)((cap >> 16) & 0xFF);
            overCap[3] = (byte)((cap >> 24) & 0xFF);
            CheckFrameProblem(problems, "header over the cap", overCap, "too_large");

            // v: exactly Python's check_version semantics — a JSON number equal
            // to 2 is accepted (2 and 2.0), everything else is a
            // proto_mismatch: a string, a bool, a list, null, a missing key, a
            // different number. 1e400 overflows to an infinity, as Python's
            // float() reads it, and mismatches rather than failing to parse.
            CheckVersionAccepted(problems, "v=2", "{\"type\":\"ping\",\"v\":2}");
            CheckVersionAccepted(problems, "v=2.0", "{\"type\":\"ping\",\"v\":2.0}");
            CheckFrameProblem(problems, "v=\"2\"",
                FrameWithHeader("{\"type\":\"ping\",\"v\":\"2\"}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=true",
                FrameWithHeader("{\"type\":\"ping\",\"v\":true}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=[2]",
                FrameWithHeader("{\"type\":\"ping\",\"v\":[2]}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=null",
                FrameWithHeader("{\"type\":\"ping\",\"v\":null}"), "proto_mismatch");
            CheckFrameProblem(problems, "v missing",
                FrameWithHeader("{\"type\":\"ping\"}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=3",
                FrameWithHeader("{\"type\":\"ping\",\"v\":3}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=2.5",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2.5}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=2.0000000000000004",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2.0000000000000004}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=1e400",
                FrameWithHeader("{\"type\":\"ping\",\"v\":1e400}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=+2 (not JSON)",
                FrameWithHeader("{\"type\":\"ping\",\"v\":+2}"), "bad_frame");

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS decoder-boundary");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL decoder-boundary");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        /// <summary>A header that must decode, as the version boundary's accept side.</summary>
        private static void CheckVersionAccepted(List<string> problems, string what, string header)
        {
            try
            {
                FrameCodec.Decode(FrameWithHeader(header));
            }
            catch (Exception ex)
            {
                problems.Add(what + ": refused (" + ex.GetType().Name + ": " + ex.Message + ")");
            }
        }

        /// <summary>A hand-built frame around raw header text and raw payload
        /// bytes, for shapes the encoder refuses to produce.</summary>
        private static byte[] FrameWithPayload(string text, string payload)
        {
            byte[] head = Encoding.UTF8.GetBytes(text);
            byte[] body = Encoding.UTF8.GetBytes(payload);
            var frame = new byte[4 + head.Length + body.Length];
            frame[0] = (byte)(head.Length & 0xFF);
            frame[1] = (byte)((head.Length >> 8) & 0xFF);
            frame[2] = (byte)((head.Length >> 16) & 0xFF);
            frame[3] = (byte)((head.Length >> 24) & 0xFF);
            Buffer.BlockCopy(head, 0, frame, 4, head.Length);
            Buffer.BlockCopy(body, 0, frame, 4 + head.Length, body.Length);
            return frame;
        }

        /// <summary>FormatDouble's boundary (P5-3): byte-identical to Python's
        /// repr for every value whose shortest form needs at most 15 significant
        /// digits — the protocol's practical range (counts, short decimals,
        /// exponents). A 16-17-digit value round-trips but can carry one digit
        /// more than Python (Mono Math.PI: 3.1415926535897931, Python:
        /// 3.141592653589793), so only the round trip is pinned there.</summary>
        private static void CheckFloatText()
        {
            var problems = new List<string>();
            CheckFloatText(problems, 1.0, "1.0");
            CheckFloatText(problems, -2.0, "-2.0");
            CheckFloatText(problems, 0.0, "0.0");
            CheckFloatText(problems, 2.5, "2.5");
            CheckFloatText(problems, 0.1, "0.1");
            CheckFloatText(problems, 1e21, "1e+21");
            CheckFloatText(problems, 1.5e-7, "1.5e-07");
            CheckFloatText(problems, 1e-7, "1e-07");

            double pi = Math.PI;
            double back;
            string text = Json.FormatDouble(pi);
            if (!double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out back)
                || back != pi)
                problems.Add("Math.PI does not round-trip: " + text);
            if (text.IndexOf('E') >= 0)
                problems.Add("uppercase exponent survived: " + text);

            CheckThrows<JsonException>(problems, "NaN text",
                delegate { Json.FormatDouble(double.NaN); });
            CheckThrows<JsonException>(problems, "infinity text",
                delegate { Json.FormatDouble(double.PositiveInfinity); });

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS float-text");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL float-text");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        private static void CheckFloatText(List<string> problems, double value, string expected)
        {
            string got = Json.FormatDouble(value);
            if (got != expected)
                problems.Add(value + " -> " + got + " != " + expected);
        }

        private static void CheckJsonWriter()
        {            var problems = new List<string>();

            // A typed list encodes exactly like List<object>. The writer used to
            // reject anything but List<object>, which crashed a live canvas the
            // first time it met the attribute descriptor list
            // ("Stage Out: cannot encode List`1").
            var descriptors = new List<Dictionary<string, object>>();
            var entry = new Dictionary<string, object>();
            entry["name"] = "test";
            entry["count"] = 2L;
            descriptors.Add(entry);
            string text = Json.WriteCanonical(descriptors);
            if (text != "[{\"count\":2,\"name\":\"test\"}]")
                problems.Add("typed list: " + text);

            var header = new Dictionary<string, object>();
            header["attributes"] = descriptors;
            string headerText = Json.WriteCanonical(header);
            if (headerText != "{\"attributes\":[{\"count\":2,\"name\":\"test\"}]}")
                problems.Add("typed list in a header: " + headerText);

            var strings = new List<string>();
            strings.Add("a");
            strings.Add("b");
            if (Json.WriteCanonical(strings) != "[\"a\",\"b\"]")
                problems.Add("string list: " + Json.WriteCanonical(strings));

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS json-writer");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL json-writer");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        private static Dictionary<string, object> PingHeader()
        {
            var header = new Dictionary<string, object>();
            header["type"] = "ping";
            return header;
        }

        /// <summary>A frame around raw header text, so a case can hand Decode
        /// something the encoder would not produce.</summary>
        private static byte[] FrameWithHeader(string text)
        {
            byte[] head = Encoding.UTF8.GetBytes(text);
            var frame = new byte[4 + head.Length];
            frame[0] = (byte)(head.Length & 0xFF);
            frame[1] = (byte)((head.Length >> 8) & 0xFF);
            frame[2] = (byte)((head.Length >> 16) & 0xFF);
            frame[3] = (byte)((head.Length >> 24) & 0xFF);
            Buffer.BlockCopy(head, 0, frame, 4, head.Length);
            return frame;
        }

        private static void CheckThrows<T>(List<string> problems, string what, Action action)
            where T : Exception
        {
            try
            {
                action();
                problems.Add(what + ": no exception");
            }
            catch (T)
            {
                // expected
            }
            catch (Exception ex)
            {
                problems.Add(what + ": threw " + ex.GetType().Name
                    + " instead of " + typeof(T).Name);
            }
        }

        private static void CheckFrameProblem(List<string> problems, string what, byte[] frame, string code)
        {
            try
            {
                FrameCodec.Decode(frame);
                problems.Add(what + ": decoded without error");
            }
            catch (FrameException ex)
            {
                if (ex.Code != code)
                    problems.Add(what + ": code " + ex.Code + " != " + code);
            }
            catch (Exception ex)
            {
                problems.Add(what + ": threw " + ex.GetType().Name + ": " + ex.Message);
            }
        }

        private static void Report(string name, List<string> problems)
        {
            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS " + name);
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL " + name);
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        private static bool BytesEqual(byte[] a, byte[] b)
        {
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++) if (a[i] != b[i]) return false;
            return true;
        }
    }
}
