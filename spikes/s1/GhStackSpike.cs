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

        // Socket thread -> UI thread handoff. Nothing touches GH objects off the
        // UI thread (plan §3, principle 7), so notes and replies are queued and
        // drained from SolveInstance.
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

                // Watches for requests the solution never answered, so the peer is
                // never left waiting (it stops sending until it gets a reply).
                if (_watchdog == null || !_watchdog.IsAlive)
                {
                    _watchdog = new Thread(WatchdogLoop);
                    _watchdog.IsBackground = true;
                    _watchdog.Name = "GhStack-S1-watchdog";
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
                    // The spike's nonce counter is per connection: the peer restarts
                    // its own numbering at 1 each time. Without this reset, the second
                    // connection's round 1 looked "already answered" and never got a
                    // reply (the real protocol uses request IDs, which never repeat).
                    lock (Gate)
                    {
                        _respondedNonce = int.MinValue;
                        _lastNonce = -1;
                    }
                    if (!wasConnected) Broadcast("connected to " + Endpoint);
                    var stopSend = new ManualResetEvent(false);
                    var sender = new Thread(delegate() { SendLoop(client, stopSend); });
                    sender.IsBackground = true;
                    sender.Name = "GhStack-S1-sender";
                    sender.Start();
                    ReceiveLoop(client);
                    // Retire this connection's sender before the client is disposed:
                    // two live senders sharing one queue would each dequeue frames,
                    // and the stale one would fail its send and drop the reply.
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

        /// <summary>Waits for the UI thread to queue a reply and sends it.</summary>
        private static void SendLoop(MiniWsClient client, ManualResetEvent stop)
        {
            // Sending from its own loop (not gated on the next receive) is what
            // avoids a deadlock: the Blender peer waits for the reply before it
            // sends the next request, so a reply queued during SolveInstance has
            // to go out without waiting for another inbound frame.
            // (plan §10: "Stage Out queues its response at the end of the solve,
            // and the socket thread sends it.")
            var handles = new WaitHandle[] { _outboxSignal, stop };
            while (_running)
            {
                try
                {
                    int fired = WaitHandle.WaitAny(handles, 250);
                    if (fired == 1) return;  // stop: this connection is over
                    if (fired == 0) SendQueued(client);
                }
                catch (Exception ex)
                {
                    Note("send loop failed: " + FirstLine(ex));
                    return;
                }
            }
        }

        /// <summary>Safety net for the case where the solve never runs (component
        /// disabled, GH busy): without it the peer would wait for a reply that
        /// never comes.

        /// The wait is deliberately long (10 s) so the solution wins the race in
        /// normal operation: a short interval here would answer every request
        /// before the solution could, so the reply would stop proving that the
        /// solve ran. The peer's own timeout is 30 s, so this stays a fallback.</summary>
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
                    if (nonce < 0) continue;          // nothing to answer yet
                    if (nonce == _respondedNonce) continue;  // the solution answered
                    json = _lastMessage;
                }
                // The solution did not answer within 1 s: queue it ourselves so the
                // request is never left unanswered.
                Reply reply = new Reply();
                reply.type = "response";
                reply.n = nonce;
                reply.solves = _solves;
                reply.msg = json;
                byte[] frame = Wire.Encode(Encode(reply));
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

        /// <summary>Sends anything the solution queued. The frame stays queued when a
        /// send fails, so a stale sender cannot swallow a reply.</summary>
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
                    return;  // leave it queued for the next attempt
                }
                lock (Gate)
                {
                    if (Outbox.Count > 0) Outbox.Dequeue();
                }
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
            Note("socket got message n=" + trigger.n + " (nonce slot updated)");
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
                Note("RequestSolve: no active GH document; message held in the slot");
                return;
            }

            try
            {
                // A non-zero delay is deliberate: 0 appears to coalesce with the
                // solution that is already scheduled and the delegate never runs,
                // so only the first message ever triggered a solve.
                doc.ScheduleSolution(10, delegate(GH_Document d)
                {
                    Note("UI thread: expiring " + Components.Count + " component(s)");
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

        /// <summary>Called from SolveInstance (UI thread): replies once per new
        /// message, after the solution has actually produced its output.</summary>
        public static void NoteSolved()
        {
            byte[] frame = null;
            lock (Gate)
            {
                // -1 means no message has arrived yet. A solve that runs before the
                // first message must not consume the nonce, or it answers with the
                // placeholder and the real message then never gets a reply.
                int nonce = _lastNonce;
                if (nonce < 0) return;
                if (nonce == _respondedNonce) return;
                _respondedNonce = nonce;
                Reply reply = new Reply();
                reply.type = "response";
                reply.n = nonce;
                reply.solves = _solves;
                reply.msg = _lastMessage;
                frame = Wire.Encode(Encode(reply));
                Outbox.Enqueue(frame);
            }
            _outboxSignal.Set();
        }

        public static void IncSolves()
        {
            Interlocked.Increment(ref _solves);
        }

        /// <summary>Drains the note queue on the UI thread. Returns the lines shown
        /// since the last call.</summary>
        public static string[] PumpNotes()
        {
            lock (Gate)
            {
                if (UiNotes.Count == 0) return new string[0];
                var lines = new List<string>();
                while (UiNotes.Count > 0)
                {
                    KeyValuePair<bool, string> kv = UiNotes.Dequeue();
                    lines.Add((kv.Key ? "[S1] " : "[S1] ") + kv.Value);
                }
                return lines.ToArray();
            }
        }

        public static void Broadcast(string text)
        {
            lock (Gate)
            {
                UiNotes.Enqueue(new KeyValuePair<bool, string>(true, text));
            }
        }

        private static void Note(string text)
        {
            lock (Gate)
            {
                UiNotes.Enqueue(new KeyValuePair<bool, string>(false, text));
            }
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
            // Names and nicknames differ, and IGH_DataAccess matches on the NAME.
            // Keep both obvious so SetData("solves") is not mistaken for the "s"
            // nickname (that was the "Output parameter index [-1]" breakpoint).
            p.AddIntegerParameter("n", "n", "Nonce of the last message from the server", GH_ParamAccess.item);
            p.AddTextParameter("msg", "msg", "Last message JSON, verbatim", GH_ParamAccess.item);
            p.AddIntegerParameter("solves", "solves", "SolveInstance calls since assembly load", GH_ParamAccess.item);
        }

        protected override void SolveInstance(IGH_DataAccess da)
        {
            bool on = true;
            // Note: GetData matches by parameter NAME, not nickname. "On" is the
            // nickname, so this has to be "Enabled" (or index 0).
            da.GetData("Enabled", ref on);
            if (on) SpikeLink.Register(this);
            else SpikeLink.Unregister(this);

            string[] notes = SpikeLink.PumpNotes();
            foreach (string n in notes)
                AddRuntimeMessage(GH_RuntimeMessageLevel.Warning, n);

            SpikeLink.IncSolves();
            int solves = SpikeLink.Solves;
            int nonce = SpikeLink.LastNonce;
            string msg = SpikeLink.LastMessage;

            // SetData matches by parameter NAME too ("solves", not the "s" nickname),
            // same as GetData above.
            da.SetData("n", nonce);
            da.SetData("msg", msg);
            da.SetData("solves", solves);
            Message = SpikeLink.Connected
                ? ("connected, s=" + solves + ", n=" + nonce)
                : "connecting...";

            // Reply only now, so a reply proves the solution really ran (plan §10:
            // Stage Out queues its response at the end of the solve).
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
