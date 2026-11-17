// One request in flight per stage, latest-wins, retry and solve scheduling.
// PendingRequests holds the latest request per key; one scheduled delegate
// owns the queue and consumes one document's requests, and _activeRequests
// names exactly the stages that solve answers.

using System;
using System.Collections.Generic;
using System.Threading;
using Grasshopper.Kernel;

namespace Stageloom
{
    internal static class RequestScheduler
    {
        private const double SolveWatchdogSeconds = 30.0;

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
        private static Dictionary<string, PendingRequest> _activeRequests;  // under State
        private static Guid _activeDocumentId = Guid.Empty;                 // under State
        private static Guid _solveDocumentId = Guid.Empty;                  // under State
        private static bool _solveQueued;                             // under State
        private static DateTime _solveQueuedAt = DateTime.MinValue;   // under State
        private static int _retryRequests;
        private static GH_Document _solveHandlerDocument;            // under State
        private static GH_Document.SolutionEndEventHandler _solveHandler;  // under State

        internal static int RetryRequests
        {
            get { return Interlocked.Add(ref _retryRequests, 0); }
        }

        internal static bool ShouldAnswer(string key)
        {
            lock (LinkSync.State)
            {
                string name = key ?? "";
                if (StageRegistry.IsConflictedLocked(name)) return false;
                if (_activeRequests == null || _activeRequests.ContainsKey(name)) return true;
                return !PendingRequests.ContainsKey(name);
            }
        }

        internal static bool IsRequestDriven(string key)
        {
            lock (LinkSync.State)
            {
                string name = key ?? "";
                return !StageRegistry.IsConflictedLocked(name)
                       && _activeRequests != null && _activeRequests.ContainsKey(name);
            }
        }

        internal static void RequestStage(string key, string rid)
        {
            RequestStage(key, rid, null);
        }

        internal static void RequestStage(string key, string rid, LinkWorker worker)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (LinkSync.State)
            {
                // A frame decoded by a worker Stop already replaced must not
                // queue work for the next session.
                if (worker != null && !LinkWorker.IsCurrentLocked(worker)) return;
                if (StageRegistry.IsConflictedLocked(key)) return;
                Guid owner = Guid.Empty;
                bool owned = StageRegistry.TryGetOwnerLocked(key, out owner);
                StageSlot slot = owned
                    ? StageRegistry.FindSlotLocked(owner, key)
                    : StageRegistry.FindAnySlotLocked(key);
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

        private static void SchedulePendingSolve(LinkWorker worker)
        {
            Guid documentId = Guid.Empty;
            Action<GH_Document.GH_ScheduleDelegate> schedule = null;
            LinkWorker token = null;
            lock (LinkSync.State)
            {
                if (worker != null && !LinkWorker.IsCurrentLocked(worker)) return;
                if (_solveQueued || PendingRequests.Count == 0) return;
                Guid first = Guid.Empty;
                foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                {
                    if (first == Guid.Empty) first = pair.Value.DocumentId;
                    Action<GH_Document.GH_ScheduleDelegate> entrySchedule;
                    if (StageRegistry.TryGetScheduleLocked(pair.Value.DocumentId, out entrySchedule))
                    {
                        documentId = pair.Value.DocumentId;
                        schedule = entrySchedule;
                        token = worker != null ? worker : LinkWorker.CurrentLocked();
                        break;
                    }
                }
                if (schedule == null)
                {
                    documentId = first;
                    LinkNotes.Add("request received, but no active document; held in the slot");
                    return;
                }
                if (token == null || !LinkWorker.IsCurrentLocked(token))
                {
                    LinkNotes.Add("request received, but no active worker; held in the slot");
                    return;
                }
                // Reserve the one scheduled delegate before leaving the lock.
                _solveQueued = true;
                _solveQueuedAt = DateTime.UtcNow;
                _solveDocumentId = documentId;
            }
            LinkWorker solveWorker = token;
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
                LinkNotes.Add("ScheduleSolution failed: " + ErrorText.FirstLine(ex));
            }
        }

        private static void ReleaseQueuedSolve(LinkWorker worker)
        {
            lock (LinkSync.State)
            {
                if (worker != null && !LinkWorker.IsCurrentLocked(worker)) return;
                _solveQueued = false;
                _solveQueuedAt = DateTime.MinValue;
                _solveDocumentId = Guid.Empty;
            }
        }

        private static void RunRequestSolve(GH_Document document, Guid documentId, LinkWorker worker)
        {
            StageRegistry.AssertUiThread("RunRequestSolve");
            List<GH_Component> expire = BeginRequestSolve(documentId, worker);
            if (expire == null)
            {
                // A duplicate delegate can arrive when a queued callback was
                // recovered and the original then runs. A delegate from a
                // stopped worker must not clear the next worker's flag.
                lock (LinkSync.State)
                {
                    if (worker != null && !LinkWorker.IsCurrentLocked(worker)) return;
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
                    lock (LinkSync.State)
                    {
                        if (!LinkWorker.IsCurrentLocked(worker)) return;
                        if (!object.ReferenceEquals(_solveHandler, handler)) return;
                        _solveHandler = null;
                        _solveHandlerDocument = null;
                        // Reconcile after the solve: every component has
                        // published, so an unclaimed provisional slot is
                        // genuinely foreign.
                        scheduleNext = FinishRequestSolveLocked();
                    }
                    StageRegistry.Reconcile();
                    Publication.Send();
                    // Requests that arrived during the solve are the next batch.
                    if (scheduleNext) SchedulePendingSolve();
                };
                lock (LinkSync.State)
                {
                    _solveHandlerDocument = document;
                    _solveHandler = handler;
                }
                document.SolutionEnd += handler;
            }
            foreach (GH_Component component in expire)
            {
                try { component.ExpireSolution(false); }
                catch (Exception ex)
                {
                    LinkNotes.Add("ExpireSolution failed: " + ErrorText.FirstLine(ex));
                }
            }
        }

        private static Guid FirstPendingDocumentLocked()
        {
            foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                return pair.Value.DocumentId;
            return Guid.Empty;
        }

        private static List<GH_Component> BeginRequestSolve(Guid documentId, LinkWorker worker)
        {
            lock (LinkSync.State)
            {
                if (worker != null && !LinkWorker.IsCurrentLocked(worker)) return null;
                var active = new Dictionary<string, PendingRequest>();
                foreach (KeyValuePair<string, PendingRequest> pair in PendingRequests)
                    if (pair.Value.DocumentId == documentId) active[pair.Key] = pair.Value;
                if (active.Count == 0) return null;
                foreach (string key in active.Keys) PendingRequests.Remove(key);
                _activeRequests = active;
                _activeDocumentId = documentId;
                // The batch owns the queue from the delegate until SolutionEnd.
                _solveQueued = true;
                _solveQueuedAt = DateTime.UtcNow;
                _solveDocumentId = documentId;
                DateTime started = DateTime.UtcNow;
                foreach (KeyValuePair<string, PendingRequest> pair in active)
                {
                    StageSlot slot = StageRegistry.FindSlotLocked(documentId, pair.Key);
                    if (slot == null) continue;
                    // Pin the answer rid per requested key before its inputs
                    // are read: a request arriving mid-solve overwrites Rid.
                    slot.BeginSolve(pair.Value.Rid, started);
                }
                return StageRegistry.StageComponentsLocked(active.Keys, documentId);
            }
        }

        internal static bool BeginRequestSolveForTest()
        {
            Guid documentId;
            lock (LinkSync.State) { documentId = FirstPendingDocumentLocked(); }
            return BeginRequestSolve(documentId, null) != null;
        }

        internal static bool BeginRequestSolveForTest(out Guid documentId)
        {
            lock (LinkSync.State) { documentId = FirstPendingDocumentLocked(); }
            return BeginRequestSolve(documentId, null) != null;
        }

        internal static void EndRequestSolveForTest()
        {
            lock (LinkSync.State) { FinishRequestSolveLocked(); }
        }

        private static bool FinishRequestSolveLocked()
        {
            if (_activeRequests != null)
            {
                foreach (string key in _activeRequests.Keys)
                {
                    StageSlot slot = StageRegistry.FindSlotLocked(_activeDocumentId, key);
                    if (slot != null) slot.EndSolve();
                }
                _activeRequests = null;
            }
            _activeDocumentId = Guid.Empty;
            _solveDocumentId = Guid.Empty;
            _solveQueued = false;
            _solveQueuedAt = DateTime.MinValue;
            return PendingRequests.Count > 0;
        }

        internal static void RetryStaleSolves()
        {
            RetryStaleSolves(null);
        }

        internal static void RetryStaleSolves(LinkWorker worker)
        {
            var waiting = new List<KeyValuePair<string, PendingRequest>>();
            bool watchdog = false;
            bool rescheduled = false;
            DateTime now = DateTime.UtcNow;
            lock (LinkSync.State)
            {
                if (worker != null && !LinkWorker.IsCurrentLocked(worker)) return;
                // ScheduleSolution erases a pending callback when another
                // solution (a canvas edit) runs first. Put the request back on
                // the timer instead of waiting for the watchdog.
                if (_solveQueued && _activeRequests == null
                    && (now - _solveQueuedAt).TotalMilliseconds >= 250)
                {
                    _solveQueued = false;
                    _solveQueuedAt = DateTime.MinValue;
                    _solveDocumentId = Guid.Empty;
                    rescheduled = true;
                }
                // A batch that never reported SolutionEnd still owns the queue.
                if (_solveQueued && (now - _solveQueuedAt).TotalSeconds >= SolveWatchdogSeconds)
                {
                    FinishRequestSolveLocked();
                    watchdog = true;
                }
                foreach (KeyValuePair<string, StageSlot> pair
                         in StageRegistry.AllSlotsWithKeysLocked())
                {
                    StageSlot slot = pair.Value;
                    if (StageRegistry.IsConflictedLocked(pair.Key)) continue;
                    // One-way stage: Stage In owns the terminal status and no
                    // Stage Out will ever answer.
                    if (!StageRegistry.StageAnswersLocked(pair.Key)) continue;
                    if (slot.Status != "waiting") continue;
                    // No request to answer: an unsolicited document solve must
                    // not leave a Stage Out looking like a stalled request.
                    if (string.IsNullOrEmpty(slot.Rid)) continue;
                    if (slot.SolveInFlight)
                    {
                        if ((now - slot.SolveStartedAt).TotalSeconds < SolveWatchdogSeconds) continue;
                        // The solve never reached SolutionEnd.
                        slot.EndSolve();
                        watchdog = true;
                    }
                    if ((now - slot.RequestedAt).TotalMilliseconds < 250) continue;
                    if (slot.ResolveAttempts >= 8) continue;
                    slot.ResolveAttempts++;
                    slot.RequestedAt = now;
                    waiting.Add(new KeyValuePair<string, PendingRequest>(
                        pair.Key, new PendingRequest(slot.DocumentId, slot.Rid)));
                }
            }
            if (watchdog) LinkNotes.Add("a solve did not report SolutionEnd; retrying the waiting stage");
            if (waiting.Count == 0)
            {
                if (rescheduled || watchdog) SchedulePendingSolve(worker);
                return;
            }
            Interlocked.Increment(ref _retryRequests);
            foreach (KeyValuePair<string, PendingRequest> pair in waiting) RequestStage(pair.Key, pair.Value.Rid, worker);
        }

        internal static void DropKeyLocked(string key)
        {
            PendingRequests.Remove(key);
            if (_activeRequests == null || !_activeRequests.Remove(key)) return;
            foreach (StageSlot slot in StageRegistry.AllSlotsLocked())
                if (slot.Key == key) slot.EndSolve();
        }

        internal static void TakeKeyLocked(string key, List<KeyValuePair<string, string>> refusals)
        {
            PendingRequest pending;
            if (PendingRequests.TryGetValue(key, out pending))
            {
                PendingRequests.Remove(key);
                if (!string.IsNullOrEmpty(pending.Rid)) refusals.Add(new KeyValuePair<string, string>(key, pending.Rid));
            }
            if (_activeRequests != null)
            {
                PendingRequest active;
                if (_activeRequests.TryGetValue(key, out active))
                {
                    if (!string.IsNullOrEmpty(active.Rid)) refusals.Add(new KeyValuePair<string, string>(key, active.Rid));
                    _activeRequests.Remove(key);
                }
            }
        }

        internal static void RemovePendingLocked(string key)
        {
            PendingRequests.Remove(key);
        }

        internal static void RemoveDocumentLocked(Guid documentId)
        {
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

        internal static void ClearAll()
        {
            lock (LinkSync.State)
            {
                PendingRequests.Clear();
                _activeRequests = null;
                _activeDocumentId = Guid.Empty;
                _solveDocumentId = Guid.Empty;
                _solveQueued = false;
                _solveQueuedAt = DateTime.MinValue;
                foreach (StageSlot slot in StageRegistry.AllSlotsLocked()) slot.EndSolve();
                _solveHandlerDocument = null;
                _solveHandler = null;
            }
        }

        internal static void TakeSolutionHandler(
            out GH_Document document, out GH_Document.SolutionEndEventHandler handler)
        {
            lock (LinkSync.State)
            {
                document = _solveHandlerDocument;
                handler = _solveHandler;
                _solveHandlerDocument = null;
                _solveHandler = null;
            }
        }
    }
}
