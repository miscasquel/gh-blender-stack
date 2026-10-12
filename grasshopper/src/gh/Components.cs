// The GH Stack components: Stage In, Stage Out, Param (plan §10).
//
// Stage In  reads the stage's latest slot and outputs the geometry Blender sent.
// Stage Out converts the graph's result back to sections and replies.
// Param     declares one parameter and outputs its current value.
//
// Runtime messages only, never dialogs (plan §3, principle 8).

using System;
using System.Collections.Generic;
using System.Drawing;
using GH_IO.Serialization;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Types;
using Rhino.Geometry;
using GhStack.Core;

namespace GhStack
{
    // ------------------------------------------------------------------ Stage In

    public class StageInComponent : GH_Component
    {
        public StageInComponent()
            : base("Stage In", "Stage In",
                   "Receives geometry from Blender for one stage key.",
                   "GH Stack", "Stack")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("a1f0c2d4-1b2e-4c3a-9d4f-5e6a7b8c9d01"); }
        }

        protected override Bitmap Icon
        {
            get { return Icons.Get("import"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key, copied from the GH Stack panel in Blender", GH_ParamAccess.item);
            p.AddBooleanParameter("Enabled", "On", "Report this stage to Blender", GH_ParamAccess.item);
            p[0].Optional = true;
            ((Grasshopper.Kernel.Parameters.Param_Boolean)p[1]).PersistentData.Append(new GH_Boolean(true));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddMeshParameter("Geometry", "G", "Mesh received from Blender", GH_ParamAccess.item);
            p.AddTextParameter("Request ID", "R", "Request this geometry belongs to", GH_ParamAccess.item);
            p.AddTextParameter("Status", "S", "Stage status", GH_ParamAccess.item);
            p.AddTextParameter("Message", "M", "Diagnostics", GH_ParamAccess.item);
            // Appended after the original outputs so documents saved before the
            // curve/point/SubD work keep their wires.
            p.AddCurveParameter("Curves", "C", "Curves received from Blender", GH_ParamAccess.list);
            p.AddPointParameter("Points", "P", "Points received from Blender", GH_ParamAccess.list);
            p.AddSubDParameter("SubD", "D", "SubD cage received from Blender", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            string key = "";
            bool enabled = true;
            da.GetData("Key", ref key);
            da.GetData("Enabled", ref enabled);
            GhLink.Register(this);
            foreach (string note in GhLink.TakeNotes())
                AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);
            if (string.IsNullOrEmpty(key))
            {
                // Unconfigured components stay quiet: no key, nothing to do.
                Message = "no key";
                da.SetData("Geometry", null);
                da.SetData("Request ID", "");
                da.SetData("Status", "no key");
                da.SetData("Message", "connect the stage key from Blender's GH Stack panel");
                return;
            }
            GhLink.PublishStage(key, enabled);

            StageSlot slot = GhLink.Slot(key);
            Mesh mesh = null;
            var curves = new List<Curve>();
            var points = new List<Point3d>();
            SubD subd = null;
            string status = slot.Status;
            if (slot.Sections != null && slot.Sections.Count > 0)
            {
                var frame = new DecodedFrame(new Dictionary<string, object>(), slot.Sections);
                double scale = GhLink.DocumentScale();
                mesh = Conversion.MeshFromSections(frame, scale);
                curves = Conversion.CurvesFromSections(frame, scale);
                points = Conversion.PointsFromSections(frame, scale);
                if (frame.Find("subd.verts") != null)
                {
                    var notes = new List<string>();
                    subd = Conversion.SubDFromSections(frame, scale, notes);
                    foreach (string note in notes)
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);
                }
            }
            else
            {
                status = GhLink.Connected ? "waiting" : "disconnected";
            }
            da.SetData("Geometry", mesh);
            da.SetDataList("Curves", curves);
            da.SetDataList("Points", points);
            da.SetData("SubD", subd);
            da.SetData("Request ID", slot.Rid);
            da.SetData("Status", status);
            da.SetData("Message", slot.Message);
            Message = GhLink.Connected ? status : "disconnected";
            GhLink.Publish(OnPingDocument());
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            GhLink.Register(this);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            GhLink.Unregister(this);
        }
    }

    // ----------------------------------------------------------------- Stage Out

    public class StageOutComponent : GH_Component
    {
        public StageOutComponent()
            : base("Stage Out", "Stage Out",
                   "Sends the graph's result back to Blender for one stage key.",
                   "GH Stack", "Stack")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("b2e1d3c5-2c3f-4d4b-8e5a-6f7b8c9d0e12"); }
        }

        protected override Bitmap Icon
        {
            get { return Icons.Get("export"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key, copied from the GH Stack panel in Blender", GH_ParamAccess.item);
            // One generic input: mesh, curves, points, SubD, or Brep. A Brep (or
            // surface) is meshed on the way out, with a warning.
            p.AddGenericParameter("Geometry", "G", "The graph's result: mesh, curve, point, SubD, or Brep", GH_ParamAccess.list);
            p.AddBooleanParameter("Enabled", "On", "Send results to Blender", GH_ParamAccess.item);
            p[0].Optional = true;
            p[1].Optional = true;
            ((Grasshopper.Kernel.Parameters.Param_Boolean)p[2]).PersistentData.Append(new GH_Boolean(true));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddBooleanParameter("Sent", "S", "A response was queued for Blender", GH_ParamAccess.item);
            p.AddTextParameter("Output hash", "H", "Hash of the sections that were sent", GH_ParamAccess.item);
            p.AddTextParameter("Status", "T", "Stage status", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            string key = "";
            bool enabled = true;
            var goos = new List<IGH_Goo>();
            da.GetData("Key", ref key);
            da.GetData("Enabled", ref enabled);
            da.GetDataList("Geometry", goos);
            GhLink.Register(this);
            foreach (string note in GhLink.TakeNotes())
                AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);
            if (string.IsNullOrEmpty(key))
            {
                Message = "no key";
                da.SetData("Sent", false);
                da.SetData("Output hash", "");
                da.SetData("Status", "no key");
                return;
            }
            GhLink.PublishStage(key, enabled);

            StageSlot slot = GhLink.Slot(key);
            bool sent = false;
            string outHash = "";
            string status = slot.Status;

            if (enabled && GhLink.Connected)
            {
                try
                {
                    var meshes = new List<Mesh>();
                    var curves = new List<Curve>();
                    var points = new List<Point3d>();
                    var subds = new List<SubD>();
                    var breps = new List<Brep>();
                    var ignored = new List<string>();
                    GooDispatch.Collect(goos, meshes, curves, points, subds, breps, ignored);
                    foreach (string typeName in ignored)
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Remark,
                            "ignored unsupported geometry: " + typeName);

                    var warnings = new List<string>();
                    Dictionary<string, object> counts;
                    List<Section> sections = Conversion.SectionsFromGeometry(
                        meshes, curves, points, subds, breps, GhLink.DocumentScale(),
                        warnings, out counts);

                    var warningList = new List<object>();
                    foreach (string w in warnings)
                    {
                        var entry = new Dictionary<string, object>();
                        entry["code"] = "conversion";
                        entry["message"] = w;
                        warningList.Add(entry);
                    }

                    outHash = FrameCodec.OutputHash(sections);
                    var header = new Dictionary<string, object>();
                    header["type"] = "response";
                    header["stage"] = key;
                    header["rid"] = slot.Rid;
                    header["out_hash"] = outHash;
                    header["status"] = "ok";
                    header["warnings"] = warningList;
                    header["counts"] = counts;
                    GhLink.Enqueue(FrameCodec.Encode(header, sections));
                    sent = true;
                    status = "ok";
                    slot.Status = "ok";
                    foreach (string w in warnings)
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, w);
                }
                catch (Exception ex)
                {
                    status = "error";
                    AddRuntimeMessage(GH_RuntimeMessageLevel.Error, "Stage Out: " + ex.Message);
                }
            }
            da.SetData("Sent", sent);
            da.SetData("Output hash", outHash);
            da.SetData("Status", status);
            Message = GhLink.Connected ? status : "disconnected";
            GhLink.Publish(OnPingDocument());
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            GhLink.Register(this);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            GhLink.Unregister(this);
        }
    }

    // -------------------------------------------------------------------- Param

    public class ParamComponent : GH_Component
    {
        public ParamComponent()
            : base("Param", "Param",
                   "Declares a parameter Blender can drive. Connect it into the graph.",
                   "GH Stack", "Stack")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("c3f2e4d6-3d4a-4e5c-9f6b-7a8b9c0d1e23"); }
        }

        protected override Bitmap Icon
        {
            get { return Icons.Get("driver"); }
        }

        private Guid _paramId = Guid.NewGuid();

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key this parameter belongs to", GH_ParamAccess.item);
            p.AddTextParameter("Label", "L", "Display label", GH_ParamAccess.item);
            p.AddIntegerParameter("Type", "T", "0 float, 1 int, 2 bool, 3 vector3, 4 color, 5 value list, 6 text", GH_ParamAccess.item);
            p.AddTextParameter("Default", "D", "Default value as text", GH_ParamAccess.item);
            p.AddNumberParameter("Min", "m", "Minimum", GH_ParamAccess.item);
            p.AddNumberParameter("Max", "M", "Maximum", GH_ParamAccess.item);
            p.AddNumberParameter("Step", "s", "Step", GH_ParamAccess.item);
            p.AddTextParameter("Values", "V", "Value list entries: a list of strings, or one per line in a Panel", GH_ParamAccess.list);
            p.AddTextParameter("Group", "G", "Group label", GH_ParamAccess.item);
            for (int i = 1; i < 9; i++) p[i].Optional = true;
            // A neutral default so an unconnected Param is already useful. Min and
            // Max stay empty: a parameter has no range until one is given.
            ((Grasshopper.Kernel.Parameters.Param_String)p[1]).PersistentData.Append(new GH_String("parameter"));
            ((Grasshopper.Kernel.Parameters.Param_Integer)p[2]).PersistentData.Append(new GH_Integer(0));
            ((Grasshopper.Kernel.Parameters.Param_String)p[3]).PersistentData.Append(new GH_String("1.0"));
            ((Grasshopper.Kernel.Parameters.Param_Number)p[6]).PersistentData.Append(new GH_Number(0.1));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddGenericParameter("Value", "V", "The value Blender sent, or the default", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            string key = "";
            string label = "parameter";
            int typeIndex = 0;
            string defaultText = "";
            double min = 0.0, max = 0.0, step = 0.01;
            var valueStrings = new List<string>();
            string group = "";
            da.GetData("Key", ref key);
            da.GetData("Label", ref label);
            da.GetData("Type", ref typeIndex);
            da.GetData("Default", ref defaultText);
            bool hasMin = da.GetData("Min", ref min);
            bool hasMax = da.GetData("Max", ref max);
            da.GetData("Step", ref step);
            da.GetDataList("Values", valueStrings);
            da.GetData("Group", ref group);
            GhLink.Register(this);

            if (string.IsNullOrEmpty(key))
            {
                Message = "no key";
                da.SetData("Value", null);
                return;
            }
            string type = TypeName(typeIndex);
            StageSlot slot = GhLink.Slot(key);
            object value = null;
            if (slot.Params.ContainsKey(_paramId.ToString()))
                value = slot.Params[_paramId.ToString()];
            if (value == null) value = ParamValues.ParseDefault(type, defaultText);
            // Coerce once to the declared type: the value may be left over from a
            // previous type, and the default must never be published untyped (a
            // text default of "[0.0, 0.0, 0.0]" is how a float parameter used to
            // blow up on the next solve).
            value = ParamValues.Cast(type, value);

            var declaration = new Dictionary<string, object>();
            declaration["id"] = _paramId.ToString();
            declaration["label"] = label;
            declaration["type"] = type;
            declaration["default"] = ParamValues.JsonValue(value);
            // No range unless one is given: Blender then leaves the property
            // unconstrained instead of inventing 0..1.
            declaration["min"] = hasMin ? (object)min : null;
            declaration["max"] = hasMax ? (object)max : null;
            declaration["step"] = step;
            var valueList = new List<object>();
            foreach (string item in valueStrings)
            {
                // A Panel arrives as one string with newlines; a List arrives as
                // one string per item. Both work.
                foreach (string v in (item ?? "").Split('\n'))
                    if (v.Trim().Length > 0) valueList.Add(v.Trim());
            }
            declaration["values"] = valueList;
            declaration["group"] = group;
            GhLink.PublishParam(key, declaration);

            da.SetData("Value", OutputValue(type, value));
            Message = type + (GhLink.Connected ? "" : " (disconnected)");
            GhLink.Publish(OnPingDocument());
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            GhLink.Register(this);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            GhLink.Unregister(this);
        }

        // The GUID persists with the document, so keyframes survive a rename
        // (plan §5).
        public override bool Write(GH_IWriter writer)
        {
            writer.SetString("param_id", _paramId.ToString());
            return base.Write(writer);
        }

        public override bool Read(GH_IReader reader)
        {
            string stored = "";
            if (reader.TryGetString("param_id", ref stored))
            {
                try { _paramId = new Guid(stored); }
                catch (Exception) { }
            }
            return base.Read(reader);
        }

        private static string TypeName(int index)
        {
            switch (index)
            {
                case 1: return "int";
                case 2: return "bool";
                case 3: return "vector3";
                case 4: return "color";
                case 5: return "value_list";
                case 6: return "text";
                default: return "float";
            }
        }

        /// <summary>The component's typed output. The conversion itself lives in
        /// the core (ParamValues), where the console tests can check it.</summary>
        private static object OutputValue(string type, object value)
        {
            object cast = ParamValues.Cast(type, value);
            if (type == "vector3")
            {
                double[] v = (double[])cast;
                return new Point3d(v[0], v[1], v[2]);
            }
            if (type == "color")
            {
                double[] c = (double[])cast;
                return System.Drawing.Color.FromArgb(
                    255,
                    (int)Math.Round(Math.Min(1.0, Math.Max(0.0, c[0])) * 255.0),
                    (int)Math.Round(Math.Min(1.0, Math.Max(0.0, c[1])) * 255.0),
                    (int)Math.Round(Math.Min(1.0, Math.Max(0.0, c[2])) * 255.0));
            }
            return cast;
        }
    }
}
