// Live Rhino check: Stage Out must encode an n-gon after a triangulated grid,
// and must not silently drop the hole of an n-gon that cannot travel as one
// wire polygon.
//
// RhinoCommon's Mesh/ngon types need a live Rhino process, so this cannot run
// in the wine console harness. Run it through Rhino's C# script editor, or via
// the Rhino MCP `run_csharp` tool, with the Stageloom .gha loaded.
//
// Expected on the fixed build:
//   PASS: n-gon encoding (pentagon boundary exact, holey n-gon flattened)
//
// The simple case guards the n-gon boundary: Stage Out must use
// FaceIndexList(), not the boundary vertex list, or faces above four sides
// throw NullReferenceException.
//
// The holey case is the L2 guard: a ring of four quads registered as one
// n-gon with an inner boundary must be sent as the underlying faces with an
// `ngon_holes` warning. The wire format carries one polygon per face_counts
// entry and cannot carry holes. Slits take the same RhinoCommon edge-count
// test (`NgonHasHoles`) and the same fan-face path.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Rhino.Geometry;

NgonCheckScript.Run();

public static class NgonCheckScript
{
    private static int _failures;

    public static void Run()
    {
        CheckSimplePentagon();
        CheckHoleyNgon();
        if (_failures == 0)
            Console.WriteLine("PASS: n-gon encoding (pentagon boundary exact, holey n-gon flattened)");
        else
            Console.WriteLine("FAIL: " + _failures + " n-gon check(s)");
    }

    private static void CheckSimplePentagon()
    {
        int nx = 4, ny = 4;
        var mesh = new Mesh();
        for (int y = 0; y <= ny; y++)
            for (int x = 0; x <= nx; x++)
                mesh.Vertices.Add(x * 0.1, y * 0.1, 0);

        for (int y = 0; y < ny; y++)
        {
            for (int x = 0; x < nx; x++)
            {
                int a = y * (nx + 1) + x;
                int b = a + 1;
                int c = a + nx + 1;
                int d = c + 1;
                mesh.Faces.AddFace(a, b, d);
                mesh.Faces.AddFace(a, d, c);
            }
        }

        int nb = mesh.Vertices.Count;
        mesh.Vertices.Add(10, 0, 0);
        mesh.Vertices.Add(11, 0, 0);
        mesh.Vertices.Add(11, 1, 0);
        mesh.Vertices.Add(10.5, 1.5, 0);
        mesh.Vertices.Add(10, 1, 0);

        var fan = new List<int>();
        for (int k = 1; k + 1 < 5; k++)
        {
            int faceIndex = mesh.Faces.Count;
            mesh.Faces.AddFace(nb, nb + k, nb + k + 1);
            fan.Add(faceIndex);
        }
        mesh.Ngons.AddNgon(MeshNgon.Create(
            new int[] { nb, nb + 1, nb + 2, nb + 3, nb + 4 }, fan.ToArray()));

        Assembly assembly = Stageloom();
        if (assembly == null) return;

        object sections = InvokeSections(assembly, new object[] { mesh, 1.0 });
        uint[] counts = SectionU32(sections, "mesh.face_counts");
        uint[] indices = SectionU32(sections, "mesh.face_indices");
        if (counts == null || indices == null) return;

        int triangles = 0;
        foreach (uint count in counts)
            if (count == 3) triangles++;

        // 32 grid triangles plus the one pentagon; the three fan triangles must
        // not leak into the output beside it.
        if (counts.Length != 33)
            Fail("simple: expected 33 output faces, got " + counts.Length);
        else if (triangles != 32)
            Fail("simple: expected 32 triangles, got " + triangles + " (fan leakage?)");
        else if (counts[counts.Length - 1] != 5)
            Fail("simple: last face is not the pentagon");
        else
        {
            int at = indices.Length - 5;
            bool exact = indices.Length == 101
                && indices[at] == nb
                && indices[at + 1] == nb + 1
                && indices[at + 2] == nb + 2
                && indices[at + 3] == nb + 3
                && indices[at + 4] == nb + 4;
            if (!exact)
                Fail("simple: pentagon boundary is " + Join(indices, at, 5)
                    + ", expected " + nb + "," + (nb + 1) + "," + (nb + 2) + ","
                    + (nb + 3) + "," + (nb + 4) + " and 101 indices");
        }
    }

    private static void CheckHoleyNgon()
    {
        // A square ring of four quads; the inner square is a hole.
        var mesh = new Mesh();
        mesh.Vertices.Add(0, 0, 0);
        mesh.Vertices.Add(4, 0, 0);
        mesh.Vertices.Add(4, 4, 0);
        mesh.Vertices.Add(0, 4, 0);
        mesh.Vertices.Add(1, 1, 0);
        mesh.Vertices.Add(3, 1, 0);
        mesh.Vertices.Add(3, 3, 0);
        mesh.Vertices.Add(1, 3, 0);
        mesh.Faces.AddFace(0, 1, 5, 4);
        mesh.Faces.AddFace(1, 2, 6, 5);
        mesh.Faces.AddFace(2, 3, 7, 6);
        mesh.Faces.AddFace(3, 0, 4, 7);
        var fan = new List<int> { 0, 1, 2, 3 };
        mesh.Ngons.AddNgon(MeshNgon.Create(new int[] { 0, 1, 2, 3 }, fan));

        Assembly assembly = Stageloom();
        if (assembly == null) return;

        Type warningType = assembly.GetType("Stageloom.Core.WireWarning");
        if (warningType == null)
        {
            Fail("holey: Stageloom.Core.WireWarning is missing");
            return;
        }
        Type warningListType = typeof(List<>).MakeGenericType(warningType);
        object warnings = Activator.CreateInstance(warningListType);

        object sections = InvokeSections(assembly,
            new object[] { mesh, 1.0, warnings });
        uint[] counts = SectionU32(sections, "mesh.face_counts");
        uint[] indices = SectionU32(sections, "mesh.face_indices");
        if (counts == null || indices == null) return;

        // The four ring quads are sent unchanged, in mesh face order.
        uint[] expected = new uint[]
        {
            0, 1, 5, 4,
            1, 2, 6, 5,
            2, 3, 7, 6,
            3, 0, 4, 7
        };
        if (counts.Length != 4 || indices.Length != expected.Length)
            Fail("holey: expected 4 quads / " + expected.Length + " indices, got "
                + counts.Length + " faces / " + indices.Length + " indices");
        else
        {
            bool exact = true;
            for (int i = 0; i < counts.Length; i++)
                exact = exact && counts[i] == 4;
            for (int i = 0; i < expected.Length; i++)
                exact = exact && indices[i] == expected[i];
            if (!exact)
                Fail("holey: fan faces changed; counts=" + Join(counts, 0, counts.Length)
                    + " indices=" + Join(indices, 0, indices.Length));
        }

        bool warned = false;
        foreach (object warning in (System.Collections.IEnumerable)warnings)
        {
            string code = (string)warning.GetType().GetField("Code").GetValue(warning);
            if (code == "ngon_holes") warned = true;
        }
        if (!warned)
            Fail("holey: expected an ngon_holes warning");

        // Without a warning channel the same mesh must fail loudly, not lose
        // the hole silently.
        try
        {
            InvokeSections(assembly, new object[] { mesh, 1.0 });
            Fail("holey: two-argument SectionsFromMesh did not reject the hole");
        }
        catch (TargetInvocationException ex)
        {
            Exception inner = ex.InnerException ?? ex;
            string message = inner.Message ?? "";
            if (inner.GetType().Name != "FrameException"
                || message.IndexOf("holes", StringComparison.OrdinalIgnoreCase) < 0)
                Fail("holey: expected FrameException about holes, got "
                    + inner.GetType().Name + ": " + message);
        }
    }

    // ------------------------------------------------------------- reflection

    private static Assembly Stageloom()
    {
        Assembly assembly = AppDomain.CurrentDomain.GetAssemblies()
            .FirstOrDefault(a => a.GetName().Name == "Stageloom");
        if (assembly == null) Fail("Stageloom assembly is not loaded");
        return assembly;
    }

    private static object InvokeSections(Assembly assembly, object[] args)
    {
        Type conversion = assembly.GetType("Stageloom.Core.Conversion");
        MethodInfo method = conversion == null ? null : conversion.GetMethods()
            .FirstOrDefault(m => m.Name == "SectionsFromMesh"
                && m.GetParameters().Length == args.Length);
        if (method == null)
        {
            Fail("SectionsFromMesh with " + args.Length + " arguments is missing");
            return null;
        }
        return method.Invoke(null, args);
    }

    private static uint[] SectionU32(object sections, string name)
    {
        if (sections == null) return null;
        foreach (object section in (System.Collections.IEnumerable)sections)
        {
            Type type = section.GetType();
            if ((string)type.GetField("Name").GetValue(section) == name)
                return (uint[])type.GetMethod("ToU32").Invoke(section, null);
        }
        Fail(name + " is missing");
        return null;
    }

    private static string Join(uint[] values, int at, int count)
    {
        var parts = new List<string>();
        for (int i = 0; i < count && at + i < values.Length; i++)
            parts.Add(values[at + i].ToString());
        return string.Join(",", parts.ToArray());
    }

    private static void Fail(string message)
    {
        _failures++;
        Console.WriteLine("FAIL: " + message);
    }
}
