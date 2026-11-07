// Live Rhino check: Stage Out must encode an n-gon after a triangulated grid.
//
// RhinoCommon's Mesh/ngon types need a live Rhino process, so this cannot run
// in the wine console harness. Run it through Rhino's C# script editor, or via
// the Rhino MCP `run_csharp` tool, with the Stageloom .gha loaded.
//
// Expected on the fixed build:
//   PASS: 33 output faces, one pentagon
//
// The old code fails with NullReferenceException because it passed
// ngon[k] (boundary vertices) to GetNgonBoundary instead of FaceIndexList().

int nx = 4, ny = 4;
var mesh = new Rhino.Geometry.Mesh();
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

var fan = new System.Collections.Generic.List<int>();
for (int k = 1; k + 1 < 5; k++)
{
    int faceIndex = mesh.Faces.Count;
    mesh.Faces.AddFace(nb, nb + k, nb + k + 1);
    fan.Add(faceIndex);
}
mesh.Ngons.AddNgon(Rhino.Geometry.MeshNgon.Create(
    new int[] { nb, nb + 1, nb + 2, nb + 3, nb + 4 }, fan.ToArray()));

try
{
    var assembly = System.AppDomain.CurrentDomain.GetAssemblies()
        .FirstOrDefault(a => a.GetName().Name == "Stageloom");
    if (assembly == null)
    {
        System.Console.WriteLine("FAIL: Stageloom assembly is not loaded");
    }
    else
    {
        var conversion = assembly.GetType("Stageloom.Core.Conversion");
        var method = conversion.GetMethod("SectionsFromMesh");
        var sections = (System.Collections.IEnumerable)method.Invoke(
            null, new object[] { mesh, 1.0 });
        uint[] counts = null;
        foreach (var section in sections)
        {
            var type = section.GetType();
            if ((string)type.GetField("Name").GetValue(section) == "mesh.face_counts")
            {
                counts = (uint[])type.GetMethod("ToU32").Invoke(section, null);
            }
        }
        if (counts == null)
        {
            System.Console.WriteLine("FAIL: mesh.face_counts is missing");
        }
        else if (counts.Length != 33)
        {
            System.Console.WriteLine("FAIL: expected 33 output faces, got " + counts.Length);
        }
        else
        {
            int pentagons = 0;
            foreach (uint count in counts)
                if (count == 5) pentagons++;
            if (pentagons != 1)
                System.Console.WriteLine("FAIL: expected one pentagon, got " + pentagons);
            else
                System.Console.WriteLine("PASS: 33 output faces, one pentagon");
        }
    }
}
catch (System.Exception ex)
{
    var inner = ex.InnerException ?? ex;
    System.Console.WriteLine("FAIL: " + inner.GetType().Name + ": " + inner.Message);
}
