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
            // conversion path cannot hide behind the raw-byte checks.
            if (name == "response-mixed")
            {
                Section verts = decoded.Find("mesh.verts");
                float[] f = verts.ToFloats();
                if (f.Length != 9 || f[3] != 1.0f)
                    System.Console.WriteLine("  WARN mesh.verts typed access looks wrong");
                Section counts = decoded.Find("crv.counts");
                uint[] c = counts.ToU32();
                if (c.Length != 2 || c[0] != 2 || c[1] != 3)
                    System.Console.WriteLine("  WARN crv.counts typed access looks wrong");
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
            if ((int)ParamValues.Cast("choice", "nonsense") != 0)
                problems.Add("Cast(choice, nonsense) was not neutral");
            if ((bool)ParamValues.Cast("bool", "not a bool") != false)
                problems.Add("Cast(bool, string) was not neutral");

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
