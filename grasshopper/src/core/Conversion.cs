// Protocol sections <-> Rhino geometry. RhinoCommon only; no Grasshopper and no
// bpy. The twin of blender/gh_stack/blender/geometry_io.py.
//
// S3 scope: meshes with n-gons and vertex colours. SubD cages and curve
// approximation follow in S4; the section names for those are not touched here.
//
// Units: the wire is meters (protocol §8.3), so both directions take the document
// scale (document units per meter). The caller gets it from
// RhinoMath.UnitScale(UnitSystem.Meters, doc.ModelUnitSystem).
//
// Timings in the reply prove where the cost goes: decode+mesh and mesh+sections
// are measured separately by the caller (the S3 spike), not here.

using System;
using System.Collections.Generic;
using System.Drawing;
using Rhino.Geometry;

namespace GhStack.Core
{
    public static class Conversion
    {
        // ------------------------------------------------------- sections -> mesh

        /// <summary>Build a Rhino mesh from mesh.* or subd.* sections.
        /// Returns null when the frame carries no mesh.</summary>
        public static Mesh MeshFromSections(DecodedFrame frame, double scale)
        {
            Section verts = frame.Find("mesh.verts") ?? frame.Find("subd.verts");
            Section counts = frame.Find("mesh.face_counts") ?? frame.Find("subd.face_counts");
            Section indices = frame.Find("mesh.face_indices") ?? frame.Find("subd.face_indices");
            if (verts == null || counts == null || indices == null) return null;

            var mesh = new Mesh();
            float[] positions = verts.ToFloats();
            // Vertices.AddVertices(IEnumerable<Point3f>) does not resolve with the
            // Mono profile mcs compiles against (its metadata mentions
            // IReadOnlyList, absent from the 4.0-api mscorlib mcs loads), so the
            // loop is deliberate. S3 measures what this actually costs.
            for (int i = 0; i < positions.Length; i += 3)
            {
                mesh.Vertices.Add(
                    (float)(positions[i] * scale),
                    (float)(positions[i + 1] * scale),
                    (float)(positions[i + 2] * scale));
            }

            AddFaces(mesh, counts.ToU32(), indices.ToU32());

            Section colors = frame.Find("mesh.colors");
            if (colors != null) SetMeshColors(mesh, colors);

            mesh.Normals.ComputeNormals();
            mesh.Compact();
            return mesh;
        }

        private static void AddFaces(Mesh mesh, uint[] counts, uint[] indices)
        {
            int at = 0;
            for (int f = 0; f < counts.Length; f++)
            {
                int n = (int)counts[f];
                if (n == 3)
                {
                    mesh.Faces.AddFace((int)indices[at], (int)indices[at + 1], (int)indices[at + 2]);
                }
                else if (n == 4)
                {
                    mesh.Faces.AddFace((int)indices[at], (int)indices[at + 1],
                                       (int)indices[at + 2], (int)indices[at + 3]);
                }
                else if (n > 4)
                {
                    // Rhino keeps n-gons as a boundary over triangles. Fan the
                    // boundary, remember those face indexes, then register the
                    // ngon over them.
                    var boundary = new List<int>();
                    var fan = new List<int>();
                    for (int k = 0; k < n; k++) boundary.Add((int)indices[at + k]);
                    for (int k = 1; k + 1 < n; k++)
                    {
                        int faceIndex = mesh.Faces.Count;
                        mesh.Faces.AddFace(boundary[0], boundary[k], boundary[k + 1]);
                        fan.Add(faceIndex);
                    }
                    MeshNgon ngon = MeshNgon.Create(boundary, fan);
                    mesh.Ngons.AddNgon(ngon);
                }
                else
                {
                    throw new FrameException("bad_frame", "face with " + n + " vertices");
                }
                at += n;
            }
        }

        private static void SetMeshColors(Mesh mesh, Section colors)
        {
            byte[] rgba = colors.Raw;
            int count = rgba.Length / 4;
            if (count != mesh.Vertices.Count)
                throw new FrameException("bad_frame",
                    "mesh.colors has " + count + " entries for " + mesh.Vertices.Count + " vertices");
            var list = new Color[count];
            for (int i = 0; i < count; i++)
            {
                // The wire is RGBA bytes; System.Drawing.Color is ARGB.
                list[i] = Color.FromArgb(rgba[i * 4 + 3], rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2]);
            }
            mesh.VertexColors.SetColors(list);
        }

        // ------------------------------------------------------- mesh -> sections

        /// <summary>Encode a Rhino mesh as mesh.* sections. Vertex colours are
        /// included only when the mesh has one colour per vertex.</summary>
        public static List<Section> SectionsFromMesh(Mesh mesh, double scale)
        {
            var sections = new List<Section>();

            Point3f[] points = mesh.Vertices.ToPoint3fArray();
            var positions = new float[points.Length * 3];
            for (int i = 0; i < points.Length; i++)
            {
                positions[i * 3] = (float)(points[i].X / scale);
                positions[i * 3 + 1] = (float)(points[i].Y / scale);
                positions[i * 3 + 2] = (float)(points[i].Z / scale);
            }
            sections.Add(Section.Floats("mesh.verts", positions));

            var counts = new List<uint>();
            var indices = new List<uint>();
            bool[] inNgon = NgonFaceMask(mesh);
            for (int f = 0; f < mesh.Faces.Count; f++)
            {
                if (inNgon[f]) continue;
                MeshFace face = mesh.Faces[f];
                counts.Add(face.IsQuad ? 4u : 3u);
                indices.Add((uint)face.A);
                indices.Add((uint)face.B);
                indices.Add((uint)face.C);
                if (face.IsQuad) indices.Add((uint)face.D);
            }
            for (int i = 0; i < mesh.Ngons.Count; i++)
            {
                MeshNgon ngon = mesh.Ngons.GetNgon(i);
                var faceList = new List<int>();
                for (int k = 0; k < ngon.FaceCount; k++) faceList.Add(ngon[k]);
                int[] boundary = mesh.Ngons.GetNgonBoundary(faceList);
                counts.Add((uint)boundary.Length);
                foreach (int v in boundary) indices.Add((uint)v);
            }
            sections.Add(Section.U32("mesh.face_counts", counts.ToArray()));
            sections.Add(Section.U32("mesh.face_indices", indices.ToArray()));

            if (mesh.VertexColors.Count == mesh.Vertices.Count && mesh.Vertices.Count > 0)
            {
                var rgba = new byte[mesh.Vertices.Count * 4];
                for (int i = 0; i < mesh.Vertices.Count; i++)
                {
                    Color c = mesh.VertexColors[i];
                    rgba[i * 4] = c.R;
                    rgba[i * 4 + 1] = c.G;
                    rgba[i * 4 + 2] = c.B;
                    rgba[i * 4 + 3] = c.A;
                }
                sections.Add(Section.Bytes("mesh.colors", rgba));
            }

            return sections;
        }

        private static bool[] NgonFaceMask(Mesh mesh)
        {
            var mask = new bool[mesh.Faces.Count];
            for (int i = 0; i < mesh.Ngons.Count; i++)
            {
                MeshNgon ngon = mesh.Ngons.GetNgon(i);
                int count = ngon.FaceCount;
                for (int k = 0; k < count; k++) mask[ngon[k]] = true;
            }
            return mask;
        }
    }
}
