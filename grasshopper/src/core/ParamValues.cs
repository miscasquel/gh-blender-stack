// Parameter value conversion, pure C#: no Grasshopper and no RhinoCommon, so the
// console tests can check it under wine (grasshopper/tests/console).
//
// The wire rules are protocol §4.3: floats as JSON numbers, ints as integers,
// bools as true/false, vectors and colours as arrays of three numbers, choices
// as integers, text as strings. The values arrive from Json.Parse, which produces
// List<object> for arrays — not double[] — which is exactly the mistake this
// module exists to avoid repeating.

using System;
using System.Collections.Generic;
using System.Globalization;

namespace GhStack.Core
{
    public static class ParamValues
    {
        /// <summary>A JSON array of numbers as doubles, or null when the value is
        /// not an array of numbers.</summary>
        public static double[] AsDoubles(object value)
        {
            var list = value as List<object>;
            if (list != null)
            {
                var outArray = new double[list.Count];
                for (int i = 0; i < list.Count; i++)
                {
                    if (list[i] == null) return null;
                    outArray[i] = Convert.ToDouble(list[i], CultureInfo.InvariantCulture);
                }
                return outArray;
            }
            var array = value as double[];
            if (array != null) return array;
            return null;
        }

        /// <summary>The default value for a parameter, parsed from its text form.</summary>
        public static object ParseDefault(string type, string text)
        {
            double number;
            if (type == "float")
                return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out number) ? number : 0.0;
            if (type == "int" || type == "choice")
                return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out number) ? (long)number : 0L;
            if (type == "bool")
                return text != null && text.Trim().ToLowerInvariant() == "true";
            if (type == "vector3" || type == "color")
            {
                string[] parts = (text ?? "").Split(',');
                if (parts.Length == 3)
                {
                    double a, b, c;
                    if (double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out a)
                        && double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out b)
                        && double.TryParse(parts[2], NumberStyles.Float, CultureInfo.InvariantCulture, out c))
                        return new double[] { a, b, c };
                }
                return new double[] { 0.0, 0.0, 0.0 };
            }
            return text ?? "";
        }

        /// <summary>A CLR value as something the canonical JSON writer can carry.</summary>
        public static object JsonValue(object value)
        {
            if (value == null) return null;
            if (value is double || value is long || value is int || value is bool || value is string) return value;
            var array = value as double[];
            if (array != null)
            {
                var list = new List<object>();
                foreach (double d in array) list.Add(d);
                return list;
            }
            var listValue = value as List<object>;
            if (listValue != null) return listValue;
            return value.ToString();
        }

        /// <summary>The value cast to its schema type: what the component outputs.</summary>
        public static object Cast(string type, object value)
        {
            if (type == "float") return Convert.ToDouble(value, CultureInfo.InvariantCulture);
            if (type == "int" || type == "choice") return (int)Convert.ToInt64(value, CultureInfo.InvariantCulture);
            if (type == "bool") return Convert.ToBoolean(value, CultureInfo.InvariantCulture);
            if (type == "vector3" || type == "color")
            {
                double[] array = AsDoubles(value);
                if (array != null && array.Length == 3) return array;
                return new double[] { 0.0, 0.0, 0.0 };
            }
            return value == null ? "" : value.ToString();
        }
    }
}
