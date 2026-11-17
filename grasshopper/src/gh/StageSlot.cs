// The per-stage frame and status halves of the link.
//
// StageSlot is the only place one stage's inbound frame, request rid and
// direction verdicts live. ProtocolHandler replaces the frame as one immutable
// snapshot; RequestScheduler pins the solve rid; Stage In/Out transition the
// halves. HeldFrame.Empty is the only "no frame".

using System;
using System.Collections.Generic;
using Stageloom.Core;

namespace Stageloom
{
    /// <summary>One complete request frame. Sections, attributes, definitions,
    /// geometry hash and rid are read together; the lists never mutate after
    /// construction, and a `ref` reuses the previous lists with a new rid.</summary>
    public sealed class HeldFrame
    {
        public readonly string Rid;
        public readonly List<Section> Sections;
        public readonly List<Dictionary<string, object>> Attributes;
        public readonly List<object> Definitions;
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
        internal Guid DocumentId = Guid.Empty;
        public string Key = "";
        public volatile string Rid = "";
        public volatile string SolveRid = "";
        private volatile Dictionary<string, object> _params = new Dictionary<string, object>();
        public Dictionary<string, object> Params
        {
            get { return _params; }
            set { _params = value; }
        }
        private volatile HeldFrame _frame = HeldFrame.Empty;
        public HeldFrame Frame
        {
            get { return _frame; }
            internal set { _frame = value; }
        }
        public volatile string Status = "waiting";
        public volatile string Message = "";
        internal volatile int StatusEpoch = 0;
        public volatile string InStatus = "waiting";
        public volatile string InMessage = "";
        public volatile string OutStatus = "waiting";
        public volatile string OutMessage = "";
        public DateTime RequestedAt = DateTime.UtcNow;
        public int ResolveAttempts = 0;
        internal bool SolveInFlight = false;
        internal DateTime SolveStartedAt = DateTime.MinValue;
        internal bool Provisional = false;
        internal volatile string LastSentRid = "";
        internal volatile string LastSentHash = "";

        internal bool ShouldSend(string rid, string outHash, bool requestDriven)
        {
            if (requestDriven) return true;
            return LastSentRid != (rid ?? "") || LastSentHash != (outHash ?? "");
        }

        internal void MarkSent(string rid, string outHash)
        {
            LastSentRid = rid ?? "";
            LastSentHash = outHash ?? "";
        }

        internal void BeginRequest(string rid, Dictionary<string, object> parameters)
        {
            Rid = rid;
            Status = "waiting";
            Message = "";
            InStatus = "waiting";
            InMessage = "";
            OutStatus = "waiting";
            OutMessage = "";
            StatusEpoch++;
            RequestedAt = DateTime.UtcNow;
            ResolveAttempts = 0;
            Params = parameters;
        }

        internal void BeginSolve(string rid, DateTime started)
        {
            SolveRid = rid;
            SolveInFlight = true;
            SolveStartedAt = started;
        }

        internal void EndSolve()
        {
            SolveRid = "";
            SolveInFlight = false;
            SolveStartedAt = DateTime.MinValue;
        }

        internal void ApplyOutStatus(string status, string message)
        {
            OutStatus = status;
            OutMessage = message ?? "";
            Status = status;
            Message = message ?? "";
        }

        internal void ApplyInStatus(HeldFrame frame, int statusEpoch,
                                    string status, string message, bool oneWay)
        {
            if (frame == null || StatusEpoch != statusEpoch
                || !object.ReferenceEquals(Frame, frame)) return;
            InStatus = status;
            InMessage = message ?? "";
            if (oneWay && !string.IsNullOrEmpty(Key))
            {
                Status = status;
                Message = message ?? "";
            }
        }

        internal void ApplyProtocolError(string text)
        {
            Status = "error";
            Message = text;
            InStatus = "error";
            InMessage = text;
            OutStatus = "error";
            OutMessage = text;
            StatusEpoch++;
        }

        internal void ApplyUnknownRef(string text)
        {
            Status = "unknown_ref";
            Message = text;
            InStatus = "unknown_ref";
            InMessage = text;
            OutStatus = "unknown_ref";
            OutMessage = text;
            StatusEpoch++;
        }

        internal static string AnswerRid(StageSlot slot)
        {
            if (slot == null) return "";
            return string.IsNullOrEmpty(slot.SolveRid) ? slot.Rid : slot.SolveRid;
        }

        internal static bool HasFrame(HeldFrame frame)
        {
            return frame != null && !object.ReferenceEquals(frame, HeldFrame.Empty);
        }

        internal static HeldFrame ReadFrame(StageSlot slot, out int statusEpoch)
        {
            if (slot == null)
            {
                statusEpoch = 0;
                return HeldFrame.Empty;
            }
            lock (LinkSync.State)
            {
                statusEpoch = slot.StatusEpoch;
                return slot.Frame;
            }
        }

        internal static void SetOutStatus(StageSlot slot, string status, string message)
        {
            lock (LinkSync.State) { slot.ApplyOutStatus(status, message); }
        }
    }
}
