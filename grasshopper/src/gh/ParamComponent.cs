// The Param component: declares one parameter Blender can drive.
//
// Grasshopper 1 has no hidden-input flag, so the unused optional fields are
// removed from the parameter list, the same "hide unused" approach as Rhino
// 8's native components. A parameter that carries data or a wire is never
// removed, and the parameter list itself is the saved visibility state.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;
using GH_IO.Serialization;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Types;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom
{
    public class ParamComponent : GH_Component, IGH_VariableParameterComponent
    {
        private const int FirstOptional = 3;
        private const int InputCount = 9;
        private static readonly string[] OptionalNames =
        {
            "Default", "Min", "Max", "Step", "Values", "Group",
        };
        private static readonly string[] OptionalNickNames =
        {
            "D", "m", "M", "s", "V", "G",
        };
        private static readonly string[] OptionalDescriptions =
        {
            "Default value as text. Empty uses 1.0.",
            "Minimum. Empty means no range.",
            "Maximum. Empty means no range.",
            "Step. Empty uses 0.1.",
            "Value list entries: a list of strings, or one per line in a Panel",
            "Group label",
        };

        private Guid _paramId = Guid.NewGuid();
        // True once Read has restored a document's parameter list. A newly
        // placed node starts compact; a loaded node keeps what was saved.
        private bool _loaded;

        public ParamComponent()
            : base("Param", "Param",
                   "Declares a parameter Blender can drive. Connect it into the graph.",
                   "Stageloom", "Stack")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("c3f2e4d6-3d4a-4e5c-9f6b-7a8b9c0d1e23"); }
        }

        protected override Bitmap Icon
        {
            get { return Icons.Get("preset"); }
        }

        internal bool HasHiddenInputs
        {
            get { return Params.Input.Count < InputCount; }
        }

        internal bool ShowToggle
        {
            get { return HasHiddenInputs || HasRemovableOptionalInputs(); }
        }

        public override void CreateAttributes()
        {
            Attributes = new ParamAttributes(this);
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddTextParameter("Key", "K", "Stage key this parameter belongs to", GH_ParamAccess.item);
            p.AddTextParameter("Label", "L", "Display label", GH_ParamAccess.item);
            p.AddIntegerParameter("Type", "T", "Type index: 0 float, 1 int, 2 bool, 3 vector3, 4 color, 5 value list, 6 text", GH_ParamAccess.item);
            // The optional declaration fields are defined once, in
            // CreateParameter, and registered here empty.
            for (int i = FirstOptional; i < InputCount; i++)
                p.AddParameter(CreateParameter(GH_ParameterSide.Input, i));
            // Key is optional too: an unconfigured Param is quiet until it is
            // wired, instead of flagging "failed to collect data".
            for (int i = 0; i < InputCount; i++) p[i].Optional = true;
            ((Grasshopper.Kernel.Parameters.Param_String)p[1]).PersistentData.Append(new GH_String("parameter"));
            ((Grasshopper.Kernel.Parameters.Param_Integer)p[2]).PersistentData.Append(new GH_Integer(0));
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
            da.GetData("Key", ref key);
            da.GetData("Label", ref label);
            da.GetData("Type", ref typeIndex);

            // Optional inputs may not exist. Empty or missing values use the
            // declaration defaults; a present input always overrides them.
            string defaultText = "1.0";
            if (HasInput("Default")) da.GetData("Default", ref defaultText);
            double min = 0.0, max = 0.0, step = 0.1;
            bool hasMin = HasInput("Min") && da.GetData("Min", ref min);
            bool hasMax = HasInput("Max") && da.GetData("Max", ref max);
            if (HasInput("Step")) da.GetData("Step", ref step);
            var valueStrings = new List<string>();
            if (HasInput("Values")) da.GetDataList("Values", valueStrings);
            string group = "";
            if (HasInput("Group")) da.GetData("Group", ref group);

            GhLink.Register(this);

            if (string.IsNullOrEmpty(key))
            {
                GhLink.Forget(this);
                Message = "No key";
                da.SetData("Value", null);
                return;
            }
            string type = TypeName(typeIndex);
            StageSlot slot = GhLink.Slot(key);
            object value = null;
            // One read of the published snapshot: the socket thread replaces the
            // dictionary whole, so a local keeps this solve on one request even
            // if another lands meanwhile.
            Dictionary<string, object> parameters = slot.Params;
            if (parameters != null)
                parameters.TryGetValue(_paramId.ToString(), out value);
            if (value == null) value = ParamValues.ParseDefault(type, defaultText);
            // Coerce once to the declared type: the value may be left over from
            // a previous type, and the default must never be published untyped.
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
            GhLink.PublishParam(this, key, declaration);

            da.SetData("Value", OutputValue(type, value));
            Message = type + (GhLink.Connected ? "" : " (disconnected)");
            GhLink.Publish(OnPingDocument());
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            GhLink.Register(this);
            ApplyInitialVisibility();
        }

        /// <summary>A new node starts compact. A node read from a document
        /// keeps the parameter list stored in that document.</summary>
        internal void ApplyInitialVisibility()
        {
            if (!_loaded) ApplyVisibility(true, false);
        }

        /// <summary>Test hook: a real document load sets this in Read.</summary>
        internal void MarkLoaded()
        {
            _loaded = true;
        }

        protected override void AppendAdditionalComponentMenuItems(ToolStripDropDown menu)
        {
            base.AppendAdditionalComponentMenuItems(menu);
            Menu_AppendSeparator(menu);
            Menu_AppendItem(menu, "Hide unused inputs", Menu_HideUnusedClicked,
                HasRemovableOptionalInputs(), false);
            Menu_AppendItem(menu, "Show all inputs", Menu_ShowAllClicked,
                HasHiddenInputs, false);
        }

        private void Menu_HideUnusedClicked(object sender, EventArgs e)
        {
            SetInputsCollapsed(true);
        }

        private void Menu_ShowAllClicked(object sender, EventArgs e)
        {
            SetInputsCollapsed(false);
        }

        // ------------------------------------------------------- variable params

        public bool CanInsertParameter(GH_ParameterSide side, int index)
        {
            return side == GH_ParameterSide.Input
                && index >= FirstOptional && index < InputCount;
        }

        public bool CanRemoveParameter(GH_ParameterSide side, int index)
        {
            if (side != GH_ParameterSide.Input
                || index < FirstOptional || index >= Params.Input.Count)
                return false;
            IGH_Param param = Params.Input[index];
            // DataType values 0 and 1 are unknown/void: the input has neither
            // persistent nor volatile data, i.e. it is truly unused.
            return param.SourceCount == 0
                && IsOptionalName(param.Name)
                && (int)param.DataType <= 1;
        }

        public IGH_Param CreateParameter(GH_ParameterSide side, int index)
        {
            if (side != GH_ParameterSide.Input
                || index < FirstOptional || index >= InputCount)
                return null;

            string name = OptionalNames[index - FirstOptional];
            IGH_Param param;
            switch (name)
            {
                case "Min":
                case "Max":
                case "Step":
                    param = new Grasshopper.Kernel.Parameters.Param_Number();
                    break;
                case "Values":
                    param = new Grasshopper.Kernel.Parameters.Param_String();
                    param.Access = GH_ParamAccess.list;
                    break;
                default:
                    // Default and Group are text.
                    param = new Grasshopper.Kernel.Parameters.Param_String();
                    break;
            }

            int spec = index - FirstOptional;
            param.Name = OptionalNames[spec];
            param.NickName = OptionalNickNames[spec];
            param.Description = OptionalDescriptions[spec];
            param.Optional = true;
            return param;
        }

        public bool DestroyParameter(GH_ParameterSide side, int index)
        {
            return CanRemoveParameter(side, index);
        }

        public void VariableParameterMaintenance()
        {
            // Open, Paste, Undo and Redo restore the saved parameter list. The
            // list is the visibility state, so there is nothing to reconcile.
        }

        // ------------------------------------------------------ collapse helpers

        internal void ToggleInputs()
        {
            // Reveal everything first when something is hidden; otherwise hide
            // the optional inputs that carry no data and have no wire.
            SetInputsCollapsed(!HasHiddenInputs);
        }

        internal void SetInputsCollapsed(bool collapsed)
        {
            if (collapsed && !HasRemovableOptionalInputs()) return;
            if (!collapsed && !HasHiddenInputs) return;
            if (OnPingDocument() != null)
                RecordUndoEvent(collapsed ? "Hide unused inputs" : "Show all inputs");
            ApplyVisibility(collapsed, true);
        }

        private void ApplyVisibility(bool collapsed, bool expire)
        {
            bool changed = collapsed ? CollapseOptionalInputs() : ExpandOptionalInputs();
            if (!changed) return;
            Params.OnParametersChanged();
            if (Attributes != null) Attributes.ExpireLayout();
            if (expire && OnPingDocument() != null)
            {
                ExpireSolution(true);
                OnDisplayExpired(true);
            }
        }

        private bool CollapseOptionalInputs()
        {
            bool changed = false;
            // Walk backwards so removing one parameter does not shift the next
            // index. An input with data or a wire stays visible.
            for (int i = Params.Input.Count - 1; i >= FirstOptional; i--)
                if (CanRemoveParameter(GH_ParameterSide.Input, i)
                    && Params.UnregisterInputParameter(Params.Input[i], true))
                    changed = true;
            return changed;
        }

        private bool ExpandOptionalInputs()
        {
            bool changed = false;
            for (int i = FirstOptional; i < InputCount; i++)
            {
                string name = OptionalNames[i - FirstOptional];
                if (Params.IndexOfInputParam(name) >= 0) continue;
                IGH_Param param = CreateParameter(GH_ParameterSide.Input, i);
                if (param != null && Params.RegisterInputParam(param, i)) changed = true;
            }
            return changed;
        }

        private bool HasRemovableOptionalInputs()
        {
            for (int i = FirstOptional; i < Params.Input.Count; i++)
                if (CanRemoveParameter(GH_ParameterSide.Input, i)) return true;
            return false;
        }

        private bool HasInput(string name)
        {
            return Params.IndexOfInputParam(name) >= 0;
        }

        private static bool IsOptionalName(string name)
        {
            return Array.IndexOf(OptionalNames, name) >= 0;
        }

        // The GUID persists with the document, so keyframes survive a rename.
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
            bool ok = base.Read(reader);
            _loaded = true;
            return ok;
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
