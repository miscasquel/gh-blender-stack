// Stageloom link: the Grasshopper side of the protocol.
//
// Owns the socket thread, the per-document stage slots, and the publication of
// `hello` and `schema`. A stage key has one owning document; a key claimed by
// two documents is a strict conflict that is neither published nor solved.
//
// Threading: GH objects only on the UI thread. The socket thread decodes frames
// and stores them in the owning document's slot; it asks the UI thread to solve
// through a dispatch captured at registration (RhinoApp.InvokeOnUiThread around
// GH_Document.ScheduleSolution — GH1 has no thread-safe document accessor, so
// the GH call runs in the UI callback, never on the socket). Document units are
// captured on the UI thread too; publication and requests read only that
// snapshot. Requests are queued with their document id: a scheduled solve only
// expires components in that document, and only expires the requested stages,
// with ExpireSolution(false) so the already-scheduled solution is the one
// recompute. Replies are queued and sent by a dedicated sender thread.

using System;
using System.Collections.Generic;
using System.Threading;
using Grasshopper.Kernel;
using Rhino;
using Stageloom.Core;
using EndpointConfig = Stageloom.Core.Endpoint;

namespace Stageloom
{
    /// <summary>One complete request frame, published as a single reference.
    ///
    /// A solve reads Sections, Attributes, Definitions, GeometryHash and Rid
    /// together. Keeping them as separate fields let a request arriving between
    /// the reads pair old sections with a new attribute list or a new rid.
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
        /// <summary>The document this slot belongs to. Filled when the slot is
        /// created through the document-scoped lookup; a request only ever
        /// reads and writes the owning document's slot.</summary>
        internal Guid DocumentId = Guid.Empty;
        /// <summary>The stage key this slot answers. Empty for the
        /// document-less slots the console suites and scripts create directly;
        /// such a slot has no published role, so Stage Out remains the
        /// aggregate status owner.</summary>
        public string Key = "";
        public volatile string Rid = "";
        /// <summary>The rid the current solve is answering. Pinned only for the
        /// stages a request solve names, because a request can overwrite `Rid`
        /// while the solve's inputs have already been read: answering the newer
        /// rid would label old geometry as the new request's answer. Empty
        /// outside a request solve, when the slot's own `Rid` is the answer.</summary>
        public volatile string SolveRid = "";
        private volatile Dictionary<string, object> _params = new Dictionary<string, object>();
        /// <summary>The request's parameter values. The socket thread publishes
        /// a fresh dictionary per request; the volatile reference lets the UI
        /// thread see a whole snapshot.</summary>
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
        /// <summary>Bumped under Gate whenever an inbound request or protocol
        /// error resets the direction statuses. A Stage In solve captures it
        /// with the frame and writes its verdict only when no newer event has
        /// intervened, even when the held frame itself did not change (an
        /// unservable ref).</summary>
        internal volatile int StatusEpoch = 0;
        /// <summary>The Stage In half's own verdict. Stage Out must not write
        /// this: a failed export is not a failed import, and the shared status
        /// made the live Stage In report Stage Out's NullReferenceException.</summary>
        public volatile string InStatus = "waiting";
        public volatile string InMessage = "";
        /// <summary>The Stage Out half's own verdict.</summary>
        public volatile string OutStatus = "waiting";
        public volatile string OutMessage = "";
        public DateTime RequestedAt = DateTime.UtcNow;
        public int ResolveAttempts = 0;
        /// <summary>True while the request solve that names this slot is
        /// running. RetryStaleSolves must not queue a second one for a solve
        /// that is merely slow (the instance-latency double-solve).</summary>
        internal bool SolveInFlight = false;
        internal DateTime SolveStartedAt = DateTime.MinValue;
        /// <summary>True while the request was accepted for a key no component
        /// has claimed yet: the first request can arrive while the document is
        /// still loading, before the first solve publishes the keys. The solve
        /// that follows either claims the key or the slot is refused as
        /// `unknown_stage` and pruned.</summary>
        internal bool Provisional = false;
        /// <summary>The rid and output hash of the last `ok` response this
        /// slot sent. A user-driven solve that recomputes the same answer is
        /// not re-sent; a request-driven one always is.</summary>
        internal volatile string LastSentRid = "";
        internal volatile string LastSentHash = "";

        /// <summary>Whether an answer with this rid and output hash must be
        /// queued. <paramref name="requestDriven"/> is true when Blender asked
        /// for this stage: the answer is expected even when unchanged.</summary>
        internal bool ShouldSend(string rid, string outHash, bool requestDriven)
        {
            if (requestDriven) return true;
            return LastSentRid != (rid ?? "") || LastSentHash != (outHash ?? "");
        }

        /// <summary>Records an enqueued `ok` answer for the unchanged-answer
        /// skip. Empty strings clear it: after an error answer the next solve
        /// must send again even when its output matches the old hash.</summary>
        internal void MarkSent(string rid, string outHash)
        {
            LastSentRid = rid ?? "";
            LastSentHash = outHash ?? "";
        }
    }

    public static class GhLink
    {
        // The endpoint is resolved at type load, before any Stage In/Stage Out
        // can register: the persisted Grasshopper setting wins over the
        // STAGELOOM_PORT development override, which wins over the default.
        // A document containing only stages therefore connects to the saved
        // port without a settings component on the canvas.
        private static volatile string _endpoint;
        private static volatile int _endpointPort;

        /// <summary>The canonical WebSocket endpoint the worker dials.</summary>
        public static string Endpoint { get { return _endpoint; } }
        public static int Port { get { return _endpointPort; } }

        static GhLink()
        {
            int persisted = 0;
            string warning;
            if (!EndpointSettings.TryLoadPort(out persisted, out warning)
                && !string.IsNullOrEmpty(warning))
                Notes.Add("[Stageloom] " + warning);

            EndpointSource source;
            _endpointPort = EndpointConfig.ResolvePort(
                persisted,
                Environment.GetEnvironmentVariable(EndpointConfig.EnvironmentVariable),
                out source, out warning);
            _endpoint = EndpointConfig.Canonical(_endpointPort);
            if (!string.IsNullOrEmpty(warning))
                Notes.Add("[Stageloom] " + warning);
            // Keep the startup choice in the diagnostics: "which port did the
            // plugin actually read?" is the first question a mismatch raises.
            Note("endpoint " + _endpoint + " (" + source + ")");
        }

        private static readonly object Gate = new object();
        // Serializes Start/Stop/Configure. It is separate from Gate so a
        // reconfigure can join the old worker without holding the lock the
        // worker needs in its exit path.
        private static readonly object LifecycleGate = new object();
        // Slots are document-scoped: a stage key names at most one slot in
        // each document. Requests route to the document that owns the key, so
        // one document's frame, rid and statuses can never be read by another.
        private static readonly Dictionary<Guid, Dictionary<string, StageSlot>> Slots =
            new Dictionary<Guid, Dictionary<string, StageSlot>>();
        // Registrations carry the document identity captured on the UI thread.
        // Components themselves stay alive in their document; the registry only
        // needs reference identity plus the owning document id.
        private static readonly List<Registration> Components = new List<Registration>();
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
        // revive against a later Start.
        private sealed class Worker
        {
            public volatile bool Running;
        }

        private static Worker _worker;      // under Gate
        private static Thread _thread;      // under Gate
        private static volatile bool _connected;
        // The capability this client advertises in hello (protocol §4.1, §8).
        internal const string Blocks2Capability = "blocks/2";
        // The peer's `caps` from its last request (protocol §4.3). Absence is
        // an empty set, never a guess: Q10b may emit multi-part definitions
        // only when Blender advertised blocks/2, and falls back otherwise.
        private static HashSet<string> _peerCaps = new HashSet<string>();
        internal static bool PeerSupports(string capability)
        {
            lock (Gate) { return _peerCaps.Contains(capability); }
        }
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
        // Request solves are serialized. PendingRequests holds the latest
        // request per stage key together with the document that owns it; at
        // most one scheduled delegate owns the queue, and it only consumes the
        // requests for one document. _activeRequests is non-null only between
        // that delegate and SolutionEnd, and names exactly the stages this
        // solve answers.
        private sealed class PendingRequest
        {
            public Guid DocumentId;
            public string Rid;
            public PendingRequest(Guid documentId, string rid)
            {
                DocumentId = documentId;
                Rid = rid ?? "";
            }
        }
        private static readonly Dictionary<string, PendingRequest> PendingRequests =
            new Dictionary<string, PendingRequest>();
        private static Dictionary<string, PendingRequest> _activeRequests;  // under Gate
        private static Guid _activeDocumentId = Guid.Empty;                // under Gate
        private static Guid _solveDocumentId = Guid.Empty;                 // under Gate
        private static bool _solveQueued;                            // under Gate
        private static DateTime _solveQueuedAt = DateTime.MinValue;  // under Gate
        // Published once per connection; read and reserved under Gate.
        private static bool _publishedThisConnection = false;
        private static MiniWsClient _client;  // under Gate; Stop disposes it

        // A registered component and the document it belongs to. The document
        // id is captured on the UI thread; the scheduler uses a separate opaque
        // scheduling snapshot, so no socket path walks a GH document.
        private sealed class Registration
        {
            public GH_Component Owner;
            public Guid DocumentId;
        }

        private sealed class DocumentEntry
        {
            public string Name;
            public int ComponentCount;
            // Captured on the UI thread at registration. The socket thread only
            // invokes this opaque snapshot; it never walks the document or calls
            // OnPingDocument. The delegate body is the one Rhino call on the
            // request path: RhinoApp.InvokeOnUiThread marshals the actual
            // GH_Document.ScheduleSolution call to the UI thread.
            public Action<GH_Document.GH_ScheduleDelegate> Schedule;
        }
        private static readonly Dictionary<Guid, DocumentEntry> Documents =
            new Dictionary<Guid, DocumentEntry>();

        // The keys live components have published. Every publish replaces its
        // owner's one entry, so the union is the document's live key set and
        // ReconcileOwnership can prune everything else. Ownership is
        // reference identity, not Equals: two components of one class must not
        // collapse.
        private enum ClaimRole { Input, Output, Param }

        private class Claim
        {
            public GH_Component Owner;
            public Guid DocumentId;
            public ClaimRole Role;
            public string StageKey;
            public string ParamKey;
            public string ParamId;
            public Dictionary<string, object> Declaration;
            // Last enabled value this component published and its publish
            // order. The newest stage claim in the owning document decides the
            // published enabled flag, exactly as a single document's solves did
            // before, without retaining state from a removed duplicate.
            public bool Enabled;
            public long Sequence;
        }

        private static readonly List<Claim> Claims = new List<Claim>();
        private static long _claimSequence = 0;  // under Gate

        // Published state, derived from claims by RebuildPublicationLocked.
        // A key appears here only while one document owns it and no duplicate
        // role claim exists. Conflicted keys are withheld from hello, schema,
        // request solves and rule responses, but their per-document slots are
        // kept so removing one claimant restores the survivor's state.
        private static readonly Dictionary<string, bool> StageEnabled = new Dictionary<string, bool>();
        private static readonly HashSet<string> StageHasIn = new HashSet<string>();
        private static readonly HashSet<string> StageHasOut = new HashSet<string>();
        // A Stage Out claim whose Enabled input is false does not answer a
        // request. Presence still comes from StageHasOut (hello keeps saying
        // the key has a Stage Out); this set decides whether a request may wait
        // for an answer, which is what the status owner and retry path need.
        private static readonly HashSet<string> StageOutEnabled = new HashSet<string>();
        private static readonly Dictionary<string, List<Dictionary<string, object>>> StageParams =
            new Dictionary<string, List<Dictionary<string, object>>>();
        // Keys that were published and are no longer claimed. A request for one
        // is refused with unknown_stage instead of becoming a provisional slot
        // nobody will ever claim. Cleared when the key is claimed again.
        private static readonly HashSet<string> RemovedKeys = new HashSet<string>();
        // One owner key -> document id, or a strict conflict message. Derived
        // from the live claims; never merged last-writer across documents.
        private static readonly Dictionary<string, Guid> KeyOwners = new Dictionary<string, Guid>();
        private static readonly Dictionary<string, string> Conflicts =
            new Dictionary<string, string>();

        // UI-thread snapshot of the document units. Read only under Gate by the
        // publication path; refreshed by CaptureDocumentSnapshot on the UI
        // thread. RhinoDoc.ActiveDoc is never read from the socket or sender.
        private static string _cachedUnits = "Meters";
        private static double _cachedUnitScale = 1.0;
        // The managed id of the first UI-thread entry point. Used only by debug
        // assertions: 0 means no UI entry point has run yet.
        private static int _uiThreadId;

        // The SolutionEnd handler of the batch currently owning the request
        // queue, kept so Stop can detach it. The handler also re-checks the
        // worker token, so a late event cannot finish a newer batch.
        private static GH_Document _solveHandlerDocument;            // under Gate
        private static GH_Document.SolutionEndEventHandler _solveHandler;  // under Gate

        public static bool Connected { get { return _connected; } }

        /// <summary>True when the caller is the Grasshopper UI thread, or when
        /// no UI-thread entry point has run yet. Debug assertions use this;
        /// release builds never call Rhino from the socket or sender.</summary>
        internal static bool IsUiThread
        {
            get
            {
                int id = _uiThreadId;
                return id == 0 || id == Thread.CurrentThread.ManagedThreadId;
            }
        }

        internal static int UiThreadId { get { return _uiThreadId; } }

        private static void CaptureUiThread()
        {
            if (_uiThreadId == 0)
                Interlocked.CompareExchange(ref _uiThreadId, Thread.CurrentThread.ManagedThreadId, 0);
        }

        private static void AssertUiThread(string what)
        {
            System.Diagnostics.Debug.Assert(IsUiThread, what + " must run on the GH UI thread");
        }

        /// <summary>True while the socket worker exists. A component can show
        /// "connecting" instead of "disconnected" between Configure and the
        /// first successful dial.</summary>
        public static bool Running
        {
            get
            {
                lock (Gate)
                {
                    return _thread != null && _thread.IsAlive;
                }
            }
        }

        // Diagnostics: live registry sizes. Internal rather than public plugin
        // API, matching ActiveSenders/RetryRequests; the console suites assert
        // that a removed component leaves no entry behind.
        internal static int ComponentCount
        {
            get { lock (Gate) { return Components.Count; } }
        }

        internal static int ClaimCount
        {
            get { lock (Gate) { return Claims.Count; } }
        }

        internal static int DocumentCount
        {
            get { lock (Gate) { return Documents.Count; } }
        }

        internal static int ConflictCount
        {
            get { lock (Gate) { return Conflicts.Count; } }
        }

        internal static int StageParamCount(string key)
        {
            lock (Gate)
            {
                List<Dictionary<string, object>> list;
                return StageParams.TryGetValue(key ?? "", out list) ? list.Count : 0;
            }
        }

        // ------------------------------------------------------------- registry

        /// <summary>Registers a component with no document identity. Scripts
        /// and the console tests use this form; product components use the
        /// document overload so ownership and solving are document-scoped.</summary>
        public static void Register(GH_Component component)
        {
            RegisterCore(component, Guid.Empty, "", null);
        }

        /// <summary>Registers a component in the document Grasshopper added it
        /// to. Called from AddedToDocument on the UI thread; the document id is
        /// the only stable identity carried into the socket-thread registries.</summary>
        public static void Register(GH_Component component, GH_Document document)
        {
            CaptureUiThread();
            Guid id = Guid.Empty;
            string name = "";
            if (document != null)
            {
                id = document.DocumentID;
                string path = document.FilePath;
                name = string.IsNullOrEmpty(path)
                    ? "document " + ShortId(id)
                    : path;
            }
            RegisterCore(component, id, name, document);
        }

        /// <summary>Test hook: registers a component under a synthetic document
        /// id. A real GH_Document cannot be constructed without a Rhino
        /// process, so the console suites exercise isolation with these ids.</summary>
        internal static void RegisterInDocumentForTest(GH_Component component, Guid documentId)
        {
            CaptureUiThread();
            RegisterCore(component, documentId, "document " + ShortId(documentId), null);
        }

        private static void RegisterCore(GH_Component component, Guid documentId,
                                         string documentName, GH_Document reference)
        {
            CaptureUiThread();
            string units = "Meters";
            double unitScale = 1.0;
            if (reference != null) ReadDocumentUnits(reference, out units, out unitScale);
            lock (Gate)
            {
                if (FindRegistrationLocked(component) == null)
                {
                    var registration = new Registration();
                    registration.Owner = component;
                    registration.DocumentId = documentId;
                    Components.Add(registration);

                    DocumentEntry entry;
                    if (!Documents.TryGetValue(documentId, out entry))
                    {
                        entry = new DocumentEntry();
                        Documents[documentId] = entry;
                    }
                    entry.ComponentCount++;
                    if (!string.IsNullOrEmpty(documentName)) entry.Name = documentName;
                    if (reference != null)
                    {
                        _cachedUnits = units;
                        _cachedUnitScale = unitScale;
                        // The captured delegate is the only Rhino entry point
                        // on the socket path. GH1 has no thread-safe
                        // GH_Document accessor, but RhinoApp.InvokeOnUiThread is
                        // the documented UI dispatch: the actual
                        // GH_Document.ScheduleSolution(10, work) call runs on
                        // the UI thread, never on the socket.
                        GH_Document document = reference;
                        entry.Schedule = delegate(GH_Document.GH_ScheduleDelegate work)
                        {
                            RhinoApp.InvokeOnUiThread((Action)delegate
                            {
                                AssertUiThread("GH_Document.ScheduleSolution");
                                document.ScheduleSolution(10, work);
                            });
                        };
                    }
                }
            }
            Start();
        }

        public static void Unregister(GH_Component component)
        {
            bool last;
            lock (Gate)
            {
                Registration registration = FindRegistrationLocked(component);
                if (registration != null)
                {
                    Guid documentId = registration.DocumentId;
                    Components.Remove(registration);
                    RemoveClaimLocked(component);

                    DocumentEntry entry;
                    if (Documents.TryGetValue(documentId, out entry))
                    {
                        entry.ComponentCount--;
                        if (entry.ComponentCount <= 0)
                        {
                            Documents.Remove(documentId);
                            RemoveDocumentStateLocked(documentId);
                        }
                    }
                }
                else
                {
                    RemoveClaimLocked(component);
                }
                last = Components.Count == 0;
            }
            // The keys this component published go with it; keys another live
            // component still claims stay, and removing a duplicate claimant
            // clears the conflict for the survivor.
            ReconcileOwnership();
            if (last)
            {
                // No component can serve anything: stop the worker rather than
                // retrying a connection nothing will answer on. Start() runs
                // again on the next Register.
                Stop();
                lock (Gate) { ClearAllLocked(); }
            }
        }

        /// <summary>Publishes one stage key claimed by a Stage In component.
        /// Called from the owner's SolveInstance on the UI thread.</summary>
        public static void PublishStageIn(GH_Component owner, string key, bool enabled)
        {
            PublishStageClaim(owner, key, enabled, ClaimRole.Input);
        }

        /// <summary>Publishes one stage key claimed by a Stage Out component.
        /// Called from the owner's SolveInstance on the UI thread.</summary>
        public static void PublishStageOut(GH_Component owner, string key, bool enabled)
        {
            PublishStageClaim(owner, key, enabled, ClaimRole.Output);
        }

        /// <summary>Compatibility alias for older scripted components. New
        /// code should use <see cref="PublishStageOut"/>.</summary>
        public static void PublishStage(GH_Component owner, string key, bool enabled)
        {
            PublishStageOut(owner, key, enabled);
        }

        private static void PublishStageClaim(GH_Component owner, string key, bool enabled, ClaimRole role)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (Gate)
            {
                Claim claim = ClaimForLocked(owner);
                if (claim == null) return;
                claim.Role = role;
                claim.StageKey = key;
                claim.ParamKey = null;
                claim.ParamId = null;
                claim.Declaration = null;
                claim.Enabled = enabled;
                claim.Sequence = ++_claimSequence;
                // Rebuild only this document's published view. A key claimed by
                // two documents stays out of hello/schema until one leaves.
                RebuildPublicationLocked();
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
                if (claim == null) return;
                string id = Json.GetString(declaration, "id");
                claim.Role = ClaimRole.Param;
                claim.StageKey = null;
                claim.ParamKey = key;
                claim.ParamId = id;
                claim.Declaration = declaration;
                RebuildPublicationLocked();
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
            bool changed = false;
            lock (Gate)
            {
                foreach (Claim claim in Claims)
                {
                    if (!object.ReferenceEquals(claim.Owner, owner)) continue;
                    claim.StageKey = null;
                    claim.ParamKey = null;
                    claim.ParamId = null;
                    claim.Declaration = null;
                    RebuildPublicationLocked();
                    changed = true;
                    break;
                }
            }
            // The key left hello/schema now, not at the next solution. The
            // rebuild also tombstones it and drops its slot, so a request for
            // the cleared key is refused instead of becoming provisional.
            if (changed) SendPublication();
        }

        public static void Note(string text)
        {
            lock (Gate)
            {
                Notes.Add("[Stageloom] " + text);
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

        /// <summary>The owner's slot for a key. Product components call this
        /// overload so a solve reads and writes only its own document's state.
        /// Called on the UI thread.</summary>
        public static StageSlot Slot(GH_Component owner, string key)
        {
            lock (Gate)
            {
                Guid documentId = DocumentIdLocked(owner);
                return GetOrCreateSlotLocked(documentId, key ?? "");
            }
        }

        /// <summary>Compatibility form for scripts and the console suites: the
        /// owning document's slot when one document owns the key, otherwise the
        /// document-less slot the tests use.</summary>
        public static StageSlot Slot(string key)
        {
            lock (Gate)
            {
                string name = key ?? "";
                Guid owner;
                if (KeyOwners.TryGetValue(name, out owner))
                    return GetOrCreateSlotLocked(owner, name);
                StageSlot existing = FindAnySlotLocked(name);
                if (existing != null) return existing;
                return GetOrCreateSlotLocked(Guid.Empty, name);
            }
        }

        /// <summary>The rid a solve answers: the pinned one when the solve was
        /// scheduled from a request, the slot's own rid otherwise.</summary>
        public static string AnswerRid(StageSlot slot)
        {
            if (slot == null) return "";
            return string.IsNullOrEmpty(slot.SolveRid) ? slot.Rid : slot.SolveRid;
        }

        /// <summary>True when this solve may answer <paramref name="key"/>.
        /// A document/user solve answers every stage that has a request. A
        /// request solve answers its active stages, and also a dependent Stage
        /// Out that a shared source ran through its branch: a changed output
        /// there is the only way Blender learns about it, since no request for
        /// that key changed its inputs. The answer uses the slot's own rid and
        /// is gated by <see cref="StageSlot.ShouldSend"/> like a user-driven
        /// solve. A stage with a newer request queued is excluded: this solve
        /// would read the old inputs and label them with that newer rid. A
        /// conflicted key never answers.</summary>
        public static bool ShouldAnswer(string key)
        {
            lock (Gate)
            {
                string name = key ?? "";
                if (Conflicts.ContainsKey(name)) return false;
                if (_activeRequests == null || _activeRequests.ContainsKey(name)) return true;
                return !PendingRequests.ContainsKey(name);
            }
        }

        /// <summary>True when the current solve was scheduled for a request
        /// naming <paramref name="key"/>. Such an answer is always sent: the
        /// peer is waiting for it even when the output did not change.</summary>
        public static bool IsRequestDriven(string key)
        {
            lock (Gate)
            {
                string name = key ?? "";
                return !Conflicts.ContainsKey(name)
                       && _activeRequests != null && _activeRequests.ContainsKey(name);
            }
        }

        /// <summary>The owning document's slot for a key, or null. Never
        /// creates one: used by paths that must not grow the registry (an error
        /// naming a stage nobody has).</summary>
        public static StageSlot FindSlot(string key)
        {
            lock (Gate)
            {
                string name = key ?? "";
                // A conflicted key has no owner: an inbound error naming it
                // must not mark either document's stage.
                if (Conflicts.ContainsKey(name)) return null;
                Guid owner;
                if (KeyOwners.TryGetValue(name, out owner))
                    return FindSlotLocked(owner, name);
                return FindAnySlotLocked(name);
            }
        }

        /// <summary>The slot for one exact document. Internal so the console
        /// suites can prove requests never create a slot in another document.</summary>
        internal static StageSlot FindSlot(Guid documentId, string key)
        {
            lock (Gate) { return FindSlotLocked(documentId, key ?? ""); }
        }

        /// <summary>The duplicate-key diagnostic for a stage, or "" when the
        /// key has one owner. Components surface it as a runtime note.</summary>
        internal static string StageConflict(string key)
        {
            lock (Gate)
            {
                string message;
                return Conflicts.TryGetValue(key ?? "", out message) ? message : "";
            }
        }

        internal static bool IsStageConflicted(string key)
        {
            lock (Gate) { return Conflicts.ContainsKey(key ?? ""); }
        }

        /// <summary>A full request with zero sections is a real frame. Only the
        /// shared <see cref="HeldFrame.Empty"/> instance means "no frame"; an
        /// empty section list is a stage that legitimately returns nothing.</summary>
        internal static bool HasFrame(HeldFrame frame)
        {
            return frame != null && !object.ReferenceEquals(frame, HeldFrame.Empty);
        }

        /// <summary>The solve verdict for a stage, written under the same lock
        /// every reader takes (RetryStaleSolves, Handle). Only the UI thread
        /// writes it.</summary>
        public static void SetOutStatus(StageSlot slot, string status, string message)
        {
            lock (Gate)
            {
                slot.OutStatus = status;
                slot.OutMessage = message ?? "";
                // Keep the aggregate/retry status in step with the only
                // component that answers Blender.
                slot.Status = status;
                slot.Message = message ?? "";
            }
        }

        /// <summary>The import verdict for a stage, written under the same lock
        /// every reader takes. <paramref name="frame"/> and
        /// <paramref name="statusEpoch"/> are the snapshot the solve read; when
        /// a newer request or protocol error arrived while the geometry was
        /// building, that event is authoritative and this late write is
        /// dropped. Stage In owns only the In half for a two-way stage: the
        /// aggregate status stays with Stage Out, the component that answers
        /// Blender. A key no enabled Stage Out can answer is one-way, so Stage
        /// In owns the aggregate and writes the terminal status itself; that is
        /// what keeps RetryStaleSolves from re-asking forever.</summary>
        public static void SetInStatus(StageSlot slot, HeldFrame frame, int statusEpoch,
                                       string status, string message)
        {
            if (slot == null) return;
            lock (Gate)
            {
                if (frame == null || slot.StatusEpoch != statusEpoch
                    || !object.ReferenceEquals(slot.Frame, frame)) return;
                slot.InStatus = status;
                slot.InMessage = message ?? "";
                if (!string.IsNullOrEmpty(slot.Key) && !StageAnswersLocked(slot.Key))
                {
                    slot.Status = status;
                    slot.Message = message ?? "";
                }
            }
        }

        /// <summary>Whether a request for this key will be answered by an
        /// enabled Stage Out. False means one-way (Stage In only, Param only,
        /// a disabled Stage Out, or no stage at all): Stage In owns the
        /// terminal status and the retry path must leave the slot alone. A
        /// conflicted key never answers. Under Gate.</summary>
        private static bool StageAnswersLocked(string key)
        {
            if (string.IsNullOrEmpty(key) || Conflicts.ContainsKey(key)) return false;
            return StageHasOut.Contains(key) && StageOutEnabled.Contains(key);
        }

        internal static bool StageAnswers(string key)
        {
            lock (Gate) { return StageAnswersLocked(key ?? ""); }
        }

        /// <summary>One frame/epoch snapshot for Stage In, read together under
        /// Gate so a request arriving between the two cannot pair a new epoch
        /// with an old frame.</summary>
        internal static HeldFrame ReadFrame(StageSlot slot, out int statusEpoch)
        {
            if (slot == null)
            {
                statusEpoch = 0;
                return HeldFrame.Empty;
            }
            lock (Gate)
            {
                statusEpoch = slot.StatusEpoch;
                return slot.Frame;
            }
        }

        /// <summary>Compatibility alias for older scripted components. New code
        /// should use <see cref="SetOutStatus"/>.</summary>
        public static void SetSlotStatus(StageSlot slot, string status, string message)
        {
            SetOutStatus(slot, status, message);
        }

        // -------------------------------------------------------------- socket

        public static void Start()
        {
            lock (LifecycleGate) { StartLocked(); }
        }

        private static void StartLocked()
        {
            lock (Gate)
            {
                if (_thread != null && _thread.IsAlive) return;
                var worker = new Worker();
                worker.Running = true;
                _worker = worker;
                _thread = new Thread(delegate() { Loop(worker); });
                _thread.IsBackground = true;
                _thread.Name = "Stageloom-socket";
                _thread.Start();
            }
        }

        /// <summary>Stops the socket worker and waits briefly for it. The
        /// receive loop is blocked in a socket read, so the client is disposed
        /// to unblock it; without that the thread would outlive the call by an
        /// unbounded time. Called when the last component leaves the document,
        /// so a canvas with no Stageloom components holds no socket.</summary>
        public static void Stop()
        {
            lock (LifecycleGate) { StopLocked(); }
        }

        private static void StopLocked()
        {
            Worker worker;
            Thread thread;
            MiniWsClient client;
            GH_Document solveDocument;
            GH_Document.SolutionEndEventHandler solveHandler;
            lock (Gate)
            {
                worker = _worker;
                thread = _thread;
                client = _client;
                _worker = null;
                _thread = null;
                _client = null;
                _connected = false;
                solveDocument = _solveHandlerDocument;
                solveHandler = _solveHandler;
                _solveHandlerDocument = null;
                _solveHandler = null;
            }
            if (worker != null) worker.Running = false;
            if (client != null) client.Dispose();
            // A late SolutionEnd from a solve that belonged to the stopped
            // worker must not finish a batch the next worker owns. Detach it
            // here; the handler also re-checks the worker token before touching
            // the queue.
            if (solveDocument != null && solveHandler != null)
            {
                try { solveDocument.SolutionEnd -= solveHandler; }
                catch (Exception) { }
            }
            OutboxSignal.Set();
            if (thread != null && thread != Thread.CurrentThread)
                thread.Join(2000);
            // The worker's finally usually clears these as it exits; if the join
            // above timed out, do not carry frames from the dead connection into
            // the next one.
            lock (Gate)
            {
                Outbox.Clear();
                OutboxBytes = 0;
                _lastSchema.Clear();
                // No request from the old session may be answered by the next
                // one, and no solve queued for it may suppress the next.
                PendingRequests.Clear();
                _activeRequests = null;
                _activeDocumentId = Guid.Empty;
                _solveDocumentId = Guid.Empty;
                _solveQueued = false;
                _solveQueuedAt = DateTime.MinValue;
                // No solve scheduled for the old session may suppress the next
                // session's retry.
                foreach (StageSlot slot in AllSlotsLocked())
                {
                    slot.SolveInFlight = false;
                    slot.SolveStartedAt = DateTime.MinValue;
                    slot.SolveRid = "";
                }
            }
        }

        /// <summary>Points the link at a loopback endpoint. A change stops the
        /// old worker (closing its socket) and starts a fresh one, whose first
        /// connection publishes hello and every schema again. The port is saved
        /// globally first; a settings failure leaves the running endpoint
        /// untouched and is returned to the caller.</summary>
        public static bool Configure(string host, int port, out string error)
        {
            return Configure(host, port, true, out error);
        }

        /// <summary>The console tests' form: validate and restart without
        /// touching the user's persisted setting.</summary>
        internal static bool Configure(string host, int port, bool persist, out string error)
        {
            string canonical;
            if (!EndpointConfig.TryCreate(host, port, out canonical, out error)) return false;
            if (persist && !EndpointSettings.SavePort(port, out error)) return false;

            bool changed;
            bool wasRunning;
            bool hasComponents;
            lock (LifecycleGate)
            {
                wasRunning = _thread != null && _thread.IsAlive;
                lock (Gate) { hasComponents = Components.Count > 0; }
                changed = !string.Equals(_endpoint, canonical, StringComparison.Ordinal);
                if (!changed) return true;
                StopLocked();
                _endpoint = canonical;
                _endpointPort = port;
                // A live document keeps its link; a settings component may be
                // configured before its first Stage In/Out registers.
                if (wasRunning || hasComponents) StartLocked();
            }
            Note("endpoint set to " + canonical);
            return true;
        }

        internal static void Configure(string host, int port, bool persist)
        {
            string error;
            if (!Configure(host, port, persist, out error)) Note(error);
        }

        /// <summary>The worker token is authoritative: a stopped or replaced
        /// worker is never current again, even if its blocked connect returns
        /// long after Stop.</summary>
        private static bool IsCurrentWorkerLocked(Worker worker)
        {
            return worker != null && object.ReferenceEquals(_worker, worker) && worker.Running;
        }

        private static bool IsCurrentWorker(Worker worker)
        {
            lock (Gate) { return IsCurrentWorkerLocked(worker); }
        }

        /// <summary>Publishes only when the calling worker still owns the link.
        /// Holding Gate across the check and the enqueue makes Stop atomic
        /// against it: either the publication is queued before Stop clears the
        /// outbox, or it is skipped. No GH or Rhino call happens here.</summary>
        private static void PublishIfCurrent(Worker worker)
        {
            lock (Gate)
            {
                if (!IsCurrentWorkerLocked(worker)) return;
                SendPublication();
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
                    lock (Gate)
                    {
                        // A handshake that completes after Stop must not adopt
                        // the stale socket, revive the connection, or clear the
                        // state of the worker that replaced it.
                        if (!IsCurrentWorkerLocked(worker)) return;
                        _client = client;
                        _connected = true;
                        _publishedThisConnection = false;
                        // A new connection must republish every schema; the
                        // previous connection's signatures describe a peer that
                        // no longer exists.
                        _lastSchema.Clear();
                        // Frames queued for any earlier connection are stale.
                        Outbox.Clear();
                        OutboxBytes = 0;
                        // The unchanged-answer skip is a fact about the socket
                        // that carried the frame, not about the document. A new
                        // peer has not received the last answer, so a user-driven
                        // solve must send it again even when the bytes match.
                        foreach (StageSlot slot in AllSlotsLocked())
                            slot.MarkSent("", "");
                    }
                    backoffMs = 500;
                    Note("connected to " + Endpoint);
                    // Publish from this thread: waiting for the next solve would
                    // delay hello until the user touches the canvas.
                    PublishIfCurrent(worker);
                    if (!IsCurrentWorker(worker)) return;
                    var stopSend = new ManualResetEvent(false);
                    var sender = new Thread(delegate() { SendLoop(client, stopSend, worker); });
                    sender.IsBackground = true;
                    sender.Name = "Stageloom-sender";
                    sender.Start();
                    try
                    {
                        ReceiveLoop(client, worker);
                    }
                    finally
                    {
                        // Always stop the sender with the connection.
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
                    lock (Gate)
                    {
                        // Only the current worker may tear down the shared
                        // connection state. A stale worker's finally used to
                        // clear the next connection's outbox and capabilities.
                        if (IsCurrentWorkerLocked(worker))
                        {
                            _connected = false;
                            if (object.ReferenceEquals(_client, client)) _client = null;
                            // Capabilities belong to the socket that carried them.
                            _peerCaps = new HashSet<string>();
                            // Frames queued for a connection that just died are stale:
                            // a reconnect publishes hello/schema again, and the engine
                            // re-derives any response it still needs. Delivering the
                            // backlog instead was the other half of the outbox bug.
                            Outbox.Clear();
                            OutboxBytes = 0;
                        }
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
                // Stop may have replaced this worker while a frame was being
                // read. Nothing decoded by a stopped worker may touch the next
                // session's slots or outbox.
                if (!worker.Running || !IsCurrentWorker(worker)) return;
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
                    // Last resort: no single frame may tear down a
                    // healthy connection — Loop's outer catch would dispose the
                    // client and reconnect. Anything reaching here is a decoder
                    // gap; note it and keep reading.
                    Note("bad frame (" + ex.GetType().Name + "): " + FirstLine(ex));
                    continue;
                }
                Handle(decoded, worker);
            }
        }

        private static void Handle(DecodedFrame decoded, Worker worker)
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
                        target.InStatus = "error";
                        target.InMessage = problem + ": " + detail;
                        target.OutStatus = "error";
                        target.OutMessage = problem + ": " + detail;
                        target.StatusEpoch++;
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

            // The request is the peer's capability advertisement (protocol
            // §4.3). A missing or malformed list is "none": a sender must not
            // infer blocks/2, and Q10b falls back with a warning instead.
            HashSet<string> peerCaps = ReadCaps(header);
            lock (Gate) { _peerCaps = peerCaps; }

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

            StageSlot slot = null;
            bool noComponents = false;
            bool removedKey = false;
            string conflict = "";
            lock (Gate)
            {
                // A key two documents claim has no proven owner. Refuse it
                // before a slot is touched: neither document may answer, and
                // neither document's state may overwrite the other.
                if (Conflicts.TryGetValue(key, out conflict))
                {
                    // The frame is answered below, outside the lock.
                }
                else if (!KeyOwners.ContainsKey(key) && RemovedKeys.Contains(key))
                {
                    // The key was published and every claimant released it.
                    // No component can ever claim it again in this state, so a
                    // provisional slot would just wait for a solve that cannot
                    // answer it.
                    removedKey = true;
                }
                else
                {
                    Guid ownerId = Guid.Empty;
                    bool owned = KeyOwners.TryGetValue(key, out ownerId);
                    slot = FindSlotLocked(ownerId, key);
                    if (slot == null)
                    {
                        // No component means no key can ever be claimed; do not
                        // grow the registry for a request nothing can answer.
                        if (Components.Count == 0)
                        {
                            noComponents = true;
                        }
                        else
                        {
                            slot = GetOrCreateSlotLocked(ownerId, key);
                            slot.Provisional = !owned;
                        }
                    }
                    else if (!owned)
                    {
                        slot.Provisional = true;
                    }
                }
            }
            if (!string.IsNullOrEmpty(conflict))
            {
                Note(conflict);
                Enqueue(ErrorFrame("duplicate_stage", conflict, key, rid));
                return;
            }
            if (removedKey)
            {
                const string message = "no Stageloom component claims this stage key";
                Note("request for removed stage " + key + " refused");
                Enqueue(ErrorFrame("unknown_stage", message, key, rid));
                return;
            }
            if (noComponents)
            {
                // The request triggers no solve: there is no document to solve.
                Note("request for stage " + key + ", but no Stageloom component is on the canvas");
                return;
            }

            bool canServe;
            lock (Gate)
            {
                slot.Rid = rid;
                slot.Status = "waiting";
                slot.Message = "";
                slot.InStatus = "waiting";
                slot.InMessage = "";
                slot.OutStatus = "waiting";
                slot.OutMessage = "";
                slot.StatusEpoch++;
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
                // A ref is servable for any frame we hold, including a full
                // request with zero sections. Only the shared Empty sentinel
                // means "no frame"; an empty section list is real geometry.
                // A missing hash (an older client) falls back to "do we hold any".
                canServe = HasFrame(current)
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
                    string message = "Not holding this geometry; asking Blender to resend it";
                    slot.Status = "unknown_ref";
                    slot.Message = message;
                    slot.InStatus = "unknown_ref";
                    slot.InMessage = message;
                    slot.OutStatus = "unknown_ref";
                    slot.OutMessage = message;
                    slot.StatusEpoch++;
                    // The refused rid cannot be answered: do not let a solve
                    // queued for it answer with the geometry we still hold.
                    PendingRequests.Remove(key);
                }
                Enqueue(ErrorFrame("unknown_ref", "no geometry for hash " + (hash ?? "(none)"), key, rid));
                return;
            }

            RequestStage(key, rid, worker);
        }

        /// <summary>The optional `caps` list from a request. Only strings are
        /// capabilities; unknown ones are retained but never acted on, and a
        /// missing/non-list field means the peer advertised nothing.</summary>
        private static HashSet<string> ReadCaps(Dictionary<string, object> header)
        {
            var caps = new HashSet<string>();
            var list = Json.Get(header, "caps") as List<object>;
            if (list == null) return caps;
            foreach (object item in list)
            {
                string cap = item as string;
                if (!string.IsNullOrEmpty(cap)) caps.Add(cap);
            }
            return caps;
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

        /// <summary>Queues one requested stage for the next request solve.
        /// Called from the receive thread. A request arriving while a batch is
        /// queued or in flight joins the next batch instead of scheduling a
        /// second solve. A retry whose rid the slot has already replaced is
        /// dropped: queueing it would answer the newer request with inputs it
        /// never sent. The owning document id travels with the request so the
        /// scheduled solve never has to guess an origin component.</summary>
        internal static void RequestStage(string key, string rid)
        {
            RequestStage(key, rid, null);
        }

        private static void RequestStage(string key, string rid, Worker worker)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (Gate)
            {
                // A frame decoded by a worker Stop already replaced must not
                // queue work for the next session.
                if (worker != null && !IsCurrentWorkerLocked(worker)) return;
                string conflict;
                if (Conflicts.TryGetValue(key, out conflict)) return;
                Guid owner = Guid.Empty;
                bool owned = KeyOwners.TryGetValue(key, out owner);
                StageSlot slot = owned ? FindSlotLocked(owner, key) : FindAnySlotLocked(key);
                if (slot != null
                    && !string.IsNullOrEmpty(slot.Rid) && slot.Rid != (rid ?? ""))
                    return;
                Guid documentId = slot != null
                    ? slot.DocumentId
                    : (owned ? owner : Guid.Empty);
                PendingRequests[key] = new PendingRequest(documentId, rid);
            }
            SchedulePendingSolve(worker);
        }

        private static void SchedulePendingSolve()
        {
            SchedulePendingSolve(null);
        }

        /// <summary>Schedules the request solve that owns the pending map. The
        /// delegate runs on the GH UI thread. The document reference and its
        /// dispatch were captured on the UI thread at registration; no
        /// OnPingDocument call is made here. The worker token travels with the
        /// delegate so a solve scheduled before Stop cannot run against the
        /// next worker's queue. When no pending document has a live reference
        /// the queue is released and the requests stay for the retry path.</summary>
        private static void SchedulePendingSolve(Worker worker)
        {
            Guid documentId = Guid.Empty;
            Action<GH_Document.GH_ScheduleDelegate> schedule = null;
            Worker token = null;
            lock (Gate)
            {
                if (worker != null && !IsCurrentWorkerLocked(worker)) return;
                if (_solveQueued || PendingRequests.Count == 0) return;
                Guid first = Guid.Empty;
                foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                {
                    if (first == Guid.Empty) first = pair.Value.DocumentId;
                    DocumentEntry entry;
                    if (Documents.TryGetValue(pair.Value.DocumentId, out entry)
                        && entry.Schedule != null)
                    {
                        documentId = pair.Value.DocumentId;
                        schedule = entry.Schedule;
                        token = worker != null ? worker : _worker;
                        break;
                    }
                }
                if (schedule == null)
                {
                    // Keep the first pending document id for the diagnostic; the
                    // pending requests are not lost and the retry path re-runs.
                    documentId = first;
                    Note("request received, but no active document; held in the slot");
                    return;
                }
                if (token == null || !IsCurrentWorkerLocked(token))
                {
                    Note("request received, but no active worker; held in the slot");
                    return;
                }
                // Reserve the one scheduled delegate before leaving the lock:
                // two requests for the same document coalesce, and a request
                // for another document waits for the next batch.
                _solveQueued = true;
                _solveQueuedAt = DateTime.UtcNow;
                _solveDocumentId = documentId;
            }
            Worker solveWorker = token;
            try
            {
                schedule(new GH_Document.GH_ScheduleDelegate(delegate(GH_Document d)
                {
                    RunRequestSolve(d, documentId, solveWorker);
                }));
            }
            catch (Exception ex)
            {
                ReleaseQueuedSolve(solveWorker);
                Note("ScheduleSolution failed: " + FirstLine(ex));
            }
        }

        private static void ReleaseQueuedSolve(Worker worker)
        {
            lock (Gate)
            {
                if (worker != null && !IsCurrentWorkerLocked(worker)) return;
                _solveQueued = false;
                _solveQueuedAt = DateTime.MinValue;
                _solveDocumentId = Guid.Empty;
            }
        }

        /// <summary>The scheduled solution's work. The pending map becomes the
        /// active context first, so requests arriving during the solve queue
        /// for the next one. Then only the requested stages, in the owning
        /// document, are expired: Stage In and Params are sources, Stage Out is
        /// the explicit sink, so a branch that is not connected still answers.
        /// recompute=false expires those objects and their dependents without
        /// asking the document for another solution; this scheduled solution is
        /// the one that recomputes.</summary>
        private static void RunRequestSolve(GH_Document document, Guid documentId, Worker worker)
        {
            AssertUiThread("RunRequestSolve");
            List<GH_Component> expire = BeginRequestSolve(documentId, worker);
            if (expire == null)
            {
                // A duplicate delegate can arrive when a queued callback was
                // recovered and the original then runs. If no other batch owns
                // the queue, release it; otherwise the owner will finish it.
                // A delegate from a stopped worker must not clear the flag of
                // the worker that replaced it.
                lock (Gate)
                {
                    if (worker != null && !IsCurrentWorkerLocked(worker)) return;
                    if (_activeRequests == null)
                    {
                        _solveQueued = false;
                        _solveQueuedAt = DateTime.MinValue;
                        _solveDocumentId = Guid.Empty;
                    }
                }
                return;
            }
            if (document != null)
            {
                // The batch owns this document's SolutionEnd. The handler
                // captures the worker token so a late event (or a Stop) cannot
                // finish a batch that belongs to a later connection.
                GH_Document.SolutionEndEventHandler handler = null;
                handler = delegate(object sender, GH_SolutionEventArgs e)
                {
                    GH_Document source = sender as GH_Document;
                    if (source != null) source.SolutionEnd -= handler;
                    bool scheduleNext;
                    lock (Gate)
                    {
                        if (!IsCurrentWorkerLocked(worker)) return;
                        if (!object.ReferenceEquals(_solveHandler, handler)) return;
                        _solveHandler = null;
                        _solveHandlerDocument = null;
                        // Reconcile when the solve has finished: every component
                        // has published by then, so an unclaimed provisional
                        // slot is genuinely foreign.
                        scheduleNext = FinishRequestSolveLocked();
                    }
                    ReconcileOwnership();
                    // Requests that arrived during the solve are the next
                    // batch. They were kept out of this context so their inputs
                    // are read fresh.
                    if (scheduleNext) SchedulePendingSolve();
                };
                lock (Gate)
                {
                    _solveHandlerDocument = document;
                    _solveHandler = handler;
                }
                document.SolutionEnd += handler;
            }
            foreach (GH_Component component in expire)
            {
                try { component.ExpireSolution(false); }
                catch (Exception ex) { Note("ExpireSolution failed: " + FirstLine(ex)); }
            }
        }

        private static Guid FirstPendingDocumentLocked()
        {
            foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                return pair.Value.DocumentId;
            return Guid.Empty;
        }

        /// <summary>Moves one document's pending requests into the active solve
        /// context. Returns the components to expire, or null when that
        /// document has nothing pending, another delegate already owns the
        /// batch, or the scheduling worker is no longer current. The caller
        /// must not hold Gate while expiring: ExpireSolution is a GH API call
        /// on the UI thread.</summary>
        private static List<GH_Component> BeginRequestSolve(Guid documentId, Worker worker)
        {
            lock (Gate)
            {
                if (worker != null && !IsCurrentWorkerLocked(worker)) return null;
                var active = new Dictionary<string, PendingRequest>();
                foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                    if (pair.Value.DocumentId == documentId)
                        active[pair.Key] = pair.Value;
                if (active.Count == 0) return null;
                foreach (string key in active.Keys) PendingRequests.Remove(key);
                _activeRequests = active;
                _activeDocumentId = documentId;
                // The batch owns the queue from the delegate until
                // SolutionEnd; a request arriving meanwhile must not schedule a
                // second solve. Restored here as well because the retry path
                // may have released the timer just before this delegate ran.
                _solveQueued = true;
                // A solve that reached its delegate is alive: the watchdog
                // measures this batch from here, not from the schedule call.
                _solveQueuedAt = DateTime.UtcNow;
                _solveDocumentId = documentId;
                DateTime started = DateTime.UtcNow;
                foreach (KeyValuePair<string, PendingRequest> pair in active)
                {
                    StageSlot slot = FindSlotLocked(documentId, pair.Key);
                    if (slot == null) continue;
                    // Pin the answer rid per requested key before its inputs
                    // are read: a request arriving mid-solve overwrites Rid,
                    // and answering the newer rid would relabel this answer.
                    slot.SolveRid = pair.Value.Rid;
                    // Only this batch's stages are in flight. RetryStaleSolves
                    // must not schedule a second solve for a merely slow one.
                    slot.SolveInFlight = true;
                    slot.SolveStartedAt = started;
                }
                return StageComponentsLocked(active.Keys, documentId);
            }
        }

        /// <summary>Every component in one document owning one of
        /// <paramref name="keys"/>: Stage In and Stage Out with that stage key,
        /// Params declaring it. Deduplicated by owner reference; the Stage Out
        /// is included whether or not its input is connected. Under Gate.</summary>
        private static List<GH_Component> StageComponentsLocked(IEnumerable<string> keys, Guid documentId)
        {
            var wanted = new HashSet<string>(keys);
            var owners = new List<GH_Component>();
            foreach (Claim claim in Claims)
            {
                if (claim.DocumentId != documentId) continue;
                bool owns;
                if (claim.Role == ClaimRole.Param)
                    owns = !string.IsNullOrEmpty(claim.ParamKey) && wanted.Contains(claim.ParamKey);
                else
                    owns = !string.IsNullOrEmpty(claim.StageKey) && wanted.Contains(claim.StageKey);
                if (!owns) continue;
                bool seen = false;
                foreach (GH_Component owner in owners)
                    if (object.ReferenceEquals(owner, claim.Owner)) { seen = true; break; }
                if (!seen) owners.Add(claim.Owner);
            }
            return owners;
        }

        /// <summary>Test hook: starts the scheduled delegate's request
        /// context without a GH document. Pair with
        /// <see cref="EndRequestSolveForTest"/>. Not public API.</summary>
        internal static bool BeginRequestSolveForTest()
        {
            Guid documentId;
            lock (Gate) { documentId = FirstPendingDocumentLocked(); }
            return BeginRequestSolve(documentId, null) != null;
        }

        /// <summary>Test hook: like <see cref="BeginRequestSolveForTest"/> but
        /// also reports which document's batch started.</summary>
        internal static bool BeginRequestSolveForTest(out Guid documentId)
        {
            lock (Gate) { documentId = FirstPendingDocumentLocked(); }
            return BeginRequestSolve(documentId, null) != null;
        }

        /// <summary>Test hook: finishes the active request context exactly as
        /// the SolutionEnd handler does, without a document solve.</summary>
        internal static void EndRequestSolveForTest()
        {
            lock (Gate) { FinishRequestSolveLocked(); }
        }

        /// <summary>Clears the active request context. Only the requested keys
        /// are unpinned: an unrelated slot was never part of this solve and a
        /// user-driven answer may own its own state. Returns true when requests
        /// arrived during the solve and a next batch is waiting. Under Gate.</summary>
        private static bool FinishRequestSolveLocked()
        {
            if (_activeRequests != null)
            {
                foreach (string key in _activeRequests.Keys)
                {
                    StageSlot slot = FindSlotLocked(_activeDocumentId, key);
                    if (slot == null) continue;
                    slot.SolveRid = "";
                    slot.SolveInFlight = false;
                    slot.SolveStartedAt = DateTime.MinValue;
                }
                _activeRequests = null;
            }
            _activeDocumentId = Guid.Empty;
            _solveDocumentId = Guid.Empty;
            _solveQueued = false;
            _solveQueuedAt = DateTime.MinValue;
            return PendingRequests.Count > 0;
        }

        /// <summary>Drops every registry entry no live component claims: stage
        /// keys, parameter declarations, and the slots they served. A
        /// provisional slot whose key nobody claimed answers `unknown_stage`
        /// before it goes, so the peer sees the mismatch instead of a
        /// timeout. Conflicted keys keep their per-document slots so removing
        /// one claimant restores the survivor's state.</summary>
        private static void ReconcileOwnership()
        {
            var refused = new List<KeyValuePair<string, string>>();
            lock (Gate)
            {
                // A claim belongs to a registered owner. Unregister removes both
                // entries under one lock; this sweep also covers a late publish
                // from a component that has already left, so the registry cannot
                // keep a claim nothing can serve or remove.
                for (int i = Claims.Count - 1; i >= 0; i--)
                    if (!IsRegisteredLocked(Claims[i].Owner)) Claims.RemoveAt(i);

                RebuildPublicationLocked();

                var claimedByDocument = new Dictionary<Guid, HashSet<string>>();
                foreach (Claim claim in Claims)
                {
                    string key = ClaimKey(claim);
                    if (string.IsNullOrEmpty(key)) continue;
                    HashSet<string> keys;
                    if (!claimedByDocument.TryGetValue(claim.DocumentId, out keys))
                    {
                        keys = new HashSet<string>();
                        claimedByDocument[claim.DocumentId] = keys;
                    }
                    keys.Add(key);
                }

                foreach (KeyValuePair<Guid, Dictionary<string, StageSlot>> document
                         in new List<KeyValuePair<Guid, Dictionary<string, StageSlot>>>(Slots))
                {
                    HashSet<string> claimed;
                    claimedByDocument.TryGetValue(document.Key, out claimed);
                    var documentSlots = new List<KeyValuePair<string, StageSlot>>(document.Value);
                    foreach (KeyValuePair<string, StageSlot> pair in documentSlots)
                    {
                        if (claimed != null && claimed.Contains(pair.Key))
                        {
                            pair.Value.Provisional = false;
                            continue;
                        }
                        document.Value.Remove(pair.Key);
                        if (pair.Value.Provisional && !string.IsNullOrEmpty(pair.Value.Rid))
                            refused.Add(new KeyValuePair<string, string>(pair.Key, pair.Value.Rid));
                    }
                    if (document.Value.Count == 0) Slots.Remove(document.Key);
                }
            }
            foreach (KeyValuePair<string, string> entry in refused)
                Enqueue(ErrorFrame("unknown_stage",
                    "no Stageloom component claims this stage key", entry.Key, entry.Value));
            // Ownership and conflict changes must reach the peer now: a key that
            // just resolved has to appear in hello/schema without waiting for
            // the next canvas solve or heartbeat.
            SendPublication();
        }

        /// <summary>Rebuilds the published view from the live claims. One key
        /// is published only when every claim for it belongs to one document
        /// and no role is duplicated within that document. A key two documents
        /// claim is a strict conflict: no hello row, no schema, no solve, and
        /// no document's state overwrites the other. A key that was published
        /// and is no longer claimed is tombstoned and its slot dropped, so a
        /// later request is refused instead of waiting forever. Under Gate.</summary>
        private static void RebuildPublicationLocked()
        {
            var previousKeys = new HashSet<string>(KeyOwners.Keys);
            foreach (string key in Conflicts.Keys) previousKeys.Add(key);

            var keyClaims = new Dictionary<string, List<Claim>>();
            foreach (Claim claim in Claims)
            {
                string key = ClaimKey(claim);
                if (string.IsNullOrEmpty(key)) continue;
                List<Claim> list;
                if (!keyClaims.TryGetValue(key, out list))
                {
                    list = new List<Claim>();
                    keyClaims[key] = list;
                }
                list.Add(claim);
            }

            var owners = new Dictionary<string, Guid>();
            var conflicts = new Dictionary<string, string>();
            var enabled = new Dictionary<string, bool>();
            var hasIn = new HashSet<string>();
            var hasOut = new HashSet<string>();
            var hasEnabledOut = new HashSet<string>();
            var parameters = new Dictionary<string, List<Dictionary<string, object>>>();

            foreach (KeyValuePair<string, List<Claim>> pair in keyClaims)
            {
                string key = pair.Key;
                List<Claim> claims = pair.Value;
                var documents = new HashSet<Guid>();
                foreach (Claim claim in claims) documents.Add(claim.DocumentId);

                string conflict = null;
                Guid owner = Guid.Empty;
                if (documents.Count > 1)
                {
                    conflict = ConflictMessage(key, documents);
                }
                else
                {
                    owner = claims[0].DocumentId;
                    int inputs = 0;
                    int outputs = 0;
                    var paramIds = new Dictionary<string, int>();
                    foreach (Claim claim in claims)
                    {
                        if (claim.Role == ClaimRole.Input) inputs++;
                        else if (claim.Role == ClaimRole.Output) outputs++;
                        else if (claim.Role == ClaimRole.Param
                                 && !string.IsNullOrEmpty(claim.ParamId))
                        {
                            int count;
                            paramIds.TryGetValue(claim.ParamId, out count);
                            paramIds[claim.ParamId] = count + 1;
                        }
                    }
                    if (inputs > 1)
                        conflict = RoleConflictMessage(key, owner, "more than one Stage In");
                    else if (outputs > 1)
                        conflict = RoleConflictMessage(key, owner, "more than one Stage Out");
                    else
                    {
                        foreach (KeyValuePair<string, int> id in paramIds)
                        {
                            if (id.Value > 1)
                            {
                                conflict = RoleConflictMessage(
                                    key, owner, "duplicate parameter id " + id.Key);
                                break;
                            }
                        }
                    }
                }

                if (!string.IsNullOrEmpty(conflict))
                {
                    conflicts[key] = conflict;
                    continue;
                }

                owners[key] = owner;
                bool stageEnabled = true;
                long newest = long.MinValue;
                bool stageCount = false;
                var paramList = new List<Dictionary<string, object>>();
                foreach (Claim claim in claims)
                {
                    if (claim.Role == ClaimRole.Param)
                    {
                        if (claim.Declaration != null) paramList.Add(claim.Declaration);
                        continue;
                    }
                    stageCount = true;
                    if (claim.Sequence >= newest)
                    {
                        newest = claim.Sequence;
                        stageEnabled = claim.Enabled;
                    }
                    if (claim.Role == ClaimRole.Input) hasIn.Add(key);
                    else if (claim.Role == ClaimRole.Output)
                    {
                        hasOut.Add(key);
                        if (claim.Enabled) hasEnabledOut.Add(key);
                    }
                }
                if (stageCount) enabled[key] = stageEnabled;
                if (paramList.Count > 0) parameters[key] = paramList;
            }

            foreach (KeyValuePair<string, string> pair in conflicts)
            {
                string previous;
                if (!Conflicts.TryGetValue(pair.Key, out previous) || previous != pair.Value)
                    Note(pair.Value);
            }
            foreach (string key in Conflicts.Keys)
            {
                if (!conflicts.ContainsKey(key))
                    Note("duplicate stage key '" + key + "' resolved; the surviving claim owns it");
            }

            // A conflicted key must not answer an already queued request. The
            // active batch drops it too; ShouldAnswer also refuses it, so a
            // component already running cannot emit a response.
            foreach (string key in conflicts.Keys)
            {
                PendingRequests.Remove(key);
                if (_activeRequests != null && _activeRequests.ContainsKey(key))
                {
                    _activeRequests.Remove(key);
                    foreach (Dictionary<string, StageSlot> documentSlots in Slots.Values)
                    {
                        StageSlot slot;
                        if (!documentSlots.TryGetValue(key, out slot)) continue;
                        slot.SolveRid = "";
                        slot.SolveInFlight = false;
                        slot.SolveStartedAt = DateTime.MinValue;
                    }
                }
            }

            StageEnabled.Clear();
            foreach (KeyValuePair<string, bool> pair in enabled) StageEnabled[pair.Key] = pair.Value;
            StageHasIn.Clear();
            foreach (string key in hasIn) StageHasIn.Add(key);
            StageHasOut.Clear();
            foreach (string key in hasOut) StageHasOut.Add(key);
            StageOutEnabled.Clear();
            foreach (string key in hasEnabledOut) StageOutEnabled.Add(key);
            StageParams.Clear();
            foreach (KeyValuePair<string, List<Dictionary<string, object>>> pair in parameters)
                StageParams[pair.Key] = pair.Value;
            KeyOwners.Clear();
            foreach (KeyValuePair<string, Guid> pair in owners) KeyOwners[pair.Key] = pair.Value;
            Conflicts.Clear();
            foreach (KeyValuePair<string, string> pair in conflicts) Conflicts[pair.Key] = pair.Value;

            // Keys that left the published view entirely are tombstones, not
            // future provisional slots. A request racing their removal is
            // refused with unknown_stage, and any slot, pending request or
            // active pin for the old key goes with it. Re-claiming the key
            // clears the tombstone below.
            var removed = new HashSet<string>(previousKeys);
            foreach (string key in owners.Keys) removed.Remove(key);
            foreach (string key in conflicts.Keys) removed.Remove(key);
            foreach (string key in owners.Keys) RemovedKeys.Remove(key);
            foreach (string key in conflicts.Keys) RemovedKeys.Remove(key);
            if (removed.Count > 0)
            {
                var refusals = new List<KeyValuePair<string, string>>();
                foreach (string key in removed)
                {
                    RemovedKeys.Add(key);
                    PendingRequest pending;
                    if (PendingRequests.TryGetValue(key, out pending))
                    {
                        PendingRequests.Remove(key);
                        if (!string.IsNullOrEmpty(pending.Rid))
                            refusals.Add(new KeyValuePair<string, string>(key, pending.Rid));
                    }
                    if (_activeRequests != null && _activeRequests.ContainsKey(key))
                    {
                        PendingRequest active;
                        if (_activeRequests.TryGetValue(key, out active)
                            && !string.IsNullOrEmpty(active.Rid))
                            refusals.Add(new KeyValuePair<string, string>(key, active.Rid));
                        _activeRequests.Remove(key);
                    }
                    foreach (Guid documentId in new List<Guid>(Slots.Keys))
                    {
                        Dictionary<string, StageSlot> documentSlots = Slots[documentId];
                        StageSlot slot;
                        if (!documentSlots.TryGetValue(key, out slot)) continue;
                        slot.SolveRid = "";
                        slot.SolveInFlight = false;
                        slot.SolveStartedAt = DateTime.MinValue;
                        documentSlots.Remove(key);
                        if (documentSlots.Count == 0) Slots.Remove(documentId);
                    }
                }
                // An answer can no longer be produced for an in-flight request.
                // Refuse it now instead of leaving Blender to a timeout.
                foreach (KeyValuePair<string, string> refusal in refusals)
                    Enqueue(ErrorFrame("unknown_stage",
                        "no Stageloom component claims this stage key",
                        refusal.Key, refusal.Value));
            }
        }

        private static string ClaimKey(Claim claim)
        {
            if (claim.Role == ClaimRole.Param) return claim.ParamKey;
            return claim.StageKey;
        }

        private static string ConflictMessage(string key, HashSet<Guid> documents)
        {
            var names = new List<string>();
            foreach (Guid id in documents) names.Add(DocumentNameLocked(id));
            names.Sort(StringComparer.Ordinal);
            return "duplicate stage key '" + key + "': claimed by "
                   + string.Join(" and ", names.ToArray())
                   + "; not publishing or solving it until one document releases it";
        }

        private static string RoleConflictMessage(string key, Guid owner, string problem)
        {
            return "duplicate stage key '" + key + "': " + problem + " in "
                   + DocumentNameLocked(owner)
                   + "; not publishing or solving it until one component is removed";
        }

        private static string DocumentNameLocked(Guid id)
        {
            DocumentEntry entry;
            if (Documents.TryGetValue(id, out entry) && !string.IsNullOrEmpty(entry.Name))
                return entry.Name;
            return "document " + ShortId(id);
        }

        private static string ShortId(Guid id)
        {
            string text = id.ToString("N");
            return text.Length > 8 ? text.Substring(0, 8) : text;
        }

        private static Registration FindRegistrationLocked(GH_Component owner)
        {
            foreach (Registration registration in Components)
                if (object.ReferenceEquals(registration.Owner, owner)) return registration;
            return null;
        }

        private static Guid DocumentIdLocked(GH_Component owner)
        {
            Registration registration = FindRegistrationLocked(owner);
            return registration == null ? Guid.Empty : registration.DocumentId;
        }

        private static StageSlot FindSlotLocked(Guid documentId, string key)
        {
            Dictionary<string, StageSlot> documentSlots;
            if (!Slots.TryGetValue(documentId, out documentSlots)) return null;
            StageSlot slot;
            return documentSlots.TryGetValue(key ?? "", out slot) ? slot : null;
        }

        private static StageSlot GetOrCreateSlotLocked(Guid documentId, string key)
        {
            Dictionary<string, StageSlot> documentSlots;
            if (!Slots.TryGetValue(documentId, out documentSlots))
            {
                documentSlots = new Dictionary<string, StageSlot>();
                Slots[documentId] = documentSlots;
            }
            string name = key ?? "";
            StageSlot slot;
            if (!documentSlots.TryGetValue(name, out slot))
            {
                slot = new StageSlot();
                slot.DocumentId = documentId;
                slot.Key = name;
                documentSlots[name] = slot;
            }
            return slot;
        }

        private static StageSlot FindAnySlotLocked(string key)
        {
            foreach (Dictionary<string, StageSlot> documentSlots in Slots.Values)
            {
                StageSlot slot;
                if (documentSlots.TryGetValue(key ?? "", out slot)) return slot;
            }
            return null;
        }

        private static List<StageSlot> AllSlotsLocked()
        {
            var slots = new List<StageSlot>();
            foreach (Dictionary<string, StageSlot> documentSlots in Slots.Values)
                foreach (StageSlot slot in documentSlots.Values)
                    slots.Add(slot);
            return slots;
        }

        private static void RemoveDocumentStateLocked(Guid documentId)
        {
            Slots.Remove(documentId);

            var pending = new List<string>();
            foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                if (pair.Value.DocumentId == documentId) pending.Add(pair.Key);
            foreach (string key in pending) PendingRequests.Remove(key);

            if (_activeRequests != null)
            {
                var active = new List<string>();
                foreach (KeyValuePair<string, PendingRequest> pair in _activeRequests)
                    if (pair.Value.DocumentId == documentId) active.Add(pair.Key);
                foreach (string key in active) _activeRequests.Remove(key);
            }
            if (_solveDocumentId == documentId && _activeRequests == null)
            {
                _solveQueued = false;
                _solveQueuedAt = DateTime.MinValue;
                _solveDocumentId = Guid.Empty;
            }
        }

        private static void ClearAllLocked()
        {
            Slots.Clear();
            Documents.Clear();
            Claims.Clear();
            _claimSequence = 0;
            KeyOwners.Clear();
            Conflicts.Clear();
            StageEnabled.Clear();
            StageHasIn.Clear();
            StageHasOut.Clear();
            StageOutEnabled.Clear();
            RemovedKeys.Clear();
            StageParams.Clear();
            PendingRequests.Clear();
            _activeRequests = null;
            _activeDocumentId = Guid.Empty;
            _solveDocumentId = Guid.Empty;
            _solveQueued = false;
            _solveQueuedAt = DateTime.MinValue;
            _lastStages.Clear();
            _lastStageIns.Clear();
            _lastStageOuts.Clear();
            _lastSchema.Clear();
            _publishedThisConnection = false;
            _cachedUnits = "Meters";
            _cachedUnitScale = 1.0;
            _lastUnits = "";
            _solveHandlerDocument = null;
            _solveHandler = null;
        }

        private static Claim ClaimForLocked(GH_Component owner)
        {
            // Only a registered owner may own a claim. A component that has
            // left the document may still run one last solve; do not resurrect
            // a claim for it, because no live owner would publish or remove it
            // afterwards.
            Registration registration = FindRegistrationLocked(owner);
            if (registration == null) return null;
            foreach (Claim claim in Claims)
            {
                if (object.ReferenceEquals(claim.Owner, owner))
                {
                    claim.DocumentId = registration.DocumentId;
                    return claim;
                }
            }
            var fresh = new Claim();
            fresh.Owner = owner;
            fresh.DocumentId = registration.DocumentId;
            Claims.Add(fresh);
            return fresh;
        }

        private static bool IsRegisteredLocked(GH_Component owner)
        {
            return FindRegistrationLocked(owner) != null;
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
                if (!IsCurrentWorker(worker)) return;
                if (fired == 0) SendFrames(client, worker);
                if (!IsCurrentWorker(worker)) return;
                RetryStaleSolves(worker);
                // Re-publish on a slow heartbeat: a component added while the link
                // was down (or a schema sent before a reconnect) is picked up
                // without the user having to touch the canvas.
                if (IsCurrentWorker(worker) && _connected
                    && (DateTime.UtcNow - LastPublishAt()).TotalSeconds >= 2.0)
                    PublishIfCurrent(worker);
            }
        }

        /// <summary>Re-queues a request that has been waiting a while.
        /// ScheduleSolution calls coalesce, and a request can arrive while the
        /// document is busy; without this the stage would sit `waiting` until a
        /// Push. A slot whose solve is younger than the watchdog is not
        /// retried: it is slow, not lost. A key with no answering Stage Out is
        /// one-way and is never retried: nothing will answer it, and eight
        /// retries only churn the solve queue.</summary>
        internal static void RetryStaleSolves()
        {
            RetryStaleSolves(null);
        }

        private static void RetryStaleSolves(Worker worker)
        {
            var waiting = new List<KeyValuePair<string, PendingRequest>>();
            bool watchdog = false;
            bool rescheduled = false;
            DateTime now = DateTime.UtcNow;
            lock (Gate)
            {
                if (worker != null && !IsCurrentWorkerLocked(worker)) return;
                // ScheduleSolution erases a pending callback when another
                // solution (a canvas edit) runs first. The request is still
                // queued: put it back on the timer instead of waiting for the
                // watchdog. An active solve is not touched here.
                if (_solveQueued && _activeRequests == null
                    && (now - _solveQueuedAt).TotalMilliseconds >= 250)
                {
                    _solveQueued = false;
                    _solveQueuedAt = DateTime.MinValue;
                    _solveDocumentId = Guid.Empty;
                    rescheduled = true;
                }
                // A batch that never reported SolutionEnd still owns the
                // queue. Release it once the watchdog covers it; otherwise the
                // requests queued below could never be scheduled.
                if (_solveQueued && (now - _solveQueuedAt).TotalSeconds >= SolveWatchdogSeconds)
                {
                    FinishRequestSolveLocked();
                    watchdog = true;
                }
                foreach (var documentSlots in Slots)
                {
                    foreach (var kv in documentSlots.Value)
                    {
                        StageSlot slot = kv.Value;
                        if (Conflicts.ContainsKey(kv.Key)) continue;
                        // One-way stage: Stage In owns the terminal status and
                        // no Stage Out will ever answer, so leave it alone.
                        if (!StageAnswersLocked(kv.Key)) continue;
                        if (slot.Status != "waiting") continue;
                        // No request to answer: an unsolicited document solve must
                        // not leave a Stage Out looking like a stalled request.
                        if (string.IsNullOrEmpty(slot.Rid)) continue;
                        if (slot.SolveInFlight)
                        {
                            if ((now - slot.SolveStartedAt).TotalSeconds < SolveWatchdogSeconds)
                                continue;
                            // The solve never reached SolutionEnd. Let the retry
                            // path take over rather than leaving the stage silent.
                            slot.SolveInFlight = false;
                            slot.SolveStartedAt = DateTime.MinValue;
                            slot.SolveRid = "";
                            watchdog = true;
                        }
                        if ((now - slot.RequestedAt).TotalMilliseconds < 250) continue;
                        if (slot.ResolveAttempts >= 8) continue;
                        slot.ResolveAttempts++;
                        slot.RequestedAt = now;
                        waiting.Add(new KeyValuePair<string, PendingRequest>(
                            kv.Key, new PendingRequest(documentSlots.Key, slot.Rid)));
                    }
                }
            }
            if (watchdog)
                Note("a solve did not report SolutionEnd; retrying the waiting stage");
            if (waiting.Count == 0)
            {
                // A recovered timer still owns any requests that arrived while
                // it was pending, even when every stage was already answered
                // (a user solve replaced ours).
                if (rescheduled || watchdog) SchedulePendingSolve(worker);
                return;
            }
            // One retry batch, one diagnostic: RequestStage below coalesces the
            // keys into the next solve.
            Interlocked.Increment(ref _retryRequests);
            foreach (KeyValuePair<string, PendingRequest> pair in waiting)
                RequestStage(pair.Key, pair.Value.Rid, worker);
        }

        private static void SendFrames(MiniWsClient client, Worker worker)
        {
            while (true)
            {
                byte[] frame;
                lock (Gate)
                {
                    // A stale sender must not take a frame out of the outbox
                    // the current worker owns.
                    if (worker != null && !IsCurrentWorkerLocked(worker)) return;
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
        /// when nothing changed. The document units are captured here, on the
        /// UI thread, before the socket thread ever reads them.</summary>
        public static void Publish(GH_Document doc)
        {
            AssertUiThread("GhLink.Publish");
            if (doc != null) CaptureDocumentSnapshot(doc);
            SendPublication();
        }

        private static volatile string _lastDocName = "(unsaved)";
        // The heartbeat timestamp is a long read/written with Interlocked: a
        // DateTime wraps two ints, which this 32-bit runtime can tear.
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
        /// Safe from either thread: it touches only the registries, the cached
        /// UI-thread units snapshot and the outbox. It never reads RhinoDoc or
        /// GH_Document.</summary>
        private static void SendPublication()
        {
            MarkPublished();
            if (!_connected) return;
            Dictionary<string, bool> stages;
            HashSet<string> stageIns;
            HashSet<string> stageOuts;
            Dictionary<string, List<Dictionary<string, object>>> paramsByStage;
            string units;
            lock (Gate)
            {
                stages = new Dictionary<string, bool>(StageEnabled);
                stageIns = new HashSet<string>(StageHasIn);
                stageOuts = new HashSet<string>(StageHasOut);
                paramsByStage = new Dictionary<string, List<Dictionary<string, object>>>();
                foreach (var kv in StageParams) paramsByStage[kv.Key] = new List<Dictionary<string, object>>(kv.Value);
                units = _cachedUnits;
            }

            // Reserve the once-per-connection hello under Gate: two threads
            // reading the flag independently both sent one.
            bool needHello;
            lock (Gate)
            {
                needHello = !_publishedThisConnection || _lastUnits != units
                            || StagesChangedLocked(stages, stageIns, stageOuts);
                if (needHello)
                {
                    _publishedThisConnection = true;
                    _lastUnits = units;
                }
            }

            if (needHello)
            {
                var list = new List<object>();
                foreach (var kv in stages)
                {
                    var entry = new Dictionary<string, object>();
                    entry["key"] = kv.Key;
                    entry["enabled"] = kv.Value;
                    // Optional additive fields (protocol §4.1): true when a
                    // Stage In / Stage Out claims the key. No Stage In means a
                    // generator; no Stage Out means a one-way consumer whose
                    // answers Blender must not wait for.
                    entry["has_stage_in"] = stageIns.Contains(kv.Key);
                    entry["has_stage_out"] = stageOuts.Contains(kv.Key);
                    list.Add(entry);
                }
                var hello = new Dictionary<string, object>();
                hello["type"] = "hello";
                // Keep in sync with blender_manifest.toml (the release path
                // enforces that one) and the example in docs/protocol.md.
                hello["client"] = "Stageloom 0.6.0";
                hello["doc"] = _lastDocName;
                hello["doc_units"] = units;
                hello["stages"] = list;
                // Optional additive field (protocol §4.1): this client reads
                // multi-part definition entries. Older Blender ignores it.
                var caps = new List<object>();
                caps.Add(Blocks2Capability);
                hello["caps"] = caps;
                Enqueue(FrameCodec.Encode(hello, null));
                lock (Gate)
                {
                    _lastStages = new Dictionary<string, bool>(stages);
                    _lastStageIns = new HashSet<string>(stageIns);
                    _lastStageOuts = new HashSet<string>(stageOuts);
                }
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
                // Publish a schema only when its canonical content changes; the
                // periodic hello remains the safety net.
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
        // The has_stage_in / has_stage_out halves of the last hello. A Stage In
        // or Stage Out appearing or disappearing must republish hello even when
        // the key set is unchanged.
        private static HashSet<string> _lastStageIns = new HashSet<string>();
        private static HashSet<string> _lastStageOuts = new HashSet<string>();
        // The doc_units of the last hello. A unit change must republish hello
        // even when the stage signature is unchanged.
        private static string _lastUnits = "";
        // Last schema declaration published per stage, by canonical JSON. Under
        // Gate; cleared on a new connection.
        private static readonly Dictionary<string, string> _lastSchema =
            new Dictionary<string, string>();

        private static bool StagesChangedLocked(Dictionary<string, bool> stages,
                                                HashSet<string> stageIns,
                                                HashSet<string> stageOuts)
        {
            if (_lastStages.Count != stages.Count) return true;
            foreach (var kv in stages)
            {
                bool other;
                if (!_lastStages.TryGetValue(kv.Key, out other) || other != kv.Value) return true;
                if (_lastStageIns.Contains(kv.Key) != stageIns.Contains(kv.Key)) return true;
                if (_lastStageOuts.Contains(kv.Key) != stageOuts.Contains(kv.Key)) return true;
            }
            return false;
        }

        /// <summary>Reads a document's model units on the UI thread. This is the
        /// only place the plugin reads Rhino units; the returned values are
        /// cached and handed to the socket/sender threads. A null document or a
        /// document with no RhinoDoc keeps the metre default.</summary>
        private static void ReadDocumentUnits(GH_Document document, out string units, out double scale)
        {
            units = "Meters";
            scale = 1.0;
            if (document == null) return;
            try
            {
                RhinoDoc rhino = document.RhinoDocument;
                if (rhino == null) return;
                UnitSystem system = rhino.ModelUnitSystem;
                units = system.ToString();
                scale = RhinoMath.UnitScale(UnitSystem.Meters, system);
            }
            catch (Exception) { }
        }

        /// <summary>Refreshes the cached units for the document a component
        /// solves in. Called on the UI thread at registration and from every
        /// Publish; never from the socket or sender.</summary>
        private static void CaptureDocumentSnapshot(GH_Document document)
        {
            if (document == null) return;
            AssertUiThread("GhLink.CaptureDocumentSnapshot");
            string units;
            double scale;
            ReadDocumentUnits(document, out units, out scale);
            string name = null;
            try
            {
                name = document.FilePath;
            }
            catch (Exception) { }
            lock (Gate)
            {
                _cachedUnits = units;
                _cachedUnitScale = scale;
                if (!string.IsNullOrEmpty(name)) _lastDocName = name;
            }
        }

        /// <summary>Test hook: installs a units snapshot without a Rhino
        /// document, the same seam the registration capture fills in. Kept
        /// internal for the console suite; not plugin API.</summary>
        internal static void SetUnitsSnapshotForTest(string units, double scale)
        {
            lock (Gate)
            {
                _cachedUnits = string.IsNullOrEmpty(units) ? "Meters" : units;
                _cachedUnitScale = scale;
            }
        }

        private static string UnitsName()
        {
            lock (Gate) { return _cachedUnits; }
        }

        // ------------------------------------------------------------- helpers

        /// <summary>The cached metres-per-document-unit scale. Safe from any
        /// thread; Stage In/Out on the UI thread should use the overload that
        /// takes their document so a unit change is captured before use.</summary>
        public static double DocumentScale()
        {
            lock (Gate) { return _cachedUnitScale; }
        }

        /// <summary>Captures the document's units on the UI thread and returns
        /// the resulting scale, so a solve uses the units its document has
        /// now rather than the previous solve's snapshot.</summary>
        public static double DocumentScale(GH_Document document)
        {
            if (document != null) CaptureDocumentSnapshot(document);
            return DocumentScale();
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
