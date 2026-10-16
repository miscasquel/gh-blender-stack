// Spike S9: the two platform questions the attributes and blocks designs rest on.
//
// Runs inside Rhino (driven by spikes/s9/driver.py through the spike .gha,
// check=s9). Two families of checks, both of which a plain console cannot run
// (RhinoCommon's native half only loads under Rhino):
//
//  A. Attributes: the geometry UserDictionary survives the operations
//     Grasshopper components perform on geometry. If Duplicate() or
//     DuplicateGeometry() drop it, the whole "attributes ride on the geometry"
//     design is wrong and the fallback (custom UserData, or a side channel) has
//     to be chosen before any product code is written.
//
//  B. Blocks: a block definition can be built graph-side (ModelObject /
//     ModelInstanceDefinition), paired with a GH_InstanceReference, previewed
//     and baked without either a document definition or a document write until
//     the user bakes. The remaining uncertainty is what BakeGeometry does when
//     the definition is not in the document.
//
// Everything runs on the Rhino UI thread (RhinoApp.InvokeAndWait): document
// operations are not safe on the socket thread the request arrives on.
//
// Results are flat key -> string/bool pairs so the reply header stays canonical
// JSON without custom writers.

using System;
using System.Collections.Generic;
using System.Drawing;
using Grasshopper.Kernel.Types;
using Grasshopper.Rhinoceros.Model;
using GhStack.Core;
using Rhino;
using Rhino.DocObjects;
using Rhino.Geometry;

namespace GhStack.Spike.S9
{
    public static class S9Checks
    {
        public static Dictionary<string, object> Run()
        {
            var results = new Dictionary<string, object>();
            results["checks_revision"] = "3f (graph-side, transform trace)";
            try
            {
                RhinoApp.InvokeAndWait(delegate()
                {
                    try
                    {
                        AttributeChecks(results);
                    }
                    catch (Exception ex)
                    {
                        results["attributes_failure"] = Describe(ex);
                    }
                    try
                    {
                        BlockChecks(results);
                    }
                    catch (Exception ex)
                    {
                        results["blocks_failure"] = Describe(ex);
                    }
                    try
                    {
                        InstanceChecks(results);
                    }
                    catch (Exception ex)
                    {
                        results["instances_failure"] = Describe(ex);
                    }
                    try
                    {
                        NestedInstanceChecks(results);
                    }
                    catch (Exception ex)
                    {
                        results["nested_failure"] = Describe(ex);
                    }
                    try
                    {
                        NamedDefinitionChecks(results);
                    }
                    catch (Exception ex)
                    {
                        results["named_failure"] = Describe(ex);
                    }
                    try
                    {
                        GraphDefinitionChecks(results);
                    }
                    catch (Exception ex)
                    {
                        results["graph_failure"] = Describe(ex);
                    }
                });
            }
            catch (Exception ex)
            {
                results["invoke_failure"] = Describe(ex);
            }
            return results;
        }

        // ------------------------------------------------------------ attributes

        private static void AttributeChecks(Dictionary<string, object> r)
        {
            var mesh = Quad();
            FillDictionary(mesh.UserDictionary);
            r["dict_roundtrip"] = DictionarySurvived(mesh.UserDictionary) ? "ok" : "LOST VALUES";

            Mesh duplicate = mesh.DuplicateMesh();
            r["duplicate"] = DictionarySurvived(duplicate.UserDictionary) ? "ok" : "LOST ON DUPLICATE";
            duplicate.Transform(Transform.Scale(Point3d.Origin, 2.0) * Transform.Translation(1, 2, 3));
            r["transform"] = DictionarySurvived(duplicate.UserDictionary) ? "ok" : "LOST ON TRANSFORM";

            var goo = new GH_Mesh(mesh);
            var gooDuplicate = goo.DuplicateGeometry() as GH_Mesh;
            r["goo_duplicate"] = gooDuplicate != null && DictionarySurvived(gooDuplicate.Value.UserDictionary)
                ? "ok" : "LOST ON DUPLICATE_GEOMETRY";

            // The same question for every other geometry kind attributes ride on.
            var curve = new LineCurve(new Point3d(0, 0, 0), new Point3d(1, 0, 0));
            curve.UserDictionary.Set("name", "bridge");
            Curve curveDuplicate = curve.DuplicateCurve();
            curveDuplicate.Transform(Transform.Translation(1, 0, 0));
            r["curve"] = HasString(curveDuplicate.UserDictionary, "name") ? "ok" : "LOST";

            var cloud = new PointCloud();
            cloud.Add(new Point3d(0, 0, 0));
            cloud.Add(new Point3d(1, 0, 0), Color.Red);
            cloud.UserDictionary.Set("sample", 3.5);
            var cloudDuplicate = cloud.Duplicate() as PointCloud;
            r["pointcloud"] = cloudDuplicate != null && HasValue(cloudDuplicate.UserDictionary, "sample")
                ? "ok" : "LOST";

            SubD subd = SubD.CreateFromMesh(Quad());
            if (subd == null)
            {
                r["subd"] = "no subd";
            }
            else
            {
                subd.UserDictionary.Set("crease_plan", "uniform");
                var subdDuplicate = subd.Duplicate() as SubD;
                r["subd"] = subdDuplicate != null && HasString(subdDuplicate.UserDictionary, "crease_plan")
                    ? "ok" : "LOST";
            }

            // Baking is a document round trip: does the dictionary survive being
            // written into the document and read back?
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["bake"] = "no active document";
                return;
            }
            Guid id = doc.Objects.AddMesh(duplicate);
            RhinoObject obj = doc.Objects.FindId(id);
            Mesh stored = obj == null ? null : obj.Geometry as Mesh;
            r["bake"] = stored != null && DictionarySurvived(stored.UserDictionary)
                ? "ok" : "LOST ON BAKE";
            doc.Objects.Delete(id, true);
        }

        private static void FillDictionary(Rhino.Collections.ArchivableDictionary dict)
        {
            dict.Set("s", "hello");
            dict.Set("i", 7);
            dict.Set("d", 2.5);
            dict.Set("b", true);
            dict.Set("ints", new int[] { 1, 2, 3 });
            dict.Set("doubles", new double[] { 0.5, 1.5 });
            dict.Set("bools", new bool[] { true, false });
            dict.Set("bytes", new byte[] { 5, 6, 7, 8 });
            dict.Set("strings", new string[] { "a", "b" });
            dict.Set("vector", new Vector3d(1, 2, 3));
            dict.Set("point", new Point3d(4, 5, 6));
            dict.Set("color", Color.FromArgb(255, 10, 20, 30));
            var nested = new Rhino.Collections.ArchivableDictionary();
            nested.Set("tags", new string[] { "x" });
            dict.Set("nested", nested);
        }

        private static bool DictionarySurvived(Rhino.Collections.ArchivableDictionary dict)
        {
            object value;
            if (!dict.TryGetValue("s", out value) || !(value is string) || (string)value != "hello") return false;
            if (!dict.TryGetValue("ints", out value)) return false;
            int[] ints = value as int[];
            if (ints == null || ints.Length != 3 || ints[2] != 3) return false;
            if (!dict.TryGetValue("doubles", out value) || !(value is double[])) return false;
            if (!dict.TryGetValue("bools", out value) || !(value is bool[])) return false;
            if (!dict.TryGetValue("bytes", out value) || !(value is byte[])) return false;
            if (!dict.TryGetValue("strings", out value) || !(value is string[])) return false;
            if (!dict.TryGetValue("vector", out value) || !(value is Vector3d)) return false;
            if (!dict.TryGetValue("point", out value) || !(value is Point3d)) return false;
            if (!dict.TryGetValue("color", out value) || !(value is Color)) return false;
            if (!dict.TryGetValue("nested", out value)) return false;
            var nested = value as Rhino.Collections.ArchivableDictionary;
            if (nested == null || !nested.TryGetValue("tags", out value) || !(value is string[])) return false;
            return true;
        }

        private static bool HasString(Rhino.Collections.ArchivableDictionary dict, string key)
        {
            object value;
            return dict.TryGetValue(key, out value) && value is string;
        }

        private static bool HasValue(Rhino.Collections.ArchivableDictionary dict, string key)
        {
            object value;
            return dict.TryGetValue(key, out value) && value != null;
        }

        private static Mesh Quad()
        {
            var mesh = new Mesh();
            mesh.Vertices.Add(0, 0, 0);
            mesh.Vertices.Add(1, 0, 0);
            mesh.Vertices.Add(1, 1, 0);
            mesh.Vertices.Add(0, 1, 0);
            mesh.Faces.AddFace(0, 1, 2, 3);
            mesh.Normals.ComputeNormals();
            return mesh;
        }

        // ------------------------------------------------------------- instances

        /// <summary>Round 3 (2026-10-16, re-run for the graph-side route): the
        /// instances path end to end inside a live Rhino. A synthetic frame goes
        /// through StageGeometry.Build — named graph-side definitions, no
        /// document write — and straight back out through Instances.Emit; a bake
        /// proves the document block materializes, and the check cleans up
        /// everything it created.</summary>
        private static void InstanceChecks(Dictionary<string, object> r)
        {
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["instances_document"] = "no active document";
                return;
            }
            // Nothing is expected in the document until a bake; the table is
            // diffed around the build and the bake separately.
            var before = new HashSet<Guid>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false)) before.Add(idef.Id);

            var header = new Dictionary<string, object>();
            var table = new List<object>();
            var entry = new Dictionary<string, object>();
            entry["name"] = "S9 Quad";
            table.Add(entry);
            header["definitions"] = table;

            var attributes = new List<Dictionary<string, object>>();
            var load = new Dictionary<string, object>();
            load["group"] = "mesh";
            load["name"] = "load";
            load["domain"] = "POINT";
            load["type"] = "FLOAT";
            load["components"] = (long)1;
            load["count"] = (long)4;
            load["section"] = "idef.0.attr.0.values";
            load["definition"] = (long)0;
            attributes.Add(load);
            header["attributes"] = attributes;

            float[] rowA = new float[]
            {
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            };
            float[] rowB = new float[]
            {
                1, 0, 0, 5,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            };
            var transforms = new float[32];
            Array.Copy(rowA, 0, transforms, 0, 16);
            Array.Copy(rowB, 0, transforms, 16, 16);

            var sections = new List<Section>
            {
                Section.Floats("idef.0.mesh.verts", new float[]
                {
                    0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                }),
                Section.U32("idef.0.mesh.face_counts", new uint[] { 4 }),
                Section.U32("idef.0.mesh.face_indices", new uint[] { 0, 1, 2, 3 }),
                Section.Floats("idef.0.attr.0.values", new float[] { 0.5f, 1.0f, 1.5f, 2.0f }),
                Section.U32("inst.ref_def", new uint[] { 0, 0 }),
                Section.Floats("inst.transforms", transforms),
            };

            var notes = new List<string>();
            var frame = new DecodedFrame(header, sections);
            List<IGH_Goo> geometry = StageGeometry.Build(frame, 1.0, attributes,
                delegate(string note) { notes.Add(note); });
            r["instances_build"] = geometry.Count == 2
                ? "2 references"
                : "got " + geometry.Count + (notes.Count > 0 ? " (" + notes[0] + ")" : "");

            var references = new List<GH_InstanceReference>();
            foreach (IGH_Goo goo in geometry)
            {
                GH_InstanceReference reference = goo as GH_InstanceReference;
                if (reference != null) references.Add(reference);
            }
            if (references.Count > 0)
            {
                GH_InstanceReference first = references[0];
                r["instances_valid"] = first.IsValid.ToString();
                BoundingBox box = first.GetBoundingBox(Transform.Identity);
                r["instances_bbox"] = box.IsValid ? "valid" : "invalid";
                r["instances_reference_id"] = first.Value != null && first.Value.ParentIdefId != Guid.Empty
                    ? "set"
                    : "empty";
                if (references.Count > 1)
                    r["instances_reference_shared_id"] = references[1].Value != null
                        && references[1].Value.ParentIdefId == first.Value.ParentIdefId
                        ? "both rows share the definition's id"
                        : "rows carry different ids";
            }

            // The definition is graph-side and named, and its geometry carries
            // the per-definition attribute. The document does not change until
            // someone bakes.
            if (references.Count > 0 && references[0].InstanceDefinition != null)
            {
                ModelInstanceDefinition definition = references[0].InstanceDefinition;
                r["instances_definition"] = "'" + DefinitionName(definition) + "', IsValid="
                    + definition.IsValid + ", Objects="
                    + (definition.Objects == null ? "null" : definition.Objects.Count.ToString());
                Mesh definitionMesh = null;
                if (definition.Objects != null)
                {
                    foreach (ModelObject obj in definition.Objects)
                    {
                        Mesh mesh;
                        if (obj.CastTo<Mesh>(out mesh) && mesh != null)
                        {
                            definitionMesh = mesh;
                            break;
                        }
                    }
                }
                if (definitionMesh != null)
                {
                    string type, domain;
                    object values;
                    bool has = AttributeStore.TryGet(definitionMesh, "load", out type, out domain, out values);
                    r["instances_attribute"] = has ? "load=" + type + "/" + domain : "missing";
                }
                else
                {
                    r["instances_attribute"] = "no mesh in the definition";
                }
            }
            else
            {
                r["instances_definition"] = "missing";
            }
            r["instances_document_added"] = Added(doc, before).Count == 0
                ? "none (materialized at bake)"
                : Added(doc, before).Count + " at solve time";

            // Straight back out: the same items become the wire's table and rows.
            var outSections = new List<Section>();
            var outDescriptors = new List<Dictionary<string, object>>();
            var definitions = new List<Dictionary<string, object>>();
            var warnings = new List<string>();
            Instances.Emit(references, 1.0, outSections, outDescriptors, definitions, warnings);
            r["instances_emit_table"] = definitions.Count == 1
                ? "1 definition"
                : "got " + definitions.Count;
            Section refDef = Find(outSections, "inst.ref_def");
            r["instances_emit_rows"] = refDef != null && refDef.ToU32().Length == 2
                ? "2 rows"
                : "missing";
            Section outTransforms = Find(outSections, "inst.transforms");
            if (outTransforms != null && outTransforms.ToFloats().Length >= 16)
            {
                float[] back = outTransforms.ToFloats();
                bool translated = Math.Abs(back[16 + 3] - 5f) < 1e-6f;
                r["instances_emit_transform"] = translated ? "5,0,0 preserved" : "mismatch";
            }
            r["instances_emit_geometry"] = Find(outSections, "idef.0.mesh.verts") != null
                ? "idef.0.mesh.verts"
                : "missing";
            r["instances_emit_attribute"] = outDescriptors.Count == 1
                && Json.GetLong(outDescriptors[0], "definition") == 0
                ? "definition-tagged"
                : "missing";

            // Baking materializes the document block; the object goes away after
            // the check, and so do the definitions the bake added.
            if (references.Count > 0)
            {
                Guid baked = Guid.Empty;
                try
                {
                    bool ok = references[0].BakeGeometry(doc, new ObjectAttributes(), ref baked);
                    r["instances_bake"] = "baked=" + ok
                        + ", object=" + (baked == Guid.Empty ? "none" : "yes");
                    if (baked != Guid.Empty) doc.Objects.Delete(baked, true);
                }
                catch (Exception ex)
                {
                    r["instances_bake"] = Describe(ex);
                    if (baked != Guid.Empty) doc.Objects.Delete(baked, true);
                }
                foreach (InstanceDefinition idef in Added(doc, before))
                {
                    int index = doc.InstanceDefinitions.InstanceDefinitionIndex(idef.Id, true);
                    if (index >= 0)
                        doc.InstanceDefinitions.Delete(index, true, false);
                }
            }
        }

        /// <summary>Every document definition that appeared since \p before.</summary>
        private static List<InstanceDefinition> Added(RhinoDoc doc, HashSet<Guid> before)
        {
            var added = new List<InstanceDefinition>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false))
                if (!before.Contains(idef.Id)) added.Add(idef);
            return added;
        }

        /// <summary>A ModelInstanceDefinition's name as a string.</summary>
        private static string DefinitionName(ModelInstanceDefinition definition)
        {
            object rawName = definition.Name;
            if (rawName != null)
            {
                string name = rawName.ToString();
                if (!string.IsNullOrEmpty(name)) return name;
            }
            return definition.DisplayName ?? "";
        }

        /// <summary>Round 3b (2026-10-16, re-run for the graph-side route):
        /// nesting. A parent definition holds a triangle plus one row of the
        /// child quad; the top level holds one row of the parent. Checks the
        /// graph-side build, the bake (which materializes both blocks), and the
        /// emit path back to the wire (parent + child groups and the nested row
        /// pair).</summary>
        private static void NestedInstanceChecks(Dictionary<string, object> r)
        {
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["nested_document"] = "no active document";
                return;
            }
            var before = new HashSet<Guid>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false)) before.Add(idef.Id);

            var header = new Dictionary<string, object>();
            var table = new List<object>();
            var child = new Dictionary<string, object>();
            child["name"] = "S9 Child";
            table.Add(child);
            var parent = new Dictionary<string, object>();
            parent["name"] = "S9 Parent";
            table.Add(parent);
            header["definitions"] = table;

            var sections = new List<Section>
            {
                // Child: a quad.
                Section.Floats("idef.0.mesh.verts", new float[]
                {
                    0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                }),
                Section.U32("idef.0.mesh.face_counts", new uint[] { 4 }),
                Section.U32("idef.0.mesh.face_indices", new uint[] { 0, 1, 2, 3 }),
                // Parent: a triangle plus one row of the child, 2 up.
                Section.Floats("idef.1.mesh.verts", new float[]
                {
                    0, 0, 0, 1, 0, 0, 0, 1, 0,
                }),
                Section.U32("idef.1.mesh.face_counts", new uint[] { 3 }),
                Section.U32("idef.1.mesh.face_indices", new uint[] { 0, 1, 2 }),
                Section.U32("idef.1.inst.ref_def", new uint[] { 0 }),
                // Row-major, translation in the fourth column (protocol §5.2):
                // the child sits 2 up.
                Section.Floats("idef.1.inst.transforms", new float[]
                {
                    1, 0, 0, 0,
                    0, 1, 0, 0,
                    0, 0, 1, 2,
                    0, 0, 0, 1,
                }),
                // Top level: one row of the parent.
                Section.U32("inst.ref_def", new uint[] { 1 }),
                Section.Floats("inst.transforms", new float[]
                {
                    1, 0, 0, 0,
                    0, 1, 0, 0,
                    0, 0, 1, 0,
                    0, 0, 0, 1,
                }),
            };

            var frame = new DecodedFrame(header, sections);
            List<IGH_Goo> geometry = StageGeometry.Build(frame, 1.0, null, null);
            r["nested_build"] = geometry.Count == 1 ? "1 reference" : "got " + geometry.Count;
            GH_InstanceReference reference = geometry.Count > 0 ? geometry[0] as GH_InstanceReference : null;
            if (reference == null)
            {
                r["nested_valid"] = "no reference";
                return;
            }
            r["nested_valid"] = reference.IsValid.ToString();
            BoundingBox box = reference.GetBoundingBox(Transform.Identity);
            r["nested_bbox"] = box.IsValid
                ? Round(box.Min) + " .. " + Round(box.Max)
                : "invalid";

            // Where the row transform goes: parsed row, then the goo it becomes.
            long[] parsedRefs;
            List<Transform> parsedTransforms;
            if (Instances.ReadRows(frame, "idef.1.", out parsedRefs, out parsedTransforms))
                r["nested_row_transform"] = "def=" + parsedRefs[0]
                    + ", z=" + parsedTransforms[0].M23.ToString("0.###");
            if (reference.InstanceDefinition != null && reference.InstanceDefinition.Objects != null
                && reference.InstanceDefinition.Objects.Count > 1)
            {
                GH_InstanceReference childGoo;
                bool childCast = reference.InstanceDefinition.Objects[1]
                    .CastTo<GH_InstanceReference>(out childGoo) && childGoo != null && childGoo.Value != null;
                r["nested_child_transform"] = childCast
                    ? "z=" + childGoo.Value.Xform.M23.ToString("0.###")
                      + ", definition=" + (childGoo.InstanceDefinition == null ? "none" : "yes")
                    : "cast failed";
            }

            // What the pair carries, for the record: Stage Out reaches the child
            // through the goo cast, because CastTo<InstanceReferenceGeometry>
            // refuses a block-instance ModelObject (probed 2026-10-16).
            var pairedShapes = new List<string>();
            if (reference.InstanceDefinition != null && reference.InstanceDefinition.Objects != null)
            {
                foreach (ModelObject obj in reference.InstanceDefinition.Objects)
                {
                    InstanceReferenceGeometry asReference;
                    bool isReference = obj.CastTo<InstanceReferenceGeometry>(out asReference)
                        && asReference != null;
                    pairedShapes.Add(obj.TypeName + "/" + obj.ObjectType
                        + (isReference ? "/casts-to-reference" : ""));
                }
            }
            r["nested_paired_objects"] = pairedShapes.Count == 0
                ? "none"
                : string.Join(", ", pairedShapes.ToArray());

            r["nested_document_added"] = Added(doc, before).Count == 0
                ? "none (materialized at bake)"
                : Added(doc, before).Count + " at solve time";

            // Bake the nested reference: Rhino materializes parent and child.
            Guid bakedId = Guid.Empty;
            try
            {
                bool baked = reference.BakeGeometry(doc, new ObjectAttributes(), ref bakedId);
                string detail = "baked=" + baked
                    + ", object=" + (bakedId == Guid.Empty ? "none" : "yes");
                if (bakedId != Guid.Empty)
                {
                    RhinoObject obj = doc.Objects.FindId(bakedId);
                    BoundingBox objectBox = obj == null || obj.Geometry == null
                        ? BoundingBox.Unset
                        : obj.Geometry.GetBoundingBox(true);
                    detail += objectBox.IsValid
                        ? ", bbox=" + Round(objectBox.Min) + " .. " + Round(objectBox.Max)
                        : ", bbox=invalid";
                }
                r["nested_bake"] = detail;
                if (bakedId != Guid.Empty) doc.Objects.Delete(bakedId, true);
            }
            catch (Exception ex)
            {
                r["nested_bake"] = Describe(ex);
                if (bakedId != Guid.Empty) doc.Objects.Delete(bakedId, true);
            }
            var bakedDefinitions = new List<string>();
            foreach (InstanceDefinition idef in Added(doc, before))
                bakedDefinitions.Add("'" + idef.Name + "'");
            r["nested_baked_definitions"] = bakedDefinitions.Count == 0
                ? "none"
                : string.Join(", ", bakedDefinitions.ToArray());

            // Back out: the parent is index 0, the child 1, and the parent's own
            // row pair must name the child.
            var outSections = new List<Section>();
            var outDescriptors = new List<Dictionary<string, object>>();
            var definitions = new List<Dictionary<string, object>>();
            var warnings = new List<string>();
            Instances.Emit(new List<GH_InstanceReference> { reference }, 1.0,
                outSections, outDescriptors, definitions, warnings);
            var names = new List<string>();
            foreach (Dictionary<string, object> definition in definitions)
                names.Add(Json.GetString(definition, "name") ?? "");
            r["nested_emit_table"] = definitions.Count + " definition(s): " + string.Join(", ", names.ToArray());
            var found = new List<string>();
            foreach (Section section in outSections)
                if (section.Name.EndsWith(".mesh.verts", StringComparison.Ordinal))
                    found.Add(section.Name);
            r["nested_emit_geometry"] = found.Count == 0
                ? "none"
                : string.Join(", ", found.ToArray());
            Section nestedRows = Find(outSections, "idef.0.inst.ref_def");
            r["nested_emit_rows"] = nestedRows != null && nestedRows.ToU32().Length == 1
                && nestedRows.ToU32()[0] == 1
                ? "parent row names the child"
                : "missing";

            // Cleanup: everything that appeared over the whole check (the bake's
            // blocks included), by id — deleting one shifts the others' indexes.
            foreach (InstanceDefinition idef in Added(doc, before))
            {
                int index = doc.InstanceDefinitions.InstanceDefinitionIndex(idef.Id, true);
                if (index >= 0)
                    doc.InstanceDefinitions.Delete(index, true, false);
            }
        }

        /// <summary>Round 3d (owner's pointer, 2026-10-16): GH's own "Model Block
        /// Definition" component builds a definition through
        /// ModelInstanceDefinition.Attributes with a Name set; our round-2 probe
        /// set Objects only. This checks what a Name changes: validity, identity,
        /// whether the document gains a definition by itself, and whether a
        /// reference built from it previews and bakes with no document write at
        /// solve time. Everything it adds is cleaned up.</summary>
        private static void NamedDefinitionChecks(Dictionary<string, object> r)
        {
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["named_document"] = "no active document";
                return;
            }
            var before = new HashSet<Guid>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false)) before.Add(idef.Id);

            var modelMesh = Quad();
            var goo = new GH_Mesh(modelMesh);
            ModelObject modelObject = ModelObject.Cast(goo);
            if (modelObject == null)
            {
                r["named_model_object"] = "cast failed";
                return;
            }
            var objects = new List<ModelObject>();
            objects.Add(modelObject);

            ModelInstanceDefinition definition = null;
            var attributes = new ModelInstanceDefinition.Attributes();
            attributes.Objects = objects;
            attributes.Name = "S9 Named Block";

            try
            {
                var constructed = new ModelInstanceDefinition(attributes);
                r["named_ctor"] = "IsValid=" + constructed.IsValid
                    + ", Id=" + (constructed.Id == null ? "(none)" : "yes")
                    + ", Objects=" + (constructed.Objects == null ? "null" : constructed.Objects.Count.ToString());
                definition = constructed;
            }
            catch (Exception ex)
            {
                r["named_ctor"] = Describe(ex);
            }
            try
            {
                ModelInstanceDefinition cast = ModelInstanceDefinition.Cast(attributes);
                r["named_cast"] = cast == null
                    ? "null"
                    : "IsValid=" + cast.IsValid
                      + ", Id=" + (cast.Id == null ? "(none)" : "yes");
                if (definition == null) definition = cast;
            }
            catch (Exception ex)
            {
                r["named_cast"] = Describe(ex);
            }
            try
            {
                object data = attributes.ToModelData();
                r["named_model_data"] = data == null ? "null" : data.GetType().Name;
                ModelInstanceDefinition cast = ModelInstanceDefinition.Cast(data);
                if (cast != null)
                {
                    r["named_model_data_cast"] = "IsValid=" + cast.IsValid
                        + ", Id=" + (cast.Id == null ? "(none)" : "yes");
                    if (definition == null) definition = cast;
                }
            }
            catch (Exception ex)
            {
                r["named_model_data"] = Describe(ex);
            }

            // Did any of that create a document definition by itself?
            var added = new List<InstanceDefinition>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false))
                if (!before.Contains(idef.Id)) added.Add(idef);
            r["named_document_added"] = added.Count == 0
                ? "none"
                : added.Count + " (" + added[0].Name + ")";

            if (definition != null)
            {
                Guid id = definition.Id == null ? Guid.NewGuid() : definition.Id.Value;
                var reference = new GH_InstanceReference(
                    new InstanceReferenceGeometry(id, Transform.Translation(5, 0, 0)),
                    definition);
                r["named_reference_valid"] = reference.IsValid.ToString()
                    + " (" + reference.IsValidWhyNot + ")";
                try
                {
                    BoundingBox box = reference.GetBoundingBox(Transform.Identity);
                    r["named_reference_bbox"] = box.IsValid
                        ? Round(box.Min) + " .. " + Round(box.Max)
                        : "invalid";
                }
                catch (Exception ex)
                {
                    r["named_reference_bbox"] = Describe(ex);
                }
                Guid baked = Guid.Empty;
                try
                {
                    bool ok = reference.BakeGeometry(doc, new ObjectAttributes(), ref baked);
                    r["named_bake"] = "baked=" + ok
                        + ", object=" + (baked == Guid.Empty ? "none" : "yes");
                    if (baked != Guid.Empty) doc.Objects.Delete(baked, true);
                }
                catch (Exception ex)
                {
                    r["named_bake"] = Describe(ex);
                    if (baked != Guid.Empty) doc.Objects.Delete(baked, true);
                }
            }

            // Cleanup: anything the named path added to the document.
            foreach (InstanceDefinition idef in added)
            {
                int index = doc.InstanceDefinitions.InstanceDefinitionIndex(idef.Id, true);
                if (index >= 0)
                    doc.InstanceDefinitions.Delete(index, true, false);
            }
        }

        /// <summary>Round 3e (2026-10-16): the graph-side route the Model Block
        /// Definition component uses, for a switch of Stage In. Pins: does a
        /// GH_InstanceReference become a ModelObject (the component wires Block
        /// Instance into Block Definition's Objects for nesting)? Can that
        /// ModelObject come back as a GH_InstanceReference (transform and
        /// definition intact) for Stage Out? Does a graph-side parent nest and
        /// bake? And does baking two references of one definition make one
        /// document block or two?</summary>
        private static void GraphDefinitionChecks(Dictionary<string, object> r)
        {
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["graph_document"] = "no active document";
                return;
            }

            var modelMesh = Quad();
            var meshGoo = new GH_Mesh(modelMesh);
            ModelObject meshObject = ModelObject.Cast(meshGoo);
            if (meshObject == null)
            {
                r["graph_model_object"] = "mesh cast failed";
                return;
            }

            // Child definition, graph-side: a quad named "S9 Graph Child".
            var childAttributes = new ModelInstanceDefinition.Attributes();
            childAttributes.Objects = new List<ModelObject> { meshObject };
            childAttributes.Name = "S9 Graph Child";
            var child = new ModelInstanceDefinition(childAttributes);

            // The nested placement, as a goo wrapped into a ModelObject.
            var nestedGoo = new GH_InstanceReference(new InstanceReferenceGeometry(Guid.NewGuid(), Transform.Translation(0, 0, 2)));
            nestedGoo.InstanceDefinition = child;
            ModelObject nestedObject = ModelObject.Cast(nestedGoo);
            r["graph_nested_object"] = nestedObject == null
                ? "cast null"
                : nestedObject.TypeName + "/" + nestedObject.ObjectType;

            // The round trip Stage Out needs: object -> goo, transform intact.
            if (nestedObject != null)
            {
                GH_InstanceReference back;
                bool cast = nestedObject.CastTo<GH_InstanceReference>(out back) && back != null;
                r["graph_nested_cast"] = cast
                    ? "transform.z=" + back.ModelTransform.M03.ToString("0.###")
                      + ", definition=" + (back.InstanceDefinition == null ? "none" : "yes")
                    : "cast failed";
            }

            // Parent definition, graph-side: a triangle plus the nested child.
            var triangle = new Mesh();
            triangle.Vertices.Add(0, 0, 0);
            triangle.Vertices.Add(1, 0, 0);
            triangle.Vertices.Add(0, 1, 0);
            triangle.Faces.AddFace(0, 1, 2);
            triangle.Normals.ComputeNormals();
            ModelObject triangleObject = ModelObject.Cast(new GH_Mesh(triangle));
            var parentAttributes = new ModelInstanceDefinition.Attributes();
            var parentObjects = new List<ModelObject>();
            parentObjects.Add(triangleObject);
            if (nestedObject != null) parentObjects.Add(nestedObject);
            parentAttributes.Objects = parentObjects;
            parentAttributes.Name = "S9 Graph Parent";
            var parent = new ModelInstanceDefinition(parentAttributes);
            r["graph_parent"] = "IsValid=" + parent.IsValid
                + ", Objects=" + (parent.Objects == null ? "null" : parent.Objects.Count.ToString())
                + ", Id=" + (parent.Id == null ? "(none)" : "yes");

            var reference = new GH_InstanceReference(
                new InstanceReferenceGeometry(Guid.NewGuid(), Transform.Identity), parent);
            r["graph_reference_valid"] = reference.IsValid + " (" + reference.IsValidWhyNot + ")";
            BoundingBox box = reference.GetBoundingBox(Transform.Identity);
            r["graph_reference_bbox"] = box.IsValid
                ? Round(box.Min) + " .. " + Round(box.Max)
                : "invalid";

            // Does the nested content bake, placed 2 up?
            var before = new HashSet<Guid>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false)) before.Add(idef.Id);
            Guid baked = Guid.Empty;
            try
            {
                bool ok = reference.BakeGeometry(doc, new ObjectAttributes(), ref baked);
                string detail = "baked=" + ok + ", object=" + (baked == Guid.Empty ? "none" : "yes");
                if (baked != Guid.Empty)
                {
                    RhinoObject obj = doc.Objects.FindId(baked);
                    BoundingBox objectBox = obj == null || obj.Geometry == null
                        ? BoundingBox.Unset
                        : obj.Geometry.GetBoundingBox(true);
                    detail += objectBox.IsValid ? ", bbox=" + Round(objectBox.Min) + " .. " + Round(objectBox.Max) : ", bbox=invalid";
                }
                r["graph_bake"] = detail;
                // A second bake of the same graph-side definition: one block or two?
                Guid again = Guid.Empty;
                bool okAgain = reference.BakeGeometry(doc, new ObjectAttributes(), ref again);
                var added = new List<InstanceDefinition>();
                foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false))
                    if (!before.Contains(idef.Id)) added.Add(idef);
                r["graph_bake_twice"] = "baked=" + okAgain
                    + ", definitions added=" + added.Count
                    + (added.Count > 0 ? " ('" + added[0].Name + "')" : "");
                if (again != Guid.Empty) doc.Objects.Delete(again, true);

                // Cleanup: the objects and definitions this check created.
                foreach (InstanceDefinition idef in added)
                {
                    int index = doc.InstanceDefinitions.InstanceDefinitionIndex(idef.Id, true);
                    if (index >= 0)
                        doc.InstanceDefinitions.Delete(index, true, false);
                }
            }
            catch (Exception ex)
            {
                r["graph_bake"] = Describe(ex);
            }
            finally
            {
                if (baked != Guid.Empty) doc.Objects.Delete(baked, true);
            }
        }

        private static Section Find(List<Section> sections, string name)
        {
            foreach (Section section in sections)
                if (section.Name == name) return section;
            return null;
        }

        // ---------------------------------------------------------------- blocks

        private static void BlockChecks(Dictionary<string, object> r)
        {
            var mesh = Quad();
            var goo = new GH_Mesh(mesh);

            ModelObject modelObject = ModelObject.Cast(goo);
            if (modelObject == null) modelObject = ModelObject.Cast(mesh);
            if (modelObject == null)
            {
                r["model_object"] = "CAST FAILED (both GH_Mesh and Mesh)";
                return;
            }
            r["model_object"] = "ok, IsValid=" + modelObject.IsValid + ", TypeName=" + modelObject.TypeName;

            var objects = new List<ModelObject>();
            objects.Add(modelObject);
            var attributes = new ModelInstanceDefinition.Attributes();
            attributes.Objects = objects;
            var definition = new ModelInstanceDefinition(attributes);
            r["definition"] = "IsValid=" + definition.IsValid
                + ", Objects=" + (definition.Objects == null ? "null" : definition.Objects.Count.ToString())
                + ", Id=" + (definition.Id == null ? "(none)" : definition.Id.ToString());

            Guid definitionId = definition.Id == null ? Guid.NewGuid() : definition.Id.Value;
            var reference = new InstanceReferenceGeometry(definitionId,
                Transform.Translation(5, 0, 0) * Transform.Rotation(0.5, Point3d.Origin));
            var ghReference = new GH_InstanceReference(reference, definition);
            r["reference_valid"] = ghReference.IsValid;
            r["reference_idef"] = ghReference.InstanceDefinition != null;
            r["reference_loaded_before"] = ghReference.IsGeometryLoaded;
            try
            {
                BoundingBox box = ghReference.GetBoundingBox(Transform.Identity);
                r["reference_bbox"] = box.IsValid
                    ? "valid: " + Round(box.Min) + " .. " + Round(box.Max)
                    : "invalid";
            }
            catch (Exception ex)
            {
                r["reference_bbox"] = Describe(ex);
            }
            try
            {
                r["reference_load"] = ghReference.LoadGeometry()
                    ? "loaded without a document definition"
                    : "did not load";
            }
            catch (Exception ex)
            {
                r["reference_load"] = Describe(ex);
            }
            GH_InstanceReference duplicate = ghReference.DuplicateInstanceReference();
            r["reference_duplicate"] = duplicate == null ? "null" : "ok, idef=" + (duplicate.InstanceDefinition != null);

            // The "under the hood" question: baking a reference whose definition
            // is not in the document. Round 1 answered it (baked=False, and that
            // attempt is what raised Rhino's "about to bake an invalid block
            // instance" warning). The bake is therefore only attempted while the
            // reference is valid — the warning was the probe's own doing, and a
            // warning-free run is the controlled check that nothing else raises
            // it (owner's question, 2026-10-16).
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["bake_reference"] = "no active document";
                return;
            }
            if (!ghReference.IsValid)
            {
                r["bake_reference"] = "skipped (invalid reference; round 1 recorded the warning)";
            }
            else
            {
                var before = new HashSet<Guid>();
                foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false)) before.Add(idef.Id);
                Guid bakedId = Guid.Empty;
                try
                {
                    bool ok = ghReference.BakeGeometry(doc, new ObjectAttributes(), ref bakedId);
                    var added = new List<InstanceDefinition>();
                    foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false))
                        if (!before.Contains(idef.Id)) added.Add(idef);
                    r["bake_reference"] = "baked=" + ok
                        + ", object=" + (bakedId == Guid.Empty ? "none" : "yes")
                        + ", definitions added=" + added.Count;
                    if (bakedId != Guid.Empty) doc.Objects.Delete(bakedId, true);
                    foreach (InstanceDefinition idef in added)
                        doc.InstanceDefinitions.Delete(idef.Index, true, false);
                }
                catch (Exception ex)
                {
                    r["bake_reference"] = Describe(ex);
                    if (bakedId != Guid.Empty) doc.Objects.Delete(bakedId, true);
                }
            }

            // Round 1 (2026-10-16): a definition assembled from Attributes alone
            // is invalid (no Id), its reference does not preview, and baking it
            // is a no-op — baking it from the canvas raises Rhino's "invalid
            // block instance" warning. These probes try the ways out: ask the
            // public Cast for an identity, and fall back to a document
            // definition. (Attribs and IsValidWhyNot are not public, so the
            // constructor route cannot carry the objects.)
            var withObjects = new ModelInstanceDefinition.Attributes();
            withObjects.Objects = objects;
            try
            {
                // The public ways to a definition: Cast from the Attributes, from
                // the object list, or from one object. Report which, if any,
                // yields a valid definition with content.
                ModelInstanceDefinition cast = ModelInstanceDefinition.Cast(withObjects);
                string source = "attributes";
                if (cast == null)
                {
                    cast = ModelInstanceDefinition.Cast(objects);
                    source = "objects";
                }
                if (cast == null && modelObject != null)
                {
                    cast = ModelInstanceDefinition.Cast(modelObject);
                    source = "object";
                }
                r["definition_cast"] = cast == null ? "null (attributes, objects, object all failed)"
                    : "from " + source + ": IsValid=" + cast.IsValid
                      + ", Objects=" + (cast.Objects == null ? "null" : cast.Objects.Count.ToString())
                      + ", Id=" + (cast.Id == null ? "(none)" : "yes");
                if (cast != null)
                {
                    Guid castId = cast.Id == null ? Guid.NewGuid() : cast.Id.Value;
                    var castReference = new GH_InstanceReference(
                        new InstanceReferenceGeometry(castId, Transform.Translation(5, 0, 0)), cast);
                    r["reference_cast"] = "IsValid=" + castReference.IsValid
                        + ", whyNot=" + castReference.IsValidWhyNot;
                    BoundingBox box = castReference.GetBoundingBox(Transform.Identity);
                    r["reference_cast_bbox"] = box.IsValid ? "valid" : "invalid";
                }
            }
            catch (Exception ex)
            {
                r["definition_cast"] = Describe(ex);
            }

            // A document definition is the fallback: build one, reference it,
            // preview it, bake it, then put the document back as it was.
            try
            {
                int docIndex = doc.InstanceDefinitions.Add("GHStack S9 probe", "spike probe",
                    Point3d.Origin, new GeometryBase[] { mesh });
                if (docIndex < 0)
                {
                    r["doc_definition"] = "InstanceDefinitions.Add returned " + docIndex;
                }
                else
                {
                    InstanceDefinition docDef = doc.InstanceDefinitions[docIndex];
                    var docReference = new GH_InstanceReference(
                        new InstanceReferenceGeometry(docDef.Id, Transform.Translation(5, 0, 0)),
                        new ModelInstanceDefinition(docDef));
                    r["doc_reference_valid"] = docReference.IsValid;
                    BoundingBox box = docReference.GetBoundingBox(Transform.Identity);
                    r["doc_reference_bbox"] = box.IsValid
                        ? "valid: " + Round(box.Min) + " .. " + Round(box.Max)
                        : "invalid";
                    Guid docBaked = Guid.Empty;
                    bool docOk = docReference.BakeGeometry(doc, new ObjectAttributes(), ref docBaked);
                    r["doc_reference_bake"] = "baked=" + docOk
                        + ", object=" + (docBaked == Guid.Empty ? "none" : "yes");
                    if (docBaked != Guid.Empty) doc.Objects.Delete(docBaked, true);
                    doc.InstanceDefinitions.Delete(docIndex, true, false);
                }
            }
            catch (Exception ex)
            {
                r["doc_definition"] = Describe(ex);
            }
        }

        private static string Round(Point3d p)
        {
            return "(" + p.X.ToString("0.###") + ", " + p.Y.ToString("0.###") + ", " + p.Z.ToString("0.###") + ")";
        }

        private static string Describe(Exception ex)
        {
            string m = ex.Message;
            if (string.IsNullOrEmpty(m)) return ex.GetType().Name;
            int i = m.IndexOf('\n');
            return ex.GetType().Name + ": " + (i > 0 ? m.Substring(0, i) : m);
        }
    }
}
