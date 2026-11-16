// Protocol sections <-> Rhino geometry. RhinoCommon only; no Grasshopper and no
// bpy. The twin of blender/stageloom/blender/geometry_io.py.
//
// Meshes, SubD cages, curves, points and Breps (meshed, warned). The wire is
// metres; both directions take the document scale. Curves travel exactly when
// Blender's knots are representable, otherwise as Bezier spans; every loss is
// reported through the warnings list.

using System;
using System.Collections.Generic;
using System.Drawing;
using Rhino;
using Rhino.Geometry;

namespace Stageloom.Core
{
    /// <summary>One conversion loss or approximation (protocol §4.4): the
    /// documented code plus the human-readable message. The channel keeps this
    /// shape rather than bare strings so a response header carries the
    /// per-cause code the fixture pins (`curve_approximated`), not one blanket
    /// "conversion". The text-only overloads of the conversion entry
    /// points stay for callers that just print a line.</summary>
    public sealed class WireWarning
    {
        public readonly string Code;
        public readonly string Message;

        public WireWarning(string code, string message)
        {
            Code = code;
            Message = message;
        }
    }

    /// <summary>The warning codes the converter emits (protocol §4.4).</summary>
    public static class WarningCodes
    {
        public const string CurveApproximated = "curve_approximated";
        public const string CurveDeviation = "curve_deviation";
        public const string CurveSampled = "curve_sampled";
        public const string CurveSkipped = "curve_skipped";
        public const string SubdCreases = "subd_creases";
        public const string SubdJoined = "subd_joined";
        public const string BrepMeshed = "brep_meshed";
        public const string InstanceSkipped = "instance_skipped";
        public const string NgonHoles = "ngon_holes";
    }

    public static class Conversion
    {
        // ------------------------------------------------------- sections -> mesh

        /// <summary>Build a Rhino mesh from mesh.* or subd.* sections.
        /// Returns null when the frame carries no mesh. A frame may carry both
        /// groups; the complete mesh group wins here, while
        /// <see cref="SubdCageFromSections"/> stays subd-only.
        /// ``prefix`` namespaces a definition group (``idef.&lt;i&gt;.``,
        /// protocol §5.2).</summary>
        public static Mesh MeshFromSections(DecodedFrame frame, double scale, string prefix = "")
        {
            MeshGroup group = SelectMeshGroup(frame, prefix);
            if (group == null) return null;
            return BuildMesh(group, scale);
        }

        /// <summary>One complete mesh or subd cage group, already chosen as a
        /// unit. A section set is never assembled from both kinds.</summary>
        internal sealed class MeshGroup
        {
            public readonly string Kind;
            public readonly Section Verts;
            public readonly Section Counts;
            public readonly Section Indices;
            public readonly Section Colors;

            public MeshGroup(string kind, Section verts, Section counts, Section indices,
                             Section colors)
            {
                Kind = kind;
                Verts = verts;
                Counts = counts;
                Indices = indices;
                Colors = colors;
            }
        }

        /// <summary>Select one complete topology group. If mesh.* and subd.*
        /// are both complete the mesh group wins; a partial group is rejected
        /// even when the other group is complete. ``prefix`` namespaces a
        /// definition group (protocol §5.2).</summary>
        internal static MeshGroup SelectMeshGroup(DecodedFrame frame, string prefix)
        {
            Section mverts = frame.Find(prefix + "mesh.verts");
            Section mcounts = frame.Find(prefix + "mesh.face_counts");
            Section mindices = frame.Find(prefix + "mesh.face_indices");
            Section mcolors = frame.Find(prefix + "mesh.colors");
            Section sverts = frame.Find(prefix + "subd.verts");
            Section scounts = frame.Find(prefix + "subd.face_counts");
            Section sindices = frame.Find(prefix + "subd.face_indices");

            bool meshAny = mverts != null || mcounts != null || mindices != null || mcolors != null;
            bool meshComplete = mverts != null && mcounts != null && mindices != null;
            bool subdAny = sverts != null || scounts != null || sindices != null;
            bool subdComplete = sverts != null && scounts != null && sindices != null;

            // A half group is not completed from the other kind: the two
            // topology sets describe different geometry and their indices are
            // not interchangeable.
            if (meshAny && !meshComplete)
                throw new FrameException("bad_frame", "mesh group is incomplete: "
                    + MissingSections(mverts, mcounts, mindices, prefix + "mesh."));
            if (subdAny && !subdComplete)
                throw new FrameException("bad_frame", "subd group is incomplete: "
                    + MissingSections(sverts, scounts, sindices, prefix + "subd."));

            if (meshComplete) return new MeshGroup("mesh", mverts, mcounts, mindices, mcolors);
            if (subdComplete) return new MeshGroup("subd", sverts, scounts, sindices, null);
            return null;
        }

        internal static MeshGroup SelectSubdGroup(DecodedFrame frame)
        {
            Section verts = frame.Find("subd.verts");
            Section counts = frame.Find("subd.face_counts");
            Section indices = frame.Find("subd.face_indices");
            if (verts == null && counts == null && indices == null) return null;
            if (verts == null || counts == null || indices == null)
                throw new FrameException("bad_frame", "subd group is incomplete: "
                    + MissingSections(verts, counts, indices, "subd."));
            return new MeshGroup("subd", verts, counts, indices, null);
        }

        private static string MissingSections(Section verts, Section counts, Section indices, string prefix)
        {
            var missing = new List<string>();
            if (verts == null) missing.Add(prefix + "verts");
            if (counts == null) missing.Add(prefix + "face_counts");
            if (indices == null) missing.Add(prefix + "face_indices");
            return string.Join(", ", missing.ToArray());
        }

        /// <summary>The cage of a subd.* group, never falling back to mesh.*:
        /// a frame may carry both groups, and the SubD's crease pairs index
        /// its own cage, not the mesh beside it.</summary>
        private static Mesh SubdCageFromSections(DecodedFrame frame, double scale)
        {
            MeshGroup group = SelectSubdGroup(frame);
            if (group == null) return null;
            return BuildMesh(group, scale);
        }

        /// <summary>A validated mesh group in wire coordinates. The face table
        /// is produced only by <see cref="ReadMeshData"/>; the encoder writes
        /// the same shape back.</summary>
        internal sealed class MeshData
        {
            public readonly float[] Positions;
            public readonly int[] FaceSizes;
            public readonly int[] FaceIndices;
            public readonly byte[] Colors;

            public MeshData(float[] positions, int[] faceSizes, int[] faceIndices, byte[] colors)
            {
                Positions = positions;
                FaceSizes = faceSizes;
                FaceIndices = faceIndices;
                Colors = colors;
            }
        }

        /// <summary>Validate every mesh section and return wire-level data
        /// before a Mesh is allocated or any vertex/face index is used. The
        /// result is empty only when the sections describe an empty mesh.</summary>
        internal static MeshData ReadMeshData(MeshGroup group)
        {
            float[] positions = ReadFloats(group.Verts, group.Verts.Name);
            if (positions.Length % 3 != 0)
                throw new FrameException("bad_frame", group.Verts.Name + " has " + positions.Length
                    + " floats; positions are xyz triples");
            int vertexCount = positions.Length / 3;

            uint[] rawCounts = ReadU32(group.Counts, group.Counts.Name);
            uint[] rawIndices = ReadU32(group.Indices, group.Indices.Name);
            FaceTable faces = ValidateFaces(rawCounts, rawIndices, vertexCount,
                                            group.Counts.Name, group.Indices.Name);
            byte[] colors = group.Colors == null ? null : ReadMeshColorBytes(group.Colors, vertexCount);

            return new MeshData(positions, faces.Sizes, faces.Indices, colors);
        }

        /// <summary>The wire sections for validated mesh data. Both the
        /// encoder and the decoder tests use this one shape.</summary>
        internal static List<Section> MeshDataSections(MeshData data, string prefix)
        {
            var sections = new List<Section>();
            sections.Add(Section.Floats(prefix + ".verts", data.Positions));

            var counts = new uint[data.FaceSizes.Length];
            for (int i = 0; i < counts.Length; i++) counts[i] = (uint)data.FaceSizes[i];
            var indices = new uint[data.FaceIndices.Length];
            for (int i = 0; i < indices.Length; i++) indices[i] = (uint)data.FaceIndices[i];
            sections.Add(Section.U32(prefix + ".face_counts", counts));
            sections.Add(Section.U32(prefix + ".face_indices", indices));

            if (data.Colors != null) sections.Add(Section.Bytes(prefix + ".colors", data.Colors));
            return sections;
        }

        private static Mesh BuildMesh(MeshGroup group, double scale)
        {
            MeshData data = ReadMeshData(group);

            var mesh = new Mesh();
            // Vertices.AddVertices(IEnumerable<Point3f>) does not resolve with the
            // Mono profile mcs compiles against (its metadata mentions
            // IReadOnlyList, absent from the 4.0-api mscorlib mcs loads), so the
            // loop is deliberate; the benchmarks measure its cost.
            for (int i = 0; i < data.Positions.Length; i += 3)
            {
                Point3d point = FromWire(data.Positions[i], data.Positions[i + 1],
                                         data.Positions[i + 2], scale);
                mesh.Vertices.Add((float)point.X, (float)point.Y, (float)point.Z);
            }

            AddFaces(mesh, new FaceTable(data.FaceSizes, data.FaceIndices));

            if (data.Colors != null) SetMeshColors(mesh, data.Colors);

            mesh.Normals.ComputeNormals();
            // Compact removes unreferenced vertices. A face-less mesh has no
            // face/edge references, so Compact would delete every vertex and
            // make the geometry disappear. The exporter now sends wire meshes
            // as curves, but another sender can still reach this path.
            if (mesh.Faces.Count > 0) mesh.Compact();
            return mesh;
        }

        /// <summary>The validated face stream: vertex indices and per-face
        /// vertex counts, both plain ints so no u32 cast can overflow during
        /// construction.</summary>
        private sealed class FaceTable
        {
            public readonly int[] Sizes;
            public readonly int[] Indices;

            public FaceTable(int[] sizes, int[] indices)
            {
                Sizes = sizes;
                Indices = indices;
            }
        }

        /// <summary>The single owner of face-array validation: counts and
        /// indices must describe exactly the vertex stream, every face has at
        /// least three vertices, and every index lies inside the vertex list.
        /// Only a table produced here reaches <see cref="AddFaces"/>.</summary>
        private static FaceTable ValidateFaces(uint[] counts, uint[] indices, int vertexCount,
                                               string countsName, string indicesName)
        {
            long total = 0;
            var sizes = new int[counts.Length];
            for (int f = 0; f < counts.Length; f++)
            {
                uint raw = counts[f];
                if (raw < 3)
                    throw new FrameException("bad_frame",
                        countsName + "[" + f + "] is " + raw + "; faces need at least 3 vertices");
                if (raw > int.MaxValue)
                    throw new FrameException("bad_frame",
                        countsName + "[" + f + "] is " + raw + ", above the supported vertex count");
                sizes[f] = (int)raw;
                total += raw;
            }
            if (total != indices.Length)
                throw new FrameException("bad_frame", countsName + " sums to " + total
                    + " vertex references but " + indicesName + " carries " + indices.Length);

            for (int i = 0; i < indices.Length; i++)
            {
                if (indices[i] >= (uint)vertexCount)
                    throw new FrameException("bad_frame", indicesName + "[" + i + "] is "
                        + indices[i] + " but the mesh has " + vertexCount + " vertices");
            }

            // A face that names one vertex twice is degenerate; RhinoCommon
            // rejects some of these with ArgumentException, so classify them
            // here instead of letting construction fail.
            int faceStart = 0;
            for (int f = 0; f < sizes.Length; f++)
            {
                int n = sizes[f];
                if (n <= 4)
                {
                    for (int a = 0; a < n; a++)
                        for (int b = a + 1; b < n; b++)
                            if (indices[faceStart + a] == indices[faceStart + b])
                                throw new FrameException("bad_frame", countsName + "[" + f
                                    + "] repeats vertex " + indices[faceStart + a]);
                }
                else
                {
                    var seen = new HashSet<uint>();
                    for (int k = 0; k < n; k++)
                    {
                        if (!seen.Add(indices[faceStart + k]))
                            throw new FrameException("bad_frame", countsName + "[" + f
                                + "] repeats vertex " + indices[faceStart + k]);
                    }
                }
                faceStart += n;
            }

            var vertexIndices = new int[indices.Length];
            for (int i = 0; i < indices.Length; i++) vertexIndices[i] = (int)indices[i];
            return new FaceTable(sizes, vertexIndices);
        }

        /// <summary>Add faces from a validated table. The table's sizes sum to
        /// its index list and every index is in range, so every read below is
        /// inside the arrays.</summary>
        private static void AddFaces(Mesh mesh, FaceTable faces)
        {
            if (faces == null)
                throw new FrameException("bad_frame", "faces were not validated");

            int[] sizes = faces.Sizes;
            int[] indices = faces.Indices;
            int at = 0;
            try
            {
                for (int f = 0; f < sizes.Length; f++)
                {
                    int n = sizes[f];
                    if (n == 3)
                    {
                        mesh.Faces.AddFace(indices[at], indices[at + 1], indices[at + 2]);
                    }
                    else if (n == 4)
                    {
                        mesh.Faces.AddFace(indices[at], indices[at + 1],
                                           indices[at + 2], indices[at + 3]);
                    }
                    else
                    {
                        // Rhino keeps n-gons as a boundary over triangles. Fan the
                        // boundary, remember those face indexes, then register the
                        // ngon over them.
                        var boundary = new List<int>();
                        var fan = new List<int>();
                        for (int k = 0; k < n; k++) boundary.Add(indices[at + k]);
                        for (int k = 1; k + 1 < n; k++)
                        {
                            int faceIndex = mesh.Faces.Count;
                            mesh.Faces.AddFace(boundary[0], boundary[k], boundary[k + 1]);
                            fan.Add(faceIndex);
                        }
                        MeshNgon ngon = MeshNgon.Create(boundary, fan);
                        if (ngon == null)
                            throw new FrameException("bad_frame",
                                "face " + f + " could not be registered as an n-gon");
                        mesh.Ngons.AddNgon(ngon);
                    }
                    at += n;
                }
            }
            catch (ArgumentException ex)
            {
                throw new FrameException("bad_frame",
                    "mesh face geometry was rejected: " + ex.Message);
            }
        }

        private static byte[] ReadMeshColorBytes(Section colors, int vertexCount)
        {
            byte[] rgba = ReadBytes(colors, colors.Name);
            if (rgba.Length % 4 != 0)
                throw new FrameException("bad_frame", colors.Name + " length " + rgba.Length
                    + " is not a whole number of RGBA entries");
            int count = rgba.Length / 4;
            if (count != vertexCount)
                throw new FrameException("bad_frame",
                    colors.Name + " has " + count + " entries for " + vertexCount + " vertices");
            return rgba;
        }

        private static void SetMeshColors(Mesh mesh, byte[] rgba)
        {
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

        /// <summary>The section-type gate for every decoded geometry array: the
        /// declared dtype must match the wire contract and the byte length must
        /// be whole elements before any cast allocates or indexes.</summary>
        private static float[] ReadFloats(Section section, string name)
        {
            RequireDtype(section, name, "f32");
            if (section.Raw.Length % 4 != 0)
                throw new FrameException("bad_frame", name + " length " + section.Raw.Length
                    + " is not a whole number of f32 values");
            return section.ToFloats();
        }

        private static uint[] ReadU32(Section section, string name)
        {
            RequireDtype(section, name, "u32");
            if (section.Raw.Length % 4 != 0)
                throw new FrameException("bad_frame", name + " length " + section.Raw.Length
                    + " is not a whole number of u32 values");
            return section.ToU32();
        }

        private static byte[] ReadBytes(Section section, string name)
        {
            RequireDtype(section, name, "u8");
            return section.Raw;
        }

        private static void RequireDtype(Section section, string name, string expected)
        {
            if (section == null)
                throw new FrameException("bad_frame", name + " is missing");
            if (section.Raw == null)
                throw new FrameException("bad_frame", name + " has no bytes");
            if (section.Dtype != expected)
                throw new FrameException("bad_frame", name + " must be " + expected
                    + ", not " + (section.Dtype ?? "(missing)"));
        }

        // ------------------------------------------------------- mesh -> sections

        /// <summary>Encode a Rhino mesh as mesh.* sections. Vertex colours are
        /// included only when the mesh has one colour per vertex. This overload
        /// has no warning channel, so a mesh whose n-gons cannot be represented
        /// (holes/slits) is rejected instead of silently flattened; use the
        /// overload with <paramref name="warnings"/> to send the fan faces.</summary>
        public static List<Section> SectionsFromMesh(Mesh mesh, double scale)
        {
            return MeshSections(mesh, scale, "mesh", true, null);
        }

        /// <summary>Encode a Rhino mesh as mesh.* sections and report every
        /// conversion loss. An n-gon with holes or slits cannot travel as one
        /// wire polygon, so its underlying fan faces are sent instead (the
        /// topology is preserved) with an <see cref="WarningCodes.NgonHoles"/>
        /// warning.</summary>
        public static List<Section> SectionsFromMesh(Mesh mesh, double scale, List<WireWarning> warnings)
        {
            return MeshSections(mesh, scale, "mesh", true, warnings);
        }

        private static List<Section> MeshSections(Mesh mesh, double scale, string prefix,
                                                  bool includeColors, List<WireWarning> warnings)
        {
            Point3f[] points = mesh.Vertices.ToPoint3fArray();
            var positions = new float[points.Length * 3];
            for (int i = 0; i < points.Length; i++)
                WriteWire(positions, i * 3, new Point3d(points[i]), scale);

            var counts = new List<int>();
            var indices = new List<int>();
            // An n-gon that cannot be represented as one wire polygon is not
            // masked out: its fan faces travel as ordinary faces below.
            bool[] flatten = NgonFallbackMask(mesh);
            bool[] inNgon = NgonFaceMask(mesh, flatten);
            for (int f = 0; f < mesh.Faces.Count; f++)
            {
                if (inNgon[f]) continue;
                MeshFace face = mesh.Faces[f];
                counts.Add(face.IsQuad ? 4 : 3);
                indices.Add(face.A);
                indices.Add(face.B);
                indices.Add(face.C);
                if (face.IsQuad) indices.Add(face.D);
            }
            for (int i = 0; i < mesh.Ngons.Count; i++)
            {
                if (flatten[i]) continue;
                MeshNgon ngon = mesh.Ngons.GetNgon(i);
                var faceList = new List<int>();
                foreach (uint faceIndex in ngon.FaceIndexList())
                    faceList.Add((int)faceIndex);
                int[] boundary = mesh.Ngons.GetNgonBoundary(faceList);
                if (boundary == null)
                    throw new FrameException("bad_frame",
                        "n-gon boundary could not be resolved from its fan faces");
                counts.Add(boundary.Length);
                foreach (int v in boundary) indices.Add(v);
            }

            byte[] rgba = null;
            if (includeColors && mesh.VertexColors.Count == mesh.Vertices.Count && mesh.Vertices.Count > 0)
            {
                rgba = new byte[mesh.Vertices.Count * 4];
                for (int i = 0; i < mesh.Vertices.Count; i++)
                {
                    Color c = mesh.VertexColors[i];
                    rgba[i * 4] = c.R;
                    rgba[i * 4 + 1] = c.G;
                    rgba[i * 4 + 2] = c.B;
                    rgba[i * 4 + 3] = c.A;
                }
            }

            List<Section> sections = MeshDataSections(
                new MeshData(positions, counts.ToArray(), indices.ToArray(), rgba), prefix);

            int flattened = 0;
            foreach (bool value in flatten)
                if (value) flattened++;
            if (flattened > 0)
            {
                string message = flattened == 1
                    ? "An n-gon with holes or a slit was sent as its underlying faces; the wire format cannot carry holes"
                    : flattened + " n-gons with holes or slits were sent as their underlying faces; the wire format cannot carry holes";
                if (warnings == null)
                    throw new FrameException("bad_frame", message);
                warnings.Add(new WireWarning(WarningCodes.NgonHoles, message));
            }

            return sections;
        }

        /// <summary>Which n-gons cannot travel as one wire polygon. RhinoCommon's
        /// <c>NgonHasHoles</c> is true when the complete edge count differs from
        /// the outer edge count; its remarks call out slits as the same shape.
        /// A repeated outer-boundary vertex is also non-simple and would be
        /// rejected or altered by Blender, so it takes the fan-face path too.</summary>
        private static bool[] NgonFallbackMask(Mesh mesh)
        {
            var flatten = new bool[mesh.Ngons.Count];
            for (int i = 0; i < mesh.Ngons.Count; i++)
                flatten[i] = NgonNeedsFanFallback(mesh, i);
            return flatten;
        }

        private static bool NgonNeedsFanFallback(Mesh mesh, int index)
        {
            if (mesh.Ngons.NgonHasHoles(index)) return true;
            MeshNgon ngon = mesh.Ngons.GetNgon(index);
            uint[] boundary = ngon.BoundaryVertexIndexList();
            int outer = mesh.Ngons.GetNgonOuterEdgeCount(index);
            if (boundary == null || boundary.Length == 0 || outer <= 0 || boundary.Length != outer)
                return true;
            var seen = new HashSet<uint>();
            foreach (uint vertex in boundary)
                if (!seen.Add(vertex)) return true;
            return false;
        }

        private static bool[] NgonFaceMask(Mesh mesh, bool[] flatten)
        {
            var mask = new bool[mesh.Faces.Count];
            for (int i = 0; i < mesh.Ngons.Count; i++)
            {
                // Flattened n-gons leave their fan faces in the ordinary face
                // stream, so they must not be masked out here.
                if (flatten[i]) continue;
                MeshNgon ngon = mesh.Ngons.GetNgon(i);
                foreach (uint faceIndex in ngon.FaceIndexList())
                {
                    if (faceIndex >= (uint)mask.Length)
                        throw new FrameException("bad_frame",
                            "n-gon fan face index is outside the mesh face list");
                    mask[(int)faceIndex] = true;
                }
            }
            return mask;
        }

        // ------------------------------------------------------- curves -> wire

        /// <summary>Encode curves as crv.* sections.
        ///
        /// Two exact routes, then one approximation route:
        ///
        /// A NURBS whose knots are one of Blender's modes (uniform open,
        /// clamped/uniform endpoint, or uniform periodic; order 2..6, positive
        /// weights) travels as one wire NURBS — the same form Blender sent —
        /// so it comes back as one spline.
        ///
        /// Everything else is decomposed with MakePiecewiseBezier (lossless knot
        /// insertion): every span becomes a single rational Bezier of the curve's
        /// degree, and Blender holds a single span exactly as a NURBS spline
        /// whose order equals its control-point count, for any order 2..6,
        /// weights included. So each span travels as one wire curve and circles,
        /// ellipses, arcs, and every NURBS Blender could not otherwise express
        /// come back exactly.
        ///
        /// Only curves that defeat both (degree above 5, non-uniform knots,
        /// degenerate weights) are fitted with cubic Beziers inside the
        /// deviation budget, and only those are counted as approximated (D3).
        ///
        /// A null curve, or a curve with no polyline and no NURBS
        /// representation, is skipped and reported as curve_skipped; only a
        /// curve actually sent as a sampled polyline counts as curve_sampled.</summary>
        public static List<Section> SectionsFromCurves(
            IList<Curve> curves, double scale, List<WireWarning> warnings, out int approximated)
        {
            approximated = 0;
            var pts = new List<float>();
            var counts = new List<uint>();
            var types = new List<byte>();
            var orders = new List<byte>();
            var modes = new List<byte>();
            var cyclic = new List<byte>();
            int sampled = 0;
            int nullCurves = 0;
            int unrepresentable = 0;
            double maxDeviation = 0.0;

            foreach (Curve curve in curves)
            {
                if (curve == null)
                {
                    nullCurves++;
                    continue;
                }

                Polyline poly;
                if (curve.TryGetPolyline(out poly) && poly.Count >= 2)
                {
                    var list = new List<Point3d>(poly);
                    bool closed = list[0].DistanceTo(list[list.Count - 1]) < 1e-9;
                    if (closed && list.Count > 2) list.RemoveAt(list.Count - 1);
                    AppendCurveSection(list, null, 0, 0, 0, closed, scale,
                        pts, counts, types, orders, modes, cyclic);
                    continue;
                }

                NurbsCurve nurbs = curve.ToNurbsCurve();
                if (nurbs == null)
                {
                    // Nothing to send: this is a dropped curve, not a sampled
                    // one. Reporting it under curve_sampled hid the loss.
                    unrepresentable++;
                    continue;
                }

                // A NURBS whose knots match one of Blender's modes travels as
                // one wire NURBS, exactly as Blender sent it: order, knot mode,
                // cyclic flag, weights. This keeps a cyclic curve a single
                // cyclic spline across the round trip instead of splitting it
                // into spans (or, before this path existed, fitting it).
                byte directMode;
                bool directCyclic;
                List<ControlPoint> directPoints;
                if (TryBlenderNurbs(nurbs, out directMode, out directCyclic, out directPoints))
                {
                    AppendNurbs(directPoints, (byte)nurbs.Order, directMode, directCyclic, scale,
                        pts, counts, types, orders, modes, cyclic);
                    continue;
                }

                NurbsCurve piece = nurbs.DuplicateCurve() as NurbsCurve;
                bool decomposed = piece != null && piece.MakePiecewiseBezier(true);
                int degree = decomposed ? piece.Order - 1 : 0;
                int spans = decomposed ? piece.SpanCount : 0;
                if (decomposed && degree >= 1 && degree <= 5
                    && piece.Points.Count == spans * degree + 1 && WeightsPositive(piece))
                {
                    for (int s = 0; s < spans; s++)
                        AppendNurbsSpan(piece, s * degree, degree + 1, scale,
                            pts, counts, types, orders, modes, cyclic);
                    continue;
                }

                // Fallback for what the span decomposition cannot carry: fit
                // non-rational cubic Beziers within the deviation budget. This is
                // the approximation D3 warns about. A curve made of disjoint
                // pieces becomes one wire Bezier net per connected piece; a
                // single 3k+1 net cannot describe them.
                BezierCurve[] beziers = BezierCurve.CreateCubicBeziers(
                    nurbs, FitTolerance(nurbs, scale), KinkTolerance);
                List<List<Point3d>> nets = CubicBezierNets(beziers);
                var fittedNets = new List<NurbsCurve>();
                foreach (List<Point3d> net in nets)
                {
                    NurbsCurve fitted = BuildBezier(net);
                    if (fitted == null) break;
                    fittedNets.Add(fitted);
                }
                if (nets.Count == 0 || fittedNets.Count != nets.Count)
                {
                    AppendSampled(curve, scale, pts, counts, types, orders, modes, cyclic);
                    sampled++;
                    continue;
                }
                for (int i = 0; i < nets.Count; i++)
                {
                    double deviation = SpanDeviation(nurbs, fittedNets[i], 24);
                    if (deviation > maxDeviation) maxDeviation = deviation;
                    AppendCurveSection(nets[i], null, 2, 4, 2, false, scale,
                        pts, counts, types, orders, modes, cyclic);
                }
                approximated++;
            }

            if (approximated > 0)
            {
                warnings.Add(new WireWarning(WarningCodes.CurveApproximated,
                    approximated + " curve(s) approximated as cubic Bezier spans"));
            }
            if (maxDeviation > DeviationBudgetMeters * Math.Abs(scale))
            {
                warnings.Add(new WireWarning(WarningCodes.CurveDeviation,
                    "Curve approximation deviation "
                    + (maxDeviation / Math.Abs(scale) * 1000.0).ToString("0.###", System.Globalization.CultureInfo.InvariantCulture)
                    + " mm exceeds the 0.1 mm budget"));
            }
            if (sampled > 0)
                warnings.Add(new WireWarning(WarningCodes.CurveSampled,
                    sampled + " curve(s) sampled to polylines; no exact representation was available"));
            if (nullCurves > 0 || unrepresentable > 0)
            {
                var parts = new List<string>();
                if (nullCurves > 0) parts.Add(nullCurves + " null curve(s)");
                if (unrepresentable > 0)
                    parts.Add(unrepresentable + " curve(s) without a polyline or NURBS representation");
                warnings.Add(new WireWarning(WarningCodes.CurveSkipped,
                    string.Join(" and ", parts.ToArray()) + " were skipped"));
            }

            return CurveDataSections(new CurveData(pts.ToArray(), counts.ToArray(),
                types.ToArray(), orders.ToArray(), modes.ToArray(), cyclic.ToArray()));
        }

        private static void AppendCurveSection(
            List<Point3d> points, List<double> weights, byte type, byte order, byte mode, bool cyclic,
            double scale, List<float> pts, List<uint> counts, List<byte> types, List<byte> orders,
            List<byte> modes, List<byte> cyc)
        {
            foreach (Point3d p in points)
            {
                pts.Add((float)(p.X / scale));
                pts.Add((float)(p.Y / scale));
                pts.Add((float)(p.Z / scale));
                pts.Add(1.0f);
            }
            counts.Add((uint)points.Count);
            types.Add(type);
            orders.Add(order);
            modes.Add(mode);
            cyc.Add((byte)(cyclic ? 1 : 0));
        }

        /// <summary>One Bezier span as its own wire curve: a single-span NURBS
        /// whose order is its control-point count, weights included, so Blender
        /// reproduces it exactly.
        ///
        /// The wire carries Euclidean coordinates plus a weight (Blender's native
        /// form); Rhino stores homogeneous coordinates (Euclidean * weight), so the
        /// Euclidean location comes from ControlPoint.Location. Mixing the two made
        /// every rational curve arrive as a parabola.</summary>
        private static void AppendNurbsSpan(
            NurbsCurve piece, int start, int count, double scale, List<float> pts, List<uint> counts,
            List<byte> types, List<byte> orders, List<byte> modes, List<byte> cyclic)
        {
            for (int i = start; i < start + count; i++)
            {
                ControlPoint cp = piece.Points[i];
                Point3d location = cp.Location;
                pts.Add((float)(location.X / scale));
                pts.Add((float)(location.Y / scale));
                pts.Add((float)(location.Z / scale));
                pts.Add((float)cp.Weight);
            }
            counts.Add((uint)count);
            types.Add(2);
            orders.Add((byte)count);
            modes.Add(1);
            cyclic.Add(0);
        }

        /// <summary>Whether the curve's knot structure is one of Blender's NURBS
        /// modes — uniform open, clamped/uniform endpoint, or uniform periodic —
        /// and if so the direct wire form: order 2..6, positive weights, distinct
        /// knots with a constant step, repeated knots only as clamped ends.
        /// Anything else (interior multiplicities, non-uniform knots, degree
        /// above 5) cannot survive Blender's NURBS representation and is
        /// decomposed into spans instead.
        ///
        /// A periodic curve sends its unique polygon: openNURBS stores the first
        /// `degree` control points repeated at the end, and Blender rebuilds the
        /// wrap from the cyclic flag.</summary>
        private static bool TryBlenderNurbs(
            NurbsCurve curve, out byte mode, out bool cyclic, out List<ControlPoint> points)
        {
            mode = 0;
            cyclic = false;
            points = null;

            int order = curve.Order;
            if (order < 2 || order > 6) return false;
            if (curve.Points.Count < order) return false;
            if (!WeightsPositive(curve)) return false;

            var knots = curve.Knots;
            bool clamped = knots.IsClampedStart && knots.IsClampedEnd;
            int interiorEnd = knots.Count - order + 1;
            double step = 0.0;
            double previous = knots[0];
            for (int i = 1; i < knots.Count; i++)
            {
                double knot = knots[i];
                if (knot == previous)
                {
                    // A repeated knot survives only as a clamped end; an interior
                    // repeat is a non-uniform knot vector Blender cannot hold (a
                    // rational circle keeps its span boundaries that way).
                    if (!clamped || (i >= order - 1 && i < interiorEnd)) return false;
                    continue;
                }
                double delta = knot - previous;
                if (!(delta > 0.0)) return false;
                if (step == 0.0) step = delta;
                else if (Math.Abs(delta - step) > 1e-9 * step) return false;
                previous = knot;
            }
            if (step == 0.0) return false;

            if (curve.IsPeriodic)
            {
                int unique = curve.Points.Count - curve.Degree;
                if (unique < order) return false;
                for (int i = unique; i < curve.Points.Count; i++)
                {
                    ControlPoint tail = curve.Points[i];
                    ControlPoint head = curve.Points[i - unique];
                    if (tail.Location.DistanceTo(head.Location) > 1e-12
                        || Math.Abs(tail.Weight - head.Weight) > 1e-12) return false;
                }
                points = new List<ControlPoint>(unique);
                for (int i = 0; i < unique; i++) points.Add(curve.Points[i]);
                mode = 0;
                cyclic = true;
                return true;
            }

            points = new List<ControlPoint>(curve.Points.Count);
            for (int i = 0; i < curve.Points.Count; i++) points.Add(curve.Points[i]);
            mode = clamped ? (byte)1 : (byte)0;
            return true;
        }

        /// <summary>One Blender-expressible NURBS as its own wire curve: Euclidean
        /// coordinates plus weight, order, knot mode and the cyclic flag — the
        /// same form Blender sent, and the one it rebuilds exactly.</summary>
        private static void AppendNurbs(
            List<ControlPoint> points, byte order, byte mode, bool cyclic, double scale,
            List<float> pts, List<uint> counts, List<byte> types, List<byte> orders,
            List<byte> modes, List<byte> cyc)
        {
            foreach (ControlPoint cp in points)
            {
                Point3d location = cp.Location;
                pts.Add((float)(location.X / scale));
                pts.Add((float)(location.Y / scale));
                pts.Add((float)(location.Z / scale));
                pts.Add((float)cp.Weight);
            }
            counts.Add((uint)points.Count);
            types.Add(2);
            orders.Add(order);
            modes.Add(mode);
            cyc.Add((byte)(cyclic ? 1 : 0));
        }

        private static bool WeightsPositive(NurbsCurve curve)
        {
            for (int i = 0; i < curve.Points.Count; i++)
                if (!(curve.Points[i].Weight > 0.0)) return false;
            return true;
        }

        private static void AppendSampled(
            Curve curve, double scale, List<float> pts, List<uint> counts, List<byte> types,
            List<byte> orders, List<byte> modes, List<byte> cyclic)
        {
            int segments = Math.Max(8, curve.SpanCount * 8);
            double[] ts = curve.DivideByCount(segments, true);
            var list = new List<Point3d>();
            if (ts != null)
                foreach (double t in ts) list.Add(curve.PointAt(t));
            if (list.Count < 2)
            {
                list.Clear();
                list.Add(curve.PointAtStart);
                list.Add(curve.PointAtEnd);
            }
            AppendCurveSection(list, null, 0, 0, 0, curve.IsClosed, scale,
                pts, counts, types, orders, modes, cyclic);
        }

        /// <summary>0.1 mm on the wire (default budget), in meters.</summary>
        private const double DeviationBudgetMeters = 1e-4;

        /// <summary>Kinks sharper than 1 degree survive as kinks in the fit.</summary>
        private const double KinkTolerance = 0.017453292519943295;

        /// <summary>Fit tolerance in document units: the budget, but never finer
        /// than the curve itself can justify.</summary>
        private static double FitTolerance(Curve curve, double scale)
        {
            double tolerance = DeviationBudgetMeters * Math.Abs(scale);
            BoundingBox box = curve.GetBoundingBox(true);
            double diagonal = box.Diagonal.Length;
            if (diagonal > 0 && tolerance > diagonal * 1e-3) tolerance = diagonal * 1e-3;
            return Math.Max(tolerance, RhinoMath.SqrtEpsilon);
        }

        /// <summary>The Blender Bezier nets (co0, hr0, hl1, co1, ...) of a
        /// chain of cubic Beziers: one net per connected piece, sharing the
        /// endpoints between consecutive segments. Every net is 3k+1 points.</summary>
        private static List<List<Point3d>> CubicBezierNets(BezierCurve[] beziers)
        {
            var nets = new List<List<Point3d>>();
            if (beziers == null) return nets;
            List<Point3d> current = null;
            foreach (BezierCurve bezier in beziers)
            {
                if (bezier == null || bezier.ControlVertexCount < 4) continue;
                Point3d v0 = bezier.GetControlVertex3d(0);
                if (current != null && current[current.Count - 1].DistanceTo(v0) < 1e-9)
                {
                    current.Add(bezier.GetControlVertex3d(1));
                    current.Add(bezier.GetControlVertex3d(2));
                    current.Add(bezier.GetControlVertex3d(3));
                }
                else
                {
                    current = new List<Point3d>();
                    for (int i = 0; i < 4; i++) current.Add(bezier.GetControlVertex3d(i));
                    nets.Add(current);
                }
            }
            return nets;
        }

        /// <summary>Largest distance from samples on the approximation back to
        /// the original.</summary>
        private static double SpanDeviation(NurbsCurve original, NurbsCurve approximation, int samples)
        {
            double max = 0.0;
            for (int span = 0; span < approximation.SpanCount; span++)
            {
                BezierCurve bezier = approximation.ConvertSpanToBezier(span);
                if (bezier == null) continue;
                for (int i = 0; i <= samples; i++)
                {
                    Point3d p = bezier.PointAt(i / (double)samples);
                    double t;
                    if (original.ClosestPoint(p, out t))
                    {
                        double d = original.PointAt(t).DistanceTo(p);
                        if (d > max) max = d;
                    }
                }
            }
            return max;
        }

        // ------------------------------------------------------- wire -> curves

        /// <summary>Rebuild curves from crv.* sections. ``prefix`` namespaces a
        /// definition group (protocol §5.2).
        ///
        /// The whole group is validated before any curve is built: section
        /// dtypes and lengths, the points/counts sum, per-curve type, order,
        /// knot mode and weights. A malformed group is a ``bad_frame``; it is
        /// never truncated to the curves that happened to fit.</summary>
        public static List<Curve> CurvesFromSections(DecodedFrame frame, double scale, string prefix = "")
        {
            var curves = new List<Curve>();
            CurveData data = ReadCurveData(frame, prefix);
            if (data == null) return curves;

            int at = 0;
            for (int i = 0; i < data.Counts.Length; i++)
            {
                int n = (int)data.Counts[i];
                var ps = new List<Point3d>();
                var ws = new List<double>();
                for (int k = 0; k < n; k++)
                {
                    int baseIndex = (at + k) * 4;
                    ps.Add(FromWire(data.Points[baseIndex], data.Points[baseIndex + 1],
                                    data.Points[baseIndex + 2], scale));
                    ws.Add(data.Points[baseIndex + 3]);
                }
                at += n;

                Curve curve;
                try
                {
                    curve = BuildCurve(ps, ws, data.Types[i], data.Modes[i],
                                       data.Orders[i], data.Cyclic[i] != 0);
                }
                catch (ArgumentException ex)
                {
                    throw new FrameException("bad_frame",
                        "curve " + i + " was rejected by RhinoCommon: " + ex.Message);
                }
                if (curve == null)
                    throw new FrameException("bad_frame",
                        "curve " + i + " could not be built as a valid curve");
                curves.Add(curve);
            }
            return curves;
        }

        /// <summary>A validated crv.* group in wire coordinates.</summary>
        internal sealed class CurveData
        {
            public readonly float[] Points;
            public readonly uint[] Counts;
            public readonly byte[] Types;
            public readonly byte[] Orders;
            public readonly byte[] Modes;
            public readonly byte[] Cyclic;

            public CurveData(float[] points, uint[] counts, byte[] types, byte[] orders,
                             byte[] modes, byte[] cyclic)
            {
                Points = points;
                Counts = counts;
                Types = types;
                Orders = orders;
                Modes = modes;
                Cyclic = cyclic;
            }
        }

        /// <summary>The single owner of curve-group validation. Every section
        /// length, the points/counts sum, per-curve type, order, knot mode and
        /// weight is checked here before any curve is built. Returns null when
        /// the frame carries no curve group.</summary>
        internal static CurveData ReadCurveData(DecodedFrame frame, string prefix)
        {
            Section pointSection = frame.Find(prefix + "crv.points");
            Section countSection = frame.Find(prefix + "crv.counts");
            Section typeSection = frame.Find(prefix + "crv.types");
            Section orderSection = frame.Find(prefix + "crv.orders");
            Section modeSection = frame.Find(prefix + "crv.knots_mode");
            Section cyclicSection = frame.Find(prefix + "crv.cyclic");

            bool any = pointSection != null || countSection != null || typeSection != null
                || orderSection != null || modeSection != null || cyclicSection != null;
            if (!any) return null;
            if (pointSection == null || countSection == null)
                throw new FrameException("bad_frame", "curve group is incomplete: "
                    + (pointSection == null ? prefix + "crv.points is missing" : prefix + "crv.counts is missing"));

            float[] raw = ReadFloats(pointSection, pointSection.Name);
            if (raw.Length % 4 != 0)
                throw new FrameException("bad_frame", pointSection.Name + " has " + raw.Length
                    + " floats; each curve point is xyz plus weight");
            int pointCount = raw.Length / 4;

            uint[] cnt = ReadU32(countSection, countSection.Name);
            byte[] types = ReadCurveBytes(typeSection, prefix + "crv.types", cnt.Length);
            byte[] orders = ReadCurveBytes(orderSection, prefix + "crv.orders", cnt.Length);
            byte[] modes = ReadCurveBytes(modeSection, prefix + "crv.knots_mode", cnt.Length);
            byte[] cyc = ReadCurveBytes(cyclicSection, prefix + "crv.cyclic", cnt.Length);

            long total = 0;
            for (int i = 0; i < cnt.Length; i++) total += cnt[i];
            if (total != pointCount)
                throw new FrameException("bad_frame", countSection.Name + " sums to " + total
                    + " points but " + pointSection.Name + " carries " + pointCount);

            // Validate every curve before constructing any Rhino geometry, so
            // a bad tail cannot return a partial result.
            int at = 0;
            for (int i = 0; i < cnt.Length; i++)
            {
                uint rawCount = cnt[i];
                if (rawCount < 2)
                    throw new FrameException("bad_frame",
                        "curve " + i + " has " + rawCount + " points; at least 2 are required");
                if (rawCount > int.MaxValue)
                    throw new FrameException("bad_frame",
                        "curve " + i + " has " + rawCount + " points, above the supported count");
                int n = (int)rawCount;

                byte type = types[i];
                byte mode = modes[i];
                byte order = orders[i];
                if (type > 2)
                    throw new FrameException("bad_frame", "curve " + i + " has type " + type);
                if (mode > 2)
                    throw new FrameException("bad_frame", "curve " + i + " has knot mode " + mode);
                if (cyc[i] > 1)
                    throw new FrameException("bad_frame", "curve " + i + " cyclic flag is " + cyc[i]);

                bool bezier = type == 1 || (type == 2 && mode == 2);
                if (bezier)
                {
                    // A Bezier curve travels as a co0,hr0,hl1,co1... net:
                    // 3k+1 points for k spans.
                    if (n < 4 || (n - 1) % 3 != 0)
                        throw new FrameException("bad_frame", "curve " + i
                            + " is a Bezier net with " + n + " points; expected 3k+1");
                }
                else if (type == 2)
                {
                    if (order < 2 || order > 6)
                        throw new FrameException("bad_frame",
                            "curve " + i + " NURBS order is " + order + "; expected 2..6");
                    if (order > n)
                        throw new FrameException("bad_frame", "curve " + i + " NURBS order "
                            + order + " exceeds its " + n + " control points");
                }

                for (int k = 0; k < n; k++)
                {
                    float w = raw[(at + k) * 4 + 3];
                    if (!(w > 0.0f) || float.IsInfinity(w))
                        throw new FrameException("bad_frame",
                            "curve " + i + " point " + k + " has weight " + w
                            + "; weights must be finite and positive");
                }
                at += n;
            }

            return new CurveData(raw, cnt, types, orders, modes, cyc);
        }

        /// <summary>The wire sections for validated curve data; the encoder
        /// and the decoder tests share this shape.</summary>
        internal static List<Section> CurveDataSections(CurveData data)
        {
            var sections = new List<Section>();
            if (data.Counts.Length == 0) return sections;
            sections.Add(Section.Floats("crv.points", data.Points));
            sections.Add(Section.U32("crv.counts", data.Counts));
            sections.Add(Section.Bytes("crv.types", data.Types));
            sections.Add(Section.Bytes("crv.orders", data.Orders));
            sections.Add(Section.Bytes("crv.knots_mode", data.Modes));
            sections.Add(Section.Bytes("crv.cyclic", data.Cyclic));
            return sections;
        }

        private static byte[] ReadCurveBytes(Section section, string name, int count)
        {
            if (section == null)
            {
                if (count == 0) return new byte[0];
                throw new FrameException("bad_frame", name + " is missing for " + count + " curves");
            }
            byte[] values = ReadBytes(section, name);
            if (values.Length != count)
                throw new FrameException("bad_frame", name + " has " + values.Length
                    + " entries for " + count + " curves");
            return values;
        }

        private static Curve BuildCurve(
            List<Point3d> ps, List<double> ws, byte type, byte mode, byte order, bool cyclic)
        {
            if (ps.Count < 2) return null;

            if (type == 0)
            {
                var poly = new Polyline(ps);
                if (cyclic && ps.Count > 2 && ps[0].DistanceTo(ps[ps.Count - 1]) > 1e-9)
                    poly.Add(ps[0]);
                return new PolylineCurve(poly);
            }

            // Types 1 and knots_mode 2 are the Bezier-net route. Their shape
            // was validated above, so a rejected net is malformed rather than
            // a reason to silently fall back to a different curve.
            if (type == 1 || mode == 2) return BuildBezier(ps);
            return BuildNurbs(ps, ws, order, mode == 1, cyclic);
        }

        /// <summary>A degree-3 curve with Bezier spans from a co0,hr0,hl1,co1...
        /// net (3k+1 control points).</summary>
        private static NurbsCurve BuildBezier(List<Point3d> ps)
        {
            int n = ps.Count;
            if (n < 4 || (n - 1) % 3 != 0) return null;
            int spans = (n - 1) / 3;

            var curve = new NurbsCurve(3, false, 4, n);
            for (int i = 0; i < n; i++)
                curve.Points.SetPoint(i, new Point4d(ps[i].X, ps[i].Y, ps[i].Z, 1.0));

            // Rhino's knot count for n control points and order 4 is n+2:
            // order-1 repeats at each end, order-1 at every interior boundary.
            var knots = new List<double>();
            for (int k = 0; k < 3; k++) knots.Add(0.0);
            for (int b = 1; b < spans; b++)
                for (int k = 0; k < 3; k++) knots.Add(b);
            for (int k = 0; k < 3; k++) knots.Add(spans);
            if (knots.Count != curve.Knots.Count) return null;
            for (int i = 0; i < knots.Count; i++) curve.Knots[i] = knots[i];
            return curve.IsValid ? curve : null;
        }

        /// <summary>One wire NURBS curve. ``order`` is Blender's order; pass the
        /// *degree*, not the order, to ``NurbsCurve.Create``.</summary>
        private static NurbsCurve BuildNurbs(
            List<Point3d> ps, List<double> ws, int order, bool endpoint, bool cyclic)
        {
            int n = ps.Count;
            int ord = order;
            bool rational = false;
            foreach (double w in ws)
                if (Math.Abs(w - 1.0) > 1e-12) { rational = true; break; }

            if (cyclic)
            {
                NurbsCurve periodic = NurbsCurve.Create(true, ord - 1, ps);
                if (periodic != null && periodic.IsValid)
                {
                    if (!rational) return periodic;
                    // The wire carries Euclidean points + weight. `SetWeight`
                    // writes the homogeneous W slot (ON_NurbsCurve_SetWeight),
                    // which would move each point to location/w; the
                    // Point3d+weight overload of SetPoint writes the
                    // homogeneous (x*w, y*w, z*w, w) the kernel expects.
                    // A periodic curve repeats the first `degree` control
                    // points at the end; their weights repeat with them.
                    if (periodic.Points.MakeRational())
                    {
                        for (int i = 0; i < periodic.Points.Count; i++)
                            periodic.Points.SetPoint(i, ps[i % n], ws[i % n]);
                        if (periodic.IsValid) return periodic;
                    }
                    // MakeRational refused (unexpected): fall through to the
                    // uniform open construction below, which keeps the weights.
                }
                // Too few points for the periodic construction: fall through to
                // the uniform open form, which is what a plain Blender spline
                // with this order evaluates to in that case.
            }

            var curve = new NurbsCurve(3, rational, ord, n);
            for (int i = 0; i < n; i++)
            {
                // The wire is Euclidean + weight; Rhino's Point4d takes
                // homogeneous coordinates (Euclidean * weight).
                double w = rational ? ws[i] : 1.0;
                curve.Points.SetPoint(i, new Point4d(ps[i].X * w, ps[i].Y * w, ps[i].Z * w, w));
            }

            int count = curve.Knots.Count;
            if (endpoint)
            {
                int interior = count - 2 * (ord - 1);
                for (int i = 0; i < count; i++)
                {
                    if (i < ord - 1) curve.Knots[i] = 0.0;
                    else if (i >= count - (ord - 1)) curve.Knots[i] = 1.0;
                    else curve.Knots[i] = (double)(i - (ord - 2)) / (double)(interior + 1);
                }
            }
            else
            {
                for (int i = 0; i < count; i++) curve.Knots[i] = i;
            }
            return curve.IsValid ? curve : null;
        }

        // ------------------------------------------------------ point scaling

        /// <summary>The wire's metres into document units. One place for the
        /// multiply: the four converted loops this replaces had drifted apart
        /// copies, which is exactly where a units bug hides.</summary>
        private static Point3d FromWire(double x, double y, double z, double scale)
        {
            return new Point3d(x * scale, y * scale, z * scale);
        }

        /// <summary>Document units onto the wire's metres, the inverse.</summary>
        private static void WriteWire(float[] target, int offset, Point3d point, double scale)
        {
            target[offset] = (float)(point.X / scale);
            target[offset + 1] = (float)(point.Y / scale);
            target[offset + 2] = (float)(point.Z / scale);
        }

        // --------------------------------------------------------------- points

        /// <summary>Points from pts.* sections.</summary>
        public static List<Point3d> PointsFromSections(DecodedFrame frame, double scale)
        {
            var result = new List<Point3d>();
            Section positions = frame.Find("pts.positions");
            if (positions == null) return result;
            float[] f = positions.ToFloats();
            for (int i = 0; i + 2 < f.Length; i += 3)
                result.Add(FromWire(f[i], f[i + 1], f[i + 2], scale));
            return result;
        }

        /// <summary>A Rhino point cloud from pts.* sections, colours included when
        /// the wire carries them. The wire's per-point radii have no place on a
        /// Rhino point cloud and are dropped (they were dropped before, too).
        /// ``prefix`` namespaces a definition group (protocol §5.2).</summary>
        public static PointCloud PointCloudFromSections(DecodedFrame frame, double scale, string prefix = "")
        {
            Section positions = frame.Find(prefix + "pts.positions");
            if (positions == null) return null;
            float[] f = positions.ToFloats();
            int n = f.Length / 3;
            if (n == 0) return null;

            Section colors = frame.Find(prefix + "pts.colors");
            byte[] rgba = colors == null ? null : colors.Raw;
            var cloud = new PointCloud();
            for (int i = 0; i < n; i++)
            {
                Point3d point = FromWire(f[i * 3], f[i * 3 + 1], f[i * 3 + 2], scale);
                if (rgba != null && rgba.Length >= (i + 1) * 4)
                    cloud.Add(point, Color.FromArgb(rgba[i * 4 + 3], rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2]));
                else
                    cloud.Add(point);
            }
            return cloud;
        }

        /// <summary>Encode points as pts.positions.</summary>
        public static List<Section> SectionsFromPoints(IList<Point3d> points, double scale)
        {
            var sections = new List<Section>();
            if (points.Count == 0) return sections;
            var positions = new float[points.Count * 3];
            for (int i = 0; i < points.Count; i++)
                WriteWire(positions, i * 3, points[i], scale);
            sections.Add(Section.Floats("pts.positions", positions));
            return sections;
        }

        // ----------------------------------------------------------------- SubD

        /// <summary>Build a SubD from subd.* sections and apply the crease tags.
        /// Blender sends fractional crease values; they are rounded to the nearest
        /// tag and the count of affected edges is warned (D2).</summary>
        public static SubD SubDFromSections(DecodedFrame frame, double scale, List<WireWarning> warnings)
        {
            Mesh cage = SubdCageFromSections(frame, scale);
            if (cage == null || cage.Vertices.Count == 0) return null;
            SubD subd = SubD.CreateFromMesh(cage);
            if (subd == null) return null;

            Section edges = frame.Find("subd.crease_edges");
            Section values = frame.Find("subd.crease_values");
            if (edges == null || values == null) return subd;

            uint[] pairs = edges.ToU32();
            float[] vals = values.ToFloats();
            var byPair = new Dictionary<long, SubDEdge>();
            foreach (SubDEdge e in subd.Edges)
            {
                int a = e.VertexFrom.ComponentIndex().Index;
                int b = e.VertexTo.ComponentIndex().Index;
                long key = PairKey(a, b);
                if (!byPair.ContainsKey(key)) byPair[key] = e;
            }

            int fractional = 0;
            int changed = 0;
            int unmatched = 0;
            for (int i = 0; i + 1 < pairs.Length; i += 2)
            {
                float v = (i / 2) < vals.Length ? vals[i / 2] : 0f;
                if (Math.Abs(v) > 1e-6 && Math.Abs(v - 1f) > 1e-6) fractional++;
                SubDEdgeTag tag = v >= 0.5f ? SubDEdgeTag.Crease : SubDEdgeTag.Smooth;
                SubDEdge edge;
                if (!byPair.TryGetValue(PairKey((int)pairs[i], (int)pairs[i + 1]), out edge))
                {
                    // A crease with no edge on the cage is a loss, and this
                    // repository does not drop losses silently (principle 6):
                    // the vertex order of a SubD cage is not stable across a
                    // round trip, so this is the visible half of that.
                    unmatched++;
                    continue;
                }
                if (edge.Tag != tag)
                {
                    edge.Tag = tag;
                    changed++;
                }
            }
            if (fractional > 0)
                warnings.Add(new WireWarning(WarningCodes.SubdCreases,
                    fractional + " fractional crease value(s) rounded to the nearest tag; "
                    + changed + " edge(s) changed"));
            if (unmatched > 0)
                warnings.Add(new WireWarning(WarningCodes.SubdCreases,
                    unmatched + " crease edge(s) had no matching cage edge and were dropped"));
            return subd;
        }

        /// <summary>Encode a SubD as its control cage plus crease tags (D2).
        /// Boundary edges of an open cage read Crease by default, so only tagged
        /// creases are sent.</summary>
        public static List<Section> SectionsFromSubD(SubD subd, double scale, List<WireWarning> warnings)
        {
            Mesh cage = Mesh.CreateFromSubDControlNet(subd);
            if (cage == null)
                throw new FrameException("bad_frame",
                    "SubD control net could not be built");
            var sections = MeshSections(cage, scale, "subd", false, warnings);

            var creaseEdges = new List<uint>();
            var creaseValues = new List<float>();
            foreach (SubDEdge e in subd.Edges)
            {
                if (e.Tag != SubDEdgeTag.Crease) continue;
                int a = e.VertexFrom.ComponentIndex().Index;
                int b = e.VertexTo.ComponentIndex().Index;
                if (a < 0 || b < 0) continue;
                creaseEdges.Add((uint)a);
                creaseEdges.Add((uint)b);
                creaseValues.Add(1.0f);
            }
            if (creaseEdges.Count > 0)
            {
                sections.Add(Section.U32("subd.crease_edges", creaseEdges.ToArray()));
                sections.Add(Section.Floats("subd.crease_values", creaseValues.ToArray()));
                // The warning is about what was sent: a cage with no tagged
                // creases has nothing to lose, and warning anyway made every
                // SubD stage read as lossy.
                warnings.Add(new WireWarning(WarningCodes.SubdCreases,
                    "Creases transferred as tags; semi-sharp and vertex creases are not available from RhinoCommon"));
            }
            return sections;
        }

        private static long PairKey(int a, int b)
        {
            int lo = Math.Min(a, b);
            int hi = Math.Max(a, b);
            return ((long)lo << 32) | (uint)hi;
        }

        // ------------------------------------------------------- stage out glue

        /// <summary>Everything a Stage Out can receive, turned into sections.
        /// Meshes are joined; Breps are meshed (warned); multiple SubDs are
        /// joined. counts feeds the response header.</summary>
        public static List<Section> SectionsFromGeometry(
            IList<Mesh> meshes, IList<Curve> curves, IList<Point3d> points, IList<SubD> subds,
            IList<Brep> breps, double scale, List<WireWarning> warnings, out Dictionary<string, object> counts)
        {
            var sections = new List<Section>();
            var allMeshes = new List<Mesh>();
            foreach (Mesh m in meshes)
                if (m != null && m.Vertices.Count > 0) allMeshes.Add(m);

            foreach (Brep b in breps)
            {
                if (b == null) continue;
                Mesh[] ms = Mesh.CreateFromBrep(b, MeshingParameters.Default);
                if (ms == null) continue;
                foreach (Mesh m in ms)
                    if (m != null && m.Vertices.Count > 0) allMeshes.Add(m);
            }
            if (breps.Count > 0)
                warnings.Add(new WireWarning(WarningCodes.BrepMeshed,
                    breps.Count + " surface(s)/Brep(s) converted to mesh; Blender has no B-rep"));

            int meshCount = 0;
            if (allMeshes.Count > 0)
            {
                Mesh joined = allMeshes.Count == 1 ? allMeshes[0] : new Mesh();
                if (allMeshes.Count > 1)
                {
                    joined.Append(allMeshes);
                    joined.Normals.ComputeNormals();
                    joined.Compact();
                }
                sections.AddRange(MeshSections(joined, scale, "mesh", true, warnings));
                meshCount = 1;
            }

            var validSubds = new List<SubD>();
            foreach (SubD candidate in subds)
                if (candidate != null) validSubds.Add(candidate);
            subds = validSubds;

            int subdCount = 0;
            if (subds.Count > 0)
            {
                SubD subd = subds[0];
                if (subds.Count > 1)
                {
                    SubD[] joined = SubD.JoinSubDs(subds, RhinoMath.ZeroTolerance, false);
                    if (joined != null && joined.Length == 1) subd = joined[0];
                    else warnings.Add(new WireWarning(WarningCodes.SubdJoined,
                        "Several SubDs arrived; only the first was sent"));
                }
                sections.AddRange(SectionsFromSubD(subd, scale, warnings));
                subdCount = 1;
            }

            int approximated = 0;
            if (curves.Count > 0)
                sections.AddRange(SectionsFromCurves(curves, scale, warnings, out approximated));

            if (points.Count > 0)
                sections.AddRange(SectionsFromPoints(points, scale));

            counts = new Dictionary<string, object>();
            counts["meshes"] = (long)meshCount;
            counts["subd"] = (long)subdCount;
            counts["curves"] = (long)EmittedCurveCount(sections);
            counts["points"] = (long)points.Count;
            counts["approximated_curves"] = (long)approximated;
            return sections;
        }

        /// <summary>Curves actually written, not the input goo count:
        /// SectionsFromCurves skips null curves and curves that defeat every
        /// representation (both reported as curve_skipped), and the header's
        /// counts describe the frame. The fixture's `counts.curves` is the
        /// emitted count.</summary>
        private static int EmittedCurveCount(List<Section> sections)
        {
            foreach (Section s in sections)
                if (s.Name == "crv.counts") return s.ElementCount;
            return 0;
        }

        // ----------------------------------------------------------- instances

        /// <summary>A Transform from the wire's 16 floats: row-major
        /// (``m[row*4+col]``), translation in the fourth column — elements 3, 7,
        /// 11 (protocol §5.2). The wire is metres; <paramref name="scale"/> is
        /// the document-units-per-metre factor, so the translation becomes
        /// document units. The linear part is dimensionless and is not scaled.</summary>
        public static Transform TransformFromWire(float[] m, double scale)
        {
            Transform t = TransformFromWire(m);
            return scale == 1.0 ? t : ScaleTransformTranslation(t, scale);
        }

        /// <summary>A Transform from the wire with no unit conversion.</summary>
        public static Transform TransformFromWire(float[] m)
        {
            if (m == null || m.Length < 16)
                throw new FrameException("bad_frame", "a transform needs 16 floats");
            var t = new Transform(1.0);
            for (int row = 0; row < 4; row++)
                for (int col = 0; col < 4; col++)
                    t[row, col] = m[row * 4 + col];
            return t;
        }

        /// <summary>The wire form of a Transform (the inverse of
        /// <see cref="TransformFromWire"/>, translation divided back to metres).</summary>
        public static float[] TransformToWire(Transform t, double scale)
        {
            // Transform is a value type: scaling this copy leaves the caller's
            // matrix alone.
            if (scale != 0.0 && scale != 1.0) t = ScaleTransformTranslation(t, 1.0 / scale);
            return TransformToWire(t);
        }

        /// <summary>The wire form of a Transform with no unit conversion.</summary>
        public static float[] TransformToWire(Transform t)
        {
            var m = new float[16];
            for (int row = 0; row < 4; row++)
                for (int col = 0; col < 4; col++)
                    m[row * 4 + col] = (float)t[row, col];
            return m;
        }

        /// <summary>Multiply a transform's translation by a unit factor. The
        /// fourth-column entries (elements 3, 7, 11) are lengths; the rest of
        /// the affine matrix is dimensionless.</summary>
        private static Transform ScaleTransformTranslation(Transform t, double factor)
        {
            t[0, 3] = t[0, 3] * factor;
            t[1, 3] = t[1, 3] * factor;
            t[2, 3] = t[2, 3] * factor;
            return t;
        }

        /// <summary>The same sections under a definition prefix
        /// (``idef.&lt;i&gt;.``, protocol §5.2).</summary>
        public static List<Section> PrefixSections(IList<Section> sections, string prefix)
        {
            var result = new List<Section>();
            if (sections == null) return result;
            foreach (Section s in sections)
                result.Add(new Section(prefix + s.Name, s.Dtype, s.Raw));
            return result;
        }

        // ------------------------------------------------- text-only warnings

        // The live checks compile against these overloads and only print a
        // warning line. The plugin uses the WireWarning overloads above.

        /// <summary>Text-only view of SectionsFromCurves' warnings.</summary>
        public static List<Section> SectionsFromCurves(
            IList<Curve> curves, double scale, List<string> warnings, out int approximated)
        {
            var rich = new List<WireWarning>();
            List<Section> sections = SectionsFromCurves(curves, scale, rich, out approximated);
            CopyMessages(rich, warnings);
            return sections;
        }

        /// <summary>Text-only view of SubDFromSections' warnings.</summary>
        public static SubD SubDFromSections(DecodedFrame frame, double scale, List<string> warnings)
        {
            var rich = new List<WireWarning>();
            SubD subd = SubDFromSections(frame, scale, rich);
            CopyMessages(rich, warnings);
            return subd;
        }

        /// <summary>Text-only view of SectionsFromSubD's warnings.</summary>
        public static List<Section> SectionsFromSubD(SubD subd, double scale, List<string> warnings)
        {
            var rich = new List<WireWarning>();
            List<Section> sections = SectionsFromSubD(subd, scale, rich);
            CopyMessages(rich, warnings);
            return sections;
        }

        /// <summary>Text-only view of SectionsFromGeometry's warnings.</summary>
        public static List<Section> SectionsFromGeometry(
            IList<Mesh> meshes, IList<Curve> curves, IList<Point3d> points, IList<SubD> subds,
            IList<Brep> breps, double scale, List<string> warnings, out Dictionary<string, object> counts)
        {
            var rich = new List<WireWarning>();
            List<Section> sections = SectionsFromGeometry(
                meshes, curves, points, subds, breps, scale, rich, out counts);
            CopyMessages(rich, warnings);
            return sections;
        }

        private static void CopyMessages(List<WireWarning> source, List<string> target)
        {
            if (target == null) return;
            foreach (WireWarning warning in source)
                target.Add(warning.Message);
        }
    }
}
