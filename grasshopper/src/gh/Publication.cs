// Publication: hello/schema dirty flags and the UI-thread document snapshot.
// Hello is published once per connection and again when the stage set, a role
// half or the units change; a schema only when its canonical declaration
// changes. Only the cached snapshot is read here, never RhinoDoc from a socket.

using System;
using System.Collections.Generic;
using System.Threading;
using Grasshopper.Kernel;
using Rhino;
using Stageloom.Core;

namespace Stageloom
{
    internal static class Publication
    {
        // Hello is published once per connection and republished when the stage
        // set, a role half or the units change; a schema only when its canonical
        // declaration changes. Nothing here reads RhinoDoc from a socket thread.
        private static bool _publishedThisConnection = false;
        private static volatile string _lastDocName = "(unsaved)";
        private static long _lastPublishTicks = DateTime.MinValue.Ticks;

        private static Dictionary<string, bool> _lastStages = new Dictionary<string, bool>();
        private static HashSet<string> _lastStageIns = new HashSet<string>();
        private static HashSet<string> _lastStageOuts = new HashSet<string>();
        private static string _lastUnits = "";
        private static readonly Dictionary<string, string> _lastSchema =
            new Dictionary<string, string>();

        private static string _cachedUnits = "Meters";
        private static double _cachedUnitScale = 1.0;

        // A publication is rebuilt only when the owner says something changed.
        // RebuildPublicationLocked compares the derived view and bumps this
        // generation; a heartbeat with no change returns before snapshotting or
        // computing a schema signature. The generation (not a bool) keeps a
        // mark made while a publication is in flight from being cleared by it.
        private static long _dirtyGeneration = 1;       // under State
        private static long _publishedGeneration = 0;   // under State

        /// <summary>Record that the published view may have changed. Callers
        /// hold State.</summary>
        internal static void MarkDirtyLocked()
        {
            _dirtyGeneration++;
        }

        private static DateTime LastPublishAt()
        {
            return new DateTime(Interlocked.Read(ref _lastPublishTicks), DateTimeKind.Utc);
        }

        private static void MarkPublished()
        {
            Interlocked.Exchange(ref _lastPublishTicks, DateTime.UtcNow.Ticks);
        }

        internal static void Publish(GH_Document doc)
        {
            StageRegistry.AssertUiThread("GhLink.Publish");
            if (doc != null) CaptureDocumentSnapshot(doc);
            SendPublication();
        }

        internal static void Send()
        {
            SendPublication();
        }

        internal static void PublishIfCurrent(LinkWorker worker)
        {
            lock (LinkSync.State)
            {
                if (!LinkWorker.IsCurrentLocked(worker)) return;
                SendPublication();
            }
        }

        internal static void HeartbeatIfDue(LinkWorker worker)
        {
            bool due;
            lock (LinkSync.State)
            {
                due = LinkWorker.IsCurrentLocked(worker) && LinkConnection.Connected
                      && (DateTime.UtcNow - LastPublishAt()).TotalSeconds >= 2.0;
            }
            if (due) PublishIfCurrent(worker);
        }

        internal static void ConnectionOpenedLocked()
        {
            _publishedThisConnection = false;
            _lastSchema.Clear();
            MarkDirtyLocked();
            _publishedGeneration = -1;
        }

        internal static void ResetConnectionLocked()
        {
            _lastSchema.Clear();
            MarkDirtyLocked();
        }

        internal static void Clear()
        {
            lock (LinkSync.State)
            {
                _lastStages = new Dictionary<string, bool>();
                _lastStageIns = new HashSet<string>();
                _lastStageOuts = new HashSet<string>();
                _lastSchema.Clear();
                _publishedThisConnection = false;
                _cachedUnits = "Meters";
                _cachedUnitScale = 1.0;
                _lastUnits = "";
                MarkDirtyLocked();
                _publishedGeneration = -1;
            }
        }

        private static void SendPublication()
        {
            MarkPublished();
            if (!LinkConnection.Connected) return;
            long generation;
            lock (LinkSync.State)
            {
                generation = _dirtyGeneration;
                // Nothing changed since the last successful publication and a
                // hello has already gone out on this connection: a heartbeat
                // must not snapshot every stage or re-encode every schema.
                if (_publishedThisConnection && generation == _publishedGeneration)
                    return;
            }
            Dictionary<string, bool> stages;
            HashSet<string> stageIns;
            HashSet<string> stageOuts;
            Dictionary<string, List<Dictionary<string, object>>> paramsByStage;
            string units;
            lock (LinkSync.State)
            {
                StageRegistry.SnapshotPublishedLocked(
                    out stages, out stageIns, out stageOuts, out paramsByStage);
                units = _cachedUnits;
            }

            // Reserve the once-per-connection hello under State: two threads
            // reading the flag independently both sent one.
            bool needHello;
            lock (LinkSync.State)
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
                foreach (KeyValuePair<string, bool> kv in stages)
                {
                    var entry = new Dictionary<string, object>();
                    entry["key"] = kv.Key;
                    entry["enabled"] = kv.Value;
                    // Optional additive fields (protocol §4.1): true when a
                    // Stage In / Stage Out claims the key.
                    entry["has_stage_in"] = stageIns.Contains(kv.Key);
                    entry["has_stage_out"] = stageOuts.Contains(kv.Key);
                    list.Add(entry);
                }
                var hello = new Dictionary<string, object>();
                hello["type"] = "hello";
                // Keep in sync with blender_manifest.toml (the release path
                // enforces that one) and the example in docs/protocol.md.
                hello["client"] = "Stageloom 0.8.1";
                hello["doc"] = _lastDocName;
                hello["doc_units"] = units;
                hello["stages"] = list;
                // Optional additive field (protocol §4.1): this client reads
                // multi-part definition entries. Older Blender ignores it.
                var caps = new List<object>();
                caps.Add(ProtocolHandler.Blocks2Capability);
                hello["caps"] = caps;
                LinkOutbox.Enqueue(FrameCodec.Encode(hello, null));
                lock (LinkSync.State)
                {
                    _lastStages = new Dictionary<string, bool>(stages);
                    _lastStageIns = new HashSet<string>(stageIns);
                    _lastStageOuts = new HashSet<string>(stageOuts);
                }
            }

            lock (LinkSync.State)
            {
                // A stage removed from the document must not leave a signature
                // behind that could suppress its schema after it comes back.
                foreach (string key in new List<string>(_lastSchema.Keys))
                    if (!paramsByStage.ContainsKey(key)) _lastSchema.Remove(key);
            }

            foreach (KeyValuePair<string, List<Dictionary<string, object>>> kv in paramsByStage)
            {
                // Publish a schema only when its canonical content changes; the
                // dirty generation has already forced this publication.
                string signature = Json.WriteCanonical(kv.Value);
                bool changed;
                lock (LinkSync.State)
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
                foreach (Dictionary<string, object> p in kv.Value) list.Add(p);
                schema["params"] = list;
                LinkOutbox.Enqueue(FrameCodec.Encode(schema, null));
            }

            lock (LinkSync.State)
            {
                // A change marked while this publication was in flight stays
                // pending for the next heartbeat; do not clear it.
                if (_dirtyGeneration == generation) _publishedGeneration = generation;
            }
        }

        private static bool StagesChangedLocked(Dictionary<string, bool> stages,
                                                HashSet<string> stageIns,
                                                HashSet<string> stageOuts)
        {
            if (_lastStages.Count != stages.Count) return true;
            foreach (KeyValuePair<string, bool> kv in stages)
            {
                bool other;
                if (!_lastStages.TryGetValue(kv.Key, out other) || other != kv.Value)
                    return true;
                if (_lastStageIns.Contains(kv.Key) != stageIns.Contains(kv.Key)) return true;
                if (_lastStageOuts.Contains(kv.Key) != stageOuts.Contains(kv.Key)) return true;
            }
            return false;
        }

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

        internal static void CaptureUnits(GH_Document document)
        {
            if (document == null) return;
            string units;
            double scale;
            ReadDocumentUnits(document, out units, out scale);
            lock (LinkSync.State)
            {
                if (_cachedUnits != units || _cachedUnitScale != scale) MarkDirtyLocked();
                _cachedUnits = units;
                _cachedUnitScale = scale;
            }
        }

        private static void CaptureDocumentSnapshot(GH_Document document)
        {
            if (document == null) return;
            StageRegistry.AssertUiThread("GhLink.CaptureDocumentSnapshot");
            string units;
            double scale;
            ReadDocumentUnits(document, out units, out scale);
            string name = null;
            try
            {
                name = document.FilePath;
            }
            catch (Exception) { }
            lock (LinkSync.State)
            {
                if (_cachedUnits != units || _cachedUnitScale != scale
                    || (!string.IsNullOrEmpty(name) && name != _lastDocName))
                    MarkDirtyLocked();
                _cachedUnits = units;
                _cachedUnitScale = scale;
                if (!string.IsNullOrEmpty(name)) _lastDocName = name;
            }
        }

        internal static void SetUnitsSnapshotForTest(string units, double scale)
        {
            lock (LinkSync.State)
            {
                string next = string.IsNullOrEmpty(units) ? "Meters" : units;
                if (_cachedUnits != next || _cachedUnitScale != scale) MarkDirtyLocked();
                _cachedUnits = next;
                _cachedUnitScale = scale;
            }
        }

        internal static double DocumentScale()
        {
            lock (LinkSync.State) { return _cachedUnitScale; }
        }

        internal static double DocumentScale(GH_Document document)
        {
            if (document != null) CaptureDocumentSnapshot(document);
            return DocumentScale();
        }
    }
}
