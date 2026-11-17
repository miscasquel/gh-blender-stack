// Document-scoped registrations, claims and slots: one key has one owning
// document, a key claimed twice is a strict conflict, and a removed key is a
// tombstone. RebuildPublicationLocked is the only writer of the published view.

using System;
using System.Collections.Generic;
using System.Threading;
using Grasshopper.Kernel;
using Rhino;
using Stageloom.Core;

namespace Stageloom
{
    internal static class StageRegistry
    {
        private static readonly Dictionary<Guid, Dictionary<string, StageSlot>> Slots =
            new Dictionary<Guid, Dictionary<string, StageSlot>>();
        private static readonly List<Registration> Components = new List<Registration>();
        private static readonly Dictionary<Guid, DocumentEntry> Documents =
            new Dictionary<Guid, DocumentEntry>();

        private sealed class Registration
        {
            public GH_Component Owner;
            public Guid DocumentId;
        }

        private sealed class DocumentEntry
        {
            public string Name;
            public int ComponentCount;
            public Action<GH_Document.GH_ScheduleDelegate> Schedule;
        }

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
            public bool Enabled;
            public long Sequence;
        }

        private static readonly List<Claim> Claims = new List<Claim>();
        private static long _claimSequence = 0;  // under State

        private static Dictionary<string, bool> StageEnabled = new Dictionary<string, bool>();
        private static HashSet<string> StageHasIn = new HashSet<string>();
        private static HashSet<string> StageHasOut = new HashSet<string>();
        private static HashSet<string> StageOutEnabled = new HashSet<string>();
        private static Dictionary<string, List<Dictionary<string, object>>> StageParams =
            new Dictionary<string, List<Dictionary<string, object>>>();
        private static HashSet<string> RemovedKeys = new HashSet<string>();
        private static Dictionary<string, Guid> KeyOwners = new Dictionary<string, Guid>();
        private static Dictionary<string, string> Conflicts = new Dictionary<string, string>();

        private static int _uiThreadId;

        internal static bool IsUiThread
        {
            get
            {
                int id = _uiThreadId;
                return id == 0 || id == Thread.CurrentThread.ManagedThreadId;
            }
        }

        internal static int UiThreadId { get { return _uiThreadId; } }

        internal static void CaptureUiThread()
        {
            if (_uiThreadId == 0)
                System.Threading.Interlocked.CompareExchange(
                    ref _uiThreadId, Thread.CurrentThread.ManagedThreadId, 0);
        }

        internal static void AssertUiThread(string what)
        {
            System.Diagnostics.Debug.Assert(IsUiThread, what + " must run on the GH UI thread");
        }

        internal static int ComponentCount
        {
            get { lock (LinkSync.State) { return Components.Count; } }
        }

        internal static int ClaimCount
        {
            get { lock (LinkSync.State) { return Claims.Count; } }
        }

        internal static int DocumentCount
        {
            get { lock (LinkSync.State) { return Documents.Count; } }
        }

        internal static int ConflictCount
        {
            get { lock (LinkSync.State) { return Conflicts.Count; } }
        }

        internal static int StageParamCount(string key)
        {
            lock (LinkSync.State)
            {
                List<Dictionary<string, object>> list;
                return StageParams.TryGetValue(key ?? "", out list) ? list.Count : 0;
            }
        }

        // ------------------------------------------------------------- registry

        public static void Register(GH_Component component)
        {
            RegisterCore(component, Guid.Empty, "", null);
        }

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

        internal static void RegisterInDocumentForTest(GH_Component component, Guid documentId)
        {
            CaptureUiThread();
            RegisterCore(component, documentId, "document " + ShortId(documentId), null);
        }

        internal static bool IsRegistered(GH_Component component)
        {
            lock (LinkSync.State) { return FindRegistrationLocked(component) != null; }
        }

        private static void RegisterCore(GH_Component component, Guid documentId,
                                         string documentName, GH_Document reference)
        {
            CaptureUiThread();
            lock (LinkSync.State)
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
        }

        internal static bool Unregister(GH_Component component)
        {
            bool last;
            lock (LinkSync.State)
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
            return last;
        }

        public static void PublishStageIn(GH_Component owner, string key, bool enabled)
        {
            PublishStageClaim(owner, key, enabled, ClaimRole.Input);
        }

        public static void PublishStageOut(GH_Component owner, string key, bool enabled)
        {
            PublishStageClaim(owner, key, enabled, ClaimRole.Output);
        }

        private static void PublishStageClaim(GH_Component owner, string key, bool enabled, ClaimRole role)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (LinkSync.State)
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
                RebuildPublicationLocked();
            }
        }

        public static void PublishParam(GH_Component owner, string key, Dictionary<string, object> declaration)
        {
            if (string.IsNullOrEmpty(key)) return;
            lock (LinkSync.State)
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

        internal static bool Forget(GH_Component owner)
        {
            lock (LinkSync.State)
            {
                foreach (Claim claim in Claims)
                {
                    if (!object.ReferenceEquals(claim.Owner, owner)) continue;
                    claim.StageKey = null;
                    claim.ParamKey = null;
                    claim.ParamId = null;
                    claim.Declaration = null;
                    RebuildPublicationLocked();
                    return true;
                }
            }
            return false;
        }

        public static StageSlot Slot(GH_Component owner, string key)
        {
            lock (LinkSync.State)
            {
                Guid documentId = DocumentIdLocked(owner);
                return GetOrCreateSlotLocked(documentId, key ?? "");
            }
        }

        public static StageSlot Slot(string key)
        {
            lock (LinkSync.State)
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

        public static StageSlot FindSlot(string key)
        {
            lock (LinkSync.State)
            {
                string name = key ?? "";
                if (Conflicts.ContainsKey(name)) return null;
                Guid owner;
                if (KeyOwners.TryGetValue(name, out owner))
                    return FindSlotLocked(owner, name);
                return FindAnySlotLocked(name);
            }
        }

        internal static StageSlot FindSlot(Guid documentId, string key)
        {
            lock (LinkSync.State) { return FindSlotLocked(documentId, key ?? ""); }
        }

        internal static StageSlot FindSlotLocked(Guid documentId, string key)
        {
            Dictionary<string, StageSlot> documentSlots;
            if (!Slots.TryGetValue(documentId, out documentSlots)) return null;
            StageSlot slot;
            return documentSlots.TryGetValue(key ?? "", out slot) ? slot : null;
        }

        internal static bool TryGetOwnerLocked(string key, out Guid owner)
        {
            return KeyOwners.TryGetValue(key ?? "", out owner);
        }

        internal static bool HasOwnerLocked(string key)
        {
            return KeyOwners.ContainsKey(key ?? "");
        }

        internal static bool TryGetConflictLocked(string key, out string message)
        {
            return Conflicts.TryGetValue(key ?? "", out message);
        }

        internal static bool IsRemovedLocked(string key)
        {
            return RemovedKeys.Contains(key ?? "");
        }

        internal static StageSlot GetOrCreateSlotLocked(Guid documentId, string key)
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

        internal static StageSlot FindAnySlotLocked(string key)
        {
            foreach (Dictionary<string, StageSlot> documentSlots in Slots.Values)
            {
                StageSlot slot;
                if (documentSlots.TryGetValue(key ?? "", out slot)) return slot;
            }
            return null;
        }

        internal static List<StageSlot> AllSlotsLocked()
        {
            var slots = new List<StageSlot>();
            foreach (Dictionary<string, StageSlot> documentSlots in Slots.Values)
                foreach (StageSlot slot in documentSlots.Values) slots.Add(slot);
            return slots;
        }

        internal static List<KeyValuePair<string, StageSlot>> AllSlotsWithKeysLocked()
        {
            var slots = new List<KeyValuePair<string, StageSlot>>();
            foreach (Dictionary<string, StageSlot> documentSlots in Slots.Values)
                foreach (KeyValuePair<string, StageSlot> pair in documentSlots) slots.Add(pair);
            return slots;
        }

        internal static void MarkAllSentLocked(string rid, string outHash)
        {
            foreach (StageSlot slot in AllSlotsLocked()) slot.MarkSent(rid, outHash);
        }

        internal static string StageConflict(string key)
        {
            lock (LinkSync.State)
            {
                string message;
                return Conflicts.TryGetValue(key ?? "", out message) ? message : "";
            }
        }

        internal static bool IsStageConflicted(string key)
        {
            lock (LinkSync.State) { return Conflicts.ContainsKey(key ?? ""); }
        }

        internal static bool IsConflictedLocked(string key)
        {
            return Conflicts.ContainsKey(key ?? "");
        }

        internal static bool StageAnswersLocked(string key)
        {
            if (string.IsNullOrEmpty(key) || Conflicts.ContainsKey(key)) return false;
            return StageHasOut.Contains(key) && StageOutEnabled.Contains(key);
        }

        internal static bool StageAnswers(string key)
        {
            lock (LinkSync.State) { return StageAnswersLocked(key ?? ""); }
        }

        internal static void SetInStatus(StageSlot slot, HeldFrame frame, int statusEpoch,
                                         string status, string message)
        {
            if (slot == null) return;
            lock (LinkSync.State)
            {
                slot.ApplyInStatus(frame, statusEpoch, status, message,
                    !StageAnswersLocked(slot.Key));
            }
        }

        internal static bool HasComponentsLocked()
        {
            return Components.Count > 0;
        }

        internal static List<GH_Component> StageComponentsLocked(IEnumerable<string> keys, Guid documentId)
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

        internal static bool TryGetScheduleLocked(Guid documentId,
            out Action<GH_Document.GH_ScheduleDelegate> schedule)
        {
            DocumentEntry entry;
            if (Documents.TryGetValue(documentId, out entry) && entry.Schedule != null)
            {
                schedule = entry.Schedule;
                return true;
            }
            schedule = null;
            return false;
        }

        internal static void SnapshotPublishedLocked(
            out Dictionary<string, bool> stages,
            out HashSet<string> stageIns,
            out HashSet<string> stageOuts,
            out Dictionary<string, List<Dictionary<string, object>>> parameters)
        {
            stages = new Dictionary<string, bool>(StageEnabled);
            stageIns = new HashSet<string>(StageHasIn);
            stageOuts = new HashSet<string>(StageHasOut);
            parameters = new Dictionary<string, List<Dictionary<string, object>>>();
            foreach (KeyValuePair<string, List<Dictionary<string, object>>> pair in StageParams)
                parameters[pair.Key] = new List<Dictionary<string, object>>(pair.Value);
        }

        // ----------------------------------------------------------- reconcile

        internal static void Reconcile()
        {
            var refused = new List<KeyValuePair<string, string>>();
            lock (LinkSync.State)
            {
                // A claim belongs to a registered owner. Unregister removes
                // both under one lock; this sweep also covers a late publish
                // from a component that has already left.
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
                LinkOutbox.Enqueue(ProtocolHandler.ErrorFrame("unknown_stage",
                    "no Stageloom component claims this stage key", entry.Key, entry.Value));
        }

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
                if (documents.Count > 1) conflict = ConflictMessage(key, documents);
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
                    if (inputs > 1) conflict = RoleConflictMessage(key, owner, "more than one Stage In");
                    else if (outputs > 1) conflict = RoleConflictMessage(key, owner, "more than one Stage Out");
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
                if (!Conflicts.TryGetValue(pair.Key, out previous) || previous != pair.Value) LinkNotes.Add(pair.Value);
            }
            foreach (string key in Conflicts.Keys)
            {
                if (!conflicts.ContainsKey(key))
                    LinkNotes.Add("duplicate stage key '" + key
                        + "' resolved; the surviving claim owns it");
            }

            // A conflicted key must not answer an already queued request. The
            // active batch drops it too; ShouldAnswer refuses it, so a
            // component already running cannot emit a response.
            foreach (string key in conflicts.Keys) RequestScheduler.DropKeyLocked(key);

            StageEnabled = enabled;
            StageHasIn = hasIn;
            StageHasOut = hasOut;
            StageOutEnabled = hasEnabledOut;
            StageParams = parameters;
            KeyOwners = owners;
            Conflicts = conflicts;

            // Keys that left the published view entirely are tombstones.
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
                    RequestScheduler.TakeKeyLocked(key, refusals);
                    foreach (Guid documentId in new List<Guid>(Slots.Keys))
                    {
                        Dictionary<string, StageSlot> documentSlots = Slots[documentId];
                        StageSlot slot;
                        if (!documentSlots.TryGetValue(key, out slot)) continue;
                        slot.EndSolve();
                        documentSlots.Remove(key);
                        if (documentSlots.Count == 0) Slots.Remove(documentId);
                    }
                }
                // An answer can no longer be produced for an in-flight request.
                foreach (KeyValuePair<string, string> refusal in refusals)
                    LinkOutbox.Enqueue(ProtocolHandler.ErrorFrame("unknown_stage",
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

        private static void RemoveDocumentStateLocked(Guid documentId)
        {
            Slots.Remove(documentId);
            RequestScheduler.RemoveDocumentLocked(documentId);
        }

        internal static void ClearAll()
        {
            lock (LinkSync.State)
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
            }
        }

        private static Claim ClaimForLocked(GH_Component owner)
        {
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
    }
}
