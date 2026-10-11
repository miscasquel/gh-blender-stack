// The GH Stack components: Stage In, Stage Out, Param (plan §10).
//
// Stage In  reads the stage's latest slot and outputs the geometry Blender sent.
// Stage Out converts the graph's result back to sections and replies.
// Param     declares one parameter and outputs its current value.
//
// Runtime messages only, never dialogs (plan §3, principle 8).

using System;
using System.Collections.Generic;
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

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key, copied from the GH Stack panel in Blender", GH_ParamAccess.item);
            p.AddBooleanParameter("Enabled", "On", "Report this stage to Blender", GH_ParamAccess.item);
            ((Grasshopper.Kernel.Parameters.Param_Boolean)p[1]).PersistentData.Append(new GH_Boolean(true));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddMeshParameter("Geometry", "G", "Geometry received from Blender", GH_ParamAccess.item);
            p.AddTextParameter("Request ID", "R", "Request this geometry belongs to", GH_ParamAccess.item);
            p.AddTextParameter("Status", "S", "Stage status", GH_ParamAccess.item);
            p.AddTextParameter("Message", "M", "Diagnostics", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            string key = "";
            bool enabled = true;
            da.GetData("Key", ref key);
            da.GetData("Enabled", ref enabled);
            GhLink.Register(this);
            GhLink.PublishStage(key, enabled);

            foreach (string note in GhLink.TakeNotes())
                AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);

            StageSlot slot = GhLink.Slot(key);
            Mesh mesh = null;
            string status = slot.Status;
            if (slot.Sections != null && slot.Sections.Count > 0)
            {
                mesh = Conversion.MeshFromSections(
                    new DecodedFrame(new Dictionary<string, object>(), slot.Sections), GhLink.DocumentScale());
            }
            else
            {
                status = GhLink.Connected ? "waiting" : "disconnected";
            }
            da.SetData("Geometry", mesh);
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

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key, copied from the GH Stack panel in Blender", GH_ParamAccess.item);
            p.AddMeshParameter("Geometry", "G", "The graph's result for this stage", GH_ParamAccess.item);
            p.AddBooleanParameter("Enabled", "On", "Send results to Blender", GH_ParamAccess.item);
            ((Grasshopper.Kernel.Parameters.Param_Boolean)p[2]).PersistentData.Append(new GH_Boolean(true));
            p[1].Optional = true;
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
            Mesh mesh = null;
            da.GetData("Key", ref key);
            da.GetData("Enabled", ref enabled);
            da.GetData("Geometry", ref mesh);
            GhLink.Register(this);
            GhLink.PublishStage(key, enabled);

            foreach (string note in GhLink.TakeNotes())
                AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);

            StageSlot slot = GhLink.Slot(key);
            bool sent = false;
            string outHash = "";
            string status = slot.Status;

            if (enabled && GhLink.Connected && mesh != null)
            {
                try
                {
                    List<Section> sections = Conversion.SectionsFromMesh(mesh, GhLink.DocumentScale());
                    outHash = FrameCodec.OutputHash(sections);
                    var header = new Dictionary<string, object>();
                    header["type"] = "response";
                    header["stage"] = key;
                    header["rid"] = slot.Rid;
                    header["out_hash"] = outHash;
                    header["status"] = "ok";
                    var counts = new Dictionary<string, object>();
                    counts["meshes"] = (long)1;
                    counts["subd"] = (long)0;
                    counts["curves"] = (long)0;
                    counts["points"] = (long)0;
                    counts["approximated_curves"] = (long)0;
                    header["counts"] = counts;
                    GhLink.Enqueue(FrameCodec.Encode(header, sections));
                    sent = true;
                    status = "ok";
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

        private Guid _paramId = Guid.NewGuid();

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key this parameter belongs to", GH_ParamAccess.item);
            p.AddTextParameter("Label", "L", "Display label", GH_ParamAccess.item);
            p.AddIntegerParameter("Type", "T", "0 float, 1 int, 2 bool, 3 vector3, 4 color, 5 choice, 6 text", GH_ParamAccess.item);
            p.AddTextParameter("Default", "D", "Default value as text", GH_ParamAccess.item);
            p.AddNumberParameter("Min", "m", "Minimum", GH_ParamAccess.item);
            p.AddNumberParameter("Max", "M", "Maximum", GH_ParamAccess.item);
            p.AddNumberParameter("Step", "s", "Step", GH_ParamAccess.item);
            p.AddTextParameter("Choices", "C", "Choice labels, one per line", GH_ParamAccess.item);
            p.AddTextParameter("Group", "G", "Group label", GH_ParamAccess.item);
            for (int i = 1; i < 9; i++) p[i].Optional = true;
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddGenericParameter("Value", "V", "The value Blender sent, or the default", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            string key = "";
            string label = "Param";
            int typeIndex = 0;
            string defaultText = "";
            double min = 0.0, max = 1.0, step = 0.01;
            string choices = "";
            string group = "";
            da.GetData("Key", ref key);
            da.GetData("Label", ref label);
            da.GetData("Type", ref typeIndex);
            da.GetData("Default", ref defaultText);
            da.GetData("Min", ref min);
            da.GetData("Max", ref max);
            da.GetData("Step", ref step);
            da.GetData("Choices", ref choices);
            da.GetData("Group", ref group);
            GhLink.Register(this);

            string type = TypeName(typeIndex);
            StageSlot slot = GhLink.Slot(key);
            object value = null;
            if (slot.Params.ContainsKey(_paramId.ToString()))
                value = slot.Params[_paramId.ToString()];
            if (value == null) value = ParseDefault(type, defaultText);

            var declaration = new Dictionary<string, object>();
            declaration["id"] = _paramId.ToString();
            declaration["label"] = label;
            declaration["type"] = type;
            declaration["default"] = JsonValue(type, value);
            declaration["min"] = min;
            declaration["max"] = max;
            declaration["step"] = step;
            var choiceList = new List<object>();
            foreach (string c in choices.Split('\n'))
                if (c.Trim().Length > 0) choiceList.Add(c.Trim());
            declaration["choices"] = choiceList;
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
                case 5: return "choice";
                case 6: return "text";
                default: return "float";
            }
        }

        private static object ParseDefault(string type, string text)
        {
            double number;
            if (type == "float") return double.TryParse(text, out number) ? number : 0.0;
            if (type == "int" || type == "choice") return double.TryParse(text, out number) ? (long)number : 0L;
            if (type == "bool") return text != null && text.Trim().ToLower() == "true";
            if (type == "vector3" || type == "color")
            {
                string[] parts = (text ?? "").Split(',');
                if (parts.Length == 3)
                {
                    double a, b, c;
                    if (double.TryParse(parts[0], out a) && double.TryParse(parts[1], out b) && double.TryParse(parts[2], out c))
                        return new double[] { a, b, c };
                }
                return new double[] { 0.0, 0.0, 0.0 };
            }
            return text ?? "";
        }

        private static object JsonValue(string type, object value)
        {
            if (value is double || value is long || value is bool || value is string) return value;
            var arr = value as double[];
            if (arr != null)
            {
                var list = new List<object>();
                foreach (double d in arr) list.Add(d);
                return list;
            }
            return value == null ? null : value.ToString();
        }

        private static object OutputValue(string type, object value)
        {
            if (type == "float") return Convert.ToDouble(value);
            if (type == "int" || type == "choice") return (int)Convert.ToInt64(value);
            if (type == "bool") return Convert.ToBoolean(value);
            if (type == "vector3" || type == "color")
            {
                var arr = value as double[];
                if (arr != null && arr.Length == 3) return new Point3d(arr[0], arr[1], arr[2]);
                return new Point3d(0, 0, 0);
            }
            return value == null ? "" : value.ToString();
        }
    }
}
