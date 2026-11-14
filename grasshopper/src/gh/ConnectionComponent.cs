// The Stageloom Connection component: the discoverable endpoint setting.
//
// The authoritative port is saved in Grasshopper's global settings
// (EndpointSettings), not in the .gh document. The input only drives that
// setting: on load it is reseeded from the saved value, so a document opened on
// another machine cannot silently override the endpoint this installation uses.

using System;
using System.Drawing;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Parameters;
using Grasshopper.Kernel.Types;
using Stageloom.Core;

namespace Stageloom
{
    public class ConnectionComponent : GH_Component
    {
        public ConnectionComponent()
            : base("Stageloom Connection", "Connection",
                   "Sets the loopback endpoint the plugin connects to. The port is saved for every Grasshopper document.",
                   "Stageloom", "Settings")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("e7b6c5d4-3a2b-4c1d-9e8f-0a1b2c3d4e5f"); }
        }

        protected override Bitmap Icon
        {
            get { return Icons.Get("driver"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddIntegerParameter("Port", "P",
                "Loopback port the plugin connects to (" + Endpoint.MinPort + ".."
                + Endpoint.MaxPort + "). The saved setting is global; this input is reseeded on load.",
                GH_ParamAccess.item);
            p[0].Optional = true;
            // A new component shows the current endpoint. AddedToDocument
            // reseeds a loaded one from the global setting.
            ((Param_Integer)p[0]).PersistentData.Append(new GH_Integer(GhLink.Port));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddTextParameter("Host", "H", "Loopback host (read-only)", GH_ParamAccess.item);
            p.AddTextParameter("Endpoint", "E", "The canonical WebSocket endpoint", GH_ParamAccess.item);
            p.AddTextParameter("Status", "S", "Connection state, or the last validation error", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            GhLink.Register(this);
            foreach (string note in GhLink.TakeNotes())
                AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);

            int port = GhLink.Port;
            da.GetData("Port", ref port);

            string status;
            string error;
            if (!Endpoint.TryValidatePort(port, out error))
            {
                AddRuntimeMessage(GH_RuntimeMessageLevel.Error, "Port: " + error);
                status = "invalid port";
            }
            else if (port != GhLink.Port)
            {
                try
                {
                    if (GhLink.Configure(Endpoint.LoopbackHost, port, out error))
                    {
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Remark,
                            "Endpoint moved to " + GhLink.Endpoint);
                        status = LinkStatus();
                    }
                    else
                    {
                        AddRuntimeMessage(GH_RuntimeMessageLevel.Error, "Endpoint: " + error);
                        status = "error";
                    }
                }
                catch (Exception ex)
                {
                    // A component must never turn a link failure into a modal
                    // exception dialog; the runtime message and Status output
                    // are the component's error channel.
                    AddRuntimeMessage(GH_RuntimeMessageLevel.Error, "Endpoint: " + ex.Message);
                    status = "error";
                }
            }
            else
            {
                status = LinkStatus();
            }

            da.SetData("Host", Endpoint.LoopbackHost);
            da.SetData("Endpoint", GhLink.Endpoint);
            da.SetData("Status", status);
            Message = status;
            GhLink.Publish(OnPingDocument());
        }

        /// <summary>Reseed the input from the global setting. A document saved
        /// with an older port must not override the endpoint this machine was
        /// last configured to use.</summary>
        private void SeedPortFromSettings()
        {
            int index = Params.IndexOfInputParam("Port");
            if (index < 0) return;
            var param = Params.Input[index] as Param_Integer;
            if (param == null) return;
            param.PersistentData.Clear();
            param.PersistentData.Append(new GH_Integer(GhLink.Port));
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            SeedPortFromSettings();
            GhLink.Register(this);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            GhLink.Unregister(this);
        }

        private static string LinkStatus()
        {
            if (GhLink.Connected) return "connected";
            return GhLink.Running ? "connecting" : "disconnected";
        }
    }
}
