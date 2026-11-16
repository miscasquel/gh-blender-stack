// Layer C, codec half: the C# codec against the golden fixtures.
//
//     StageloomCoreTests.exe <fixtures root>
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
using Stageloom.Core;

namespace Stageloom.Tests.Console
{
    public static class Program
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            if (args.Length < 1)
            {
                System.Console.WriteLine("usage: StageloomCoreTests.exe <fixtures root>");
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

            CheckHashOrder();

            CheckDecoderBoundary();

            CheckFloatText();

            CheckNumberCorpus(root);

            CheckJsonWriter();

            CheckEndpoint();

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

            // The multi-part fixtures pin the grammar the hash order depends on:
            // the explicit `parts`, the part namespaces, the attribute's part
            // and the definition-level nested row pair.
            if (name == "request-instances-multipart" || name == "response-instances-multipart")
            {
                var definitions = Json.Get(decoded.Header, "definitions") as List<object>;
                if (definitions == null || definitions.Count != 2)
                {
                    problems.Add("definitions table missing or wrong length");
                }
                else
                {
                    var first = definitions[0] as Dictionary<string, object>;
                    var second = definitions[1] as Dictionary<string, object>;
                    if (first == null || !(Json.Get(first, "parts") is long)
                        || (long)Json.Get(first, "parts") != 2)
                        problems.Add("definition 0 does not declare parts=2");
                    if (second == null || second.ContainsKey("parts"))
                        problems.Add("definition 1 should use the single-part namespace");
                }
                if (decoded.Find("idef.0.0.mesh.verts") == null
                    || decoded.Find("idef.0.1.mesh.verts") == null)
                    problems.Add("part geometry sections are missing");
                if (decoded.Find("idef.0.inst.ref_def") == null)
                    problems.Add("definition-level nested row is missing");
                var attributes = Json.Get(decoded.Header, "attributes") as List<object>;
                var descriptor = attributes != null && attributes.Count == 1
                    ? attributes[0] as Dictionary<string, object> : null;
                if (descriptor == null
                    || !(Json.Get(descriptor, "definition") is long)
                    || (long)Json.Get(descriptor, "definition") != 0
                    || !(Json.Get(descriptor, "part") is long)
                    || (long)Json.Get(descriptor, "part") !=
                       (name == "request-instances-multipart" ? 1L : 0L))
                    problems.Add("part attribute descriptor is wrong");
            }

            Report(name, problems);

            // Exercise the typed accessors on a couple of fixtures, so a broken
            // conversion path cannot hide behind the raw-byte checks. Guarded:
            // the fixture is regenerated from time to time and a missing
            // section must report, not dereference null.
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
            if (ParamValues.AsDoubles(Json.Parse("[\"2.0\", \"2.0\", \"2.0\"]")) != null)
                problems.Add("AsDoubles accepted strings");
            if (ParamValues.AsDoubles(Json.Parse("[true, false, true]")) != null)
                problems.Add("AsDoubles accepted bools");
            if (ParamValues.AsDoubles(Json.Parse("[1, null, 3]")) != null)
                problems.Add("AsDoubles accepted null");
            if (ParamValues.AsDoubles(new double[] { 1.0, double.NaN, 3.0 }) != null)
                problems.Add("AsDoubles accepted a non-finite array");
            double[] integerArray = ParamValues.AsDoubles(Json.Parse("[1, 2.5, -3]"));
            if (integerArray == null || integerArray[0] != 1.0
                || integerArray[1] != 2.5 || integerArray[2] != -3.0)
                problems.Add("AsDoubles rejected a mixed integer/float array");

            object cast = ParamValues.Cast("vector3", parsed);
            var castArray = cast as double[];
            if (castArray == null || castArray[2] != 2.0)
                problems.Add("Cast(vector3) lost the parsed array");
            object fallback = ParamValues.Cast("vector3", 1.04);
            var fallbackArray = fallback as double[];
            if (fallbackArray == null || fallbackArray[0] != 0.0)
                problems.Add("Cast(vector3) did not fall back to zeros");
            var nonFiniteVector = ParamValues.Cast("vector3",
                new double[] { 1.0, double.NaN, 3.0 }) as double[];
            if (nonFiniteVector == null || nonFiniteVector[0] != 0.0
                || nonFiniteVector[1] != 0.0 || nonFiniteVector[2] != 0.0)
                problems.Add("Cast(vector3, non-finite) was not neutral");
            var nonFiniteColor = ParamValues.Cast("color",
                new double[] { 1.0, 0.0, double.PositiveInfinity }) as double[];
            if (nonFiniteColor == null || nonFiniteColor[0] != 0.0
                || nonFiniteColor[1] != 0.0 || nonFiniteColor[2] != 0.0)
                problems.Add("Cast(color, non-finite) was not neutral");
            var stringVector = ParamValues.Cast("vector3",
                Json.Parse("[\"1\", \"2\", \"3\"]")) as double[];
            if (stringVector == null || stringVector[0] != 0.0)
                problems.Add("Cast(vector3, string array) was not neutral");

            object def = ParamValues.ParseDefault("vector3", "1, 2.5, 3");
            var defArray = def as double[];
            if (defArray == null || defArray[1] != 2.5)
                problems.Add("ParseDefault(vector3) failed");
            if ((string)ParamValues.ParseDefault("text", null) != "")
                problems.Add("ParseDefault(text, null) failed");
            if (!(bool)ParamValues.ParseDefault("bool", "true"))
                problems.Add("ParseDefault(bool) failed");
            if ((double)ParamValues.ParseDefault("float", "2.5") != 2.5
                || (long)ParamValues.ParseDefault("value_list", "2.9") != 2L)
                problems.Add("ParseDefault lost a valid default");

            // A default that parses to NaN/Infinity, overflows to infinity or
            // does not fit the target is the type's neutral value. Otherwise
            // FormatDouble would throw while publishing the schema.
            if ((double)ParamValues.ParseDefault("float", "NaN") != 0.0
                || (double)ParamValues.ParseDefault("float", "Infinity") != 0.0
                || (double)ParamValues.ParseDefault("float", "-Infinity") != 0.0
                || (double)ParamValues.ParseDefault("float", "1e400") != 0.0)
                problems.Add("ParseDefault(float) accepted a non-finite value");
            if ((long)ParamValues.ParseDefault("int", "NaN") != 0L
                || (long)ParamValues.ParseDefault("int", "Infinity") != 0L
                || (long)ParamValues.ParseDefault("int", "1e300") != 0L)
                problems.Add("ParseDefault(int) accepted a non-finite/overflowing value");
            if ((long)ParamValues.ParseDefault("value_list", "-Infinity") != 0L
                || (long)ParamValues.ParseDefault("value_list", "9223372036854775808") != 0L)
                problems.Add("ParseDefault(value_list) accepted an out-of-range value");
            var badVectorDefault = ParamValues.ParseDefault("vector3", "1e400, 2, 3") as double[];
            if (badVectorDefault == null || badVectorDefault[0] != 0.0
                || badVectorDefault[1] != 0.0 || badVectorDefault[2] != 0.0)
                problems.Add("ParseDefault(vector3) accepted a non-finite component");

            // Cast must be total: a value left from another type (or a racing
            // request) never throws into SolveInstance.
            if (!(ParamValues.Cast("float", "[0.0, 0.0, 0.0]") is double))
                problems.Add("Cast(float, vector string) did not fall back to a double");
            if ((double)ParamValues.Cast("float", "[0.0, 0.0, 0.0]") != 0.0)
                problems.Add("Cast(float, vector string) was not neutral");
            if ((double)ParamValues.Cast("float", "2.5") != 2.5)
                problems.Add("Cast(float, numeric string) lost a valid value");
            if ((string)ParamValues.Cast("text", parsed) != "")
                problems.Add("Cast(text, list) was not empty");
            var junk = ParamValues.Cast("vector3", "junk") as double[];
            if (junk == null || junk[0] != 0.0)
                problems.Add("Cast(vector3, junk) was not neutral");
            if ((int)ParamValues.Cast("value_list", "nonsense") != 0)
                problems.Add("Cast(value_list, nonsense) was not neutral");
            if ((bool)ParamValues.Cast("bool", "not a bool") != false)
                problems.Add("Cast(bool, string) was not neutral");

            // Non-finite input never survives the float/vector path: JSON
            // cannot carry it and FormatDouble would throw later.
            if ((double)ParamValues.Cast("float", double.NaN) != 0.0
                || (double)ParamValues.Cast("float", double.PositiveInfinity) != 0.0
                || (double)ParamValues.Cast("float", double.NegativeInfinity) != 0.0
                || (double)ParamValues.Cast("float", "NaN") != 0.0
                || (double)ParamValues.Cast("float", "Infinity") != 0.0)
                problems.Add("Cast(float) accepted a non-finite value");
            if ((int)ParamValues.Cast("int", double.NaN) != 0
                || (int)ParamValues.Cast("value_list", double.PositiveInfinity) != 0)
                problems.Add("Cast(int/value_list) accepted a non-finite value");
            if ((int)ParamValues.Cast("int", long.MaxValue) != 0)
                problems.Add("Cast(int, 64-bit overflow) was not neutral");

            // Text follows Python's str(): 2.0 keeps its ".0", ints stay
            // integral, bools are capitalised, and non-finite floats use
            // Python's lowercase spellings.
            if ((string)ParamValues.Cast("text", 2.0) != "2.0")
                problems.Add("Cast(text, 2.0) was not Python repr");
            if ((string)ParamValues.Cast("text", 1e15) != "1000000000000000.0"
                || (string)ParamValues.Cast("text", 1e16) != "1e+16"
                || (string)ParamValues.Cast("text", -0.0) != "-0.0")
                problems.Add("Cast(text, float) did not use Python repr");
            if ((string)ParamValues.Cast("text", 2L) != "2"
                || (string)ParamValues.Cast("text", 2) != "2")
                problems.Add("Cast(text, int) was not integral text");
            if ((string)ParamValues.Cast("text", true) != "True"
                || (string)ParamValues.Cast("text", false) != "False")
                problems.Add("Cast(text, bool) was not Python str");
            if ((string)ParamValues.Cast("text", double.NaN) != "nan"
                || (string)ParamValues.Cast("text", double.PositiveInfinity) != "inf"
                || (string)ParamValues.Cast("text", double.NegativeInfinity) != "-inf")
                problems.Add("Cast(text, non-finite) was not Python str");

            // The two sides' int rules read from params.py: `int` is
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

            // JsonValue is the last guard before canonical JSON: a non-finite
            // number becomes the neutral 0.0 rather than throwing in
            // FormatDouble while the schema is published.
            object nanJson = ParamValues.JsonValue(double.NaN);
            if (!(nanJson is double) || (double)nanJson != 0.0)
                problems.Add("JsonValue(NaN) was not the neutral double");
            object infJson = ParamValues.JsonValue(double.PositiveInfinity);
            if (!(infJson is double) || (double)infJson != 0.0)
                problems.Add("JsonValue(Infinity) was not the neutral double");
            var guardedVector = ParamValues.JsonValue(
                new double[] { 1.0, double.NaN, double.PositiveInfinity }) as List<object>;
            if (guardedVector == null || guardedVector.Count != 3
                || (double)guardedVector[0] != 1.0
                || (double)guardedVector[1] != 0.0
                || (double)guardedVector[2] != 0.0)
                problems.Add("JsonValue(double[]) did not neutralise non-finite elements");
            if (Json.WriteCanonical(guardedVector) != "[1.0,0.0,0.0]")
                problems.Add("guarded JsonValue did not encode canonically");
            var withNaN = new List<object>();
            withNaN.Add(double.NegativeInfinity);
            withNaN.Add("x");
            var guardedList = ParamValues.JsonValue(withNaN) as List<object>;
            if (guardedList == null || guardedList.Count != 2
                || (double)guardedList[0] != 0.0 || (string)guardedList[1] != "x")
                problems.Add("JsonValue(List<object>) did not neutralise non-finite elements");

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS param-values");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL param-values");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        /// <summary>Section-name hash order, including multi-part definitions
        /// (protocol §6.1). Mirrors the Python core tests: numeric definition
        /// and part order, single-part order unchanged, and definition-level
        /// nested rows after all parts.</summary>
        private static void CheckHashOrder()
        {
            var problems = new List<string>();

            CheckNames(problems, "single-part definition order unchanged",
                new string[]
                {
                    "idef.0.mesh.verts", "idef.0.mesh.face_counts",
                    "idef.0.inst.transforms", "idef.0.attr.0.values",
                },
                FrameCodec.OrderedNames(new string[]
                {
                    "idef.0.inst.transforms", "idef.0.mesh.face_counts",
                    "idef.0.mesh.verts", "idef.0.attr.0.values",
                }));
            CheckNames(problems, "definition index is numeric",
                new string[]
                {
                    "mesh.verts", "idef.2.mesh.verts", "idef.10.mesh.verts",
                },
                FrameCodec.OrderedNames(new string[]
                {
                    "idef.10.mesh.verts", "idef.2.mesh.verts", "mesh.verts",
                }));
            CheckNames(problems, "part index is numeric",
                new string[]
                {
                    "idef.0.2.mesh.verts", "idef.0.2.mesh.face_counts",
                    "idef.0.2.crv.points", "idef.0.10.mesh.verts",
                    "idef.0.10.crv.points", "idef.0.inst.ref_def",
                    "idef.0.inst.transforms",
                },
                FrameCodec.OrderedNames(new string[]
                {
                    "idef.0.10.mesh.verts", "idef.0.2.crv.points",
                    "idef.0.2.mesh.face_counts", "idef.0.2.mesh.verts",
                    "idef.0.inst.ref_def", "idef.0.inst.transforms",
                    "idef.0.10.crv.points",
                }));
            CheckNames(problems, "definition before part",
                new string[]
                {
                    "idef.2.0.mesh.verts", "idef.2.1.mesh.verts",
                    "idef.10.0.mesh.verts",
                },
                FrameCodec.OrderedNames(new string[]
                {
                    "idef.10.0.mesh.verts", "idef.2.1.mesh.verts",
                    "idef.2.0.mesh.verts",
                }));

            string a = FrameCodec.GeometryHash(new Section[]
            {
                new Section("idef.0.10.mesh.verts", "f32", new byte[] { 1, 2 }),
                new Section("idef.0.2.mesh.verts", "f32", new byte[] { 3, 4 }),
            });
            string b = FrameCodec.GeometryHash(new Section[]
            {
                new Section("idef.0.2.mesh.verts", "f32", new byte[] { 3, 4 }),
                new Section("idef.0.10.mesh.verts", "f32", new byte[] { 1, 2 }),
            });
            if (a != b) problems.Add("multipart geometry hash depends on section order");

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS hash-order");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL hash-order");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        private static void CheckNames(List<string> problems, string label,
                                       string[] expected, List<string> got)
        {
            if (got.Count != expected.Length)
            {
                problems.Add(label + ": got " + got.Count + " names, expected " + expected.Length
                             + " (" + string.Join(",", got.ToArray()) + ")");
                return;
            }
            for (int i = 0; i < expected.Length; i++)
            {
                if (got[i] == expected[i]) continue;
                problems.Add(label + ": name " + i + " is " + got[i] + ", expected " + expected[i]);
                return;
            }
        }

        /// <summary>Decoder boundary: malformed input must fail as a classified
        /// exception, never as a raw one a caller cannot catch. The frame cases
        /// mirror blender/tests/core/test_codec_boundaries.py, and every frame
        /// rejection is checked for its FrameException.Code.</summary>
        private static void CheckDecoderBoundary()
        {
            var problems = new List<string>();

            // The parser's contract is JsonException.
            CheckThrows<JsonException>(problems, "null input",
                delegate { Json.Parse(null); });
            CheckThrows<JsonException>(problems, "\\uZZZZ escape",
                delegate { Json.Parse("\"\\uZZZZ\""); });
            CheckThrows<JsonException>(problems, "\\u12 short escape",
                delegate { Json.Parse("\"\\u12\""); });
            CheckThrows<JsonException>(problems, "\\u12G4 escape",
                delegate { Json.Parse("\"\\u12G4\""); });
            CheckThrows<JsonException>(problems, "\\u whitespace",
                delegate { Json.Parse("\"\\u 123\""); });
            CheckThrows<JsonException>(problems, "1.2.3",
                delegate { Json.Parse("1.2.3"); });
            CheckThrows<JsonException>(problems, "1e",
                delegate { Json.Parse("1e"); });
            // A megabyte of '[' would push the recursive parser into an
            // uncatchable StackOverflowException without the depth cap.
            CheckThrows<JsonException>(problems, "deep nesting",
                delegate { Json.Parse(new string('[', 1000) + new string(']', 1000)); });

            // The number grammar is JSON's, not double.TryParse's.
            CheckThrows<JsonException>(problems, "+2",
                delegate { Json.Parse("+2"); });
            CheckThrows<JsonException>(problems, "01",
                delegate { Json.Parse("01"); });
            CheckThrows<JsonException>(problems, "1.",
                delegate { Json.Parse("1."); });
            CheckThrows<JsonException>(problems, ".5",
                delegate { Json.Parse(".5"); });

            // Integers are signed 64-bit, as on the Python side. A token one
            // past either bound is a bad_frame, never a silently rounded
            // double; the bounds themselves decode as long.
            object maxLong = Json.Parse("9223372036854775807");
            object minLong = Json.Parse("-9223372036854775808");
            if (!(maxLong is long) || (long)maxLong != long.MaxValue)
                problems.Add("long.MaxValue did not decode as a long");
            if (!(minLong is long) || (long)minLong != long.MinValue)
                problems.Add("long.MinValue did not decode as a long");
            CheckThrows<JsonException>(problems, "integer above signed 64-bit",
                delegate { Json.Parse("9223372036854775808"); });
            CheckThrows<JsonException>(problems, "integer below signed 64-bit",
                delegate { Json.Parse("-9223372036854775809"); });
            CheckThrows<JsonException>(problems, "astronomical integer",
                delegate { Json.Parse("1000000000000000000000000000000"); });

            // Duplicate keys are rejected at every nesting level, not last-wins.
            CheckThrows<JsonException>(problems, "duplicate top-level key",
                delegate { Json.Parse("{\"a\":1,\"a\":2}"); });
            CheckThrows<JsonException>(problems, "duplicate nested key",
                delegate { Json.Parse("{\"outer\":{\"a\":1,\"a\":2}}"); });
            CheckThrows<JsonException>(problems, "duplicate key inside an array object",
                delegate { Json.Parse("[{\"a\":1,\"a\":2}]"); });

            // Raw control characters are not JSON; their escapes still are.
            CheckThrows<JsonException>(problems, "raw newline in string",
                delegate { Json.Parse("\"line\nbreak\""); });
            CheckThrows<JsonException>(problems, "raw tab in string",
                delegate { Json.Parse("\"tab\there\""); });
            string escaped = (string)Json.Parse("\"caf\\u00e9\\u0000\"");
            if (escaped != "caf" + (char)0x00E9 + (char)0)
                problems.Add("valid \\u escapes did not decode");
            string pair = (string)Json.Parse("\"\\ud83d\\ude00\"");
            if (pair != char.ConvertFromUtf32(0x1F600))
                problems.Add("a valid surrogate pair did not decode");

            // Python's decoder accepts escaped lone surrogates; strict UTF-8
            // cannot carry them, so both sides reject them after parsing.
            CheckThrows<JsonException>(problems, "lone high surrogate",
                delegate { Json.Parse("\"\\ud800\""); });
            CheckThrows<JsonException>(problems, "lone low surrogate",
                delegate { Json.Parse("\"\\udfff\""); });
            CheckThrows<JsonException>(problems, "lone surrogate key",
                delegate { Json.Parse("{\"\\ud800\":1}"); });
            CheckThrows<JsonException>(problems, "lone surrogate nested in an array",
                delegate { Json.Parse("{\"items\":[\"ok\",\"\\udfff\"]}"); });

            // Non-finite values are not JSON and Q6 rejects their JSON extension
            // spellings; a float that overflows to infinity is refused too.
            CheckThrows<JsonException>(problems, "NaN literal",
                delegate { Json.Parse("NaN"); });
            CheckThrows<JsonException>(problems, "Infinity literal",
                delegate { Json.Parse("Infinity"); });
            CheckThrows<JsonException>(problems, "-Infinity literal",
                delegate { Json.Parse("-Infinity"); });
            CheckThrows<JsonException>(problems, "1e400 overflow",
                delegate { Json.Parse("1e400"); });
            CheckThrows<JsonException>(problems, "-1e400 overflow",
                delegate { Json.Parse("-1e400"); });
            CheckThrows<JsonException>(problems, "nested 1e400 overflow",
                delegate { Json.Parse("{\"a\":[1e400]}"); });

            // A header that is not valid JSON decodes as a bad_frame.
            CheckFrameProblem(problems, "truncated header json",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2"), "bad_frame");
            CheckFrameProblem(problems, "deeply nested header",
                FrameWithHeader(new string('[', 1000) + new string(']', 1000)), "bad_frame");
            CheckFrameProblem(problems, "header is not an object",
                FrameWithHeader("[]"), "bad_frame");

            // The JSON boundary applies to the header as a whole, including
            // duplicate keys, controls, escapes and surrogates.
            CheckFrameProblem(problems, "duplicate top-level json key",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"type\":\"pong\"}"),
                "bad_frame");
            CheckFrameProblem(problems, "duplicate nested json key",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"outer\":{\"a\":1,\"a\":2}}"),
                "bad_frame");
            CheckFrameProblem(problems, "raw control character in header",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"note\":\"line\nbreak\"}"),
                "bad_frame");
            CheckFrameProblem(problems, "bad unicode escape in header",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"note\":\"\\u12G4\"}"),
                "bad_frame");
            CheckFrameProblem(problems, "lone surrogate value in header",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"note\":\"\\ud800\"}"),
                "bad_frame");
            CheckFrameProblem(problems, "lone surrogate key in header",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"\\ud800\":1}"),
                "bad_frame");
            CheckFrameProblem(problems, "lone surrogate nested in header",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"items\":[\"ok\",\"\\udfff\"]}"),
                "bad_frame");
            CheckFrameProblem(problems, "invalid UTF-8 header",
                FrameWithRawHeader(InvalidUtf8Header()), "bad_frame");
            CheckFrameProblem(problems, "NaN header value",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"number\":NaN}"), "bad_frame");
            CheckFrameProblem(problems, "Infinity header value",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"number\":Infinity}"), "bad_frame");
            CheckFrameProblem(problems, "overflowing float header value",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"number\":1e400}"), "bad_frame");

            // A valid escape and finite number still decode; this is the accept
            // side of the tightening above.
            DecodedFrame escapedFrame = CheckFrameAccepted(problems,
                "valid escapes and finite numbers",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"note\":\"caf\\u00e9\",\"number\":1e308}"));
            if (escapedFrame != null)
            {
                if (Json.GetString(escapedFrame.Header, "note") != "caf" + (char)0x00E9)
                    problems.Add("valid escape did not reach the header");
                object number = Json.Get(escapedFrame.Header, "number");
                if (!(number is double) || (double)number <= 1e307)
                    problems.Add("finite 1e308 did not decode as a double");
            }
            DecodedFrame boundsFrame = CheckFrameAccepted(problems,
                "signed 64-bit integer bounds",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"low\":-9223372036854775808,\"high\":9223372036854775807}"));
            if (boundsFrame != null)
            {
                if (!(Json.Get(boundsFrame.Header, "low") is long)
                    || (long)Json.Get(boundsFrame.Header, "low") != long.MinValue)
                    problems.Add("long.MinValue did not reach the header as a long");
                if (!(Json.Get(boundsFrame.Header, "high") is long)
                    || (long)Json.Get(boundsFrame.Header, "high") != long.MaxValue)
                    problems.Add("long.MaxValue did not reach the header as a long");
            }
            CheckFrameProblem(problems, "integer above signed 64-bit in header",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"number\":9223372036854775808}"),
                "bad_frame");
            CheckFrameProblem(problems, "integer below signed 64-bit in header",
                FrameWithHeader("{\"type\":\"x\",\"v\":2,\"number\":-9223372036854775809}"),
                "bad_frame");

            // The section table's field types are part of the contract: a
            // number where a name belongs, or a string where a length belongs,
            // is a bad_frame.
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
            CheckFrameProblem(problems, "beyond-long section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":1000000000000000000000000000000}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "header sections is null",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":null}"),
                "bad_frame");

            string[] notArrays = new string[] { "{}", "\"mesh.verts\"", "null", "7", "true" };
            foreach (string value in notArrays)
                CheckFrameProblem(problems, "sections is " + value,
                    FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":" + value + "}"),
                    "bad_frame");

            string[] badEntries = new string[] { "42", "\"mesh.verts\"", "[]", "null", "true" };
            foreach (string entry in badEntries)
                CheckFrameProblem(problems, "section entry is " + entry,
                    FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[" + entry + "]}"),
                    "bad_frame");

            // Name and dtype: missing, null, empty and the wrong JSON type are
            // all bad_frames before any payload is read.
            CheckFrameProblem(problems, "missing section name",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"dtype\":\"f32\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "empty section name",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"\",\"dtype\":\"f32\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "null section name",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":null,\"dtype\":\"f32\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "array section name",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":[],\"dtype\":\"f32\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "missing section dtype",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "null section dtype",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":null,\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "empty section dtype",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "unknown section dtype",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f64\",\"bytes\":4}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "float32 section dtype",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"float32\",\"bytes\":4}]}"),
                "bad_frame");

            // bytes must be a JSON integer, not a float, bool, string or null.
            CheckFrameProblem(problems, "missing section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\"}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "null section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":null}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "float section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":4.0}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "bool section bytes",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":true}]}"),
                "bad_frame");

            CheckFrameProblem(problems, "duplicate section names",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":["
                    + "{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":4},"
                    + "{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"bytes\":4}]}"),
                "bad_frame");

            // shape, when present, is an array of non-negative integers whose
            // product is bytes/itemsize.
            string[] badShapes = new string[] { "\"4\"", "4", "null", "{}" };
            foreach (string shape in badShapes)
                CheckFrameProblem(problems, "shape is " + shape,
                    FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"shape\":"
                        + shape + ",\"bytes\":4}]}"),
                    "bad_frame");

            string[] badShapeValues = new string[] { "[true]", "[-1]", "[1.0]", "[\"1\"]", "[[1]]", "[null]" };
            foreach (string shape in badShapeValues)
                CheckFrameProblem(problems, "shape holds " + shape,
                    FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"f32\",\"shape\":"
                        + shape + ",\"bytes\":4}]}"),
                    "bad_frame");

            string[] badProducts = new string[]
            {
                "{\"name\":\"a\",\"dtype\":\"f32\",\"shape\":[2],\"bytes\":4}",
                "{\"name\":\"a\",\"dtype\":\"f32\",\"shape\":[1],\"bytes\":8}",
                "{\"name\":\"a\",\"dtype\":\"f32\",\"shape\":[2,2],\"bytes\":8}",
                "{\"name\":\"a\",\"dtype\":\"f32\",\"shape\":[],\"bytes\":8}",
            };
            foreach (string entry in badProducts)
                CheckFrameProblem(problems, "shape product mismatch",
                    FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[" + entry + "]}"),
                    "bad_frame");

            DecodedFrame shape22 = CheckFrameAccepted(problems, "shape [2,2] u8",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[2,2],\"bytes\":4}]}",
                    new byte[] { 1, 2, 3, 4 }));
            if (shape22 == null || shape22.Sections.Count != 1 || shape22.Sections[0].Raw.Length != 4)
                problems.Add("shape [2,2] u8 did not decode");
            DecodedFrame shapeEmpty = CheckFrameAccepted(problems, "empty shape u8",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[],\"bytes\":1}]}",
                    new byte[] { 5 }));
            if (shapeEmpty == null || shapeEmpty.Sections.Count != 1 || shapeEmpty.Sections[0].ElementCount != 1)
                problems.Add("empty shape u8 did not decode as one element");
            DecodedFrame shapeZero = CheckFrameAccepted(problems, "zero-length f32",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"f32\",\"shape\":[0],\"bytes\":0}]}"));
            if (shapeZero == null || shapeZero.Sections.Count != 1 || shapeZero.Sections[0].ElementCount != 0)
                problems.Add("zero-length f32 did not decode");
            DecodedFrame shapeOptional = CheckFrameAccepted(problems, "shape omitted",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"f32\",\"bytes\":4}]}",
                    new byte[4]));
            if (shapeOptional == null || shapeOptional.Sections.Count != 1)
                problems.Add("a section without shape did not decode");

            // A zero dimension wins over huge siblings in Python's
            // arbitrary-precision product; the C# checked multiply must not
            // overflow before it sees the zero.
            CheckFrameAccepted(problems, "shape [LONG_MAX,0]",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[9223372036854775807,0],\"bytes\":0}]}"));
            CheckFrameProblem(problems, "shape [LONG_MAX,2] overflows",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[9223372036854775807,2],\"bytes\":0}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "shape [LONG_MAX] mismatches",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[9223372036854775807],\"bytes\":0}]}"),
                "bad_frame");
            CheckFrameProblem(problems, "shape dimension beyond long",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u8\",\"shape\":[1000000000000000000000000000000],\"bytes\":0}]}"),
                "bad_frame");

            // Alignment and payload extent.
            CheckFrameProblem(problems, "misaligned f32",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"f32\",\"bytes\":3}]}",
                    new byte[3]), "bad_frame");
            CheckFrameProblem(problems, "misaligned i32",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"i32\",\"bytes\":6}]}",
                    new byte[6]), "bad_frame");
            CheckFrameProblem(problems, "misaligned u32",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"u32\",\"bytes\":1}]}",
                    new byte[1]), "bad_frame");
            CheckFrameProblem(problems, "section past the payload",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"a\",\"dtype\":\"f32\",\"bytes\":4}]}",
                    new byte[2]), "bad_frame");
            CheckFrameProblem(problems, "trailing bytes after a section",
                FrameWithPayload("{\"type\":\"ping\",\"v\":2,\"sections\":[{\"name\":\"mesh.verts\",\"dtype\":\"u8\",\"bytes\":1}]}", "xy"),
                "bad_frame");
            CheckFrameProblem(problems, "trailing bytes after multiple sections",
                FrameWithBody("{\"type\":\"ping\",\"v\":2,\"sections\":["
                    + "{\"name\":\"a\",\"dtype\":\"u8\",\"bytes\":2},"
                    + "{\"name\":\"b\",\"dtype\":\"u8\",\"bytes\":2}]}",
                    new byte[5]), "bad_frame");
            CheckFrameProblem(problems, "trailing bytes with no sections",
                FrameWithBody("{\"type\":\"ping\",\"v\":2}", new byte[] { 0 }), "bad_frame");

            // An empty table is valid and the body must then be empty.
            DecodedFrame emptyTable = CheckFrameAccepted(problems, "empty sections list",
                FrameWithHeader("{\"type\":\"response\",\"v\":2,\"sections\":[]}"));
            if (emptyTable == null || emptyTable.Sections.Count != 0)
                problems.Add("empty sections list did not decode");

            // The size caps are too_large, the code protocol §4.6 reserves for
            // "message exceeds the size limit"; answerable frames are the ones
            // the receive path sends an error for.
            byte[] overCap = new byte[4];
            long cap = FrameCodec.MaxHeaderBytes + 1;
            overCap[0] = (byte)(cap & 0xFF);
            overCap[1] = (byte)((cap >> 8) & 0xFF);
            overCap[2] = (byte)((cap >> 16) & 0xFF);
            overCap[3] = (byte)((cap >> 24) & 0xFF);
            CheckFrameProblem(problems, "header over the cap", overCap, "too_large");

            // v: exactly Python's check_version semantics — only the JSON
            // integer 2 is accepted. 2.0 and true compare equal in C# but are
            // different JSON values, so they are proto_mismatch like a string,
            // a list, null, a missing key or a different integer. 1e400 is
            // non-finite after parsing, so it is a bad_frame before the version
            // check, exactly as Q6 rejected it.
            CheckVersionAccepted(problems, "v=2", "{\"type\":\"ping\",\"v\":2}");
            CheckFrameProblem(problems, "v=2.0",
                FrameWithHeader("{\"type\":\"ping\",\"v\":2.0}"), "proto_mismatch");
            CheckFrameProblem(problems, "v=1e0",
                FrameWithHeader("{\"type\":\"ping\",\"v\":1e0}"), "proto_mismatch");
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
                FrameWithHeader("{\"type\":\"ping\",\"v\":1e400}"), "bad_frame");
            CheckFrameProblem(problems, "v=+2 (not JSON)",
                FrameWithHeader("{\"type\":\"ping\",\"v\":+2}"), "bad_frame");

            // type must be a non-empty string.
            CheckFrameProblem(problems, "type missing",
                FrameWithHeader("{\"v\":2}"), "bad_frame");
            CheckFrameProblem(problems, "type empty",
                FrameWithHeader("{\"type\":\"\",\"v\":2}"), "bad_frame");
            CheckFrameProblem(problems, "type null",
                FrameWithHeader("{\"type\":null,\"v\":2}"), "bad_frame");
            CheckFrameProblem(problems, "type number",
                FrameWithHeader("{\"type\":5,\"v\":2}"), "bad_frame");
            CheckFrameProblem(problems, "type array",
                FrameWithHeader("{\"type\":[],\"v\":2}"), "bad_frame");
            CheckFrameProblem(problems, "type bool",
                FrameWithHeader("{\"type\":true,\"v\":2}"), "bad_frame");

            // The encoder must refuse what the decoder refuses.
            CheckEncodeProblem(problems, "encode null header", null, null, "bad_frame");

            var noType = new Dictionary<string, object>();
            CheckEncodeProblem(problems, "encode type missing", noType, null, "bad_frame");
            CheckEncodeProblem(problems, "encode type empty", HeaderWith("", null), null, "bad_frame");
            CheckEncodeProblem(problems, "encode type null", HeaderWith(null, null), null, "bad_frame");
            CheckEncodeProblem(problems, "encode type number", HeaderWith(5L, null), null, "bad_frame");
            CheckEncodeProblem(problems, "encode type array", HeaderWith(new List<object>(), null), null, "bad_frame");

            object[] badVersions = new object[] { 1L, 3L, 2.0, true, "2", null };
            foreach (object badVersion in badVersions)
            {
                var badHeader = new Dictionary<string, object>();
                badHeader["type"] = "x";
                badHeader["v"] = badVersion;
                CheckEncodeProblem(problems, "encode v=" + (badVersion ?? "null"),
                    badHeader, null, "proto_mismatch");
            }

            var duplicateSections = new List<Section>();
            duplicateSections.Add(new Section("a", "u8", new byte[] { 1 }));
            duplicateSections.Add(new Section("a", "u8", new byte[] { 2 }));
            CheckEncodeProblem(problems, "encode duplicate section names",
                PingHeader(), duplicateSections, "bad_frame");
            CheckEncodeProblem(problems, "encode unknown dtype",
                PingHeader(), new List<Section> { new Section("a", "f64", new byte[4]) }, "bad_frame");
            CheckEncodeProblem(problems, "encode empty section name",
                PingHeader(), new List<Section> { new Section("", "u8", new byte[1]) }, "bad_frame");
            CheckEncodeProblem(problems, "encode lone surrogate section name",
                PingHeader(), new List<Section> { new Section("a\ud800", "u8", new byte[1]) }, "bad_frame");
            CheckEncodeProblem(problems, "encode null section",
                PingHeader(), new List<Section> { null }, "bad_frame");
            CheckEncodeProblem(problems, "encode null section bytes",
                PingHeader(), new List<Section> { new Section("a", "f32", null) }, "bad_frame");
            CheckEncodeProblem(problems, "encode misaligned section",
                PingHeader(), new List<Section> { new Section("a", "f32", new byte[3]) }, "bad_frame");

            var nanHeader = PingHeader();
            nanHeader["number"] = double.NaN;
            CheckEncodeProblem(problems, "encode NaN header value", nanHeader, null, "bad_frame");
            var infinityHeader = PingHeader();
            infinityHeader["number"] = double.PositiveInfinity;
            CheckEncodeProblem(problems, "encode infinity header value", infinityHeader, null, "bad_frame");
            var surrogateValue = PingHeader();
            surrogateValue["note"] = "\ud800";
            CheckEncodeProblem(problems, "encode lone surrogate header value",
                surrogateValue, null, "bad_frame");
            var surrogateKey = PingHeader();
            surrogateKey["\ud800"] = 1L;
            CheckEncodeProblem(problems, "encode lone surrogate header key",
                surrogateKey, null, "bad_frame");

            // Python validates type and the whole tree before check_version, so
            // a bad type or a non-finite value wins over a bad v.
            CheckEncodeProblem(problems, "encode bad type with bad v",
                HeaderWith("", 3L), null, "bad_frame");
            var nanAndBadVersion = HeaderWith("x", 3L);
            nanAndBadVersion["number"] = double.NaN;
            CheckEncodeProblem(problems, "encode NaN with bad v",
                nanAndBadVersion, null, "bad_frame");

            // A valid surrogate pair is a legal UTF-16 string and must encode.
            var pairHeader = PingHeader();
            pairHeader["note"] = char.ConvertFromUtf32(0x1F600);
            DecodedFrame pairDecoded = CheckFrameAccepted(problems, "encode valid surrogate pair",
                FrameCodec.Encode(pairHeader, null));
            if (pairDecoded != null
                && Json.GetString(pairDecoded.Header, "note") != char.ConvertFromUtf32(0x1F600))
                problems.Add("valid surrogate pair did not round-trip");

            // v defaults to 2; an explicit integer 2 is accepted and a caller's
            // header is not mutated.
            var defaultHeader = PingHeader();
            byte[] defaultFrame = FrameCodec.Encode(defaultHeader, null);
            DecodedFrame defaultDecoded = CheckFrameAccepted(problems, "encode default v", defaultFrame);
            if (defaultDecoded != null && !(Json.Get(defaultDecoded.Header, "v") is long
                                            && (long)Json.Get(defaultDecoded.Header, "v") == 2L))
                problems.Add("encode did not write integer v=2");
            if (defaultHeader.ContainsKey("v") || defaultHeader.ContainsKey("sections"))
                problems.Add("encode mutated the caller header");

            CheckFrameAccepted(problems, "encode explicit long v=2",
                FrameCodec.Encode(HeaderWith("x", 2L), null));
            CheckFrameAccepted(problems, "encode explicit int v=2",
                FrameCodec.Encode(HeaderWith("x", 2), null));

            // Valid sections keep their order and survive decode/re-encode.
            var validSections = new List<Section>();
            validSections.Add(Section.Floats("mesh.verts", new float[] { 0.0f, 1.5f, -2.0f }));
            validSections.Add(Section.U32("mesh.face_counts", new uint[] { 3 }));
            validSections.Add(Section.Ints("attr.0.values", new int[] { -1, 0, 7 }));
            validSections.Add(Section.Bytes("mesh.colors", new byte[] { 1, 2, 3, 4 }));
            var validHeader = PingHeader();
            byte[] validFrame = FrameCodec.Encode(validHeader, validSections);
            DecodedFrame validDecoded = CheckFrameAccepted(problems, "encode multi-section", validFrame);
            if (validDecoded != null)
            {
                string[] expectedNames = new string[] { "mesh.verts", "mesh.face_counts", "attr.0.values", "mesh.colors" };
                if (validDecoded.Sections.Count != expectedNames.Length)
                    problems.Add("multi-section count changed");
                else
                    for (int i = 0; i < expectedNames.Length; i++)
                        if (validDecoded.Sections[i].Name != expectedNames[i])
                            problems.Add("section order changed at " + i);
                byte[] rebuilt = FrameCodec.Encode(
                    new Dictionary<string, object>(validDecoded.Header), validDecoded.Sections);
                if (!BytesEqual(validFrame, rebuilt))
                    problems.Add("decode/re-encode changed valid frame bytes");
            }
            if (validHeader.ContainsKey("v") || validHeader.ContainsKey("sections"))
                problems.Add("encode with sections mutated the caller header");

            // A truncation at any byte must still be a classified FrameException,
            // as Python's boundary suite asserts for its valid frame.
            for (int cut = 0; cut < validFrame.Length; cut++)
            {
                var truncated = new byte[cut];
                Array.Copy(validFrame, truncated, cut);
                try
                {
                    FrameCodec.Decode(truncated);
                    problems.Add("truncated at " + cut + ": decoded without error");
                }
                catch (FrameException ex)
                {
                    if (ex.Code != "bad_frame")
                        problems.Add("truncated at " + cut + ": code " + ex.Code);
                }
                catch (Exception ex)
                {
                    problems.Add("truncated at " + cut + ": threw "
                        + ex.GetType().Name + ": " + ex.Message);
                }
            }

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS decoder-boundary");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL decoder-boundary");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        private static Dictionary<string, object> HeaderWith(object type, object version)
        {
            var header = new Dictionary<string, object>();
            header["type"] = type;
            if (version != null) header["v"] = version;
            return header;
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

        private static DecodedFrame CheckFrameAccepted(List<string> problems, string what, byte[] frame)
        {
            try
            {
                return FrameCodec.Decode(frame);
            }
            catch (Exception ex)
            {
                problems.Add(what + ": refused (" + ex.GetType().Name + ": " + ex.Message + ")");
                return null;
            }
        }

        private static void CheckEncodeProblem(List<string> problems, string what,
            Dictionary<string, object> header, IList<Section> sections, string code)
        {
            try
            {
                FrameCodec.Encode(header, sections);
                problems.Add(what + ": encoded without error");
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

        /// <summary>A hand-built frame around raw header text and raw payload
        /// bytes, for shapes the encoder refuses to produce.</summary>
        private static byte[] FrameWithPayload(string text, string payload)
        {
            return FrameWithBody(text, Encoding.UTF8.GetBytes(payload));
        }

        private static byte[] FrameWithBody(string text, byte[] body)
        {
            return FrameWithRawHeaderAndBody(Encoding.UTF8.GetBytes(text), body);
        }

        private static byte[] FrameWithRawHeader(byte[] head)
        {
            return FrameWithRawHeaderAndBody(head, new byte[0]);
        }

        private static byte[] FrameWithRawHeaderAndBody(byte[] head, byte[] body)
        {
            var frame = new byte[4 + head.Length + body.Length];
            frame[0] = (byte)(head.Length & 0xFF);
            frame[1] = (byte)((head.Length >> 8) & 0xFF);
            frame[2] = (byte)((head.Length >> 16) & 0xFF);
            frame[3] = (byte)((head.Length >> 24) & 0xFF);
            Buffer.BlockCopy(head, 0, frame, 4, head.Length);
            Buffer.BlockCopy(body, 0, frame, 4 + head.Length, body.Length);
            return frame;
        }

        /// <summary>UTF-8 bytes with one invalid 0xFF inside a header string.</summary>
        private static byte[] InvalidUtf8Header()
        {
            var head = new List<byte>(Encoding.UTF8.GetBytes("{\"type\":\"ping\",\"v\":2,\"note\":\""));
            head.Add(0xFF);
            head.AddRange(Encoding.UTF8.GetBytes("\"}"));
            return head.ToArray();
        }

        /// <summary>FormatDouble's boundary cases: byte-identical to Python's
        /// repr across the notation thresholds, -0.0, subnormals, the finite
        /// extremes and 16–17-digit values. The full contract is the shared
        /// number corpus below.</summary>
        private static void CheckFloatText()
        {
            var problems = new List<string>();
            CheckFloatText(problems, 1.0, "1.0");
            CheckFloatText(problems, -2.0, "-2.0");
            CheckFloatText(problems, 0.0, "0.0");
            CheckFloatText(problems, -0.0, "-0.0");
            CheckFloatText(problems, 2.5, "2.5");
            CheckFloatText(problems, 0.1, "0.1");
            CheckFloatText(problems, 0.2 + 0.1, "0.30000000000000004");
            CheckFloatText(problems, 0.0001, "0.0001");
            CheckFloatText(problems, -0.0001, "-0.0001");
            CheckFloatText(problems, 0.00001, "1e-05");
            CheckFloatText(problems, 1e15, "1000000000000000.0");
            CheckFloatText(problems, -1e15, "-1000000000000000.0");
            CheckFloatText(problems, 1e16, "1e+16");
            CheckFloatText(problems, 1e21, "1e+21");
            CheckFloatText(problems, 1.5e-7, "1.5e-07");
            CheckFloatText(problems, 1e-7, "1e-07");
            CheckFloatText(problems, 9007199254740992.0, "9007199254740992.0");
            CheckFloatText(problems, 12345678901234568.0, "1.2345678901234568e+16");
            CheckFloatText(problems, Math.PI, "3.141592653589793");
            CheckFloatText(problems, 5e-324, "5e-324");
            CheckFloatText(problems, double.MaxValue, "1.7976931348623157e+308");

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
            CheckThrows<JsonException>(problems, "negative infinity text",
                delegate { Json.FormatDouble(double.NegativeInfinity); });

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS float-text");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL float-text");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        /// <summary>The cross-language number contract: every finite double in
        /// protocol/number-corpus.json must format — and cast to a text
        /// parameter — as its stored Python repr.</summary>
        private static void CheckNumberCorpus(string fixturesRoot)
        {
            var problems = new List<string>();
            string path = Path.Combine(fixturesRoot, "..", "number-corpus.json");
            if (!File.Exists(path))
            {
                problems.Add("missing number corpus at " + Path.GetFullPath(path));
            }
            else
            {
                var entries = Json.Parse(File.ReadAllText(path, Encoding.UTF8)) as List<object>;
                if (entries == null)
                {
                    problems.Add("number corpus is not a JSON array");
                }
                else
                {
                    if (entries.Count < 100)
                        problems.Add("number corpus has only " + entries.Count + " values");
                    var seen = new HashSet<string>(StringComparer.Ordinal);
                    int counted = 0;
                    foreach (object raw in entries)
                    {
                        var entry = raw as Dictionary<string, object>;
                        if (entry == null)
                        {
                            problems.Add("number corpus entry is not an object");
                            continue;
                        }
                        string bits = Json.GetString(entry, "bits");
                        string expected = Json.GetString(entry, "text");
                        if (bits == null || bits.Length != 16)
                        {
                            problems.Add("number corpus entry has no 16-digit bits");
                            continue;
                        }
                        ulong rawBits;
                        if (!ulong.TryParse(bits, NumberStyles.HexNumber,
                                CultureInfo.InvariantCulture, out rawBits))
                        {
                            problems.Add("number corpus bits are not hex: " + bits);
                            continue;
                        }
                        if (!seen.Add(bits))
                        {
                            problems.Add("number corpus repeats " + bits);
                            continue;
                        }
                        counted++;
                        double value = BitConverter.Int64BitsToDouble(unchecked((long)rawBits));
                        if (double.IsNaN(value) || double.IsInfinity(value))
                        {
                            problems.Add(bits + ": non-finite corpus value");
                            continue;
                        }
                        string got = Json.FormatDouble(value);
                        if (got != expected)
                            problems.Add(bits + ": " + got + " != " + expected);
                        string text = (string)ParamValues.Cast("text", value);
                        if (text != expected)
                            problems.Add(bits + ": text " + text + " != " + expected);
                    }
                    if (counted < 100)
                        problems.Add("number corpus has only " + counted + " usable values");
                }
            }

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS number-corpus");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL number-corpus");
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

            // A typed list encodes exactly like List<object>.
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

        /// <summary>The endpoint helper's pure half: defaults, loopback-only
        /// hosts, the port range, canonical text and the persisted > env >
        /// default precedence. No Grasshopper or socket is involved.</summary>
        private static void CheckEndpoint()
        {
            var problems = new List<string>();

            int defaultPort = Endpoint.DefaultPort;
            if (defaultPort != 59130)
                problems.Add("default port is " + defaultPort + ", not 59130");
            if (!Endpoint.IsValidPort(1024) || !Endpoint.IsValidPort(65535))
                problems.Add("the inclusive port range was rejected");
            if (Endpoint.IsValidPort(1023) || Endpoint.IsValidPort(65536)
                || Endpoint.IsValidPort(0) || Endpoint.IsValidPort(-1))
                problems.Add("an out-of-range port was accepted");
            if (Endpoint.Canonical(59130) != "ws://127.0.0.1:59130/")
                problems.Add("canonical endpoint: " + Endpoint.Canonical(59130));
            CheckThrows<ArgumentOutOfRangeException>(problems,
                "canonical endpoint for port 80",
                delegate { Endpoint.Canonical(80); });

            string normalized;
            string error;
            CheckHostAccepted(problems, "127.0.0.1");
            CheckHostAccepted(problems, "localhost");
            CheckHostAccepted(problems, " LOCALHOST ");
            CheckHostRejected(problems, "0.0.0.0");
            CheckHostRejected(problems, "::1");
            CheckHostRejected(problems, "example.com");
            CheckHostRejected(problems, "");
            CheckHostRejected(problems, null);
            if (Endpoint.TryNormalizeHost("localhost", out normalized, out error)
                && normalized != "127.0.0.1")
                problems.Add("localhost normalized to " + normalized);

            string endpoint;
            if (!Endpoint.TryCreate("localhost", 59130, out endpoint, out error)
                || endpoint != "ws://127.0.0.1:59130/")
                problems.Add("TryCreate(localhost): " + (endpoint ?? "(none)") + " / " + error);
            if (Endpoint.TryCreate("0.0.0.0", 59130, out endpoint, out error))
                problems.Add("TryCreate accepted 0.0.0.0");
            if (Endpoint.TryCreate("127.0.0.1", 80, out endpoint, out error))
                problems.Add("TryCreate accepted port 80");

            int port;
            if (!Endpoint.TryParsePort(" 59130 ", out port, out error) || port != 59130)
                problems.Add("TryParsePort accepted text did not parse");
            if (!Endpoint.TryParsePort("1024", out port, out error) || port != 1024)
                problems.Add("TryParsePort rejected the low boundary");
            if (!Endpoint.TryParsePort("65535", out port, out error) || port != 65535)
                problems.Add("TryParsePort rejected the high boundary");
            if (Endpoint.TryParsePort("80", out port, out error))
                problems.Add("TryParsePort accepted 80");
            if (Endpoint.TryParsePort("65536", out port, out error))
                problems.Add("TryParsePort accepted 65536");
            if (Endpoint.TryParsePort("+59130", out port, out error))
                problems.Add("TryParsePort accepted a plus sign");
            if (Endpoint.TryParsePort("59,130", out port, out error))
                problems.Add("TryParsePort accepted a grouping separator");
            if (Endpoint.TryParsePort("abc", out port, out error))
                problems.Add("TryParsePort accepted text");
            if (Endpoint.TryParsePort("", out port, out error))
                problems.Add("TryParsePort accepted an empty value");

            EndpointSource source;
            string warning;
            int resolved = Endpoint.ResolvePort(60000, "62000", out source, out warning);
            if (resolved != 60000 || source != EndpointSource.Persisted || warning != null)
                problems.Add("persisted did not win: " + resolved + "/" + source + "/" + warning);

            // An invalid saved value is ignored, but a valid environment value
            // still applies as the documented development override.
            resolved = Endpoint.ResolvePort(80, "62000", out source, out warning);
            if (resolved != 62000 || source != EndpointSource.Environment
                || string.IsNullOrEmpty(warning))
                problems.Add("invalid persisted did not fall to env: " + resolved + "/" + source);

            resolved = Endpoint.ResolvePort(0, "62000", out source, out warning);
            if (resolved != 62000 || source != EndpointSource.Environment || warning != null)
                problems.Add("env did not apply: " + resolved + "/" + source + "/" + warning);

            // A malformed environment value must not stop the plugin loading.
            resolved = Endpoint.ResolvePort(0, "not-a-port", out source, out warning);
            if (resolved != Endpoint.DefaultPort || source != EndpointSource.Default
                || string.IsNullOrEmpty(warning))
                problems.Add("bad env did not fall to default: " + resolved + "/" + source);

            resolved = Endpoint.ResolvePort(80, null, out source, out warning);
            if (resolved != Endpoint.DefaultPort || source != EndpointSource.Default
                || string.IsNullOrEmpty(warning))
                problems.Add("bad persisted did not fall to default: " + resolved + "/" + source);

            resolved = Endpoint.ResolvePort(0, "  ", out source, out warning);
            if (resolved != Endpoint.DefaultPort || source != EndpointSource.Default || warning != null)
                problems.Add("blank env was not treated as unset: " + resolved + "/" + source);

            // A valid persisted value wins even when the environment is invalid.
            resolved = Endpoint.ResolvePort(60000, "not-a-port", out source, out warning);
            if (resolved != 60000 || source != EndpointSource.Persisted || warning != null)
                problems.Add("persisted lost to a bad env value: " + resolved + "/" + source);

            if (problems.Count == 0)
            {
                System.Console.WriteLine("PASS endpoint");
                return;
            }
            _failures += problems.Count;
            System.Console.WriteLine("FAIL endpoint");
            foreach (string p in problems) System.Console.WriteLine("     " + p);
        }

        private static void CheckHostAccepted(List<string> problems, string host)
        {
            string normalized;
            string error;
            if (!Endpoint.TryNormalizeHost(host, out normalized, out error)
                || normalized != Endpoint.LoopbackHost)
                problems.Add("host not accepted: " + (host ?? "(null)") + " -> "
                    + (normalized ?? "(none)") + " / " + error);
        }

        private static void CheckHostRejected(List<string> problems, string host)
        {
            string normalized;
            string error;
            if (Endpoint.TryNormalizeHost(host, out normalized, out error))
                problems.Add("host accepted: " + (host ?? "(null)") + " -> " + normalized);
            else if (string.IsNullOrEmpty(error))
                problems.Add("host rejected without a reason: " + (host ?? "(null)"));
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
            return FrameWithRawHeader(Encoding.UTF8.GetBytes(text));
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
