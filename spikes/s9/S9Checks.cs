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

        /// <summary>Round 3 (2026-10-16): the instances path end to end inside a
        /// live Rhino. A synthetic frame goes through StageGeometry.Build (the
        /// definitions become document definitions, the rows become
        /// GH_InstanceReference items) and straight back out through
        /// Instances.Emit; the check then cleans the definitions it added.</summary>
        private static void InstanceChecks(Dictionary<string, object> r)
        {
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["instances_document"] = "no active document";
                return;
            }
            // Leave no leftovers from an earlier run: unreferenced `GHStack *`
            // definitions in the open document are this product's (or this
            // check's), and a reused one would hide the attribute check below.
            var stale = new List<InstanceDefinition>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false))
                if (idef.Name != null && idef.Name.StartsWith(Instances.DefinitionPrefix, StringComparison.Ordinal)
                    && idef.UseCount() == 0)
                    stale.Add(idef);
            foreach (InstanceDefinition idef in stale)
                doc.InstanceDefinitions.Delete(idef.Index, true, false);
            r["instances_preclean"] = stale.Count == 0
                ? "none"
                : stale.Count + " stale definition(s) removed";

            // The check owns whatever definition Build adds; the table is
            // diffed rather than trusting an id accessor (ModelInstanceDefinition.Id
            // is not populated for document-backed definitions, probed 2026-10-16).
            var before = new HashSet<Guid>();
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false)) before.Add(idef.Id);
            InstanceDefinition added = null;

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
                r["instances_reference_id"] = first.Value != null
                    && first.Value.ParentIdefId != Guid.Empty
                    && !before.Contains(first.Value.ParentIdefId)
                    ? "names a definition this check added"
                    : "does not match an added definition";
            }

            // The definition is in the document, content-hash named, and its
            // geometry carries the per-definition attribute.
            InstanceDefinition definition = Newest(doc, before);
            added = definition;
            if (definition != null)
            {
                RhinoObject[] objects = definition.GetObjects();
                r["instances_definition"] = "document definition '" + definition.Name
                    + "', objects=" + objects.Length;
                if (objects.Length > 0)
                {
                    string type, domain;
                    object values;
                    bool has = AttributeStore.TryGet(objects[0].Geometry, "load", out type, out domain, out values);
                    r["instances_attribute"] = has ? "load=" + type + "/" + domain : "missing";
                }
            }
            else
            {
                r["instances_definition"] = "missing";
            }

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

            // Cleanup: the check added the definition; put the document back.
            if (added != null)
                doc.InstanceDefinitions.Delete(added.Index, true, false);
        }

        /// <summary>The definition that appeared since \p before, or null.</summary>
        private static InstanceDefinition Newest(RhinoDoc doc, HashSet<Guid> before)
        {
            foreach (InstanceDefinition idef in doc.InstanceDefinitions.GetList(false))
                if (!before.Contains(idef.Id)) return idef;
            return null;
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
            // is not in the document. Record what the document gains, then undo.
            RhinoDoc doc = RhinoDoc.ActiveDoc;
            if (doc == null)
            {
                r["bake_reference"] = "no active document";
                return;
            }
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
