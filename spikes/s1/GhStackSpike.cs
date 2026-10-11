// GhStack spike S1: the transport gate.
//
// Question (plan docs/plan.md §15): does a C# .gha under wine connect, trigger a
// solve from a background message via ScheduleSolution, send a response back, and
// survive save and reopen with no dialog?
//
// This is a spike, not product code. The real plugin (GH Stack components) is
// milestone M4. The component GUID below is a spike GUID only.
//
// Build: spikes/s1/build.sh (wine csc.exe, C# 5, per plan §10).
//
// Findings that shape this spike (2026-10-11, recorded for M4):
// - The wine csc.exe is Mono's mcs, not Microsoft's csc.
// - ClientWebSocket exists in Mono's real System.dll, but referencing it makes mcs
//   import two System.dlls and fail on ambiguous types ("Queue<T> is defined multiple
//   times"), and /nostdlib is not honoured by this mcs. The spike therefore uses a
//   minimal hand-rolled client over TcpClient (miniws.cs), which compiles and runs
//   cleanly. The real plugin should revisit: either wire in ClientWebSocket with a
//   working reference set, or take the same TcpClient route.
// - DataContractJsonSerializer works, via /r:System.ServiceModel.Web.dll plus
//   /r:System.Runtime.Serialization.dll (see build.sh).
//
// Wire format for the spike: the same header shape as the protocol frame
// (docs/protocol.md §3), [u32 little-endian JSON length][UTF-8 JSON], carried as a
// binary WebSocket message. The full codec is M2.
//
// The Python peer is spikes/s1/server.py.

using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;
using Grasshopper;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Parameters;
using Grasshopper.Kernel.Types;
using Rhino;

namespace GhStack.Spike.S1
{
    // ------------------------------------------------------------------ socket

    /// <summary>Owns the background socket thread and the per-stage latest slot.</summary>
    public static class SpikeLink
    {
        public const string Endpoint = "ws://127.0.0.1:9131/s1";

        private static readonly object Gate = new object();
        private static readonly List<SpikeComponent> Components = new List<SpikeComponent>();

        private static Thread _thread;
        private static volatile bool _running;
        private static volatile bool _connected;
        private static volatile int _reconnects;
        private static int _solves;
        private static string _lastMessage = "(no message yet)";
        private static int _lastNonce = -1;
        private static int _respondedNonce = int.MinValue;

        public static int LastNonce { get { return _lastNonce; } }
        public static string LastMessage { get { return _lastMessage; } }
        public static int Solves { get { return _solves; } }
        public static bool Connected { get { return _connected; } }
        public static int Reconnects { get { return _reconnects; } }

        public static void Register(SpikeComponent c)
        {
            lock (Gate) { if (!Components.Contains(c)) Components.Add(c); }
            Start();
        }

        public static void Unregister(SpikeComponent c)
        {
            lock (Gate) { Components.Remove(c); }
        }

        public static void Start()
        {
            lock (Gate)
            {
                if (_thread != null && _thread.IsAlive) return;
                _running = true;
                _thread = new Thread(Loop);
                _thread.IsBackground = true;
                _thread.Name = "GhStack-S1-socket";
                _thread.Start();
            }
        }

        public static void Stop()
        {
            _running = false;
            _connected = false;
        }

        private static void Loop()
        {
            int backoffMs = 500;
            while (_running)
            {
                MiniWsClient client = null;
                try
                {
                    client = new MiniWsClient(Endpoint);
                    client.Connect();
                    bool wasConnected = _connected;
                    _connected = true;
                    backoffMs = 500;
                    _reconnects++;
                    if (!wasConnected) Broadcast("connected to " + Endpoint);
                    ReceiveLoop(client);
                }
                catch (Exception ex)
                {
                    Note("connect failed: " + FirstLine(ex));
                }
                finally
                {
                    if (client != null) client.Dispose();
                    bool wasConnected = _connected;
                    _connected = false;
                    if (wasConnected) Note("connection closed; retrying");
                }

                int slept = 0;
                while (_running && slept < backoffMs)
                {
                    Thread.Sleep(100);
                    slept += 100;
                }
                if (backoffMs < 5000) backoffMs = Math.Min(5000, backoffMs * 2);
            }
        }

        private static void ReceiveLoop(MiniWsClient client)
        {
            while (_running)
            {
                byte[] frame = client.ReceiveBinary();
                if (frame == null) return;
                Handle(frame);
                Reply(client);
            }
        }

        private static void Handle(byte[] frame)
        {
            if (frame.Length < 4) return;
            int len = BitConverter.ToInt32(frame, 0);
            if (len <= 0 || 4 + len > frame.Length) return;
            string json = Encoding.UTF8.GetString(frame, 4, len);

            Trigger trigger;
            try
            {
                trigger = Decode<Trigger>(json);
            }
            catch (Exception ex)
            {
                Note("bad trigger: " + FirstLine(ex));
                return;
            }

            lock (Gate)
            {
                _lastMessage = json;
                _lastNonce = trigger.n;
            }
            SpikeComponent origin;
            lock (Gate) { origin = Components.Count > 0 ? Components[0] : null; }
            RequestSolve(origin);
        }

        /// <summary>Asks the UI thread to solve, exactly as plan §10 describes.</summary>
        private static void RequestSolve(SpikeComponent origin)
        {
            GH_Document doc = null;
            try
            {
                doc = origin == null ? null : origin.OnPingDocument();
            }
            catch (Exception ex)
            {
                Note("OnPingDocument failed: " + FirstLine(ex));
            }
            if (doc == null)
            {
                // No document yet (Rhino without a GH window open): the message stays in
                // the slot and the next solution picks it up. Runtime message only.
                Note("no active GH document; message held in the slot");
                return;
            }

            try
            {
                doc.ScheduleSolution(0, delegate(GH_Document d)
                {
                    lock (Gate)
                    {
                        foreach (SpikeComponent c in Components) c.ExpireSolution(true);
                    }
                });
            }
            catch (Exception ex)
            {
                Note("ScheduleSolution failed (" + FirstLine(ex) + "); trying InvokeOnUiThread");
                try
                {
                    RhinoApp.InvokeOnUiThread(new Action(delegate
                    {
                        lock (Gate)
                        {
                            foreach (SpikeComponent c in Components) c.ExpireSolution(true);
                        }
                    }));
                }
                catch (Exception ex2)
                {
                    Note("InvokeOnUiThread failed too: " + FirstLine(ex2));
                }
            }
        }

        /// <summary>Replies to the message that is currently held in the slot.</summary>
        private static void Reply(MiniWsClient client)
        {
            byte[] frame;
            lock (Gate)
            {
                int nonce = _lastNonce;
                if (nonce == _respondedNonce) return;
                _respondedNonce = nonce;
                Reply reply = new Reply();
                reply.type = "response";
                reply.n = nonce;
                reply.solves = _solves;
                reply.msg = _lastMessage;
                string json = Encode(reply);
                frame = Frame(json);
            }
            try
            {
                client.SendBinary(frame);
            }
            catch (Exception ex)
            {
                Note("send failed: " + FirstLine(ex));
            }
        }

        public static void IncSolves()
        {
            Interlocked.Increment(ref _solves);
        }

        public static void Broadcast(string text)
        {
            lock (Gate)
            {
                foreach (SpikeComponent c in Components)
                    c.AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, "[S1] " + text);
            }
        }

        private static void Note(string text)
        {
            lock (Gate)
            {
                foreach (SpikeComponent c in Components)
                    c.AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, "[S1] " + text);
            }
        }

        public static byte[] Frame(string json)
        {
            byte[] body = Encoding.UTF8.GetBytes(json);
            var ms = new MemoryStream(4 + body.Length);
            byte[] head = BitConverter.GetBytes(body.Length);
            ms.Write(head, 0, 4);
            ms.Write(body, 0, body.Length);
            return ms.ToArray();
        }

        public static T Decode<T>(string json)
        {
            using (var ms = new MemoryStream(Encoding.UTF8.GetBytes(json)))
            {
                var ser = new DataContractJsonSerializer(typeof(T));
                return (T)ser.ReadObject(ms);
            }
        }

        public static string Encode(object value)
        {
            var ser = new DataContractJsonSerializer(value.GetType());
            using (var ms = new MemoryStream())
            {
                ser.WriteObject(ms, value);
                return Encoding.UTF8.GetString(ms.ToArray());
            }
        }

        private static string FirstLine(Exception ex)
        {
            string m = ex.Message;
            if (string.IsNullOrEmpty(m)) return ex.GetType().Name;
            int i = m.IndexOf('\n');
            return i > 0 ? m.Substring(0, i) : m;
        }
    }

    // ------------------------------------------------------------- component

    public class SpikeComponent : GH_Component
    {
        public SpikeComponent()
            : base("S1 Spike", "S1Spike",
                   "Spike S1: websocket client, ScheduleSolution trigger, reply. Not product code.",
                   "GH Stack S1", "Spike")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("9c2f0a1e-5b3d-4c21-9f77-6a1d2c0e4b71"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddBooleanParameter("Enabled", "On", "Connect and solve when true", GH_ParamAccess.item);
            ((Param_Boolean)p[0]).PersistentData.Append(new GH_Boolean(true));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddIntegerParameter("n", "n", "Nonce of the last message from the server", GH_ParamAccess.item);
            p.AddTextParameter("msg", "msg", "Last message JSON, verbatim", GH_ParamAccess.item);
            p.AddIntegerParameter("solves", "s", "SolveInstance calls since assembly load", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            bool on = true;
            da.GetData("On", ref on);
            if (on) SpikeLink.Register(this);
            else SpikeLink.Unregister(this);

            SpikeLink.IncSolves();
            da.SetData("n", SpikeLink.LastNonce);
            da.SetData("msg", SpikeLink.LastMessage);
            da.SetData("s", SpikeLink.Solves);
            Message = SpikeLink.Connected ? "connected" : "connecting...";
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            SpikeLink.Register(this);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            SpikeLink.Unregister(this);
        }
    }

    // ------------------------------------------------------------------- json

    [DataContract]
    public class Trigger
    {
        [DataMember(Name = "type")] public string type;
        [DataMember(Name = "n")] public int n;
        [DataMember(Name = "text")] public string text;
    }

    [DataContract]
    public class Reply
    {
        [DataMember(Name = "type")] public string type;
        [DataMember(Name = "n")] public int n;
        [DataMember(Name = "solves")] public int solves;
        [DataMember(Name = "msg")] public string msg;
    }
}
