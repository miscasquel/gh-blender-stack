// Definition identity: one owner of definition names, fallbacks and content hashing.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom
{
    internal static class DefinitionIdentity
    {
        public const string Prefix = "Stageloom ";

        /// <summary>A deterministic fallback name for an unnamed definition.</summary>
        public static string NameOrFallback(string name, string contentKey)
        {
            return string.IsNullOrEmpty(name) ? Prefix + contentKey : name;
        }

        /// <summary>The fallback split's distinct per-part name.</summary>
        public static string SplitName(string name, string contentKey, int part, int parts)
        {
            string suffix = " [part " + part.ToString(CultureInfo.InvariantCulture)
                + "/" + parts.ToString(CultureInfo.InvariantCulture) + "]";
            return NameOrFallback(name, contentKey) + suffix;
        }

        public static string Label(string name)
        {
            return string.IsNullOrEmpty(name) ? "(unnamed)" : name;
        }

        /// <summary>The definition index a descriptor names, or -1 without a marker.</summary>
        public static bool DescriptorDefinition(Dictionary<string, object> descriptor,
                                                out long definition)
        {
            definition = -1;
            object raw;
            if (!descriptor.TryGetValue("definition", out raw) || raw == null) return false;
            return DefinitionTable.TryInteger(raw, out definition) && definition >= 0;
        }

        /// <summary>A stable fallback name. Single-part hashing keeps the legacy
        /// order; multi-part adds the part count, full names and descriptors.</summary>
        public static string ContentHash(DecodedFrame frame, long index, int parts,
                                         List<Dictionary<string, object>> attributes,
                                         double scale, Func<long, string> childHash)
        {
            return parts > 1
                ? MultiPartHash(frame, index, parts, attributes, scale, childHash)
                : SinglePartHash(frame, index, scale, childHash);
        }

        private static string SinglePartHash(DecodedFrame frame, long index, double scale,
                                             Func<long, string> childHash)
        {
            string prefix = DefinitionTable.Prefix(index, -1);
            var byName = new Dictionary<string, Section>();
            foreach (Section section in frame.Sections)
                if (section.Name.StartsWith(prefix, StringComparison.Ordinal))
                    byName[section.Name.Substring(prefix.Length)] = section;

            long[] nested;
            List<Transform> nestedTransforms;
            InstanceBuild.ReadRows(frame, prefix, scale, out nested, out nestedTransforms);

            using (var hash = new DefinitionHash())
            {
                foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                    hash.Add(name, byName[name].Raw);
                AddNested(hash, frame, nested, nestedTransforms, scale, childHash);
                return hash.Finish(12);
            }
        }

        private static string MultiPartHash(DecodedFrame frame, long index, int parts,
                                            List<Dictionary<string, object>> attributes,
                                            double scale, Func<long, string> childHash)
        {
            string prefix = DefinitionTable.Prefix(index, -1);
            var byName = new Dictionary<string, Section>();
            foreach (Section section in frame.Sections)
                if (section.Name.StartsWith(prefix, StringComparison.Ordinal))
                    byName[section.Name] = section;

            long[] nested;
            List<Transform> nestedTransforms;
            InstanceBuild.ReadRows(frame, prefix, scale, out nested, out nestedTransforms);

            var descriptors = new List<Dictionary<string, object>>();
            if (attributes != null)
            {
                foreach (Dictionary<string, object> descriptor in attributes)
                {
                    long definition;
                    if (descriptor != null && DescriptorDefinition(descriptor, out definition)
                        && definition == index)
                        descriptors.Add(descriptor);
                }
            }

            using (var hash = new DefinitionHash())
            {
                hash.Add("definition.parts", BitConverter.GetBytes((long)parts));
                foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                    hash.Add(name, byName[name].Raw);
                hash.AddJson("attr.descriptors", descriptors);
                AddNested(hash, frame, nested, nestedTransforms, scale, childHash);
                return hash.Finish(12);
            }
        }

        private static void AddNested(DefinitionHash hash, DecodedFrame frame,
                                      long[] nested, List<Transform> nestedTransforms,
                                      double scale, Func<long, string> childHash)
        {
            if (nested == null) return;
            for (int i = 0; i < nested.Length; i++)
            {
                hash.Add("inst.ref_def", BitConverter.GetBytes((uint)nested[i]));
                float[] wire = Conversion.TransformToWire(nestedTransforms[i], scale);
                var raw = new byte[wire.Length * 4];
                Buffer.BlockCopy(wire, 0, raw, 0, raw.Length);
                hash.Add("inst.transforms", raw);
            }
            for (int i = 0; i < nested.Length; i++)
                hash.Add("inst.name", Encoding.UTF8.GetBytes(DefinitionTable.NameOf(frame, nested[i])));
            for (int i = 0; i < nested.Length; i++)
                hash.Add("inst.content", Encoding.UTF8.GetBytes(childHash(nested[i])));
        }

        /// <summary>A canonical name+bytes stream for a definition hash.</summary>
        internal sealed class DefinitionHash : IDisposable
        {
            private readonly HashAlgorithm _sha = SHA256.Create();

            public void Add(string name, byte[] raw)
            {
                byte[] encoded = Encoding.UTF8.GetBytes(name);
                _sha.TransformBlock(encoded, 0, encoded.Length, null, 0);
                _sha.TransformBlock(new byte[] { 0 }, 0, 1, null, 0);
                _sha.TransformBlock(raw, 0, raw.Length, null, 0);
            }

            /// <summary>Add a canonical JSON value; an unhashable one keeps the
            /// old sentinel instead of throwing.</summary>
            public void AddJson(string name, object value)
            {
                byte[] json;
                try
                {
                    json = Json.WriteCanonicalUtf8(value);
                }
                catch (JsonException)
                {
                    json = Encoding.UTF8.GetBytes("unhashable");
                }
                Add(name, json);
            }

            public string Finish(int chars)
            {
                _sha.TransformFinalBlock(new byte[0], 0, 0);
                var text = new StringBuilder();
                byte[] digest = _sha.Hash;
                for (int i = 0; i < digest.Length && text.Length < chars; i++)
                    text.Append(digest[i].ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }

            public void Dispose()
            {
                _sha.Dispose();
            }
        }
    }
}
