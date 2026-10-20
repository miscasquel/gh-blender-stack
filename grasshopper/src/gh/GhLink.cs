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
using Grasshopper.Kernel;
using Rhino;
using GhStack.Core;

namespace GhStack
{
    /// <summary>One complete request frame, published as a single reference.
    ///
    /// A solve reads Sections, Attributes, Definitions, GeometryHash and Rid
    /// together. Keeping them as separate fields let a request arriving between
    /// the reads pair old sections with a new attribute list or a new rid (F5).
    /// The lists are never mutated after construction; a `ref` publishes a new
    /// object that reuses the previous geometry lists and carries the new rid.</summary>
    public sealed class HeldFrame
    {
        public readonly string Rid;
        public readonly List<Section> Sections;
        public readonly List<Dictionary<string, object>> Attributes;
        /// <summary>The instance-definition table in wire shape (`List<object>`),
        /// because rebuilt headers are read through casts to that shape.</summary>
        public readonly List<object> Definitions;
        /// <summary>The `geom.hash` the held sections were sent for. A `ref` for
        /// any other hash cannot be served and must be refused.</summary>
        public readonly string GeometryHash;

        public HeldFrame(string rid, List<Section> sections,
                         List<Dictionary<string, object>> attributes,
                         List<object> definitions, string geometryHash)
        {
            Rid = rid ?? "";
            Sections = sections ?? new List<Section>();
            Attributes = attributes ?? new List<Dictionary<string, object>>();
            Definitions = definitions ?? new List<object>();
            GeometryHash = geometryHash ?? "";
        }

        public static readonly HeldFrame Empty = new HeldFrame(
            "", new List<Section>(), new List<Dictionary<string, object>>(),
            new List<object>(), "");
    }

    /// <summary>What one stage has been told by Blender.</summary>
    public class StageSlot
    {
        public volatile string Rid = "";
        /// <summary>The rid the current solve is answering. Pinned when a solve
        /// is scheduled, because a request can overwrite `Rid` while the
        /// solve's inputs have already been read: answering the newer rid would
        /// label old geometry as the new request's answer. Empty outside a
        /// solve, when the slot's own `Rid` is the answer.</summary>
        public volatile string SolveRid = "";
        private volatile Dictionary<string, object> _params = new Dictionary<string, object>();
        /// <summary>The request's parameter values. The socket thread publishes a
        /// fresh dictionary per request and never mutates it afterwards; the
        /// volatile reference is what makes the UI thread see a whole snapshot
        /// (P3-3).</summary>
        public Dictionary<string, object> Params
        {
            get { return _params; }
            set { _params = value; }
        }
        private volatile HeldFrame _frame = HeldFrame.Empty;
        /// <summary>The current request frame. Written under Gate and read once
        /// by a solve, so the geometry and its rid are one snapshot.</summary>
        public HeldFrame Frame
        {
            get { return _frame; }
            internal set { _frame = value; }
        }
        public volatile string Status = "waiting";
        public volatile string Message = "";
        public DateTime RequestedAt = DateTime.UtcNow;
        public int ResolveAttempts = 0;
        /// <summary>True while the solve scheduled for this slot is running.
        /// RetryStaleSolves must not schedule a second one for a solve that is
        /// merely slow (the instance-latency double-solve).</summary>
        internal bool SolveInFlight = false;
        internal DateTime SolveStartedAt = DateTime.MinValue;
        /// <summary>True while the request was accepted for a key no component
        /// has claimed yet: the first request can arrive while the document is
        /// still loading, before the first solve publishes the keys. The solve
        /// that follows either claims the key or the slot is refused as
        /// `unknown_stage` and pruned (P3-5).</summary>
        internal bool Provisional = false;
    }

    public static class GhLink
    {
        /// <summary>The WebSocket endpoint to connect to. Set it before the
        /// first component registers; a running link keeps the endpoint its
        /// worker connected with.</summary>
        public static string Endpoint = "ws://127.0.0.1:" + DefaultPort() + "/";

        private static readonly object Gate = new object();
        private static readonly Dictionary<string, StageSlot> Slots = new Dictionary<string, StageSlot>();
        private static readonly List<GH_Component> Components = new List<GH_Component>();
        private static readonly List<string> Notes = new List<string>();
        private static readonly Queue<byte[]> Outbox = new Queue<byte[]>();
        private static long OutboxBytes = 0;  // total frame bytes queued; under Gate
        // The outbox is a best-effort pipe, not a durable log: a response the
        // engine still needs is fetched again by the next pass, and hello and
        // schema are republished. If the sender cannot drain it, older frames
        // are dropped so a stalled link cannot accumulate a stale backlog.
        private const long MaxOutboxBytes = 64L * 1024 * 1024;
        private static readonly AutoResetEvent OutboxSignal = new AutoResetEvent(false);

        // One worker per Start: the running flag lives in the worker, not in a
        // static, so an old thread that a Stop could not join in time can never
        // revive against a later Start (P3-1).
        private sealed class Worker
        {
            public volatile bool Running;
        }

        private static Worker _worker;      // under Gate
        private static Thread _thread;      // under Gate
        private static volatile bool _connected;
        private static int _activeSenders;
        private static int _retryRequests;
        // Test/diagnostic: one sender loop per connection. It must not outlive
        // the connection that started it.
        internal static int ActiveSenders
        {
            get { return Interlocked.Add(ref _activeSenders, 0); }
        }
        internal static int RetryRequests
        {
            get { return Interlocked.Add(ref _retryRequests, 0); }
        }
        // A solve that has not reached SolutionEnd after this long is treated as
        // lost: retrying is the only way out, and a second solve is better than
        // a stage that waits forever. The normal path clears the flag in
        // OnSolutionEnd; this only covers a missed event or a wedged document.
        private const double SolveWatchdogSeconds = 30.0;
        // Published once per connection; read and reserved under Gate (P3-4).
        private static bool _publishedThisConnection = false;
        private static MiniWsClient _client;  // under Gate; Stop disposes it

        // The keys live components have published. Every publish replaces its
        // owner's one entry, so the union is the document's live key set and
        // ReconcileOwnership can prune everything else (P3-5). Ownership is
        // reference identity, not Equals: two components of one class must not
        // collapse.
        private class Claim
        {
            public GH_Component Owner;
            public string StageKey;
            public string ParamKey;
            public string ParamId;
        }

        private static readonly List<Claim> Claims = new List<Claim>();

        // The published document state: stages present and parameter declarations.
        private static readonly Dictionary<string, bool> StageEnabled = new Dictionary<string, bool>();
        private static readonly Dictionary<string, List<Dictionary<string, object>>> StageParams =
            new Dictionary<string, List<Dictionary<string, object>>>();

        public static bool Connected { get { return _connected; } }

        private static string DefaultPort()
        {
            string port = Environment.GetEnvironmentVariable("GHSTACK_PORT");
            return string.IsNullOrEmpty(port) ? "59130" : port;
        }

        // ------------------------------------------------------------- registry

        public static void Register(GH_Component component)
        {
            lock (Gate) { if (!Components.Contains(component)) Components.Add(component); }
            Start();
        }

        public static void Unregister(GH_Component component)
        {
            bool last;
            lock (Gate)
            {
                Components.Remove(component);
                RemoveClaimLocked(component);
                last = Components.Count == 0;
            }
            // The keys this component published go with it; keys another live
            // component still claims stay (P3-5).
            ReconcileOwnership();
            if (last)
            {
                // No component can serve anything: stop the worker rather than
                // retrying a connection nothing will answer on. Start() runs
                // again on the next Register.
                Stop();
                lock (Gate)
                {
                    Slots.Clear();
                    StageEnabled.Clear();
                    StageParams.Clear();
                    _lastStages.Clear();
                    _lastSchema.Clear();
                    _publishedThisConnection = false;
                }
            }
        }

        /// <summary>Publishes one stage key, owned by <paramref name="owner"/>.
        /// Called from the owner's SolveInstance on the UI thread.</summary>
        public static void PublishStage(GH_Component owner, string key, bool enabled)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (Gate)
            {
                Claim claim = ClaimForLocked(owner);
                claim.StageKey = key;
                claim.ParamKey = null;
                claim.ParamId = null;
                StageEnabled[key] = enabled;
            }
        }

        /// <summary>Publishes one parameter declaration, owned by
        /// <paramref name="owner"/>. Called from the owner's SolveInstance on
        /// the UI thread.</summary>
        public static void PublishParam(GH_Component owner, string key, Dictionary<string, object> declaration)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (Gate)
            {
                Claim claim = ClaimForLocked(owner);
                string id = Json.GetString(declaration, "id");
                claim.StageKey = null;
                claim.ParamKey = key;
                claim.ParamId = id;

                List<Dictionary<string, object>> list;
                if (!StageParams.TryGetValue(key, out list))
                {
                    list = new List<Dictionary<string, object>>();
                    StageParams[key] = list;
                }
                for (int i = 0; i < list.Count; i++)
                    if (Json.GetString(list[i], "id") == id) { list[i] = declaration; return; }
                list.Add(declaration);
            }
        }

        /// <summary>Drops every key <paramref name="owner"/> claimed. Called
        /// when a component's key goes empty: the publish calls run no more, so
        /// without this its old key would stay in hello and its slot in the
        /// registry until the component is deleted. The registries themselves
        /// are pruned by the next ReconcileOwnership, which is why this must
        /// not prune here: a solve in progress may not have published its other
        /// components yet.</summary>
        public static void Forget(GH_Component owner)
        {
            lock (Gate)
            {
                foreach (Claim claim in Claims)
                {
                    if (!object.ReferenceEquals(claim.Owner, owner)) continue;
                    claim.StageKey = null;
                    claim.ParamKey = null;
                    claim.ParamId = null;
                    return;
                }
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

        /// <summary>The rid a solve answers: the pinned one when the solve was
        /// scheduled from a request, the slot's own rid otherwise.</summary>
        public static string AnswerRid(StageSlot slot)
        {
            if (slot == null) return "";
            return string.IsNullOrEmpty(slot.SolveRid) ? slot.Rid : slot.SolveRid;
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

        /// <summary>The solve verdict for a stage, written under the same lock
        /// every reader takes (RetryStaleSolves, Handle). Only the UI thread
        /// writes it.</summary>
        public static void SetSlotStatus(StageSlot slot, string status, string message)
        {
            lock (Gate)
            {
                slot.Status = status;
                slot.Message = message ?? "";
            }
        }

        // -------------------------------------------------------------- socket

        public static void Start()
        {
            lock (Gate)
            {
                if (_thread != null && _thread.IsAlive) return;
                var worker = new Worker();
                worker.Running = true;
                _worker = worker;
                _thread = new Thread(delegate() { Loop(worker); });
                _thread.IsBackground = true;
                _thread.Name = "GhStack-socket";
                _thread.Start();
            }
        }

        /// <summary>Stops the socket worker and waits briefly for it. The
        /// receive loop is blocked in a socket read, so the client is disposed
        /// to unblock it; without that the thread would outlive the call by an
        /// unbounded time. Called when the last component leaves the document,
        /// so a canvas with no GH Stack components holds no socket.</summary>
        public static void Stop()
        {
            Worker worker;
            Thread thread;
            MiniWsClient client;
            lock (Gate)
            {
                worker = _worker;
                thread = _thread;
                client = _client;
                _worker = null;
                _thread = null;
            }
            if (worker != null) worker.Running = false;
            if (client != null) client.Dispose();
            OutboxSignal.Set();
            if (thread != null && thread != Thread.CurrentThread)
                thread.Join(2000);
            _connected = false;
            // The worker's finally usually clears these as it exits; if the join
            // above timed out, do not carry frames from the dead connection into
            // the next one.
            lock (Gate)
            {
                Outbox.Clear();
                OutboxBytes = 0;
                _lastSchema.Clear();
                // No solve scheduled for the old session may suppress the next
                // session's retry.
                foreach (StageSlot slot in Slots.Values)
                {
                    slot.SolveInFlight = false;
                    slot.SolveStartedAt = DateTime.MinValue;
                    slot.SolveRid = "";
                }
            }
        }

        private static void Loop(Worker worker)
        {
            int backoffMs = 500;
            while (worker.Running)
            {
                MiniWsClient client = null;
                try
                {
                    client = new MiniWsClient(Endpoint);
                    client.Connect();
                    lock (Gate) { _client = client; }
                    _connected = true;
                    lock (Gate)
                    {
                        _publishedThisConnection = false;
                        // A new connection must republish every schema; the
                        // previous connection's signatures describe a peer that
                        // no longer exists.
                        _lastSchema.Clear();
                    }
                    backoffMs = 500;
                    Note("connected to " + Endpoint);
                    // Publish from this thread: waiting for the next solve would
                    // delay hello until the user touches the canvas.
                    SendPublication();
                    var stopSend = new ManualResetEvent(false);
                    var sender = new Thread(delegate() { SendLoop(client, stopSend, worker); });
                    sender.IsBackground = true;
                    sender.Name = "GhStack-sender";
                    sender.Start();
                    try
                    {
                        ReceiveLoop(client, worker);
                    }
                    finally
                    {
                        // Always stop the sender with the connection. A receive
                        // exception used to skip both lines, leaving the old
                        // sender alive to publish on the next connection.
                        stopSend.Set();
                        sender.Join(500);
                    }
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
                    lock (Gate)
                    {
                        if (_client == client) _client = null;
                        // Frames queued for a connection that just died are stale:
                        // a reconnect publishes hello/schema again, and the engine
                        // re-derives any response it still needs. Delivering the
                        // backlog instead was the other half of the outbox bug.
                        Outbox.Clear();
                        OutboxBytes = 0;
                    }
                }
                int slept = 0;
                while (worker.Running && slept < backoffMs)
                {
                    Thread.Sleep(100);
                    slept += 100;
                }
                if (backoffMs < 5000) backoffMs = Math.Min(5000, backoffMs * 2);
            }
        }

        private static void ReceiveLoop(MiniWsClient client, Worker worker)
        {
            while (worker.Running)
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
                    // The two failures the peer can act on are answered before
                    // the frame is dropped: a version mismatch and an
                    // oversized frame. bad_frame stays a local note; its
                    // sender already knows the bytes cannot be decoded, and
                    // Blender dedupes the text.
                    if (ex.Code == "proto_mismatch" || ex.Code == "too_large")
                        Enqueue(ErrorFrame(ex.Code, ex.Message, null, null));
                    continue;
                }
                catch (JsonException ex)
                {
                    Note("bad frame json: " + ex.Message);
                    continue;
                }
                catch (Exception ex)
                {
                    // Last resort (P2-1): no single frame may tear down a
                    // healthy connection — Loop's outer catch would dispose the
                    // client and reconnect. Anything reaching here is a decoder
                    // gap; note it and keep reading.
                    Note("bad frame (" + ex.GetType().Name + "): " + FirstLine(ex));
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
            if (string.IsNullOrEmpty(key))
            {
                Note("request without a stage key");
                return;
            }
            var geom = Json.Get(header, "geom") as Dictionary<string, object>;
            string mode = geom == null ? null : Json.GetString(geom, "mode");
            string hash = geom == null ? null : Json.GetString(geom, "hash");

            StageSlot slot;
            bool noComponents = false;
            lock (Gate)
            {
                Slots.TryGetValue(key ?? "", out slot);
                if (slot == null)
                {
                    // No component means no key can ever be claimed; do not grow
                    // the registry for a request nothing can answer.
                    if (Components.Count == 0)
                    {
                        noComponents = true;
                    }
                    else
                    {
                        slot = new StageSlot();
                        slot.Provisional = !IsOwnedLocked(key);
                        Slots[key ?? ""] = slot;
                    }
                }
            }
            if (noComponents)
            {
                // The request triggers no solve: there is no document to solve.
                Note("request for stage " + key + ", but no GH Stack component is on the canvas");
                return;
            }

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

                HeldFrame current = slot.Frame;
                // A ref is servable only for the geometry we actually hold. A
                // missing hash (an older client) falls back to "do we hold any".
                canServe = current != null && current.Sections.Count > 0
                           && (string.IsNullOrEmpty(hash) || hash == current.GeometryHash);

                // Publish one complete frame: a full request replaces the
                // geometry, attributes and definitions; a ref keeps them and
                // carries the new rid. Never fill a live frame field by field.
                if (mode == "full")
                {
                    slot.Frame = new HeldFrame(
                        rid, decoded.Sections, AttributeList(header),
                        HeaderObjects(header, "definitions"), hash ?? "");
                }
                else if (canServe)
                {
                    slot.Frame = new HeldFrame(
                        rid, current.Sections, current.Attributes,
                        current.Definitions, current.GeometryHash);
                }
            }

            if (mode == "ref" && !canServe)
            {
                // We cannot answer: a client that restarted, a document that was
                // replaced, a stage whose slot was cleared. Say so (protocol §4.3)
                // so Blender resends in full. Silently dropping it instead leaves
                // Blender waiting out its timeout, and its retry is another ref —
                // a loop with no way out, which is what a reopened .blend looked
                // like: no geometry, no error, forever.
                lock (Gate)
                {
                    slot.Status = "unknown_ref";
                    slot.Message = "Not holding this geometry; asking Blender to resend it";
                }
                Enqueue(ErrorFrame("unknown_ref", "no geometry for hash " + (hash ?? "(none)"), key, rid));
                return;
            }

            RequestSolve();
        }

        /// <summary>The request's attribute descriptors as a list, tolerating a
        /// frame that carries none (or a malformed list).</summary>
        private static List<Dictionary<string, object>> AttributeList(Dictionary<string, object> header)
        {
            return HeaderList(header, "attributes");
        }

        /// <summary>One header list of objects as dictionaries, empty when the
        /// header does not carry it.</summary>
        private static List<Dictionary<string, object>> HeaderList(
            Dictionary<string, object> header, string key)
        {
            var list = new List<Dictionary<string, object>>();
            var raw = Json.Get(header, key) as List<object>;
            if (raw == null) return list;
            foreach (object entry in raw)
            {
                var dict = entry as Dictionary<string, object>;
                if (dict != null) list.Add(dict);
            }
            return list;
        }

        /// <summary>One header list kept in wire shape. Stage In hands this
        /// straight to `DecodedFrame.FromSections`; readers cast the header
        /// value to `IList`, and a typed list does not cast across (invariance).</summary>
        private static List<object> HeaderObjects(
            Dictionary<string, object> header, string key)
        {
            var raw = Json.Get(header, key) as List<object>;
            return raw == null ? new List<object>() : new List<object>(raw);
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
                    // Reconcile when the solve has finished: every component has
                    // published by then, so an unclaimed provisional slot is
                    // genuinely foreign (P3-5). Subscribing here and
                    // unsubscribing in the handler keeps one subscription per
                    // scheduled solve.
                    d.SolutionEnd += OnSolutionEnd;
                    lock (Gate)
                    {
                        // Pin the rid each solve is answering before the inputs
                        // are read: a request arriving mid-solve overwrites
                        // slot.Rid, and answering that newer rid would label the
                        // old-geometry answer as the new request's (F5).
                        DateTime started = DateTime.UtcNow;
                        foreach (KeyValuePair<string, StageSlot> pair in Slots)
                        {
                            pair.Value.SolveRid = pair.Value.Rid;
                            // The solve is now in flight. RetryStaleSolves must
                            // not schedule a second one merely because this one
                            // takes longer than its 250 ms retry interval.
                            pair.Value.SolveInFlight = true;
                            pair.Value.SolveStartedAt = started;
                        }
                        foreach (GH_Component c in Components) c.ExpireSolution(true);
                    }
                });
            }
            catch (Exception ex)
            {
                Note("ScheduleSolution failed: " + FirstLine(ex));
            }
        }

        private static void OnSolutionEnd(object sender, GH_SolutionEventArgs e)
        {
            GH_Document document = sender as GH_Document;
            if (document != null) document.SolutionEnd -= OnSolutionEnd;
            lock (Gate)
            {
                // The solve is over: the pins and the in-flight marker must not
                // leak into the next (unsolicited) solve, whose answer is the
                // slot's latest rid.
                foreach (KeyValuePair<string, StageSlot> pair in Slots)
                {
                    pair.Value.SolveRid = "";
                    pair.Value.SolveInFlight = false;
                    pair.Value.SolveStartedAt = DateTime.MinValue;
                }
            }
            ReconcileOwnership();
        }

        /// <summary>Drops every registry entry no live component claims: stage
        /// keys, parameter declarations, and the slots they served. A
        /// provisional slot whose key nobody claimed answers `unknown_stage`
        /// before it goes, so the peer sees the mismatch instead of a
        /// timeout.</summary>
        private static void ReconcileOwnership()
        {
            var refused = new List<KeyValuePair<string, string>>();
            lock (Gate)
            {
                var stageKeys = new HashSet<string>();
                var paramIds = new Dictionary<string, HashSet<string>>();
                foreach (Claim claim in Claims)
                {
                    if (!string.IsNullOrEmpty(claim.StageKey)) stageKeys.Add(claim.StageKey);
                    if (!string.IsNullOrEmpty(claim.ParamKey) && claim.ParamId != null)
                    {
                        HashSet<string> ids;
                        if (!paramIds.TryGetValue(claim.ParamKey, out ids))
                        {
                            ids = new HashSet<string>();
                            paramIds[claim.ParamKey] = ids;
                        }
                        ids.Add(claim.ParamId);
                    }
                }

                foreach (string key in new List<string>(StageEnabled.Keys))
                    if (!stageKeys.Contains(key)) StageEnabled.Remove(key);

                foreach (string key in new List<string>(StageParams.Keys))
                {
                    HashSet<string> ids;
                    if (paramIds.TryGetValue(key, out ids))
                    {
                        List<Dictionary<string, object>> list = StageParams[key];
                        for (int i = list.Count - 1; i >= 0; i--)
                            if (!ids.Contains(Json.GetString(list[i], "id") ?? ""))
                                list.RemoveAt(i);
                        if (list.Count > 0) continue;
                    }
                    StageParams.Remove(key);
                }

                foreach (KeyValuePair<string, StageSlot> pair in new List<KeyValuePair<string, StageSlot>>(Slots))
                {
                    if (stageKeys.Contains(pair.Key) || paramIds.ContainsKey(pair.Key))
                    {
                        pair.Value.Provisional = false;
                        continue;
                    }
                    Slots.Remove(pair.Key);
                    if (pair.Value.Provisional && !string.IsNullOrEmpty(pair.Value.Rid))
                        refused.Add(new KeyValuePair<string, string>(pair.Key, pair.Value.Rid));
                }
            }
            foreach (KeyValuePair<string, string> entry in refused)
                Enqueue(ErrorFrame("unknown_stage",
                    "no GH Stack component claims this stage key", entry.Key, entry.Value));
        }

        private static bool IsOwnedLocked(string key)
        {
            return StageEnabled.ContainsKey(key ?? "") || StageParams.ContainsKey(key ?? "");
        }

        private static Claim ClaimForLocked(GH_Component owner)
        {
            foreach (Claim claim in Claims)
                if (object.ReferenceEquals(claim.Owner, owner)) return claim;
            var fresh = new Claim();
            fresh.Owner = owner;
            Claims.Add(fresh);
            return fresh;
        }

        private static void RemoveClaimLocked(GH_Component owner)
        {
            for (int i = 0; i < Claims.Count; i++)
            {
                if (object.ReferenceEquals(Claims[i].Owner, owner))
                {
                    Claims.RemoveAt(i);
                    return;
                }
            }
        }

        private static void SendLoop(MiniWsClient client, ManualResetEvent stop, Worker worker)
        {
            Interlocked.Increment(ref _activeSenders);
            try
            {
                SendLoopCore(client, stop, worker);
            }
            finally
            {
                Interlocked.Decrement(ref _activeSenders);
            }
        }

        private static void SendLoopCore(MiniWsClient client, ManualResetEvent stop, Worker worker)
        {
            var handles = new WaitHandle[] { OutboxSignal, stop };
            while (worker.Running)
            {
                int fired = WaitHandle.WaitAny(handles, 250);
                if (fired == 1) return;
                if (fired == 0) SendFrames(client);
                if (worker.Running) RetryStaleSolves();
                // Re-publish on a slow heartbeat: a component added while the link
                // was down (or a schema sent before a reconnect) is picked up
                // without the user having to touch the canvas.
                if (_connected && (DateTime.UtcNow - LastPublishAt()).TotalSeconds >= 2.0)
                    SendPublication();
            }
        }

        /// <summary>Re-asks for a solve when a request has been waiting a while.
        /// ScheduleSolution calls coalesce: rapid edits can leave a request with no
        /// solve, and without this the stage would sit `waiting` until a Push.</summary>
        internal static void RetryStaleSolves()
        {
            bool retry = false;
            bool watchdog = false;
            DateTime now = DateTime.UtcNow;
            lock (Gate)
            {
                foreach (var kv in Slots)
                {
                    StageSlot slot = kv.Value;
                    if (slot.Status != "waiting") continue;
                    if (slot.SolveInFlight)
                    {
                        if ((now - slot.SolveStartedAt).TotalSeconds < SolveWatchdogSeconds)
                            continue;
                        // The solve never reached SolutionEnd. Let the retry
                        // path take over rather than leaving the stage silent.
                        slot.SolveInFlight = false;
                        watchdog = true;
                    }
                    if ((now - slot.RequestedAt).TotalMilliseconds < 250) continue;
                    if (slot.ResolveAttempts >= 8) continue;
                    slot.ResolveAttempts++;
                    slot.RequestedAt = now;
                    retry = true;
                }
            }
            if (watchdog)
                Note("a solve did not report SolutionEnd; retrying the waiting stage");
            if (retry)
            {
                Interlocked.Increment(ref _retryRequests);
                RequestSolve();
            }
        }

        private static void SendFrames(MiniWsClient client)
        {
            while (true)
            {
                byte[] frame;
                lock (Gate)
                {
                    if (Outbox.Count == 0) break;
                    // Take the frame out before sending: a failed send must not
                    // leave it at the head for every later frame to queue
                    // behind. The connection is broken anyway — Loop clears the
                    // queue and reconnects, and the next pass re-derives
                    // anything the engine still needs.
                    frame = Outbox.Dequeue();
                    OutboxBytes -= frame.Length;
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
            }
        }

        public static void Enqueue(byte[] frame)
        {
            lock (Gate)
            {
                Outbox.Enqueue(frame);
                OutboxBytes += frame.Length;
                // Drop the oldest, but never the newest: a single oversized
                // frame (the protocol allows up to 512 MB) must still be able
                // to go out on a healthy link.
                while (Outbox.Count > 1 && OutboxBytes > MaxOutboxBytes)
                {
                    byte[] dropped = Outbox.Dequeue();
                    OutboxBytes -= dropped.Length;
                }
            }
            OutboxSignal.Set();
        }

        /// <summary>One `error` frame (protocol §4.6). `stage` and `rid` are
        /// omitted when the frame being answered carries none.</summary>
        private static byte[] ErrorFrame(string code, string message, string stage, string rid)
        {
            var header = new Dictionary<string, object>();
            header["type"] = "error";
            header["code"] = code;
            header["message"] = message;
            if (!string.IsNullOrEmpty(stage)) header["stage"] = stage;
            if (!string.IsNullOrEmpty(rid)) header["rid"] = rid;
            return FrameCodec.Encode(header, null);
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
        // The heartbeat timestamp is a long read/written with Interlocked: a
        // DateTime wraps two ints, which this 32-bit runtime can tear (P3-4).
        private static long _lastPublishTicks = DateTime.MinValue.Ticks;

        private static DateTime LastPublishAt()
        {
            return new DateTime(Interlocked.Read(ref _lastPublishTicks), DateTimeKind.Utc);
        }

        private static void MarkPublished()
        {
            Interlocked.Exchange(ref _lastPublishTicks, DateTime.UtcNow.Ticks);
        }

        /// <summary>Builds and enqueues hello/schema from the published state.
        /// Safe from either thread: it only touches the registries and the outbox.</summary>
        private static void SendPublication()
        {
            MarkPublished();
            if (!_connected) return;
            Dictionary<string, bool> stages;
            Dictionary<string, List<Dictionary<string, object>>> paramsByStage;
            lock (Gate)
            {
                stages = new Dictionary<string, bool>(StageEnabled);
                paramsByStage = new Dictionary<string, List<Dictionary<string, object>>>();
                foreach (var kv in StageParams) paramsByStage[kv.Key] = new List<Dictionary<string, object>>(kv.Value);
            }

            // Reserve the once-per-connection hello under Gate: two threads
            // reading the flag independently both sent one (P3-4).
            bool needHello;
            lock (Gate)
            {
                needHello = !_publishedThisConnection || StagesChangedLocked(stages);
                if (needHello) _publishedThisConnection = true;
            }

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
                // Keep in sync with blender_manifest.toml (the release path
                // enforces that one) and the example in docs/protocol.md.
                hello["client"] = "GhStack 0.3.0";
                hello["doc"] = _lastDocName;
                hello["doc_units"] = UnitsName();
                hello["stages"] = list;
                Enqueue(FrameCodec.Encode(hello, null));
                lock (Gate) { _lastStages = new Dictionary<string, bool>(stages); }
            }

            lock (Gate)
            {
                // A stage removed from the document must not leave a signature
                // behind that could suppress its schema after it comes back.
                foreach (string key in new List<string>(_lastSchema.Keys))
                    if (!paramsByStage.ContainsKey(key)) _lastSchema.Remove(key);
            }

            foreach (var kv in paramsByStage)
            {
                // The heartbeat used to enqueue one schema per stage every two
                // seconds, even when nothing had changed; Blender then decoded
                // each frame and scanned the scene just to skip the UI work.
                // Keep the periodic hello as the safety net, and gate the schema
                // on its canonical content.
                string signature = Json.WriteCanonical(kv.Value);
                bool changed;
                lock (Gate)
                {
                    string previous;
                    changed = !_lastSchema.TryGetValue(kv.Key, out previous)
                              || previous != signature;
                    if (changed) _lastSchema[kv.Key] = signature;
                }
                if (!changed) continue;

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
        // Last schema declaration published per stage, by canonical JSON. Under
        // Gate; cleared on a new connection.
        private static readonly Dictionary<string, string> _lastSchema =
            new Dictionary<string, string>();

        private static bool StagesChangedLocked(Dictionary<string, bool> stages)
        {
            if (_lastStages.Count != stages.Count) return true;
            foreach (var kv in stages)
            {
                bool other;
                if (!_lastStages.TryGetValue(kv.Key, out other) || other != kv.Value) return true;
            }
            return false;
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
