// Parameter value conversion, pure C#: no Grasshopper and no RhinoCommon, so the
// console tests can check it under wine (grasshopper/tests/console).
//
// The wire rules are protocol §4.3: floats as JSON numbers, ints as integers,
// bools as true/false, vectors and colours as arrays of three numbers, value
// lists as integers (the index), text as strings. The values arrive from Json.Parse, which produces
// List<object> for arrays — not double[] — which is exactly the mistake this
// module exists to avoid repeating.

using System;
using System.Collections.Generic;
using System.Globalization;

namespace Stageloom.Core
{
    public static class ParamValues
    {
        /// <summary>A JSON array of numbers as doubles, or null when the value is
        /// not an array of finite numbers.
        ///
        /// Only the numeric JSON representation counts: long, int and double.
        /// Convert.ToDouble also accepts "2.5", "true" and (on some runtimes)
        /// null, which would turn a malformed peer value into a vector; those
        /// are rejected here instead. A non-finite double is not JSON and is
        /// rejected for the same reason.</summary>
        public static double[] AsDoubles(object value)
        {
            var list = value as List<object>;
            if (list != null)
            {
                var outArray = new double[list.Count];
                for (int i = 0; i < list.Count; i++)
                {
                    double number;
                    if (!TryNumber(list[i], out number)) return null;
                    outArray[i] = number;
                }
                return outArray;
            }
            var array = value as double[];
            if (array != null)
            {
                foreach (double d in array)
                    if (!IsFinite(d)) return null;
                return array;
            }
            return null;
        }

        /// <summary>The default value for a parameter, parsed from its text form.
        ///
        /// Total like Cast: "NaN", "Infinity", an overflowing "1e400" or a
        /// value that does not fit the target's range returns the type's
        /// neutral value. The default travels in the schema header and a
        /// non-finite number cannot be encoded as JSON.</summary>
        public static object ParseDefault(string type, string text)
        {
            double number;
            if (type == "float")
                return TryFinite(text, out number) ? number : 0.0;
            if (type == "int")
            {
                if (!TryFinite(text, out number)) return 0L;
                double rounded = Math.Round(number);  // banker's, like Python's round()
                return FitsLong(rounded) ? (long)rounded : 0L;
            }
            if (type == "value_list")
            {
                if (!TryFinite(text, out number)) return 0L;
                double truncated = Math.Truncate(number);
                return FitsLong(truncated) ? (long)truncated : 0L;
            }
            if (type == "bool")
                return text != null && text.Trim().ToLowerInvariant() == "true";
            if (type == "vector3" || type == "color")
            {
                string[] parts = (text ?? "").Split(',');
                if (parts.Length == 3)
                {
                    double a, b, c;
                    if (TryFinite(parts[0], out a)
                        && TryFinite(parts[1], out b)
                        && TryFinite(parts[2], out c))
                        return new double[] { a, b, c };
                }
                return new double[] { 0.0, 0.0, 0.0 };
            }
            return text ?? "";
        }

        /// <summary>A CLR value as something the canonical JSON writer can carry.
        ///
        /// A non-finite number is replaced by its neutral (0.0, or zero in a
        /// vector) instead of reaching FormatDouble, where it would throw and
        /// abort schema publishing. Cast already returns only finite values;
        /// this is the second guard for a value that bypassed it.</summary>
        public static object JsonValue(object value)
        {
            if (value == null) return null;
            if (value is double) return IsFinite((double)value) ? value : 0.0;
            if (value is float)
            {
                double number = (float)value;
                return IsFinite(number) ? (object)number : 0.0;
            }
            if (value is long || value is int || value is bool || value is string) return value;
            var array = value as double[];
            if (array != null)
            {
                var list = new List<object>();
                foreach (double d in array) list.Add(IsFinite(d) ? (object)d : 0.0);
                return list;
            }
            var listValue = value as List<object>;
            if (listValue != null)
            {
                var sanitized = new List<object>();
                foreach (object item in listValue) sanitized.Add(JsonValue(item));
                return sanitized;
            }
            return value.ToString();
        }

        /// <summary>The value cast to its schema type: what the component outputs.
        ///
        /// Total by design: a schema change can race an in-flight request, so the
        /// value may belong to the previous type (a vector string arriving at a
        /// float parameter, say). Any conversion failure returns the type's
        /// neutral value instead of throwing into SolveInstance.</summary>
        public static object Cast(string type, object value)
        {
            try
            {
                if (type == "float")
                {
                    double number = Convert.ToDouble(value, CultureInfo.InvariantCulture);
                    return IsFinite(number) ? number : 0.0;
                }
                // Python's providers: int(round(float(value))) for `int`, plain
                // int(value) (truncation) for `value_list` (params.py:64-70).
                // Convert.ToInt32(double) is banker's rounding, as Python's
                // round() is; out-of-range input throws and lands on the type's
                // neutral value below, so no undefined cast survives.
                if (type == "int")
                {
                    double number = Convert.ToDouble(value, CultureInfo.InvariantCulture);
                    return IsFinite(number) ? Convert.ToInt32(number, CultureInfo.InvariantCulture) : 0;
                }
                if (type == "value_list")
                {
                    double number = Convert.ToDouble(value, CultureInfo.InvariantCulture);
                    return IsFinite(number)
                        ? Convert.ToInt32(Math.Truncate(number), CultureInfo.InvariantCulture)
                        : 0;
                }
                if (type == "bool") return Convert.ToBoolean(value, CultureInfo.InvariantCulture);
                if (type == "vector3" || type == "color")
                {
                    double[] array = AsDoubles(value);
                    if (array != null && array.Length == 3) return array;
                    return new double[] { 0.0, 0.0, 0.0 };
                }
                if (type == "text")
                {
                    if (value == null) return "";
                    if (value is string) return value;
                    // Python's str(): floats use repr (so 2.0 stays "2.0"),
                    // ints stay integral, bools are "True"/"False", and the
                    // non-finite spellings are lowercase.
                    if (value is bool) return (bool)value ? "True" : "False";
                    if (value is long) return ((long)value).ToString(CultureInfo.InvariantCulture);
                    if (value is int) return ((int)value).ToString(CultureInfo.InvariantCulture);
                    if (value is double) return FormatTextDouble((double)value);
                    if (value is float) return FormatTextDouble((float)value);
                    return "";  // a vector or a list is not text
                }
            }
            catch (Exception)
            {
                // fall through to the neutral value
            }
            return Neutral(type);
        }

        /// <summary>Python str(float) text for a text parameter. Finite values
        /// use Json's canonical repr; the non-finite spellings never reach
        /// JSON as numbers, because this branch produces a string.</summary>
        private static string FormatTextDouble(double value)
        {
            if (double.IsNaN(value)) return "nan";
            if (double.IsPositiveInfinity(value)) return "inf";
            if (double.IsNegativeInfinity(value)) return "-inf";
            return Json.FormatDouble(value);
        }

        /// <summary>A parsed double that JSON can carry.</summary>
        private static bool TryFinite(string text, out double number)
        {
            if (double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out number)
                && IsFinite(number))
                return true;
            number = 0.0;
            return false;
        }

        /// <summary>One JSON array item as a double: long, int or double only,
        /// and finite. Strings and bools are not numbers here.</summary>
        private static bool TryNumber(object value, out double number)
        {
            if (value is long)
            {
                number = (long)value;
                return true;
            }
            if (value is int)
            {
                number = (int)value;
                return true;
            }
            if (value is double)
            {
                double numberValue = (double)value;
                if (IsFinite(numberValue))
                {
                    number = numberValue;
                    return true;
                }
            }
            number = 0.0;
            return false;
        }

        private static bool IsFinite(double value)
        {
            return !double.IsNaN(value) && !double.IsInfinity(value);
        }

        /// <summary>The inclusive/exclusive bounds of a signed 64-bit integer.
        /// The upper bound is exclusive because 2^63 is not a long; using the
        /// exact power of two avoids the undefined result of casting an
        /// out-of-range double.</summary>
        private static bool FitsLong(double value)
        {
            return value >= -9223372036854775808.0 && value < 9223372036854775808.0;
        }

        /// <summary>The value a parameter has when nothing usable was given.</summary>
        public static object Neutral(string type)
        {
            if (type == "float") return 0.0;
            if (type == "int" || type == "value_list") return 0;
            if (type == "bool") return false;
            if (type == "vector3" || type == "color") return new double[] { 0.0, 0.0, 0.0 };
            return "";
        }
    }
}
