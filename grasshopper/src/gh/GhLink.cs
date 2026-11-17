// Stageloom link facade for components, live scripts and the console suites.
// StageRegistry owns claims/slots, LinkConnection the socket/outbox,
// RequestScheduler the one in-flight solve, ProtocolHandler inbound frames,
// Publication hello/schema/units, and StageSlot/HeldFrame the per-stage
// snapshot. This type forwards the stable names and coordinates the few
// lifecycle transitions that span owners. It owns only the note buffer.

using System;
using System.Collections.Generic;
using Grasshopper.Kernel;

namespace Stageloom
{
    /// <summary>The link's bounded diagnostic log; components drain it.</summary>
    internal static class LinkNotes
    {
        private static readonly List<string> Notes = new List<string>();
        internal static void Add(string text)
        {
            lock (LinkSync.State)
            {
                Notes.Add("[Stageloom] " + text);
                if (Notes.Count > 50) Notes.RemoveAt(0);
            }
        }
        internal static string[] Take()
        {
            lock (LinkSync.State)
            {
                if (Notes.Count == 0) return new string[0];
                string[] lines = Notes.ToArray();
                Notes.Clear();
                return lines;
            }
        }
    }

    public static class GhLink
    {
        internal const string Blocks2Capability = ProtocolHandler.Blocks2Capability;

        public static string Endpoint { get { return LinkConnection.Endpoint; } }
        public static int Port { get { return LinkConnection.Port; } }
        public static bool Connected { get { return LinkConnection.Connected; } }
        public static bool Running { get { return LinkConnection.Running; } }
        internal static bool IsUiThread { get { return StageRegistry.IsUiThread; } }
        internal static int UiThreadId { get { return StageRegistry.UiThreadId; } }
        internal static int ComponentCount { get { return StageRegistry.ComponentCount; } }
        internal static int ClaimCount { get { return StageRegistry.ClaimCount; } }
        internal static int DocumentCount { get { return StageRegistry.DocumentCount; } }
        internal static int ConflictCount { get { return StageRegistry.ConflictCount; } }
        internal static int ActiveSenders { get { return LinkConnection.ActiveSenders; } }
        internal static int RetryRequests { get { return RequestScheduler.RetryRequests; } }
        internal static int StageParamCount(string key) { return StageRegistry.StageParamCount(key); }

        public static void Register(GH_Component component)
        {
            StageRegistry.Register(component);
            LinkConnection.Start();
        }

        public static void Register(GH_Component component, GH_Document document)
        {
            // A re-registration is idempotent and keeps the previous snapshot,
            // exactly as the single-class version did.
            if (document != null && !StageRegistry.IsRegistered(component)) Publication.CaptureUnits(document);
            StageRegistry.Register(component, document);
            LinkConnection.Start();
        }

        internal static void RegisterInDocumentForTest(GH_Component component, Guid documentId)
        {
            StageRegistry.RegisterInDocumentForTest(component, documentId);
            LinkConnection.Start();
        }

        public static void Unregister(GH_Component component)
        {
            bool last = StageRegistry.Unregister(component);
            // Keys another live component still claims stay; removing a
            // duplicate claimant clears the conflict for the survivor.
            StageRegistry.Reconcile();
            Publication.Send();
            if (!last) return;
            // No component can serve anything: stop the worker rather than
            // retrying a connection nothing will answer on.
            LinkConnection.Stop();
            StageRegistry.ClearAll();
            Publication.Clear();
            RequestScheduler.ClearAll();
        }

        public static void PublishStageIn(GH_Component owner, string key, bool enabled)
        {
            StageRegistry.PublishStageIn(owner, key, enabled);
        }

        public static void PublishStageOut(GH_Component owner, string key, bool enabled)
        {
            StageRegistry.PublishStageOut(owner, key, enabled);
        }

        public static void PublishParam(GH_Component owner, string key,
                                        Dictionary<string, object> declaration)
        {
            StageRegistry.PublishParam(owner, key, declaration);
        }

        /// <summary>A cleared key leaves hello/schema now, not at the next solve.</summary>
        public static void Forget(GH_Component owner)
        {
            if (StageRegistry.Forget(owner)) Publication.Send();
        }

        public static void Note(string text) { LinkNotes.Add(text); }
        public static string[] TakeNotes() { return LinkNotes.Take(); }
        public static StageSlot Slot(GH_Component owner, string key)
        {
            return StageRegistry.Slot(owner, key);
        }
        public static StageSlot Slot(string key) { return StageRegistry.Slot(key); }
        public static string AnswerRid(StageSlot slot) { return StageSlot.AnswerRid(slot); }
        public static bool ShouldAnswer(string key) { return RequestScheduler.ShouldAnswer(key); }
        public static bool IsRequestDriven(string key)
        {
            return RequestScheduler.IsRequestDriven(key);
        }
        public static StageSlot FindSlot(string key) { return StageRegistry.FindSlot(key); }
        internal static StageSlot FindSlot(Guid documentId, string key)
        {
            return StageRegistry.FindSlot(documentId, key);
        }
        internal static string StageConflict(string key) { return StageRegistry.StageConflict(key); }
        internal static bool IsStageConflicted(string key)
        {
            return StageRegistry.IsStageConflicted(key);
        }
        internal static bool HasFrame(HeldFrame frame) { return StageSlot.HasFrame(frame); }
        public static void SetOutStatus(StageSlot slot, string status, string message)
        {
            StageSlot.SetOutStatus(slot, status, message);
        }
        public static void SetInStatus(StageSlot slot, HeldFrame frame, int statusEpoch,
                                       string status, string message)
        {
            StageRegistry.SetInStatus(slot, frame, statusEpoch, status, message);
        }
        internal static bool StageAnswers(string key) { return StageRegistry.StageAnswers(key); }
        internal static HeldFrame ReadFrame(StageSlot slot, out int statusEpoch)
        {
            return StageSlot.ReadFrame(slot, out statusEpoch);
        }
        internal static bool PeerSupports(string capability)
        {
            return ProtocolHandler.PeerSupports(capability);
        }

        public static void Start() { LinkConnection.Start(); }
        public static void Stop() { LinkConnection.Stop(); }
        public static bool Configure(string host, int port, out string error)
        {
            return LinkConnection.Configure(host, port, out error);
        }
        internal static bool Configure(string host, int port, bool persist, out string error)
        {
            return LinkConnection.Configure(host, port, persist, out error);
        }
        internal static void Configure(string host, int port, bool persist)
        {
            LinkConnection.Configure(host, port, persist);
        }
        internal static void RequestStage(string key, string rid)
        {
            RequestScheduler.RequestStage(key, rid);
        }
        internal static bool BeginRequestSolveForTest()
        {
            return RequestScheduler.BeginRequestSolveForTest();
        }
        internal static bool BeginRequestSolveForTest(out Guid documentId)
        {
            return RequestScheduler.BeginRequestSolveForTest(out documentId);
        }
        internal static void EndRequestSolveForTest()
        {
            RequestScheduler.EndRequestSolveForTest();
        }
        internal static void RetryStaleSolves() { RequestScheduler.RetryStaleSolves(); }
        public static void Enqueue(byte[] frame) { LinkOutbox.Enqueue(frame); }
        public static void Publish(GH_Document doc) { Publication.Publish(doc); }
        internal static void SetUnitsSnapshotForTest(string units, double scale)
        {
            Publication.SetUnitsSnapshotForTest(units, scale);
        }
        public static double DocumentScale() { return Publication.DocumentScale(); }
        public static double DocumentScale(GH_Document document)
        {
            return Publication.DocumentScale(document);
        }
    }
}
