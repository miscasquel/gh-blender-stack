// Instances: Blender's instance rows <-> Rhino block instances (protocol §5.2).
//
// Stage In: a frame's `definitions` table and `idef.<i>.*` groups become
// document instance definitions; the frame's `inst.*` rows become
// GH_InstanceReference items. S9 round 2 (spikes/results/s9.txt §C) is the
// evidence for the document write: no graph-side definition can preview or
// bake, while a document definition does both. Names are content hashes
// (`GHStack <hash>`), so an unchanged definition is found and reused on the
// next solve instead of piling up.
//
// Stage Out: GH_InstanceReference items become the same table and rows. A
// definition's geometry comes from its paired ModelInstanceDefinition.Objects;
// a nested reference whose pairing is missing falls back to the document
// definition by id.
//
// Definition geometry is read and written through the same Conversion and
// AttributeStore code as the frame's own geometry, under the `idef.<i>.`
// prefix.

using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using Grasshopper.Kernel.Types;
using Grasshopper.Rhinoceros.Model;
using Rhino;
using Rhino.DocObjects;
using Rhino.Geometry;
using GhStack.Core;

namespace GhStack
{
    public static class Instances
    {
        /// <summary>The name prefix of every document definition this side
        /// creates. The rest is the content hash, so the same definition is
        /// found again across solves and sessions.</summary>
        public const string DefinitionPrefix = "GHStack ";

        /// <summary>Definition rows and transforms, decoded (protocol §5.2).
        /// Returns false when the pair is absent; both halves are all-or-nothing.</summary>
        public static bool ReadRows(DecodedFrame frame, string prefix,
                                    out long[] refDef, out List<Transform> transforms)
        {
            refDef = null;
            transforms = null;
            Section refs = frame.Find(prefix + "inst.ref_def");
            Section mats = frame.Find(prefix + "inst.transforms");
            if (refs == null || mats == null) return false;
            uint[] defs = refs.ToU32();
            float[] raw = mats.ToFloats();
            int rows = Math.Min(defs.Length, raw.Length / 16);
            refDef = new long[rows];
            transforms = new List<Transform>(rows);
            for (int i = 0; i < rows; i++)
            {
                refDef[i] = defs[i];
                var m = new float[16];
                Array.Copy(raw, i * 16, m, 0, 16);
                transforms.Add(Conversion.TransformFromWire(m));
            }
            return rows > 0;
        }

        /// <summary>The display name of a definition table entry, or "".</summary>
        private static string NameOf(DecodedFrame frame, long index)
        {
            var table = Json.Get(frame.Header, "definitions") as List<object>;
            if (table == null || index < 0 || index >= table.Count) return "";
            var entry = table[(int)index] as Dictionary<string, object>;
            return entry == null ? "" : (Json.GetString(entry, "name") ?? "");
        }

        // ==================================================================
        //  Stage In: frame -> document definitions + GH_InstanceReference
        // ==================================================================

        /// <summary>Append the frame's instances to the stage geometry, building
        /// the document definitions they reference. Silently does nothing when
        /// the frame carries no definitions.</summary>
        public static void Build(DecodedFrame frame, double scale,
                                 List<Dictionary<string, object>> attributes,
                                 List<IGH_Goo> geometry, Action<string> note)
        {
            if (Json.Get(frame.Header, "definitions") == null) return;

            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                if (note != null) note("instances skipped: no open document to hold the block definitions");
                return;
            }

            var builder = new DefinitionBuilder(frame, scale, attributes, doc, note);
            long[] refDef;
            List<Transform> transforms;
            if (!ReadRows(frame, "", out refDef, out transforms))
            {
                if (note != null) note("definitions arrived without instance rows");
                return;
            }

            for (int row = 0; row < refDef.Length; row++)
            {
                InstanceDefinition idef = builder.Definition(refDef[row]);
                if (idef == null) continue;
                var reference = new InstanceReferenceGeometry(idef.Id, transforms[row]);
                geometry.Add(new GH_InstanceReference(reference, new ModelInstanceDefinition(idef)));
            }
        }

        private class DefinitionBuilder
        {
            private readonly DecodedFrame _frame;
            private readonly double _scale;
            private readonly List<Dictionary<string, object>> _attributes;
            private readonly RhinoDoc _doc;
            private readonly Action<string> _note;
            private readonly Dictionary<long, InstanceDefinition> _done = new Dictionary<long, InstanceDefinition>();
            private readonly HashSet<long> _visiting = new HashSet<long>();

            public DefinitionBuilder(DecodedFrame frame, double scale,
                                     List<Dictionary<string, object>> attributes,
                                     RhinoDoc doc, Action<string> note)
            {
                _frame = frame;
                _scale = scale;
                _attributes = attributes;
                _doc = doc;
                _note = note;
            }

            public InstanceDefinition Definition(long index)
            {
                InstanceDefinition cached;
                if (_done.TryGetValue(index, out cached)) return cached;
                var table = Json.Get(_frame.Header, "definitions") as List<object>;
                if (table == null || index < 0 || index >= table.Count)
                {
                    Note("instance row references definition " + index + ", which the frame does not carry");
                    return null;
                }
                if (!_visiting.Add(index))
                {
                    Note("definition " + index + " is nested inside itself; the row is skipped");
                    return null;
                }

                try
                {
                    string prefix = "idef." + index + ".";
                    var objects = new List<GeometryBase>();
                    var sources = new List<GeometryBase>();

                    Mesh mesh = Conversion.MeshFromSections(_frame, _scale, prefix);
                    if (mesh != null && mesh.Vertices.Count > 0)
                    {
                        objects.Add(mesh);
                        sources.Add(mesh);
                    }
                    foreach (Curve curve in Conversion.CurvesFromSections(_frame, _scale, prefix))
                        objects.Add(curve);
                    PointCloud cloud = Conversion.PointCloudFromSections(_frame, _scale, prefix);
                    if (cloud != null)
                    {
                        objects.Add(cloud);
                        sources.Add(cloud);
                    }

                    long[] nested;
                    List<Transform> nestedTransforms;
                    var nestedNames = new List<string>();
                    if (ReadRows(_frame, prefix, out nested, out nestedTransforms))
                    {
                        for (int row = 0; row < nested.Length; row++)
                        {
                            InstanceDefinition child = Definition(nested[row]);
                            if (child == null) continue;
                            objects.Add(new InstanceReferenceGeometry(child.Id, nestedTransforms[row]));
                            nestedNames.Add(child.Name ?? "");
                        }
                    }

                    if (objects.Count == 0)
                    {
                        Note("definition " + index + " is empty; skipped");
                        return null;
                    }

                    // Attributes ride on the definition's own geometry, exactly
                    // as they do on the frame's — and they are attached before
                    // the definition is added: the table stores its own copy of
                    // the geometry, so a later write to the local objects never
                    // reaches the document.
                    foreach (GeometryBase source in sources)
                    {
                        string group = AttributeStore.GroupOf(source);
                        if (group != null)
                            AttributeStore.Attach(source, group, _attributes, _frame.Sections, index);
                    }

                    string name = DefinitionPrefix + ContentHash(prefix, nestedNames);
                    InstanceDefinition existing = _doc.InstanceDefinitions.Find(name);
                    InstanceDefinition definition;
                    if (existing != null)
                    {
                        definition = existing;
                    }
                    else
                    {
                        string from = NameOf(_frame, index);
                        string description = string.IsNullOrEmpty(from)
                            ? "Instance definitions sent from Blender"
                            : "Blender definition \"" + from + "\"";
                        int added = _doc.InstanceDefinitions.Add(name, description, Point3d.Origin, objects);
                        if (added < 0)
                        {
                            Note("could not add block definition " + name + " to the document");
                            return null;
                        }
                        definition = _doc.InstanceDefinitions[added];
                    }

                    _done[index] = definition;
                    return definition;
                }
                finally
                {
                    _visiting.Remove(index);
                }
            }

            /// <summary>The canonical content hash of one definition: its own
            /// sections in protocol order, then one entry per nested row
            /// (the child's content-hash name and its transform), so two
            /// definitions with equal geometry but different children differ.</summary>
            private string ContentHash(string prefix, List<string> nestedNames)
            {
                var byName = new Dictionary<string, Section>();
                foreach (Section section in _frame.Sections)
                    if (section.Name.StartsWith(prefix, StringComparison.Ordinal))
                        byName[section.Name.Substring(prefix.Length)] = section;

                long[] nested;
                List<Transform> nestedTransforms;
                ReadRows(_frame, prefix, out nested, out nestedTransforms);

                using (var sha = SHA256.Create())
                {
                    foreach (string name in FrameCodec.OrderedNames(byName.Keys))
                        Update(sha, name, byName[name].Raw);
                    if (nested != null)
                    {
                        for (int i = 0; i < nested.Length && i < nestedNames.Count; i++)
                        {
                            Update(sha, "inst.ref_def", BitConverter.GetBytes((uint)nested[i]));
                            var m = Conversion.TransformToWire(nestedTransforms[i]);
                            var raw = new byte[m.Length * 4];
                            Buffer.BlockCopy(m, 0, raw, 0, raw.Length);
                            Update(sha, "inst.transforms", raw);
                        }
                        // The child names take part: the transform rows alone
                        // cannot tell two children with equal placement apart.
                        foreach (string childName in nestedNames)
                            Update(sha, "inst.name", System.Text.Encoding.UTF8.GetBytes(childName));
                    }
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    return Hex(sha.Hash, 16);
                }
            }

            private void Note(string message)
            {
                if (_note != null) _note(message);
            }
        }

        // ==================================================================
        //  Stage Out: GH_InstanceReference -> definition table + rows
        // ==================================================================

        /// <summary>Write GH_InstanceReference items into a response: definitions
        /// the rows reference go into <paramref name="definitions"/> (header) and
        /// their geometry into <paramref name="sections"/> as `idef.<i>.*`; the
        /// rows themselves land in `inst.ref_def` / `inst.transforms`. Does
        /// nothing when the list is empty.</summary>
        public static void Emit(IList<GH_InstanceReference> instances, double scale,
                                List<Section> sections,
                                List<Dictionary<string, object>> attributes,
                                List<Dictionary<string, object>> definitions,
                                List<string> warnings)
        {
            if (instances == null || instances.Count == 0) return;
            var writer = new DefinitionWriter(scale, sections, attributes, definitions, warnings);
            var refDef = new List<uint>();
            var transforms = new List<float>();
            foreach (GH_InstanceReference instance in instances)
            {
                if (instance == null || instance.Value == null)
                {
                    warnings.Add("An instance reference without a value was ignored");
                    continue;
                }
                int index = writer.Definition(instance.InstanceDefinition, instance.Value.ParentIdefId);
                if (index < 0) continue;
                refDef.Add((uint)index);
                transforms.AddRange(Conversion.TransformToWire(instance.Value.Xform));
            }
            if (refDef.Count == 0) return;
            sections.Add(Section.U32("inst.ref_def", refDef.ToArray()));
            sections.Add(Section.Floats("inst.transforms", transforms.ToArray()));
        }

        private class DefinitionWriter
        {
            private readonly double _scale;
            private readonly List<Section> _sections;
            private readonly List<Dictionary<string, object>> _attributes;
            private readonly List<Dictionary<string, object>> _definitions;
            private readonly List<string> _warnings;
            private readonly Dictionary<string, int> _byKey = new Dictionary<string, int>();

            public DefinitionWriter(double scale, List<Section> sections,
                                    List<Dictionary<string, object>> attributes,
                                    List<Dictionary<string, object>> definitions,
                                    List<string> warnings)
            {
                _scale = scale;
                _sections = sections;
                _attributes = attributes;
                _definitions = definitions;
                _warnings = warnings;
            }

            /// <summary>The table index for one instance's definition, writing
            /// the definition on first use. <paramref name="referenceId"/> is the
            /// doument id the instance names; it identifies the definition when
            /// the paired object carries none.</summary>
            public int Definition(ModelInstanceDefinition paired, Guid referenceId)
            {
                string key = Key(paired, referenceId);
                int known;
                if (key != null && _byKey.TryGetValue(key, out known)) return known;

                int index = _definitions.Count;
                var entry = new Dictionary<string, object>();
                entry["name"] = DisplayName(paired);
                _definitions.Add(entry);
                if (key != null) _byKey[key] = index;

                string prefix = "idef." + index + ".";
                var meshes = new List<Mesh>();
                var curves = new List<Curve>();
                var points = new List<Point3d>();
                var subds = new List<SubD>();
                var breps = new List<Brep>();
                var nested = new List<InstanceReferenceGeometry>();
                var sources = new List<GeometryBase>();

                if (paired != null && paired.Objects != null)
                {
                    foreach (ModelObject obj in paired.Objects)
                        CollectObject(obj, meshes, curves, points, subds, breps, nested, sources);
                }

                WriteGeometry(index, prefix, meshes, curves, points, subds, breps, nested, sources);
                return index;
            }

            /// <summary>A document definition by id, for a nested reference whose
            /// pairing is gone (a definition another document object owns).</summary>
            private int DefinitionFromDocument(Guid id)
            {
                string key = "id:" + id.ToString("N");
                int known;
                if (_byKey.TryGetValue(key, out known)) return known;

                RhinoDoc doc = RhinoDoc.ActiveDoc;
                InstanceDefinition docDef = doc == null ? null : doc.InstanceDefinitions.Find(id, true);
                if (docDef == null)
                {
                    _warnings.Add("A nested block (" + key + ") is not in the document and was skipped");
                    return -1;
                }

                int index = _definitions.Count;
                var entry = new Dictionary<string, object>();
                entry["name"] = docDef.Name ?? "";
                _definitions.Add(entry);
                _byKey[key] = index;

                string prefix = "idef." + index + ".";
                var meshes = new List<Mesh>();
                var curves = new List<Curve>();
                var points = new List<Point3d>();
                var subds = new List<SubD>();
                var breps = new List<Brep>();
                var nested = new List<InstanceReferenceGeometry>();
                var sources = new List<GeometryBase>();
                foreach (RhinoObject obj in docDef.GetObjects())
                    CollectGeometry(obj.Geometry, meshes, curves, points, subds, breps, nested, sources);

                WriteGeometry(index, prefix, meshes, curves, points, subds, breps, nested, sources);
                return index;
            }

            private void WriteGeometry(int index, string prefix,
                                       List<Mesh> meshes, List<Curve> curves, List<Point3d> points,
                                       List<SubD> subds, List<Brep> breps,
                                       List<InstanceReferenceGeometry> nested,
                                       List<GeometryBase> sources)
            {
                if (meshes.Count > 0 || curves.Count > 0 || points.Count > 0
                    || subds.Count > 0 || breps.Count > 0)
                {
                    Dictionary<string, object> counts;
                    List<Section> geometry = Conversion.SectionsFromGeometry(
                        meshes, curves, points, subds, breps, _scale, _warnings, out counts);
                    _sections.AddRange(Conversion.PrefixSections(geometry, prefix));
                    foreach (GeometryBase source in sources)
                    {
                        string group = AttributeStore.GroupOf(source);
                        if (group != null)
                            AttributeStore.Collect(source, group, _sections, _attributes, prefix, index);
                    }
                }

                if (nested.Count > 0)
                {
                    var refDef = new List<uint>();
                    var transforms = new List<float>();
                    foreach (InstanceReferenceGeometry reference in nested)
                    {
                        int child = DefinitionFromDocument(reference.ParentIdefId);
                        if (child < 0) continue;
                        refDef.Add((uint)child);
                        transforms.AddRange(Conversion.TransformToWire(reference.Xform));
                    }
                    if (refDef.Count > 0)
                    {
                        _sections.Add(Section.U32(prefix + "inst.ref_def", refDef.ToArray()));
                        _sections.Add(Section.Floats(prefix + "inst.transforms", transforms.ToArray()));
                    }
                }
            }

            private void CollectObject(ModelObject obj, List<Mesh> meshes, List<Curve> curves,
                                       List<Point3d> points, List<SubD> subds, List<Brep> breps,
                                       List<InstanceReferenceGeometry> nested,
                                       List<GeometryBase> sources)
            {
                GeometryBase geometry = GeometryOf(obj, nested);
                if (geometry != null)
                    CollectGeometry(geometry, meshes, curves, points, subds, breps, nested, sources);
            }

            /// <summary>One geometry out of a ModelObject, or null. Nested
            /// references go to ``nested`` instead of the geometry lists.</summary>
            private GeometryBase GeometryOf(ModelObject obj, List<InstanceReferenceGeometry> nested)
            {
                // ObjectType first: a cast chain would happily mesh a Brep or
                // edge-ify a mesh (GH_Convert Secondary), which is the lossy
                // direction for definitions.
                ObjectType type = obj.ObjectType;
                if (type == ObjectType.InstanceReference)
                {
                    InstanceReferenceGeometry reference;
                    if (obj.CastTo<InstanceReferenceGeometry>(out reference) && reference != null)
                    {
                        nested.Add(reference);
                        return null;
                    }
                }
                if (type == ObjectType.Mesh)
                {
                    Mesh mesh;
                    if (obj.CastTo<Mesh>(out mesh) && mesh != null) return mesh;
                }
                if (type == ObjectType.Brep || type == ObjectType.Surface)
                {
                    Brep brep;
                    if (obj.CastTo<Brep>(out brep) && brep != null) return brep;
                }
                if (type == ObjectType.Curve)
                {
                    Curve curve;
                    if (obj.CastTo<Curve>(out curve) && curve != null) return curve;
                }
                if (type == ObjectType.PointSet || type == ObjectType.Point)
                {
                    PointCloud cloud;
                    if (obj.CastTo<PointCloud>(out cloud) && cloud != null) return cloud;
                }
                if (type == ObjectType.SubD)
                {
                    SubD subd;
                    if (obj.CastTo<SubD>(out subd) && subd != null) return subd;
                }
                // Last resort: whichever of the transportable kinds it becomes.
                Mesh fallbackMesh;
                if (obj.CastTo<Mesh>(out fallbackMesh) && fallbackMesh != null) return fallbackMesh;
                Curve fallbackCurve;
                if (obj.CastTo<Curve>(out fallbackCurve) && fallbackCurve != null) return fallbackCurve;
                _warnings.Add("An object in a block definition (" + obj.TypeName + ") has no wire form and was skipped");
                return null;
            }

            private void CollectGeometry(GeometryBase geometry, List<Mesh> meshes, List<Curve> curves,
                                         List<Point3d> points, List<SubD> subds, List<Brep> breps,
                                         List<InstanceReferenceGeometry> nested,
                                         List<GeometryBase> sources)
            {
                if (geometry == null) return;
                Mesh mesh = geometry as Mesh;
                if (mesh != null) { meshes.Add(mesh); sources.Add(mesh); return; }
                SubD subd = geometry as SubD;
                if (subd != null) { subds.Add(subd); sources.Add(subd); return; }
                PointCloud cloud = geometry as PointCloud;
                if (cloud != null)
                {
                    points.AddRange(cloud.GetPoints());
                    sources.Add(cloud);
                    return;
                }
                Brep brep = geometry as Brep;
                if (brep != null) { breps.Add(brep); return; }
                Curve curve = geometry as Curve;
                if (curve != null) { curves.Add(curve); return; }
                InstanceReferenceGeometry reference = geometry as InstanceReferenceGeometry;
                if (reference != null) { nested.Add(reference); return; }
                if (geometry is Point) { points.Add(((Point)geometry).Location); return; }
                _warnings.Add("A block definition object (" + geometry.ObjectType + ") has no wire form and was skipped");
            }

            private static string Key(ModelInstanceDefinition paired, Guid referenceId)
            {
                if (paired != null && paired.Id.HasValue) return "id:" + paired.Id.Value.ToString("N");
                if (referenceId != Guid.Empty) return "id:" + referenceId.ToString("N");
                if (paired != null) return "obj:" + System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(paired);
                return null;
            }

            private static string DisplayName(ModelInstanceDefinition paired)
            {
                if (paired == null) return "";
                string display = paired.DisplayName;
                if (!string.IsNullOrEmpty(display)) return display;
                object rawName = paired.Name;
                if (rawName != null)
                {
                    string name = rawName.ToString();
                    if (!string.IsNullOrEmpty(name)) return name;
                }
                return "";
            }
        }

        // ----------------------------------------------------------------- utility

        private static void Update(HashAlgorithm sha, string name, byte[] raw)
        {
            byte[] encoded = System.Text.Encoding.UTF8.GetBytes(name);
            sha.TransformBlock(encoded, 0, encoded.Length, null, 0);
            sha.TransformBlock(new byte[] { 0 }, 0, 1, null, 0);
            sha.TransformBlock(raw, 0, raw.Length, null, 0);
        }

        private static string Hex(byte[] digest, int chars)
        {
            var text = new System.Text.StringBuilder();
            for (int i = 0; i < digest.Length && text.Length < chars; i++)
                text.Append(digest[i].ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
            return text.ToString();
        }
    }
}
