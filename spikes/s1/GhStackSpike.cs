// GH Stack spikes: S1 (transport gate) and S3 (geometry round trip).
//
// S1, already passed: does a .gha under wine connect, trigger a solve from a
// background message via ScheduleSolution, reply, and survive save/reopen?
//
// S3: decode a protocol frame with mesh sections, convert to a Rhino Mesh, convert
// back to sections, and reply with timings. The Python peer is spikes/s3/driver.py.
//
// This file speaks the real protocol frames now (FrameCodec in
// grasshopper/src/core), so a section-less frame is the same bytes the S1 spike
// used. The S1 reply fields (n, solves, msg) are kept for the S1 driver.
//
// Not product code. The real components are M4.
//
// Findings that shape this spike (see the plan's evidence log):
// - GetData/SetData match parameter names, not nicknames.
// - ScheduleSolution(0, ...) coalesces and drops the delegate; 10 ms works.
// - Replies must leave from a thread that is not blocked on the next receive.
// - A watchdog that answers too fast hides whether the solve really ran.
// - wine's Framework csc.exe compiles against a 4.0 profile that cannot resolve
//   RhinoCommon; Mono's real csc.exe is used (build.sh).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using Grasshopper;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Parameters;
using Grasshopper.Kernel.Types;
using Rhino;
using Rhino.Geometry;
using GhStack.Core;

namespace GhStack.Spike.S1
{
    // ------------------------------------------------------------------ socket

    public static class SpikeLink
    {
        public const string Endpoint = "ws://127.0.0.1:9131/s1";

        private static readonly object Gate = new object();
        private static readonly List<SpikeComponent> Components = new List<SpikeComponent>();
        private static readonly List<GeometryComponent> GeometryComponents = new List<GeometryComponent>();
        private static readonly Queue<KeyValuePair<bool, string>> UiNotes =
            new Queue<KeyValuePair<bool, string>>();
        private static readonly Queue<byte[]> Outbox = new Queue<byte[]>();
        private static readonly AutoResetEvent _outboxSignal = new AutoResetEvent(false);

        private static Thread _thread;
        private static Thread _watchdog;
        private static volatile bool _running;
        private static volatile bool _connected;
        private static volatile int _reconnects;
        private static int _solves;
        private static string _lastMessage = "(no message yet)";
        private static int _lastNonce = -1;
        private static int _respondedNonce = int.MinValue;

        // S3 counters, shown on the GeometryComponent. doubles cannot be volatile;
        // the lock in the writers and the UI-thread readers make that acceptable
        // for a spike display.
        private static volatile int _lastVerts;
        private static volatile int _lastFaces;
        private static double _lastInMs;
        private static double _lastOutMs;
        private static volatile string _lastStage = "";

        public static int LastNonce { get { return _lastNonce; } }
        public static string LastMessage { get { return _lastMessage; } }
        public static int Solves { get { return _solves; } }
        public static bool Connected { get { return _connected; } }
        public static int Reconnects { get { return _reconnects; } }
        public static int LastVerts { get { return _lastVerts; } }
        public static int LastFaces { get { return _lastFaces; } }
        public static double LastInMs { get { return _lastInMs; } }
        public static double LastOutMs { get { return _lastOutMs; } }
        public static string LastStage { get { return _lastStage; } }

        public static void Register(SpikeComponent c)
        {
            lock (Gate) { if (!Components.Contains(c)) Components.Add(c); }
            Start();
        }

        public static void RegisterGeometry(GeometryComponent c)
        {
            lock (Gate) { if (!GeometryComponents.Contains(c)) GeometryComponents.Add(c); }
            Start();
        }

        public static void Unregister(SpikeComponent c)
        {
            lock (Gate) { Components.Remove(c); }
        }

        public static void UnregisterGeometry(GeometryComponent c)
        {
            lock (Gate) { GeometryComponents.Remove(c); }
        }

        public static void Start()
        {
            lock (Gate)
            {
                if (_thread != null && _thread.IsAlive) return;
                _running = true;
                _thread = new Thread(Loop);
                _thread.IsBackground = true;
                _thread.Name = "GhStack-spike-socket";
                _thread.Start();
                if (_watchdog == null || !_watchdog.IsAlive)
                {
                    _watchdog = new Thread(WatchdogLoop);
                    _watchdog.IsBackground = true;
                    _watchdog.Name = "GhStack-spike-watchdog";
                    _watchdog.Start();
                }
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
                    lock (Gate)
                    {
                        _respondedNonce = int.MinValue;
                        _lastNonce = -1;
                    }
                    if (!wasConnected) Broadcast("connected to " + Endpoint);
                    var stopSend = new ManualResetEvent(false);
                    var sender = new Thread(delegate() { SendLoop(client, stopSend); });
                    sender.IsBackground = true;
                    sender.Name = "GhStack-spike-sender";
                    sender.Start();
                    ReceiveLoop(client);
                    stopSend.Set();
                    sender.Join(500);
                    Note("read loop ended; reconnecting");
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
                if (frame == null)
                {
                    Note("socket read returned null (peer closed or reset); stopping read loop");
                    return;
                }
                Handle(frame);
            }
        }

        private static void SendLoop(MiniWsClient client, ManualResetEvent stop)
        {
            var handles = new WaitHandle[] { _outboxSignal, stop };
            while (_running)
            {
                try
                {
                    int fired = WaitHandle.WaitAny(handles, 250);
                    if (fired == 1) return;
                    if (fired == 0) SendQueued(client);
                }
                catch (Exception ex)
                {
                    Note("send loop failed: " + FirstLine(ex));
                    return;
                }
            }
        }

        private static void SendQueued(MiniWsClient client)
        {
            while (true)
            {
                byte[] frame;
                lock (Gate)
                {
                    if (Outbox.Count == 0) return;
                    frame = Outbox.Peek();
                }
                try
                {
                    client.SendBinary(frame);
                }
                catch (Exception ex)
                {
                    Note("send failed: " + FirstLine(ex));
                    return;
                }
                lock (Gate) { if (Outbox.Count > 0) Outbox.Dequeue(); }
            }
        }

        private static void WatchdogLoop()
        {
            while (_running)
            {
                Thread.Sleep(10000);
                int nonce;
                string json;
                lock (Gate)
                {
                    nonce = _lastNonce;
                    if (nonce < 0) continue;
                    if (nonce == _respondedNonce) continue;
                    json = _lastMessage;
                }
                var header = new Dictionary<string, object>();
                header["type"] = "response";
                header["n"] = (long)nonce;
                header["solves"] = (long)_solves;
                header["msg"] = json;
                byte[] frame = FrameCodec.Encode(header, null);
                lock (Gate)
                {
                    if (_lastNonce == _respondedNonce) continue;
                    _respondedNonce = _lastNonce;
                    Outbox.Enqueue(frame);
                }
                _outboxSignal.Set();
                Note("watchdog replied to n=" + nonce + "; the solution did not answer in 10 s");
            }
        }

        // --------------------------------------------------------------- dispatch

        private static void Handle(byte[] frame)
        {
            DecodedFrame decoded;
            try
            {
                decoded = FrameCodec.Decode(frame);
            }
            catch (FrameException ex)
            {
                Note("bad frame (" + ex.Code + "): " + ex.Message);
                return;
            }
            catch (JsonException ex)
            {
                Note("bad frame json: " + ex.Message);
                return;
            }

            Dictionary<string, object> header = decoded.Header;
            string type = Json.GetString(header, "type");
            var geom = Json.Get(header, "geom") as Dictionary<string, object>;
            string mode = geom == null ? null : Json.GetString(geom, "mode");
            string check = Json.GetString(header, "check");

            if (type == "request" && check == "s4")
            {
                HandleS4Check(header);
                return;
            }
            if (type == "request" && check == "s6")
            {
                HandleS6Check(header);
                return;
            }
            if (type == "request" && check == "s9")
            {
                HandleS9Check(header);
                return;
            }
            if (type == "request" && geom != null && mode == "full")
            {
                HandleGeometry(decoded);
                return;
            }
            if (type == "request")
            {
                HandleSimple(header);
                return;
            }
            Note("unhandled message type " + type);
        }

        private static void HandleSimple(Dictionary<string, object> header)
        {
            object nonceObj = Json.Get(header, "n");
            int nonce = nonceObj == null ? -1 : (int)Convert.ToInt64(nonceObj);
            string json = Json.WriteCanonical(header);
            lock (Gate)
            {
                _lastMessage = json;
                _lastNonce = nonce;
            }
            Note("socket got message n=" + nonce + " (nonce slot updated)");
            SpikeComponent origin;
            lock (Gate) { origin = Components.Count > 0 ? Components[0] : null; }
            RequestSolve(origin);
        }

        /// <summary>S4: run the SubD and curve checks and reply with the results.</summary>
        private static void HandleS4Check(Dictionary<string, object> header)
        {
            var reply = new Dictionary<string, object>();
            reply["type"] = "response";
            reply["check"] = "s4";
            reply["stage"] = Json.GetString(header, "stage");
            try
            {
                reply["results"] = GhStack.Spike.S4.S4Checks.Run();
                reply["status"] = "ok";
            }
            catch (Exception ex)
            {
                reply["status"] = "error";
                reply["error"] = ex.GetType().Name + ": " + FirstLine(ex);
            }
            Enqueue(FrameCodec.Encode(reply, null));
            Note("S4 checks ran");
        }

        /// <summary>S6: run the curve/point/SubD/Brep conversion checks.</summary>
        private static void HandleS6Check(Dictionary<string, object> header)
        {
            var reply = new Dictionary<string, object>();
            reply["type"] = "response";
            reply["check"] = "s6";
            reply["stage"] = Json.GetString(header, "stage");
            try
            {
                reply["results"] = GhStack.Spike.S6.S6Checks.Run();
                reply["status"] = "ok";
            }
            catch (Exception ex)
            {
                reply["status"] = "error";
                reply["error"] = ex.GetType().Name + ": " + FirstLine(ex);
            }
            Enqueue(FrameCodec.Encode(reply, null));
            Note("S6 checks ran");
        }

        /// <summary>S9: user-dictionary survival and graph-side block definitions
        /// (the platform questions behind attributes and instances).</summary>
        private static void HandleS9Check(Dictionary<string, object> header)
        {
            var reply = new Dictionary<string, object>();
            reply["type"] = "response";
            reply["check"] = "s9";
            reply["stage"] = Json.GetString(header, "stage");
            try
            {
                reply["results"] = GhStack.Spike.S9.S9Checks.Run();
                reply["status"] = "ok";
            }
            catch (Exception ex)
            {
                reply["status"] = "error";
                reply["error"] = ex.GetType().Name + ": " + FirstLine(ex);
            }
            Enqueue(FrameCodec.Encode(reply, null));
            Note("S9 checks ran");
        }

        /// <summary>S3: convert the frame's geometry through a Rhino Mesh and reply
        /// with the output sections and the timings. Runs on the socket thread:
        /// Rhino geometry is not UI-bound, and this keeps the measurement clean
        /// (M4 converts during SolveInstance instead).</summary>
        private static void HandleGeometry(DecodedFrame decoded)
        {
            Dictionary<string, object> header = decoded.Header;
            string stage = Json.GetString(header, "stage");
            string rid = Json.GetString(header, "rid");

            double scale = 1.0;
            try
            {
                if (RhinoDoc.ActiveDoc != null)
                    scale = RhinoMath.UnitScale(UnitSystem.Meters, RhinoDoc.ActiveDoc.ModelUnitSystem);
            }
            catch (Exception ex)
            {
                Note("unit scale unavailable, using meters: " + FirstLine(ex));
            }

            var reply = new Dictionary<string, object>();
            reply["type"] = "response";
            reply["stage"] = stage;
            reply["rid"] = rid;

            List<Section> outSections = null;
            var timings = new Dictionary<string, object>();
            try
            {
                var sw = Stopwatch.StartNew();
                Mesh mesh = Conversion.MeshFromSections(decoded, scale);
                sw.Stop();
                double inMs = sw.Elapsed.TotalMilliseconds;
                if (mesh == null)
                {
                    reply["status"] = "error";
                    reply["error"] = "no mesh sections in the frame";
                    Enqueue(FrameCodec.Encode(reply, null));
                    Note("geometry request without mesh sections");
                    return;
                }

                sw = Stopwatch.StartNew();
                outSections = Conversion.SectionsFromMesh(mesh, scale);
                sw.Stop();
                double outMs = sw.Elapsed.TotalMilliseconds;

                var counts = new Dictionary<string, object>();
                counts["verts"] = (long)mesh.Vertices.Count;
                counts["faces"] = (long)(mesh.Faces.Count - NgonTriangleCount(mesh));
                counts["ngons"] = (long)mesh.Ngons.Count;
                timings["in_ms"] = inMs;
                timings["out_ms"] = outMs;
                reply["status"] = "ok";
                reply["scale"] = scale;
                reply["counts"] = counts;
                reply["timings"] = timings;

                lock (Gate)
                {
                    _lastVerts = mesh.Vertices.Count;
                    _lastFaces = (int)Convert.ToInt64(counts["faces"]);
                    _lastInMs = inMs;
                    _lastOutMs = outMs;
                    _lastStage = stage;
                }
                Note(string.Format("geometry {0}: {1} verts, {2} faces, in {3:F1} ms, out {4:F1} ms",
                    stage, mesh.Vertices.Count, mesh.Faces.Count - NgonTriangleCount(mesh), inMs, outMs));
            }
            catch (Exception ex)
            {
                reply["status"] = "error";
                reply["error"] = ex.GetType().Name + ": " + FirstLine(ex);
                Note("geometry conversion failed: " + FirstLine(ex));
            }

            Enqueue(FrameCodec.Encode(reply, outSections));
            BroadcastGeometry("geometry reply sent");
        }

        private static int NgonTriangleCount(Mesh mesh)
        {
            int triangles = 0;
            for (int i = 0; i < mesh.Ngons.Count; i++)
                triangles += mesh.Ngons.GetNgon(i).FaceCount;
            return triangles;
        }

        private static void Enqueue(byte[] frame)
        {
            lock (Gate) { Outbox.Enqueue(frame); }
            _outboxSignal.Set();
        }

        // ------------------------------------------------------------------ solve

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
                Note("RequestSolve: no active GH document; message held in the slot");
                return;
            }
            try
            {
                // A non-zero delay is deliberate: 0 coalesces with the already
                // scheduled solution and the delegate never runs.
                doc.ScheduleSolution(10, delegate(GH_Document d)
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

        public static void NoteSolved()
        {
            byte[] frame = null;
            lock (Gate)
            {
                int nonce = _lastNonce;
                if (nonce < 0) return;
                if (nonce == _respondedNonce) return;
                _respondedNonce = nonce;
                var header = new Dictionary<string, object>();
                header["type"] = "response";
                header["n"] = (long)nonce;
                header["solves"] = (long)_solves;
                header["msg"] = _lastMessage;
                frame = FrameCodec.Encode(header, null);
                Outbox.Enqueue(frame);
            }
            _outboxSignal.Set();
        }

        public static void IncSolves()
        {
            Interlocked.Increment(ref _solves);
        }

        public static string[] PumpNotes()
        {
            lock (Gate)
            {
                if (UiNotes.Count == 0) return new string[0];
                var lines = new List<string>();
                while (UiNotes.Count > 0)
                {
                    KeyValuePair<bool, string> kv = UiNotes.Dequeue();
                    lines.Add("[S1] " + kv.Value);
                }
                return lines.ToArray();
            }
        }

        public static void Broadcast(string text)
        {
            lock (Gate) { UiNotes.Enqueue(new KeyValuePair<bool, string>(true, text)); }
        }

        public static void BroadcastGeometry(string text)
        {
            lock (Gate) { UiNotes.Enqueue(new KeyValuePair<bool, string>(true, text)); }
        }

        private static void Note(string text)
        {
            lock (Gate) { UiNotes.Enqueue(new KeyValuePair<bool, string>(false, text)); }
        }

        private static string FirstLine(Exception ex)
        {
            string m = ex.Message;
            if (string.IsNullOrEmpty(m)) return ex.GetType().Name;
            int i = m.IndexOf('\n');
            return i > 0 ? m.Substring(0, i) : m;
        }
    }

    // ------------------------------------------------------------- components

    public class SpikeComponent : GH_Component
    {
        public SpikeComponent()
            : base("S1 Spike", "S1Spike",
                   "Spike S1: websocket client, ScheduleSolution trigger, reply. Not product code.",
                   "GH Stack", "Spikes")
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
            p.AddIntegerParameter("solves", "solves", "SolveInstance calls since assembly load", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            bool on = true;
            da.GetData("Enabled", ref on);
            if (on) SpikeLink.Register(this);
            else SpikeLink.Unregister(this);

            string[] notes = SpikeLink.PumpNotes();
            foreach (string n in notes) AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, n);

            SpikeLink.IncSolves();
            int solves = SpikeLink.Solves;
            int nonce = SpikeLink.LastNonce;
            string msg = SpikeLink.LastMessage;
            da.SetData("n", nonce);
            da.SetData("msg", msg);
            da.SetData("solves", solves);
            Message = SpikeLink.Connected ? ("connected, s=" + solves + ", n=" + nonce) : "connecting...";
            SpikeLink.NoteSolved();
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

    public class GeometryComponent : GH_Component
    {
        public GeometryComponent()
            : base("S3 Geometry", "S3Geom",
                   "Spike S3: convert incoming geometry frames through a Rhino Mesh and reply. Not product code.",
                   "GH Stack", "Spikes")
        {
        }

        public override Guid ComponentGuid
        {
            get { return new Guid("3e7a41d0-6c9b-4f2a-8b1e-2d5c8a9f0e33"); }
        }

        protected override System.Drawing.Bitmap Icon
        {
            get { return GhStack.Icons.Get("geometry_nodes"); }
        }

        protected override void RegisterInputParams(GH_InputParamManager p)
        {
            p.AddBooleanParameter("Enabled", "On", "Connect when true", GH_ParamAccess.item);
            ((Param_Boolean)p[0]).PersistentData.Append(new GH_Boolean(true));
        }

        protected override void RegisterOutputParams(GH_OutputParamManager p)
        {
            p.AddIntegerParameter("verts", "v", "Vertices of the last converted mesh", GH_ParamAccess.item);
            p.AddIntegerParameter("faces", "f", "Faces of the last converted mesh", GH_ParamAccess.item);
            p.AddNumberParameter("in_ms", "i", "Sections to Rhino Mesh, milliseconds", GH_ParamAccess.item);
            p.AddNumberParameter("out_ms", "o", "Rhino Mesh to sections, milliseconds", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            bool on = true;
            da.GetData("Enabled", ref on);
            if (on) SpikeLink.RegisterGeometry(this);
            else SpikeLink.UnregisterGeometry(this);

            string[] notes = SpikeLink.PumpNotes();
            foreach (string n in notes) AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, n);

            da.SetData("verts", SpikeLink.LastVerts);
            da.SetData("faces", SpikeLink.LastFaces);
            da.SetData("in_ms", SpikeLink.LastInMs);
            da.SetData("out_ms", SpikeLink.LastOutMs);
            Message = SpikeLink.Connected
                ? ("connected; last " + SpikeLink.LastStage + ": " + SpikeLink.LastVerts + " v, " +
                   SpikeLink.LastInMs.ToString("F1") + "+" + SpikeLink.LastOutMs.ToString("F1") + " ms")
                : "connecting...";
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            SpikeLink.RegisterGeometry(this);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            SpikeLink.UnregisterGeometry(this);
        }
    }
}
