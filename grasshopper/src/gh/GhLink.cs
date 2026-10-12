// GhStack link: the Grasshopper side of the protocol (plan §10).
//
// Owns the socket thread, the per-stage latest slots, and the publication of
// `hello` and `schema`. Adapted from the S1/S3 spike, which passed the transport
// gate; the lessons are kept in the comments.
//
// Threading (plan §3, principle 7): GH objects only on the UI thread. The socket
// thread decodes frames and stores them in slots; it asks the UI thread to solve
// through GH_Document.ScheduleSolution(10, ...). A non-zero delay is required:
// ScheduleSolution(0, ...) coalesces and silently drops the delegate (S1).
// Replies are queued and sent by a dedicated sender thread, so a reply never
// waits for the next inbound frame (S1).

using System;
using System.Collections.Generic;
using System.Threading;
using Grasshopper;
using Grasshopper.Kernel;
using Rhino;
using Rhino.Geometry;
using GhStack.Core;

namespace GhStack
{
    /// <summary>What one stage has been told by Blender.</summary>
    public class StageSlot
    {
        public string Rid = "";
        public Dictionary<string, object> Params = new Dictionary<string, object>();
        public List<Section> Sections;
        /// <summary>The `geom.hash` the held sections were sent for. A `ref` for
        /// any other hash cannot be served and must be refused.</summary>
        public string GeometryHash = "";
        public string Status = "waiting";
        public string Message = "";
        public DateTime RequestedAt = DateTime.UtcNow;
        public int ResolveAttempts = 0;
    }

    public static class GhLink
    {
        public static string Endpoint = "ws://127.0.0.1:" + DefaultPort() + "/";

        private static readonly object Gate = new object();
        private static readonly Dictionary<string, StageSlot> Slots = new Dictionary<string, StageSlot>();
        private static readonly List<GH_Component> Components = new List<GH_Component>();
        private static readonly List<string> Notes = new List<string>();
        private static readonly Queue<byte[]> Outbox = new Queue<byte[]>();
        private static readonly AutoResetEvent OutboxSignal = new AutoResetEvent(false);

        private static Thread _thread;
        private static volatile bool _running;
        private static volatile bool _connected;
        private static volatile bool _publishedThisConnection;
        private static MiniWsClient _client;

        // The published document state: stages present and parameter declarations.
        private static readonly Dictionary<string, bool> StageEnabled = new Dictionary<string, bool>();
        private static readonly Dictionary<string, List<Dictionary<string, object>>> StageParams =
            new Dictionary<string, List<Dictionary<string, object>>>();

        public static bool Connected { get { return _connected; } }

        private static string DefaultPort()
        {
            string port = Environment.GetEnvironmentVariable("GHSTACK_PORT");
            return string.IsNullOrEmpty(port) ? "9130" : port;
        }

        // ------------------------------------------------------------- registry

        public static void Register(GH_Component component)
        {
            lock (Gate) { if (!Components.Contains(component)) Components.Add(component); }
            Start();
        }

        public static void Unregister(GH_Component component)
        {
            lock (Gate) { Components.Remove(component); }
        }

        public static void PublishStage(string key, bool enabled)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (Gate) { StageEnabled[key] = enabled; }
        }

        public static void PublishParam(string key, Dictionary<string, object> declaration)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (Gate)
            {
                List<Dictionary<string, object>> list;
                if (!StageParams.TryGetValue(key, out list))
                {
                    list = new List<Dictionary<string, object>>();
                    StageParams[key] = list;
                }
                string id = Json.GetString(declaration, "id");
                for (int i = 0; i < list.Count; i++)
                    if (Json.GetString(list[i], "id") == id) { list[i] = declaration; return; }
                list.Add(declaration);
            }
        }

        public static void Note(string text)
        {
            lock (Gate)
            {
                Notes.Add("[GH Stack] " + text);
                if (Notes.Count > 50) Notes.RemoveAt(0);
            }
        }

        public static string[] TakeNotes()
        {
            lock (Gate)
            {
                if (Notes.Count == 0) return new string[0];
                string[] lines = Notes.ToArray();
                Notes.Clear();
                return lines;
            }
        }

        public static StageSlot Slot(string key)
        {
            lock (Gate)
            {
                StageSlot slot;
                if (!Slots.TryGetValue(key ?? "", out slot))
                {
                    slot = new StageSlot();
                    Slots[key ?? ""] = slot;
                }
                return slot;
            }
        }

        /// <summary>The slot for a key, or null. Never creates one: used by paths
        /// that must not grow the registry (an error naming a stage nobody has).</summary>
        public static StageSlot FindSlot(string key)
        {
            lock (Gate)
            {
                StageSlot slot;
                return Slots.TryGetValue(key ?? "", out slot) ? slot : null;
            }
        }

        // -------------------------------------------------------------- socket

        public static void Start()
        {
            lock (Gate)
            {
                if (_thread != null && _thread.IsAlive) return;
                _running = true;
                _thread = new Thread(Loop);
                _thread.IsBackground = true;
                _thread.Name = "GhStack-socket";
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
                    _client = client;
                    _connected = true;
                    _publishedThisConnection = false;
                    backoffMs = 500;
                    Note("connected to " + Endpoint);
                    // Publish from this thread: waiting for the next solve would
                    // delay hello until the user touches the canvas.
                    SendPublication();
                    var stopSend = new ManualResetEvent(false);
                    var sender = new Thread(delegate() { SendLoop(client, stopSend); });
                    sender.IsBackground = true;
                    sender.Name = "GhStack-sender";
                    sender.Start();
                    ReceiveLoop(client);
                    stopSend.Set();
                    sender.Join(500);
                    Note("connection closed; retrying");
                }
                catch (Exception ex)
                {
                    Note("connect failed: " + FirstLine(ex));
                }
                finally
                {
                    if (client != null) client.Dispose();
                    _connected = false;
                    _client = null;
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
                DecodedFrame decoded;
                try
                {
                    decoded = FrameCodec.Decode(frame);
                }
                catch (FrameException ex)
                {
                    Note("bad frame (" + ex.Code + "): " + ex.Message);
                    continue;
                }
                catch (JsonException ex)
                {
                    Note("bad frame json: " + ex.Message);
                    continue;
                }
                Handle(decoded);
            }
        }

        private static void Handle(DecodedFrame decoded)
        {
            Dictionary<string, object> header = decoded.Header;
            string type = Json.GetString(header, "type");

            if (type == "error")
            {
                // A protocol error from Blender (protocol §4.6). Surface it on the
                // stage, not only in the log: a mismatch nobody sees is a mismatch
                // nobody fixes.
                string stageKey = Json.GetString(header, "stage");
                string problem = Json.GetString(header, "code");
                string detail = Json.GetString(header, "message");
                StageSlot target = string.IsNullOrEmpty(stageKey) ? null : FindSlot(stageKey);
                if (target != null)
                {
                    lock (Gate)
                    {
                        target.Status = "error";
                        target.Message = problem + ": " + detail;
                    }
                }
                Note("protocol error " + problem + ": " + detail);
                return;
            }

            if (type != "request")
            {
                Note("ignoring message type " + type);
                return;
            }

            string key = Json.GetString(header, "stage");
            string rid = Json.GetString(header, "rid");
            var geom = Json.Get(header, "geom") as Dictionary<string, object>;
            string mode = geom == null ? null : Json.GetString(geom, "mode");
            string hash = geom == null ? null : Json.GetString(geom, "hash");

            StageSlot slot = Slot(key);
            bool canServe;
            lock (Gate)
            {
                slot.Rid = rid;
                slot.Status = "waiting";
                slot.Message = "";
                slot.RequestedAt = DateTime.UtcNow;
                slot.ResolveAttempts = 0;
                // Build the dictionary locally and publish it in one assignment:
                // a solve on the UI thread reads slot.Params, and filling the
                // field in place would show it half-populated.
                var paramsObj = Json.Get(header, "params") as Dictionary<string, object>;
                var incoming = paramsObj == null
                    ? new Dictionary<string, object>()
                    : new Dictionary<string, object>(paramsObj);
                slot.Params = incoming;
                if (mode == "full")
                {
                    slot.Sections = decoded.Sections;
                    slot.GeometryHash = hash ?? "";
                }
                // A ref is servable only for the geometry we actually hold. A
                // missing hash (an older client) falls back to "do we hold any".
                canServe = slot.Sections != null && slot.Sections.Count > 0
                           && (string.IsNullOrEmpty(hash) || hash == slot.GeometryHash);
            }

            if (mode == "ref" && !canServe)
            {
                // We cannot answer: a client that restarted, a document that was
                // replaced, a stage whose slot was cleared. Say so (protocol §4.3)
                // so Blender resends in full. Silently dropping it instead leaves
                // Blender waiting out its timeout, and its retry is another ref —
                // a loop with no way out, which is what a reopened .blend looked
                // like: no geometry, no error, forever.
                var refusal = new Dictionary<string, object>();
                refusal["type"] = "error";
                refusal["code"] = "unknown_ref";
                refusal["message"] = "no geometry for hash " + (hash ?? "(none)");
                refusal["stage"] = key;
                refusal["rid"] = rid;
                lock (Gate)
                {
                    slot.Status = "unknown_ref";
                    slot.Message = "not holding this geometry; asking Blender to resend it";
                }
                Enqueue(FrameCodec.Encode(refusal, null));
                return;
            }

            RequestSolve();
        }

        private static void RequestSolve()
        {
            GH_Component origin;
            lock (Gate) { origin = Components.Count > 0 ? Components[0] : null; }
            if (origin == null)
            {
                Note("request received, but no GH Stack component is on the canvas");
                return;
            }
            GH_Document doc = null;
            try { doc = origin.OnPingDocument(); }
            catch (Exception ex) { Note("OnPingDocument failed: " + FirstLine(ex)); }
            if (doc == null)
            {
                Note("request received, but no active document; held in the slot");
                return;
            }
            try
            {
                doc.ScheduleSolution(10, delegate(GH_Document d)
                {
                    lock (Gate)
                    {
                        foreach (GH_Component c in Components) c.ExpireSolution(true);
                    }
                });
            }
            catch (Exception ex)
            {
                Note("ScheduleSolution failed: " + FirstLine(ex));
            }
        }

        private static void SendLoop(MiniWsClient client, ManualResetEvent stop)
        {
            var handles = new WaitHandle[] { OutboxSignal, stop };
            while (_running)
            {
                int fired = WaitHandle.WaitAny(handles, 250);
                if (fired == 1) return;
                if (fired == 0) SendFrames(client);
                RetryStaleSolves();
                // Re-publish on a slow heartbeat: a component added while the link
                // was down (or a schema sent before a reconnect) is picked up
                // without the user having to touch the canvas.
                if (_connected && (DateTime.UtcNow - _lastPublishAt).TotalSeconds >= 2.0)
                {
                    _lastPublishAt = DateTime.UtcNow;
                    SendPublication();
                }
            }
        }

        /// <summary>Re-asks for a solve when a request has been waiting a while.
        /// ScheduleSolution calls coalesce: rapid edits can leave a request with no
        /// solve, and without this the stage would sit `waiting` until a Push.</summary>
        private static void RetryStaleSolves()
        {
            bool retry = false;
            lock (Gate)
            {
                foreach (var kv in Slots)
                {
                    StageSlot slot = kv.Value;
                    if (slot.Status != "waiting") continue;
                    if ((DateTime.UtcNow - slot.RequestedAt).TotalMilliseconds < 250) continue;
                    if (slot.ResolveAttempts >= 8) continue;
                    slot.ResolveAttempts++;
                    slot.RequestedAt = DateTime.UtcNow;
                    retry = true;
                }
            }
            if (retry) RequestSolve();
        }

        private static void SendFrames(MiniWsClient client)
        {
            while (true)
            {
                byte[] frame;
                lock (Gate)
                {
                    if (Outbox.Count == 0) break;
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

        public static void Enqueue(byte[] frame)
        {
            lock (Gate) { Outbox.Enqueue(frame); }
            OutboxSignal.Set();
        }

        // ------------------------------------------------------------- publish

        /// <summary>Sends hello and schema once per connection, from the UI thread
        /// (called during a solve, with the calling component's document). Cheap
        /// when nothing changed.</summary>
        public static void Publish(GH_Document doc)
        {
            if (doc != null && !string.IsNullOrEmpty(doc.FilePath)) _lastDocName = doc.FilePath;
            SendPublication();
        }

        private static volatile string _lastDocName = "(unsaved)";
        private static DateTime _lastPublishAt = DateTime.MinValue;

        /// <summary>Builds and enqueues hello/schema from the published state.
        /// Safe from either thread: it only touches the registries and the outbox.</summary>
        private static void SendPublication()
        {
            _lastPublishAt = DateTime.UtcNow;
            if (!_connected) return;
            Dictionary<string, bool> stages;
            Dictionary<string, List<Dictionary<string, object>>> paramsByStage;
            lock (Gate)
            {
                stages = new Dictionary<string, bool>(StageEnabled);
                paramsByStage = new Dictionary<string, List<Dictionary<string, object>>>();
                foreach (var kv in StageParams) paramsByStage[kv.Key] = new List<Dictionary<string, object>>(kv.Value);
            }

            bool needHello = !_publishedThisConnection || stagesChanged;

            if (needHello)
            {
                var list = new List<object>();
                foreach (var kv in stages)
                {
                    var entry = new Dictionary<string, object>();
                    entry["key"] = kv.Key;
                    entry["enabled"] = kv.Value;
                    list.Add(entry);
                }
                var hello = new Dictionary<string, object>();
                hello["type"] = "hello";
                hello["client"] = "GhStack 0.1.1";
                hello["doc"] = _lastDocName;
                hello["doc_units"] = UnitsName();
                hello["stages"] = list;
                Enqueue(FrameCodec.Encode(hello, null));
                _publishedThisConnection = true;
                lock (Gate) { _lastStages = new Dictionary<string, bool>(stages); }
            }

            foreach (var kv in paramsByStage)
            {
                var schema = new Dictionary<string, object>();
                schema["type"] = "schema";
                schema["stage"] = kv.Key;
                var list = new List<object>();
                foreach (var p in kv.Value) list.Add(p);
                schema["params"] = list;
                Enqueue(FrameCodec.Encode(schema, null));
            }
        }

        private static Dictionary<string, bool> _lastStages = new Dictionary<string, bool>();

        private static bool stagesChanged
        {
            get
            {
                lock (Gate)
                {
                    if (_lastStages.Count != StageEnabled.Count) return true;
                    foreach (var kv in StageEnabled)
                    {
                        bool other;
                        if (!_lastStages.TryGetValue(kv.Key, out other) || other != kv.Value) return true;
                    }
                    return false;
                }
            }
        }

        public static void Republish()
        {
            _publishedThisConnection = false;
        }

        private static string UnitsName()
        {
            try
            {
                if (RhinoDoc.ActiveDoc != null) return RhinoDoc.ActiveDoc.ModelUnitSystem.ToString();
            }
            catch (Exception) { }
            return "Meters";
        }

        // ------------------------------------------------------------- helpers

        public static double DocumentScale()
        {
            try
            {
                if (RhinoDoc.ActiveDoc != null)
                    return RhinoMath.UnitScale(UnitSystem.Meters, RhinoDoc.ActiveDoc.ModelUnitSystem);
            }
            catch (Exception) { }
            return 1.0;
        }

        private static string FirstLine(Exception ex)
        {
            string m = ex.Message;
            if (string.IsNullOrEmpty(m)) return ex.GetType().Name;
            int i = m.IndexOf('\n');
            return i > 0 ? m.Substring(0, i) : m;
        }
    }
}
