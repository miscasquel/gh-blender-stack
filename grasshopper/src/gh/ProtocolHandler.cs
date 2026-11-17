// Inbound protocol: frame decode, request/error dispatch and capability state.
// Bad frames never tear down a healthy connection: bad_frame is a local note,
// proto_mismatch/too_large are answered with an error frame, and a full request
// replaces the frame atomically while a ref reuses it with a new rid.

using System;
using System.Collections.Generic;
using Stageloom.Core;

namespace Stageloom
{
    internal static class ProtocolHandler
    {
        // The capability this client advertises in hello (protocol §4.1, §8).
        internal const string Blocks2Capability = "blocks/2";

        private static HashSet<string> _peerCaps = new HashSet<string>();

        internal static bool PeerSupports(string capability)
        {
            lock (LinkSync.State) { return _peerCaps.Contains(capability); }
        }

        internal static void ResetPeerCapsLocked()
        {
            _peerCaps = new HashSet<string>();
        }

        internal static void HandleFrame(byte[] frame, LinkWorker worker)
        {
            DecodedFrame decoded;
            try
            {
                decoded = FrameCodec.Decode(frame);
            }
            catch (FrameException ex)
            {
                LinkNotes.Add("bad frame (" + ex.Code + "): " + ex.Message);
                // The two failures the peer can act on are answered before the
                // frame is dropped. bad_frame stays a local note.
                if (ex.Code == "proto_mismatch" || ex.Code == "too_large") LinkOutbox.Enqueue(ErrorFrame(ex.Code, ex.Message, null, null));
                return;
            }
            catch (JsonException ex)
            {
                LinkNotes.Add("bad frame json: " + ex.Message);
                return;
            }
            catch (Exception ex)
            {
                // Anything reaching here is a decoder gap; note and keep reading.
                LinkNotes.Add("bad frame (" + ex.GetType().Name + "): "
                    + ErrorText.FirstLine(ex));
                return;
            }
            Handle(decoded, worker);
        }

        private static void Handle(DecodedFrame decoded, LinkWorker worker)
        {
            Dictionary<string, object> header = decoded.Header;
            string type = Json.GetString(header, "type");

            if (type == "error")
            {
                // A protocol error from Blender (protocol §4.6). Surface it on
                // the stage, not only in the log.
                string stageKey = Json.GetString(header, "stage");
                string problem = Json.GetString(header, "code");
                string detail = Json.GetString(header, "message");
                StageSlot target = string.IsNullOrEmpty(stageKey)
                    ? null
                    : StageRegistry.FindSlot(stageKey);
                if (target != null)
                {
                    lock (LinkSync.State) { target.ApplyProtocolError(problem + ": " + detail); }
                }
                LinkNotes.Add("protocol error " + problem + ": " + detail);
                return;
            }

            if (type != "request")
            {
                LinkNotes.Add("ignoring message type " + type);
                return;
            }

            HashSet<string> peerCaps = ReadCaps(header);
            lock (LinkSync.State) { _peerCaps = peerCaps; }

            string key = Json.GetString(header, "stage");
            string rid = Json.GetString(header, "rid");
            if (string.IsNullOrEmpty(key))
            {
                LinkNotes.Add("request without a stage key");
                return;
            }
            var geom = Json.Get(header, "geom") as Dictionary<string, object>;
            string mode = geom == null ? null : Json.GetString(geom, "mode");
            string hash = geom == null ? null : Json.GetString(geom, "hash");

            StageSlot slot = null;
            bool noComponents = false;
            bool removedKey = false;
            string conflict = "";
            lock (LinkSync.State)
            {
                // A key two documents claim has no proven owner. Refuse it
                // before a slot is touched.
                if (StageRegistry.TryGetConflictLocked(key, out conflict))
                {
                }
                else if (!StageRegistry.HasOwnerLocked(key)
                         && StageRegistry.IsRemovedLocked(key))
                {
                    removedKey = true;
                }
                else
                {
                    Guid ownerId = Guid.Empty;
                    bool owned = StageRegistry.TryGetOwnerLocked(key, out ownerId);
                    slot = StageRegistry.FindSlotLocked(ownerId, key);
                    if (slot == null)
                    {
                        // No component means no key can ever be claimed; do not
                        // grow the registry for a request nothing can answer.
                        if (!StageRegistry.HasComponentsLocked()) { noComponents = true; }
                        else
                        {
                            slot = StageRegistry.GetOrCreateSlotLocked(ownerId, key);
                            slot.Provisional = !owned;
                        }
                    }
                    else if (!owned) { slot.Provisional = true; }
                }
            }
            if (!string.IsNullOrEmpty(conflict))
            {
                LinkNotes.Add(conflict);
                LinkOutbox.Enqueue(ErrorFrame("duplicate_stage", conflict, key, rid));
                return;
            }
            if (removedKey)
            {
                const string message = "no Stageloom component claims this stage key";
                LinkNotes.Add("request for removed stage " + key + " refused");
                LinkOutbox.Enqueue(ErrorFrame("unknown_stage", message, key, rid));
                return;
            }
            if (noComponents)
            {
                LinkNotes.Add("request for stage " + key
                    + ", but no Stageloom component is on the canvas");
                return;
            }

            bool canServe;
            lock (LinkSync.State)
            {
                var paramsObj = Json.Get(header, "params") as Dictionary<string, object>;
                var incoming = paramsObj == null
                    ? new Dictionary<string, object>()
                    : new Dictionary<string, object>(paramsObj);
                slot.BeginRequest(rid, incoming);

                HeldFrame current = slot.Frame;
                // A ref is servable for any frame we hold, including a full
                // request with zero sections. Only Empty means "no frame".
                canServe = StageSlot.HasFrame(current)
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
                // Say so (protocol §4.3) so Blender resends in full. Silently
                // dropping it leaves Blender waiting out its timeout, and its
                // retry is another ref.
                lock (LinkSync.State)
                {
                    const string message =
                        "Not holding this geometry; asking Blender to resend it";
                    slot.ApplyUnknownRef(message);
                    // The refused rid cannot be answered: a solve queued for
                    // it must not answer with the geometry we still hold.
                    RequestScheduler.RemovePendingLocked(key);
                }
                LinkOutbox.Enqueue(ErrorFrame("unknown_ref",
                    "no geometry for hash " + (hash ?? "(none)"), key, rid));
                return;
            }

            RequestScheduler.RequestStage(key, rid, worker);
        }

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

        private static List<Dictionary<string, object>> AttributeList(Dictionary<string, object> header)
        {
            return HeaderList(header, "attributes");
        }

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

        private static List<object> HeaderObjects(
            Dictionary<string, object> header, string key)
        {
            var raw = Json.Get(header, key) as List<object>;
            return raw == null ? new List<object>() : new List<object>(raw);
        }

        internal static byte[] ErrorFrame(string code, string message, string stage, string rid)
        {
            var header = new Dictionary<string, object>();
            header["type"] = "error";
            header["code"] = code;
            header["message"] = message;
            if (!string.IsNullOrEmpty(stage)) header["stage"] = stage;
            if (!string.IsNullOrEmpty(rid)) header["rid"] = rid;
            return FrameCodec.Encode(header, null);
        }
    }
}
