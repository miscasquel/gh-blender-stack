// Live Rhino check: document units <-> wire metres for every supported unit.
//
// RhinoCommon's Mesh/Curve/SubD types need a live Rhino process, so this cannot
// run in the wine console harness (same rule as NgonCheck.cs). Run it through
// Rhino's C# script editor, or through the Rhino MCP `run_csharp` tool, with
// the Stageloom .gha loaded.
//
// Expected on a correct build:
//   PASS: units matrix (mm/cm/m/in/ft, mesh/curve/points, block placements)
//
// The block half is the one this check was written to settle: geometry is
// scaled in every direction, but a placement transform is a length too. A block
// whose definition is converted (1000x in a millimetre document) and whose
// translation is not lands at the origin instead of its model position. The
// Blender tests cannot see this because Blender has no document unit system.
//
// Stage In's row reader is exercised directly, without a Grasshopper solve:
// Instances.ReadRows() is the code that turns wire rows into Rhino transforms,
// and the def->row relationship it returns is what DefinitionBuilder places.
// The shared TransformFromWire/TransformToWire helpers are also covered
// headlessly in ComponentTests; Emit's writer uses the same helper.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Rhino.Geometry;

UnitsCheckScript.Run();

public static class UnitsCheckScript
{
    private static int _failures;
    private static readonly double[] Scales =
    {
        1000.0,                 // millimetres
        100.0,                  // centimetres
        1.0,                    // metres
        39.37007874015748,      // inches per metre
        3.280839895013123,      // feet per metre
    };
    private static readonly string[] Names = { "mm", "cm", "m", "in", "ft" };

    public static void Run()
    {
        Assembly assembly = Stageloom();
        if (assembly == null) return;

        for (int i = 0; i < Scales.Length; i++)
            CheckGeometry(assembly, Names[i], Scales[i]);

        CheckBlockRows(assembly);

        if (_failures == 0)
            Console.WriteLine("PASS: units matrix (mm/cm/m/in/ft, mesh/curve/points, block placements)");
        else
            Console.WriteLine("FAIL: " + _failures + " unit check(s)");
    }

    // ------------------------------------------------------------- geometry

    private static void CheckGeometry(Assembly assembly, string unit, double scale)
    {
        // A quad in document units; its wire form must be metres.
        var mesh = new Mesh();
        mesh.Vertices.Add(0, 0, 0);
        mesh.Vertices.Add(2 * scale, 0, 0);
        mesh.Vertices.Add(2 * scale, 3 * scale, 0);
        mesh.Vertices.Add(0, 3 * scale, 0);
        mesh.Faces.AddFace(0, 1, 2, 3);

        object meshSections = Invoke(assembly, "Stageloom.Core.Conversion", "SectionsFromMesh",
            new object[] { mesh, scale });
        object meshFrame = Frame(assembly, meshSections);

        // Reflection does not fill optional parameters: pass the empty prefix.
        object docMesh = Invoke(assembly, "Stageloom.Core.Conversion", "MeshFromSections",
            new object[] { meshFrame, scale, "" });
        object wireMesh = Invoke(assembly, "Stageloom.Core.Conversion", "MeshFromSections",
            new object[] { meshFrame, 1.0, "" });
        Check(unit + ": mesh returns to document units",
            Near(VertexX(docMesh, 2), 2 * scale), "x=" + VertexX(docMesh, 2));
        Check(unit + ": mesh wire is metres",
            Near(VertexX(wireMesh, 2), 2.0), "x=" + VertexX(wireMesh, 2));

        // A polyline.
        var curve = new PolylineCurve(new[]
        {
            new Point3d(0, 0, 0),
            new Point3d(2 * scale, 0, 0),
            new Point3d(2 * scale, 3 * scale, 0),
        });
        var curves = new List<Curve> { curve };
        var warnings = new List<string>();
        object[] curveArgs = { curves, scale, warnings, 0 };
        object curveSections = Invoke(assembly, "Stageloom.Core.Conversion", "SectionsFromCurves",
            curveArgs);
        object curveFrame = Frame(assembly, curveSections);
        object backCurves = Invoke(assembly, "Stageloom.Core.Conversion", "CurvesFromSections",
            new object[] { curveFrame, scale, "" });
        Check(unit + ": curve returns to document units",
            Near(CurveX(backCurves, 2), 2 * scale), "x=" + CurveX(backCurves, 2));

        // Points.
        var points = new List<Point3d>
        {
            new Point3d(scale, 2 * scale, 3 * scale),
            new Point3d(4 * scale, 5 * scale, 6 * scale),
        };
        object pointSections = Invoke(assembly, "Stageloom.Core.Conversion", "SectionsFromPoints",
            new object[] { points, scale });
        object pointFrame = Frame(assembly, pointSections);
        object backPoints = Invoke(assembly, "Stageloom.Core.Conversion", "PointsFromSections",
            new object[] { pointFrame, scale });
        Point3d first = FirstPoint(backPoints);
        Check(unit + ": points return to document units",
            Near(first.X, scale) && Near(first.Y, 2 * scale) && Near(first.Z, 3 * scale),
            first.ToString());
    }

    // --------------------------------------------------------------- blocks

    private static void CheckBlockRows(Assembly assembly)
    {
        // The wire says: definition 0 placed at (5, 4, 3) metres.
        Type sectionType = assembly.GetType("Stageloom.Core.Section");
        if (sectionType == null)
        {
            Fail("Stageloom.Core.Section is missing");
            return;
        }
        object refs = sectionType.GetMethod("U32", new[] { typeof(string), typeof(uint[]) })
            .Invoke(null, new object[] { "inst.ref_def", new uint[] { 0 } });
        object transforms = sectionType.GetMethod("Floats", new[] { typeof(string), typeof(float[]) })
            .Invoke(null, new object[]
            {
                "inst.transforms",
                new float[]
                {
                    1, 0, 0, 5,
                    0, 1, 0, 4,
                    0, 0, 1, 3,
                    0, 0, 0, 1,
                }
            });
        object frame = Frame(assembly, new List<object> { refs, transforms });

        Type instances = assembly.GetType("Stageloom.Instances");
        MethodInfo readRows = instances == null ? null : instances.GetMethods()
            .FirstOrDefault(m => m.Name == "ReadRows");
        if (readRows == null)
        {
            Fail("Instances.ReadRows is missing");
            return;
        }

        object[] args = { frame, "", 1000.0, null, null };
        readRows.Invoke(null, args);
        IList matrices = (IList)args[4];
        double actual = matrices == null || matrices.Count == 0
            ? double.NaN
            : ((Transform)matrices[0])[0, 3];
        Check("blocks: a 5 m wire placement becomes 5000 mm",
            Near(actual, 5000.0),
            "translation x=" + actual.ToString("0.###") + " (expected 5000)");
    }

    // ---------------------------------------------------------- reflection

    private static Assembly Stageloom()
    {
        Assembly assembly = AppDomain.CurrentDomain.GetAssemblies()
            .FirstOrDefault(a => a.GetName().Name == "Stageloom");
        if (assembly == null) Fail("Stageloom assembly is not loaded");
        return assembly;
    }

    private static object Invoke(Assembly assembly, string typeName, string methodName, object[] args)
    {
        Type type = assembly.GetType(typeName);
        MethodInfo method = type == null ? null : type.GetMethods()
            .FirstOrDefault(m => m.Name == methodName
                && m.GetParameters().Length == args.Length
                && ParametersMatch(m.GetParameters(), args));
        if (method == null)
        {
            Fail(typeName + "." + methodName + " with " + args.Length + " arguments is missing");
            return null;
        }
        try
        {
            return method.Invoke(null, args);
        }
        catch (TargetInvocationException ex)
        {
            Exception inner = ex.InnerException ?? ex;
            Fail(typeName + "." + methodName + ": " + inner.GetType().Name + ": " + inner.Message);
            return null;
        }
    }

    private static object Frame(Assembly assembly, object sections)
    {
        Type frameType = assembly.GetType("Stageloom.Core.DecodedFrame");
        Type sectionType = assembly.GetType("Stageloom.Core.Section");
        if (frameType == null || sectionType == null)
        {
            Fail("the codec frame types are missing");
            return null;
        }
        Type listType = typeof(List<>).MakeGenericType(sectionType);
        object list = Activator.CreateInstance(listType);
        if (sections is IEnumerable enumerable)
            foreach (object section in enumerable) ((IList)list).Add(section);
        else
            ((IList)list).Add(sections);
        return frameType.GetMethod("FromSections").Invoke(null, new object[]
        {
            new Dictionary<string, object>(), list,
        });
    }

    private static bool ParametersMatch(ParameterInfo[] parameters, object[] args)
    {
        for (int i = 0; i < parameters.Length; i++)
        {
            if (parameters[i].ParameterType.IsByRef) continue;
            if (args[i] == null)
            {
                if (parameters[i].ParameterType.IsValueType) return false;
                continue;
            }
            if (!parameters[i].ParameterType.IsInstanceOfType(args[i])) return false;
        }
        return true;
    }

    private static double VertexX(object mesh, int index)
    {
        Mesh value = mesh as Mesh;
        return value == null || value.Vertices.Count <= index ? double.NaN : value.Vertices[index].X;
    }

    private static double CurveX(object curves, int index)
    {
        var list = curves as IList;
        if (list == null || list.Count == 0) return double.NaN;
        Curve curve = list[0] as Curve;
        Polyline polyline;
        if (curve != null && curve.TryGetPolyline(out polyline) && polyline.Count > index)
            return polyline[index].X;
        return double.NaN;
    }

    private static Point3d FirstPoint(object points)
    {
        var list = points as IList;
        return list == null || list.Count == 0 ? new Point3d() : (Point3d)list[0];
    }

    private static bool Near(double value, double expected)
    {
        double tolerance = Math.Max(1e-5, Math.Abs(expected) * 1e-5);
        return Math.Abs(value - expected) <= tolerance;
    }

    private static void Check(string what, bool ok, string detail)
    {
        if (ok)
        {
            Console.WriteLine("PASS: " + what);
            return;
        }
        _failures++;
        Console.WriteLine("FAIL: " + what + " (" + detail + ")");
    }

    private static void Fail(string message)
    {
        _failures++;
        Console.WriteLine("FAIL: " + message);
    }
}
