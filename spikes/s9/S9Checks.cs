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
