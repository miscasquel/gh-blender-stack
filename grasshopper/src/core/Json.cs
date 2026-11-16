// Minimal JSON for the Stageloom codec: parse and canonical encode, with no
// third-party dependencies. This is the C# half of the protocol's header format
// (docs/protocol.md §3): UTF-8, keys sorted, no insignificant whitespace.
//
// Representation is plain CLR values, the same shape System.Text.Json uses:
//   null, bool, long, double, string, List<object>, Dictionary<string, object>
//
// Integers stay long and floats stay double, because Python's json module writes
// them differently ("4" vs "4.0") and the golden fixtures compare bytes. Floats
// are formatted as Python's repr for the full finite double range: shortest
// round-trip digits, Python's fixed/scientific thresholds and exponent shape.
// protocol/number-corpus.json pins that contract from both languages.
//
// A hand-written writer keeps the no-third-party rule and gives byte-stable
// output with sorted keys.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Numerics;
using System.Text;

namespace Stageloom.Core
{
    public static class Json
    {
        // ------------------------------------------------------------ parse

        public static object Parse(string text)
        {
            if (text == null) throw new JsonException("input is null");
            int pos = 0;
            object value = ParseValue(text, ref pos, 0);
            SkipWhitespace(text, ref pos);
            if (pos != text.Length)
                throw new JsonException("trailing content at index " + pos);
            // Python's json decoder accepts two things strict UTF-8 cannot
            // carry: an escaped lone surrogate and a float that overflowed to
            // an infinity. Walking the tree once here keeps both codecs on the
            // same set of valid headers, whether the value is a key, a value or
            // nested inside an array.
            ValidateTree(value);
            return value;
        }

        // A 1 MiB header of '[' would drive the recursive parser into an
        // uncatchable StackOverflowException, which takes the connection (and
        // the process) down; the protocol's headers nest a handful deep.
        private const int MaxDepth = 64;

        private static object ParseValue(string s, ref int pos, int depth)
        {
            SkipWhitespace(s, ref pos);
            if (pos >= s.Length) throw new JsonException("unexpected end of input");
            if (depth > MaxDepth) throw new JsonException("nesting over the depth cap");
            char c = s[pos];
            if (c == '{') return ParseObject(s, ref pos, depth);
            if (c == '[') return ParseArray(s, ref pos, depth);
            if (c == '"') return ParseString(s, ref pos);
            if (c == 't') { Expect(s, ref pos, "true"); return true; }
            if (c == 'f') { Expect(s, ref pos, "false"); return false; }
            if (c == 'n') { Expect(s, ref pos, "null"); return null; }
            return ParseNumber(s, ref pos);
        }

        private static Dictionary<string, object> ParseObject(string s, ref int pos, int depth)
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
                // Python's object_pairs_hook rejects a repeated key; keeping the
                // last one would let two different frame texts decode to the
                // same header and let a peer smuggle data past validation.
                if (obj.ContainsKey(key))
                    throw new JsonException("duplicate object key");
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length || s[pos] != ':')
                    throw new JsonException("expected ':' at index " + pos);
                pos++;
                obj[key] = ParseValue(s, ref pos, depth + 1);
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length) throw new JsonException("unterminated object");
                if (s[pos] == ',') { pos++; continue; }
                if (s[pos] == '}') { pos++; return obj; }
                throw new JsonException("expected ',' or '}' at index " + pos);
            }
        }

        private static List<object> ParseArray(string s, ref int pos, int depth)
        {
            var list = new List<object>();
            pos++; // [
            SkipWhitespace(s, ref pos);
            if (pos < s.Length && s[pos] == ']') { pos++; return list; }
            while (true)
            {
                list.Add(ParseValue(s, ref pos, depth + 1));
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
                if (c != '\\')
                {
                    // JSON forbids raw C0 controls inside a string; the escaped
                    // forms below stay valid. A lenient reader here would let a
                    // peer send a byte sequence Python refuses.
                    if (c < 0x20)
                        throw new JsonException("raw control character in string at index " + (pos - 1));
                    sb.Append(c);
                    continue;
                }
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
                        // Exactly four hex digits. NumberStyles.HexNumber, the
                        // old route through int.TryParse, also accepts leading
                        // and trailing whitespace, and Substring hid that.
                        if (pos + 4 > s.Length)
                            throw new JsonException("bad unicode escape");
                        int code = 0;
                        for (int i = 0; i < 4; i++)
                        {
                            int digit = HexDigit(s[pos + i]);
                            if (digit < 0)
                                throw new JsonException("bad unicode escape at index " + pos);
                            code = (code << 4) | digit;
                        }
                        sb.Append((char)code);
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("bad escape '\\" + e + "'");
                }
            }
        }

        private static int HexDigit(char c)
        {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return -1;
        }

        /// <summary>Reject a string with a UTF-16 surrogate that is not half of
        /// a valid pair; strict UTF-8 cannot carry one.</summary>
        private static void ValidateString(string s)
        {
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                if (char.IsHighSurrogate(c))
                {
                    if (i + 1 >= s.Length || !char.IsLowSurrogate(s[i + 1]))
                        throw new JsonException("lone surrogate in string");
                    i++;
                }
                else if (char.IsLowSurrogate(c))
                {
                    throw new JsonException("lone surrogate in string");
                }
            }
        }

        /// <summary>Reject a value tree a strict UTF-8 JSON peer cannot carry.
        /// The parser calls this after decoding; the frame encoder calls it
        /// before it writes, so both directions refuse the same headers.
        /// A valid high/low surrogate pair is one character (Python combines
        /// it too); either half on its own is a lone surrogate.</summary>
        internal static void ValidateTree(object value)
        {
            var text = value as string;
            if (text != null) { ValidateString(text); return; }
            if (value == null || value is bool || value is long || value is int) return;
            if (value is double)
            {
                if (double.IsNaN((double)value) || double.IsInfinity((double)value))
                    throw new JsonException("non-finite number");
                return;
            }
            if (value is float)
            {
                if (float.IsNaN((float)value) || float.IsInfinity((float)value))
                    throw new JsonException("non-finite number");
                return;
            }
            var obj = value as Dictionary<string, object>;
            if (obj != null)
            {
                foreach (KeyValuePair<string, object> entry in obj)
                {
                    ValidateString(entry.Key);
                    ValidateTree(entry.Value);
                }
                return;
            }
            var list = value as System.Collections.IList;
            if (list != null)
            {
                foreach (object item in list) ValidateTree(item);
                return;
            }
            // Json.WriteValue would throw the same typed error, but validation
            // has to happen before the version check to keep Python's order.
            throw new JsonException("cannot encode " + value.GetType().Name);
        }

        private static bool IsDigit(char c)
        {
            return c >= '0' && c <= '9';
        }

        /// <summary>One JSON number: -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?.
        /// The grammar is enforced here rather than left to double.TryParse:
        /// TryParse also takes "+2", "01" and "1.", which Python's json module
        /// rejects, and a peer must not be accepted on one side and refused on
        /// the other. An overflowing float is Python's JSON extension
        /// ``Infinity`` after parsing, but Q6 rejects that as non-finite, so
        /// this parser refuses it too: a header number is never unrepresentable.</summary>
        private static object ParseNumber(string s, ref int pos)
        {
            int start = pos;
            bool isFloat = false;
            if (pos < s.Length && s[pos] == '-') pos++;
            if (pos >= s.Length || !IsDigit(s[pos]))
                throw new JsonException("bad number at index " + start);
            if (s[pos] == '0') pos++;
            else while (pos < s.Length && IsDigit(s[pos])) pos++;
            if (pos < s.Length && s[pos] == '.')
            {
                isFloat = true;
                pos++;
                if (pos >= s.Length || !IsDigit(s[pos]))
                    throw new JsonException("bad number at index " + start);
                while (pos < s.Length && IsDigit(s[pos])) pos++;
            }
            if (pos < s.Length && (s[pos] == 'e' || s[pos] == 'E'))
            {
                isFloat = true;
                pos++;
                if (pos < s.Length && (s[pos] == '+' || s[pos] == '-')) pos++;
                if (pos >= s.Length || !IsDigit(s[pos]))
                    throw new JsonException("bad number at index " + start);
                while (pos < s.Length && IsDigit(s[pos])) pos++;
            }
            string token = s.Substring(start, pos - start);
            if (!isFloat)
            {
                long l;
                if (long.TryParse(token, NumberStyles.Integer, CultureInfo.InvariantCulture, out l))
                    return l;
                // Python's json decoder carries arbitrary-precision integers,
                // but every valid frame integer fits signed 64-bit. Falling
                // back to double would silently round a huge token; the
                // protocol refuses it instead (codec.py rejects the same
                // range).
                throw new JsonException("integer out of range at index " + start);
            }
            double value;
            // On .NET Framework and Mono an overflow may report failure rather
            // than infinity; both are non-finite for the protocol, so both take
            // the same classified path. Underflow to 0.0 is finite and fine.
            if (double.TryParse(token, NumberStyles.Float, CultureInfo.InvariantCulture, out value)
                && !double.IsNaN(value) && !double.IsInfinity(value))
                return value;
            throw new JsonException("non-finite number at index " + start);
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
            // Any IList, not only List<object>: the decoder produces List<object>,
            // but callers build typed lists (attribute descriptors, for example),
            // and "cannot encode List`1" is a runtime crash in a live canvas.
            var items = value as System.Collections.IList;
            if (items != null) { WriteArray(sb, items); return; }
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

        private static void WriteArray(StringBuilder sb, System.Collections.IList list)
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
            // A lone surrogate cannot be encoded as UTF-8; Encoding.UTF8 would
            // silently write U+FFFD, inventing a value no Python peer produced.
            ValidateString(s);
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

        /// <summary>Python's repr(float) text for every finite double.
        ///
        /// Mono's "R"/"G17"/"E16" are not a contract: they differ from Python
        /// at 1e15 (1E+15 vs 1000000000000000.0), lose -0.0's sign, and round
        /// some 16–17 digit values one unit too high. This formatter therefore
        /// works from the exact decimal expansion of the IEEE 754 value,
        /// rounds it to 1..17 significant digits, and accepts the first
        /// precision whose decimal rounds back to the same double. That is
        /// Python's shortest-round-trip rule; the notation then follows its
        /// thresholds (fixed for -4 <= exponent < 16, scientific otherwise),
        /// uses a lowercase `e` with at least two exponent digits, and keeps
        /// ".0" on integral fixed values.
        ///
        /// The round-trip check compares exact rationals against the midpoints
        /// between adjacent doubles, so it does not depend on the platform's
        /// decimal parser either. protocol/number-corpus.json holds the
        /// cross-language vector for this method.</summary>
        public static string FormatDouble(double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d))
                throw new JsonException("JSON cannot carry " + d);
            if (d == 0.0)
                return BitConverter.DoubleToInt64Bits(d) < 0 ? "-0.0" : "0.0";

            bool negative = d < 0.0;
            double magnitude = negative ? -d : d;

            BigInteger exactDigits;
            int exactExponent;
            ExactDecimalDigits(magnitude, out exactDigits, out exactExponent);
            int exactLength = exactDigits.ToString(CultureInfo.InvariantCulture).Length;

            BigInteger digits = exactDigits;
            int exponent = exactExponent;
            bool found = false;
            for (int precision = 1; precision <= 17; precision++)
            {
                int drop = exactLength > precision ? exactLength - precision : 0;
                BigInteger floor = exactDigits;
                BigInteger remainder = BigInteger.Zero;
                BigInteger half = BigInteger.Zero;
                if (drop > 0)
                {
                    BigInteger divisor = BigInteger.Pow(10, drop);
                    floor = BigInteger.DivRem(exactDigits, divisor, out remainder);
                    half = divisor / 2;
                }

                BigInteger lowerDigits;
                int lowerExponent;
                NormalizeDecimal(floor, exactExponent + drop,
                                 out lowerDigits, out lowerExponent);
                bool lowerWorks = RoundTripsTo(magnitude, lowerDigits, lowerExponent);

                // The nearest p-digit decimal is not always the one inside the
                // double's round-trip interval: an asymmetric interval can
                // exclude it while admitting the neighbour on the other side.
                // Test both and prefer the nearer one when both work.
                bool upperWorks = false;
                BigInteger upperDigits = BigInteger.Zero;
                int upperExponent = 0;
                if (drop > 0)
                {
                    NormalizeDecimal(floor + 1, exactExponent + drop,
                                     out upperDigits, out upperExponent);
                    upperWorks = RoundTripsTo(magnitude, upperDigits, upperExponent);
                }
                if (!lowerWorks && !upperWorks) continue;

                bool useUpper;
                if (lowerWorks && upperWorks)
                {
                    int compare = remainder.CompareTo(half);
                    if (compare < 0) useUpper = false;
                    else if (compare > 0) useUpper = true;
                    else useUpper = !floor.IsEven;  // round half to even
                }
                else
                {
                    useUpper = upperWorks;
                }
                digits = useUpper ? upperDigits : lowerDigits;
                exponent = useUpper ? upperExponent : lowerExponent;
                found = true;
                break;
            }

            // Seventeen significant decimal digits always identify a double, so
            // the loop cannot exhaust for a finite value. If it does, the
            // formatter has a bug: fail visibly instead of emitting text that
            // is valid JSON but not Python's repr.
            if (!found)
                throw new JsonException("cannot format " + d + " as Python repr");
            return FormatDecimalDigits(digits, exponent, negative);
        }

        /// <summary>The exact decimal significand of a positive finite double:
        /// value = digits * 10^exponent. A double is m * 2^e, so a negative e
        /// is turned into m * 5^-e with a decimal exponent of e.</summary>
        private static void ExactDecimalDigits(double d, out BigInteger digits, out int exponent)
        {
            long bits = BitConverter.DoubleToInt64Bits(d);
            int binaryExponent = (int)((bits >> 52) & 0x7FF);
            long fraction = bits & 0xFFFFFFFFFFFFFL;
            BigInteger significand;
            int scale;
            if (binaryExponent == 0)
            {
                significand = fraction;
                scale = -1074;
            }
            else
            {
                significand = fraction | (1L << 52);
                scale = binaryExponent - 1075;
            }
            if (scale >= 0)
            {
                digits = significand << scale;
                exponent = 0;
            }
            else
            {
                int power = -scale;
                digits = significand * BigInteger.Pow(5, power);
                exponent = -power;
            }
        }

        /// <summary>Strip decimal trailing zeros; the value is unchanged.</summary>
        private static void NormalizeDecimal(BigInteger value, int exponent,
                                             out BigInteger digits, out int resultExponent)
        {
            while (!value.IsZero && (value % 10).IsZero)
            {
                value /= 10;
                exponent++;
            }
            digits = value;
            resultExponent = exponent;
        }

        /// <summary>True when digits * 10^exponent is inside d's IEEE round-to-
        /// nearest interval. The boundaries are exact midpoints with the
        /// adjacent doubles; a midpoint goes to the even significand, which is
        /// d exactly when its low bit is clear.</summary>
        private static bool RoundTripsTo(double d, BigInteger digits, int exponent)
        {
            long bits = BitConverter.DoubleToInt64Bits(d);

            BigInteger lower;
            int lowerExponent;
            Midpoint(BitConverter.Int64BitsToDouble(bits - 1), d, out lower, out lowerExponent);
            int compare = CompareDecimalToDyadic(digits, exponent, lower, lowerExponent);
            if (compare < 0) return false;
            if (compare == 0 && (bits & 1L) != 0L) return false;

            long nextBits = bits + 1;
            BigInteger upper;
            int upperExponent;
            if (nextBits == 0x7FF0000000000000L)
                Midpoint(d, double.PositiveInfinity, out upper, out upperExponent);
            else
                Midpoint(d, BitConverter.Int64BitsToDouble(nextBits), out upper, out upperExponent);
            compare = CompareDecimalToDyadic(digits, exponent, upper, upperExponent);
            if (compare > 0) return false;
            if (compare == 0 && (bits & 1L) != 0L) return false;
            return true;
        }

        /// <summary>The exact midpoint of two positive doubles, as
        /// significand * 2^exponent. Positive infinity stands in for the value
        /// just past the largest finite double.</summary>
        private static void Midpoint(double a, double b,
                                     out BigInteger significand, out int exponent)
        {
            BigInteger aSignificand, bSignificand;
            int aExponent, bExponent;
            Dyadic(a, out aSignificand, out aExponent);
            Dyadic(b, out bSignificand, out bExponent);
            if (aExponent == bExponent)
            {
                significand = aSignificand + bSignificand;
                exponent = aExponent;
            }
            else if (aExponent > bExponent)
            {
                significand = (aSignificand << (aExponent - bExponent)) + bSignificand;
                exponent = bExponent;
            }
            else
            {
                significand = aSignificand + (bSignificand << (bExponent - aExponent));
                exponent = aExponent;
            }
            exponent -= 1;
        }

        /// <summary>A non-negative finite double (or positive infinity) as
        /// significand * 2^exponent, exactly.</summary>
        private static void Dyadic(double d, out BigInteger significand, out int exponent)
        {
            if (double.IsPositiveInfinity(d))
            {
                significand = BigInteger.One;
                exponent = 1024;
                return;
            }
            long bits = BitConverter.DoubleToInt64Bits(d);
            int binaryExponent = (int)((bits >> 52) & 0x7FF);
            long fraction = bits & 0xFFFFFFFFFFFFFL;
            if (binaryExponent == 0)
            {
                significand = fraction;
                exponent = -1074;
            }
            else
            {
                significand = fraction | (1L << 52);
                exponent = binaryExponent - 1075;
            }
        }

        /// <summary>Compare digits * 10^exponent with
        /// significand * 2^binaryExponent exactly, without a floating-point
        /// round trip.</summary>
        private static int CompareDecimalToDyadic(BigInteger digits, int exponent,
                                                  BigInteger significand, int binaryExponent)
        {
            BigInteger leftNumerator, leftDenominator, rightNumerator, rightDenominator;
            if (exponent >= 0)
            {
                leftNumerator = digits * BigInteger.Pow(10, exponent);
                leftDenominator = BigInteger.One;
            }
            else
            {
                leftNumerator = digits;
                leftDenominator = BigInteger.Pow(10, -exponent);
            }
            if (binaryExponent >= 0)
            {
                rightNumerator = significand << binaryExponent;
                rightDenominator = BigInteger.One;
            }
            else
            {
                rightNumerator = significand;
                rightDenominator = BigInteger.One << -binaryExponent;
            }
            return (leftNumerator * rightDenominator)
                .CompareTo(rightNumerator * leftDenominator);
        }

        /// <summary>Python's repr notation for a normalized decimal significand:
        /// fixed for a decimal point at -3..16, scientific outside that, with
        /// ".0" on integral fixed values and a two-digit minimum exponent.</summary>
        private static string FormatDecimalDigits(BigInteger digits, int exponent, bool negative)
        {
            string text = digits.ToString(CultureInfo.InvariantCulture);
            int decimalPoint = text.Length + exponent;
            string body;
            if (decimalPoint <= -4 || decimalPoint > 16)
            {
                body = text.Substring(0, 1);
                if (text.Length > 1) body += "." + text.Substring(1);
                int scientific = decimalPoint - 1;
                body += "e" + (scientific < 0 ? "-" : "+")
                    + Math.Abs(scientific).ToString("00", CultureInfo.InvariantCulture);
            }
            else if (decimalPoint <= 0)
            {
                body = "0." + new string('0', -decimalPoint) + text;
            }
            else if (decimalPoint >= text.Length)
            {
                body = text + new string('0', decimalPoint - text.Length) + ".0";
            }
            else
            {
                body = text.Substring(0, decimalPoint) + "." + text.Substring(decimalPoint);
            }
            return negative ? "-" + body : body;
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
