// C# parameter and schema microbenchmark.
//
// This measures the pure managed layer that the Blender matrix cannot see:
// Json.FormatDouble over the number corpus, ParamValues.Cast/JsonValue for
// every declared parameter type, the canonical JSON/schema-header encode for a
// representative 100-parameter stage, and the frame/section decode path over a
// 100k-triangle mesh frame (whole-frame decode and typed section access). It
// does not measure RhinoCommon or Grasshopper object allocation, Stage In
// conversion, publication or the solve; those are Rhino/GH-only paths and are
// recorded as skips in benchmarks/README.md.
//
//     StageloomParamBench.exe > result.json
//
// The process writes one canonical JSON object on stdout. scripts/bench-gha
// builds it with the same wine/Mono csc environment as the test suites and
// stores the result under benchmarks/results/. C# 5/Mono is the production
// constraint, so this file uses no newer language features.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using Stageloom.Core;

namespace Stageloom.Bench
{
    public static class ParamBench
    {
        private const int Samples = 15;
        private static object _sink;

        private sealed class Metric
        {
            public string Name;
            public string Unit;
            public int OpsPerSample;
            public List<double> Values;

            public Dictionary<string, object> ToJson()
            {
                var samples = new List<object>();
                foreach (double value in Values) samples.Add(value);
                var entry = new Dictionary<string, object>();
                entry["name"] = Name;
                entry["unit"] = Unit;
                entry["ops_per_sample"] = OpsPerSample;
                entry["p50"] = Percentile(Values, 0.50);
                entry["p95"] = Percentile(Values, 0.95);
                entry["min"] = Minimum(Values);
                entry["max"] = Maximum(Values);
                entry["samples"] = samples;
                return entry;
            }
        }

        public static int Main(string[] args)
        {
            try
            {
                var root = new Dictionary<string, object>();
                root["format"] = "stageloom-gha-bench/1";
                root["meta"] = Metadata();
                var metrics = new List<object>();
                foreach (Metric metric in RunAll()) metrics.Add(metric.ToJson());
                root["metrics"] = metrics;
                Console.Out.Write(Json.WriteCanonical(root));
                Console.Out.Write("\n");
                return 0;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("param bench failed: " + error);
                return 1;
            }
        }

        private static Dictionary<string, object> Metadata()
        {
            var meta = new Dictionary<string, object>();
            meta["tool"] = "Stageloom.ParamBench";
            meta["timestamp"] = DateTime.UtcNow.ToString(
                "yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);
            meta["os"] = Environment.OSVersion.VersionString;
            meta["runtime"] = Environment.Version.ToString();
            meta["processor_count"] = Environment.ProcessorCount;
            meta["samples"] = Samples;
            meta["note"] =
                "Measures the pure C# JSON/parameter layer and the frame/section "
                + "decode path only; it excludes RhinoCommon/Grasshopper object "
                + "allocation, Stage In conversion, publication and the solve.";
            return meta;
        }

        private static List<Metric> RunAll()
        {
            var metrics = new List<Metric>();
            metrics.Add(FormatDoubleMetric());
            metrics.AddRange(ParamValueMetrics());
            metrics.AddRange(SchemaMetrics());
            metrics.AddRange(FrameMetrics());
            return metrics;
        }

        private static readonly double[] NumberCorpus = new double[]
        {
            0.0,
            -0.0,
            1.0,
            -1.0,
            0.1,
            1.0 / 3.0,
            2.5,
            9007199254740992.0,       // 2^53, the last integer exactly in a double
            1e15,
            1e16,
            1e-5,
            1e-4,
            1e21,
            double.MaxValue,
            double.Epsilon,
            -2.2250738585072014e-308, // -Double.MinValue
            123456789012345.0,
            999999999999999.9,
            -1234.56789012345,
            1000000000000000.0,
            Math.PI,
            Math.E,
        };

        private static Metric FormatDoubleMetric()
        {
            int index = 0;
            return Measure(
                "format_double",
                "ns/value",
                1e6,
                2000,
                delegate
                {
                    _sink = Json.FormatDouble(NumberCorpus[index]);
                    index = (index + 1) % NumberCorpus.Length;
                });
        }

        private static readonly string[] ParamTypes = new string[]
        {
            "float", "int", "bool", "vector3", "color", "value_list", "text",
        };

        private static object ParamValue(string type)
        {
            if (type == "float") return 2.5;
            if (type == "int") return 2.6;
            if (type == "bool") return true;
            if (type == "value_list") return 2.9;
            if (type == "text") return "panel";
            return new List<object>(new object[] { 1.0, 2.0, 3.0 });
        }

        private static List<Metric> ParamValueMetrics()
        {
            var metrics = new List<Metric>();
            foreach (string type in ParamTypes)
            {
                object value = ParamValue(type);
                object cast = ParamValues.Cast(type, value);
                metrics.Add(Measure(
                    "cast." + type,
                    "ns/value",
                    1e6,
                    50000,
                    delegate { _sink = ParamValues.Cast(type, value); }));
                metrics.Add(Measure(
                    "json." + type,
                    "ns/value",
                    1e6,
                    50000,
                    delegate { _sink = ParamValues.JsonValue(cast); }));
            }
            return metrics;
        }

        private static List<Metric> SchemaMetrics()
        {
            var parameters = new List<object>();
            for (int i = 0; i < 100; i++)
            {
                string type = ParamTypes[i % ParamTypes.Length];
                var declaration = new Dictionary<string, object>();
                declaration["id"] = i.ToString(CultureInfo.InvariantCulture);
                declaration["label"] = "Parameter " + i.ToString(CultureInfo.InvariantCulture);
                declaration["type"] = type;
                declaration["default"] = ParamValues.JsonValue(ParamValue(type));
                declaration["min"] = null;
                declaration["max"] = null;
                declaration["step"] = 0.1;
                declaration["values"] = new List<object>();
                declaration["group"] = i < 50 ? "Shape" : "Style";
                parameters.Add(declaration);
            }
            var schema = new Dictionary<string, object>();
            schema["type"] = "schema";
            schema["stage"] = "bench-stage";
            schema["params"] = parameters;

            var metrics = new List<Metric>();
            metrics.Add(Measure(
                "schema_signature",
                "ms/encode",
                1.0,
                50,
                delegate { _sink = Json.WriteCanonical(parameters); }));
            metrics.Add(Measure(
                "schema_frame",
                "ms/encode",
                1.0,
                50,
                delegate { _sink = FrameCodec.Encode(schema, null); }));
            return metrics;
        }

        /// <summary>One encoded 100k-triangle mesh frame: the same section shape
        /// the link decodes and the Stage In converters read.</summary>
        private sealed class FrameCorpus
        {
            public byte[] Bytes;
            public Section Verts;
            public Section Counts;
            public Section Indices;
        }

        private static FrameCorpus BuildFrameCorpus()
        {
            const int n = 225;
            int vertices = n * n;
            int quads = (n - 1) * (n - 1);
            var positions = new float[vertices * 3];
            for (int i = 0; i < positions.Length; i++)
                positions[i] = (float)(i % 17) * 0.125f;
            var counts = new uint[quads];
            var indices = new uint[quads * 4];
            for (int quad = 0; quad < quads; quad++)
            {
                counts[quad] = 4;
                int x = quad % (n - 1);
                int y = quad / (n - 1);
                int a = y * n + x;
                indices[quad * 4] = (uint)a;
                indices[quad * 4 + 1] = (uint)(a + n);
                indices[quad * 4 + 2] = (uint)(a + n + 1);
                indices[quad * 4 + 3] = (uint)(a + 1);
            }
            var header = new Dictionary<string, object>();
            header["type"] = "request";
            header["stage"] = "bench-stage";
            var sections = new List<Section>();
            var corpus = new FrameCorpus();
            corpus.Verts = Section.Floats("mesh.verts", positions);
            corpus.Counts = Section.U32("mesh.face_counts", counts);
            corpus.Indices = Section.U32("mesh.face_indices", indices);
            sections.Add(corpus.Verts);
            sections.Add(corpus.Counts);
            sections.Add(corpus.Indices);
            corpus.Bytes = FrameCodec.Encode(header, sections);
            return corpus;
        }

        private static List<Metric> FrameMetrics()
        {
            FrameCorpus corpus = BuildFrameCorpus();
            var metrics = new List<Metric>();
            metrics.Add(Measure(
                "frame_decode",
                "ms/frame",
                1.0,
                10,
                delegate
                {
                    DecodedFrame decoded = FrameCodec.Decode(corpus.Bytes);
                    _sink = decoded.Sections.Count + decoded.Header.Count;
                }));
            metrics.Add(Measure(
                "section_floats",
                "ms/op",
                1.0,
                100,
                delegate { _sink = corpus.Verts.ToFloats(); }));
            metrics.Add(Measure(
                "section_u32",
                "ms/op",
                1.0,
                100,
                delegate { _sink = corpus.Indices.ToU32(); }));
            metrics.Add(Measure(
                "frame_mesh_arrays",
                "ms/frame",
                1.0,
                10,
                delegate
                {
                    DecodedFrame decoded = FrameCodec.Decode(corpus.Bytes);
                    _sink = decoded.Find("mesh.verts").ToFloats();
                    _sink = decoded.Find("mesh.face_counts").ToU32();
                    _sink = decoded.Find("mesh.face_indices").ToU32();
                }));
            return metrics;
        }

        private static Metric Measure(
            string name, string unit, double scale, int batch, Action action)
        {
            GC.Collect();
            GC.WaitForPendingFinalizers();
            action(); // warm the JIT and the allocator before the timed samples
            var values = new List<double>();
            for (int sample = 0; sample < Samples; sample++)
            {
                var watch = Stopwatch.StartNew();
                for (int operation = 0; operation < batch; operation++) action();
                watch.Stop();
                values.Add(watch.Elapsed.TotalMilliseconds * scale / batch);
            }
            var metric = new Metric();
            metric.Name = name;
            metric.Unit = unit;
            metric.OpsPerSample = batch;
            metric.Values = values;
            return metric;
        }

        private static double Percentile(List<double> values, double fraction)
        {
            var ordered = new List<double>(values);
            ordered.Sort();
            if (ordered.Count == 1) return ordered[0];
            double rank = (ordered.Count - 1) * fraction;
            int lower = (int)Math.Floor(rank);
            int upper = (int)Math.Ceiling(rank);
            if (lower == upper) return ordered[lower];
            double weight = rank - lower;
            return ordered[lower] + (ordered[upper] - ordered[lower]) * weight;
        }

        private static double Minimum(List<double> values)
        {
            double value = values[0];
            for (int i = 1; i < values.Count; i++)
                if (values[i] < value) value = values[i];
            return value;
        }

        private static double Maximum(List<double> values)
        {
            double value = values[0];
            for (int i = 1; i < values.Count; i++)
                if (values[i] > value) value = values[i];
            return value;
        }
    }
}
