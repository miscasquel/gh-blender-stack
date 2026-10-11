// Minimal JSON for the GH Stack codec: parse and canonical encode, with no
// third-party dependencies. This is the C# half of the protocol's header format
// (docs/protocol.md §3): UTF-8, keys sorted, no insignificant whitespace.
//
// Representation is plain CLR values, the same shape System.Text.Json uses:
//   null, bool, long, double, string, List<object>, Dictionary<string, object>
//
// Integers stay long and floats stay double, because Python's json module writes
// them differently ("4" vs "4.0") and the golden fixtures compare bytes. Floats
// are formatted the way Python's repr does for the ranges the protocol uses:
// shortest round-trip text, with ".0" appended to integral values.
//
// Deliberate deviation from the plan's "DataContractJsonSerializer" (§10): that
// serializer emits members in declaration order and cannot sort keys, which the
// canonical header requires. A hand-written writer keeps the no-third-party rule
// and gives byte-stable output.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace GhStack.Core
{
    public static class Json
    {
        // ------------------------------------------------------------ parse

        public static object Parse(string text)
        {
            int pos = 0;
            object value = ParseValue(text, ref pos);
            SkipWhitespace(text, ref pos);
            if (pos != text.Length)
                throw new JsonException("trailing content at index " + pos);
            return value;
        }

        private static object ParseValue(string s, ref int pos)
        {
            SkipWhitespace(s, ref pos);
            if (pos >= s.Length) throw new JsonException("unexpected end of input");
            char c = s[pos];
            if (c == '{') return ParseObject(s, ref pos);
            if (c == '[') return ParseArray(s, ref pos);
            if (c == '"') return ParseString(s, ref pos);
            if (c == 't') { Expect(s, ref pos, "true"); return true; }
            if (c == 'f') { Expect(s, ref pos, "false"); return false; }
            if (c == 'n') { Expect(s, ref pos, "null"); return null; }
            return ParseNumber(s, ref pos);
        }

        private static Dictionary<string, object> ParseObject(string s, ref int pos)
        {
            var obj = new Dictionary<string, object>();
            pos++; // {
            SkipWhitespace(s, ref pos);
            if (pos < s.Length && s[pos] == '}') { pos++; return obj; }
            while (true)
            {
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length || s[pos] != '"')
                    throw new JsonException("expected a key string at index " + pos);
                string key = ParseString(s, ref pos);
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length || s[pos] != ':')
                    throw new JsonException("expected ':' at index " + pos);
                pos++;
                obj[key] = ParseValue(s, ref pos);
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length) throw new JsonException("unterminated object");
                if (s[pos] == ',') { pos++; continue; }
                if (s[pos] == '}') { pos++; return obj; }
                throw new JsonException("expected ',' or '}' at index " + pos);
            }
        }

        private static List<object> ParseArray(string s, ref int pos)
        {
            var list = new List<object>();
            pos++; // [
            SkipWhitespace(s, ref pos);
            if (pos < s.Length && s[pos] == ']') { pos++; return list; }
            while (true)
            {
                list.Add(ParseValue(s, ref pos));
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length) throw new JsonException("unterminated array");
                if (s[pos] == ',') { pos++; continue; }
                if (s[pos] == ']') { pos++; return list; }
                throw new JsonException("expected ',' or ']' at index " + pos);
            }
        }

        private static string ParseString(string s, ref int pos)
        {
            pos++; // opening quote
            var sb = new StringBuilder();
            while (true)
            {
                if (pos >= s.Length) throw new JsonException("unterminated string");
                char c = s[pos++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (pos >= s.Length) throw new JsonException("unterminated escape");
                char e = s[pos++];
                switch (e)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (pos + 4 > s.Length) throw new JsonException("bad unicode escape");
                        sb.Append((char)Convert.ToInt32(s.Substring(pos, 4), 16));
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("bad escape '\\" + e + "'");
                }
            }
        }

        private static object ParseNumber(string s, ref int pos)
        {
            int start = pos;
            bool isFloat = false;
            if (pos < s.Length && (s[pos] == '-' || s[pos] == '+')) pos++;
            while (pos < s.Length)
            {
                char c = s[pos];
                if (c >= '0' && c <= '9') { pos++; continue; }
                if (c == '.' || c == 'e' || c == 'E')
                {
                    isFloat = true;
                    pos++;
                    continue;
                }
                break;
            }
            string token = s.Substring(start, pos - start);
            if (token.Length == 0) throw new JsonException("empty number at index " + start);
            if (isFloat)
                return double.Parse(token, CultureInfo.InvariantCulture);
            long l;
            if (long.TryParse(token, NumberStyles.Integer, CultureInfo.InvariantCulture, out l))
                return l;
            // Too big for long: the protocol never uses such integers in headers,
            // but do not lose the value.
            return double.Parse(token, CultureInfo.InvariantCulture);
        }

        private static void Expect(string s, ref int pos, string word)
        {
            if (pos + word.Length > s.Length || s.Substring(pos, word.Length) != word)
                throw new JsonException("expected '" + word + "' at index " + pos);
            pos += word.Length;
        }

        private static void SkipWhitespace(string s, ref int pos)
        {
            while (pos < s.Length)
            {
                char c = s[pos];
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        // ------------------------------------------------------------ write

        /// <summary>Canonical encoding: sorted keys, tight separators, raw UTF-8.
        /// Produces the same text Python's json.dumps(sort_keys=True,
        /// separators=(",", ":"), ensure_ascii=False) does for the same values.</summary>
        public static string WriteCanonical(object value)
        {
            var sb = new StringBuilder();
            WriteValue(sb, value);
            return sb.ToString();
        }

        public static byte[] WriteCanonicalUtf8(object value)
        {
            return Encoding.UTF8.GetBytes(WriteCanonical(value));
        }

        private static void WriteValue(StringBuilder sb, object value)
        {
            if (value == null) { sb.Append("null"); return; }
            if (value is bool) { sb.Append((bool)value ? "true" : "false"); return; }
            if (value is string) { WriteString(sb, (string)value); return; }
            if (value is long) { sb.Append(((long)value).ToString(CultureInfo.InvariantCulture)); return; }
            if (value is int) { sb.Append(((int)value).ToString(CultureInfo.InvariantCulture)); return; }
            if (value is double) { sb.Append(FormatDouble((double)value)); return; }
            if (value is float) { sb.Append(FormatDouble((float)value)); return; }
            var dict = value as Dictionary<string, object>;
            if (dict != null) { WriteObject(sb, dict); return; }
            var list = value as List<object>;
            if (list != null) { WriteArray(sb, list); return; }
            throw new JsonException("cannot encode " + value.GetType().Name);
        }

        private static void WriteObject(StringBuilder sb, Dictionary<string, object> obj)
        {
            sb.Append('{');
            var keys = new List<string>(obj.Keys);
            keys.Sort(StringComparer.Ordinal);
            for (int i = 0; i < keys.Count; i++)
            {
                if (i > 0) sb.Append(',');
                WriteString(sb, keys[i]);
                sb.Append(':');
                WriteValue(sb, obj[keys[i]]);
            }
            sb.Append('}');
        }

        private static void WriteArray(StringBuilder sb, List<object> list)
        {
            sb.Append('[');
            for (int i = 0; i < list.Count; i++)
            {
                if (i > 0) sb.Append(',');
                WriteValue(sb, list[i]);
            }
            sb.Append(']');
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                            sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        /// <summary>Python-repr style float text: shortest round-trip, with ".0"
        /// appended so integral floats stay distinguishable from integers.</summary>
        public static string FormatDouble(double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d))
                throw new JsonException("JSON cannot carry " + d);
            string s = d.ToString("R", CultureInfo.InvariantCulture);
            if (s.IndexOf('.') < 0 && s.IndexOf('e') < 0 && s.IndexOf('E') < 0)
                s += ".0";
            return s;
        }

        // ------------------------------------------------------- comparison

        /// <summary>Deep equality, comparing numbers numerically (a fixture's 2 and
        /// 2.0 compare equal) and everything else structurally.</summary>
        public static bool DeepEquals(object a, object b)
        {
            if (a == null || b == null) return a == null && b == null;
            if (IsNumber(a) && IsNumber(b))
                return Convert.ToDouble(a, CultureInfo.InvariantCulture)
                    == Convert.ToDouble(b, CultureInfo.InvariantCulture);
            if (a is bool || b is bool) return a is bool && b is bool && (bool)a == (bool)b;
            if (a is string || b is string) return a is string && b is string && (string)a == (string)b;
            var da = a as Dictionary<string, object>;
            var db = b as Dictionary<string, object>;
            if (da != null || db != null)
            {
                if (da == null || db == null || da.Count != db.Count) return false;
                foreach (var kv in da)
                {
                    object other;
                    if (!db.TryGetValue(kv.Key, out other)) return false;
                    if (!DeepEquals(kv.Value, other)) return false;
                }
                return true;
            }
            var la = a as List<object>;
            var lb = b as List<object>;
            if (la != null || lb != null)
            {
                if (la == null || lb == null || la.Count != lb.Count) return false;
                for (int i = 0; i < la.Count; i++)
                    if (!DeepEquals(la[i], lb[i])) return false;
                return true;
            }
            return false;
        }

        public static bool IsNumber(object v)
        {
            return v is long || v is int || v is double || v is float;
        }

        // ----------------------------------------------------------- helpers

        public static object Get(object obj, string key)
        {
            var d = obj as Dictionary<string, object>;
            if (d == null) throw new JsonException("not an object");
            object v;
            return d.TryGetValue(key, out v) ? v : null;
        }

        public static string GetString(object obj, string key)
        {
            object v = Get(obj, key);
            return v == null ? null : (string)v;
        }

        public static long GetLong(object obj, string key)
        {
            object v = Get(obj, key);
            return Convert.ToInt64(v, CultureInfo.InvariantCulture);
        }
    }

    public class JsonException : Exception
    {
        public JsonException(string message) : base(message) { }
    }
}
