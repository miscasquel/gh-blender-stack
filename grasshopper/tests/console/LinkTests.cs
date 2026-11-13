// Layer C, link half: one round trip against the real GhLink.
//
//     StageloomLinkTests.exe
//
// Drives the product GhLink over a scripted fake WebSocket server: hello and
// schema content (including has_stage_in), slot bookkeeping, the unknown_ref
// refusal, the error path, a same-rid re-send, the queued request context and
// retry path, and the frames the receive path answers for a version mismatch
// and an oversized frame. GhLink only needs Grasshopper types for its solve
// trigger; a component that is not on a GH_Document makes OnPingDocument()
// return null, so no GH solve machinery (and no Rhino) is exercised here. The
// pending-to-active handoff is driven through the internal test hooks, which
// run the product context code without faking a document solve. The
// connection, decoder, slots, notes and outbox are all the product's.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using System.Text;
using System.Threading;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Types;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom.Tests.Console
{
    public static class LinkTests
    {
        private static int _failures;
        private static readonly List<string> Notes = new List<string>();

        public static int Main(string[] args)
        {
            System.Console.WriteLine("link round-trip");
            var server = new FakeServer();
            try
            {
                GhLink.Endpoint = "ws://127.0.0.1:" + server.Port + "/";
                GhLink.Start();
                server.Accept();
                Check("connect accepted", true, "");

                // 1. hello on connect: no components, so no stages; the client
                //    string is the one release version bump to keep in sync.
                Dictionary<string, object> hello = ReadUntil(server, "hello");
                Check("hello client", (string)Json.Get(hello, "client") == "Stageloom 0.5.0",
                    "client=" + Json.Get(hello, "client"));
                Check("hello doc", (string)Json.Get(hello, "doc") == "(unsaved)",
                    "doc=" + Json.Get(hello, "doc"));
                Check("hello units", !string.IsNullOrEmpty((string)Json.Get(hello, "doc_units")),
                    "doc_units=" + Json.Get(hello, "doc_units"));
                Check("hello stages empty", ((List<object>)Json.Get(hello, "stages")).Count == 0, "");

                // 2. a request before any component: no slot, a note, no growth.
                server.Send(Frame(Header("request", "k0", "r0"), null));
                DateTime noteDeadline = DateTime.UtcNow.AddSeconds(2);
                while (DateTime.UtcNow < noteDeadline && !HasNote("no Stageloom component"))
                {
                    CollectNotes();
                    Thread.Sleep(20);
                }
                Check("foreign request makes no slot", GhLink.FindSlot("k0") == null, "slot exists");

                // 3. components register and publish; hello follows.
                var stage = new TestComponent();
                GhLink.Register(stage);
                // PublishStage is the compatibility alias; it claims a Stage
                // Out, so this key has no Stage In yet.
                GhLink.PublishStage(stage, "k1", true);
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                var stages = (List<object>)Json.Get(hello, "stages");
                Dictionary<string, object> stageRow = FindStage(hello, "k1");
                Check("hello carries the published stage",
                    stageRow != null && (bool)Json.Get(stageRow, "enabled"),
                    "stages=" + Json.WriteCanonical(stages));
                object hasStageIn = stageRow == null ? null : Json.Get(stageRow, "has_stage_in");
                object hasStageOut = stageRow == null ? null : Json.Get(stageRow, "has_stage_out");
                Check("a stage out alone has no stage in",
                    hasStageIn is bool && !(bool)hasStageIn,
                    "row=" + (stageRow == null ? "(none)" : Json.WriteCanonical(stageRow)));
                Check("a stage out alone has a stage out",
                    hasStageOut is bool && (bool)hasStageOut,
                    "row=" + (stageRow == null ? "(none)" : Json.WriteCanonical(stageRow)));

                // A Stage In on the same key flips has_stage_in. The stage set
                // does not change, so this is the republish-on-presence case.
                var stageIn = new TestComponent();
                GhLink.Register(stageIn);
                GhLink.PublishStageIn(stageIn, "k1", true);
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                stageRow = FindStage(hello, "k1");
                hasStageIn = stageRow == null ? null : Json.Get(stageRow, "has_stage_in");
                hasStageOut = stageRow == null ? null : Json.Get(stageRow, "has_stage_out");
                Check("a stage in republishes hello",
                    hasStageIn is bool && (bool)hasStageIn
                    && hasStageOut is bool && (bool)hasStageOut,
                    "row=" + (stageRow == null ? "(none)" : Json.WriteCanonical(stageRow)));

                // Removing it returns the key to generator status.
                GhLink.Unregister(stageIn);
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                stageRow = FindStage(hello, "k1");
                hasStageIn = stageRow == null ? null : Json.Get(stageRow, "has_stage_in");
                Check("removing a stage in republishes hello",
                    hasStageIn is bool && !(bool)hasStageIn,
                    "row=" + (stageRow == null ? "(none)" : Json.WriteCanonical(stageRow)));

                // A different key with a Stage In reports true in the same
                // hello that still reports k1 false.
                var stageInOnly = new TestComponent();
                GhLink.Register(stageInOnly);
                GhLink.PublishStageIn(stageInOnly, "k1-in", true);
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                Dictionary<string, object> inRow = FindStage(hello, "k1-in");
                stageRow = FindStage(hello, "k1");
                Check("hello reports has_stage_in per key",
                    inRow != null && (bool)Json.Get(inRow, "has_stage_in")
                    && stageRow != null && !(bool)Json.Get(stageRow, "has_stage_in"),
                    "stages=" + Json.WriteCanonical(Json.Get(hello, "stages")));
                Check("a stage in alone is one-way",
                    inRow != null && Json.Get(inRow, "has_stage_out") is bool
                    && !(bool)Json.Get(inRow, "has_stage_out"),
                    "row=" + (inRow == null ? "(none)" : Json.WriteCanonical(inRow)));

                // Adding a Stage Out for the same key flips it back to a
                // two-way stage and republishes hello on presence alone.
                var stageOutOnly = new TestComponent();
                GhLink.Register(stageOutOnly);
                GhLink.PublishStageOut(stageOutOnly, "k1-in", true);
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                inRow = FindStage(hello, "k1-in");
                Check("a stage out republishes one-way status",
                    inRow != null && Json.Get(inRow, "has_stage_out") is bool
                    && (bool)Json.Get(inRow, "has_stage_out"),
                    "row=" + (inRow == null ? "(none)" : Json.WriteCanonical(inRow)));
                GhLink.Unregister(stageOutOnly);
                GhLink.Unregister(stageInOnly);

                // 4. a parameter declaration becomes a schema frame.
                var declaration = new Dictionary<string, object>();
                declaration["id"] = "p1";
                declaration["label"] = "Radius";
                declaration["type"] = "float";
                declaration["default"] = 2.0;
                declaration["step"] = 0.01;
                declaration["values"] = new List<object>();
                declaration["group"] = "";
                GhLink.PublishParam(stage, "k1", declaration);
                GhLink.Publish(null);
                Dictionary<string, object> schema = ReadUntil(server, "schema");
                Check("schema stage", (string)Json.Get(schema, "stage") == "k1",
                    "stage=" + Json.Get(schema, "stage"));
                var schemaParams = (List<object>)Json.Get(schema, "params");
                var first = schemaParams.Count == 1 ? (Dictionary<string, object>)schemaParams[0] : null;
                Check("schema declaration", first != null
                    && (string)Json.Get(first, "id") == "p1"
                    && Convert.ToDouble(Json.Get(first, "default"), System.Globalization.CultureInfo.InvariantCulture) == 2.0,
                    "params=" + Json.WriteCanonical(schemaParams));

                // 4b. an unchanged schema is quiet; a changed declaration is
                //     sent again. The periodic hello remains the safety net.
                GhLink.Publish(null);
                Check("unchanged schema is quiet", NoFrameOfType(server, "schema", 600), "");
                declaration["default"] = 3.0;
                GhLink.PublishParam(stage, "k1", declaration);
                GhLink.Publish(null);
                schema = ReadUntil(server, "schema");
                var changedParams = (List<object>)Json.Get(schema, "params");
                Check("changed schema is sent",
                    changedParams.Count == 1
                    && Convert.ToDouble(Json.Get(changedParams[0], "default"),
                        System.Globalization.CultureInfo.InvariantCulture) == 3.0,
                    "params=" + Json.WriteCanonical(changedParams));

                // 5. a full request lands in the slot with its parameter
                //    snapshot and geometry hash.
                var requestHeader = Header("request", "k1", "sha256:r1");
                requestHeader["geom"] = Geom("full", "sha256:h1");
                var parameters = new Dictionary<string, object>();
                parameters["p1"] = 2.5;
                requestHeader["params"] = parameters;
                server.Send(Frame(requestHeader, MeshSections()));
                Check("request fills the slot", WaitFor(delegate { return GhLink.FindSlot("k1") != null; }), "");
                StageSlot slot = GhLink.FindSlot("k1");
                Check("slot rid", slot.Rid == "sha256:r1", "rid=" + slot.Rid);
                Check("slot waiting", slot.Status == "waiting", "status=" + slot.Status);
                Check("slot geometry hash", slot.Frame.GeometryHash == "sha256:h1",
                    "hash=" + slot.Frame.GeometryHash);
                Check("slot params snapshot", slot.Params != null && slot.Params.ContainsKey("p1")
                    && Convert.ToDouble(slot.Params["p1"], System.Globalization.CultureInfo.InvariantCulture) == 2.5,
                    "params=" + Json.WriteCanonical(slot.Params));
                Check("slot sections kept", slot.Frame.Sections.Count == 3, "");

                // 6. a ref for geometry the slot does not hold is refused with
                //    unknown_ref before any timeout can start.
                var refHeader = Header("request", "k1", "sha256:r2");
                refHeader["geom"] = Geom("ref", "sha256:h2");
                server.Send(Frame(refHeader, null));
                Dictionary<string, object> refusal = ReadUntil(server, "error");
                Check("unknown_ref code", (string)Json.Get(refusal, "code") == "unknown_ref",
                    "code=" + Json.Get(refusal, "code"));
                Check("unknown_ref stage", (string)Json.Get(refusal, "stage") == "k1", "");
                Check("unknown_ref rid", (string)Json.Get(refusal, "rid") == "sha256:r2", "");
                Check("refusal status on the slot",
                    WaitFor(delegate { return GhLink.FindSlot("k1").Status == "unknown_ref"; }), "");

                // 7. the client re-sends on its own, same rid: the slot must
                //    come back to waiting and keep answering that rid.
                server.Send(Frame(requestHeader, MeshSections()));
                Check("same-rid re-send returns to waiting",
                    WaitFor(delegate { return GhLink.FindSlot("k1").Status == "waiting"; }), "");
                Check("same-rid re-send keeps the rid", GhLink.FindSlot("k1").Rid == "sha256:r1", "");

                // 7a. a held frame is one immutable snapshot: publishing a new
                //     request replaces it whole, and a reader that took the old
                //     reference still sees the old sections, attributes, hash
                //     and rid together.
                HeldFrame firstFrame = GhLink.FindSlot("k1").Frame;
                var secondHeader = Header("request", "k1", "sha256:r1b");
                secondHeader["geom"] = Geom("full", "sha256:h1b");
                secondHeader["attributes"] = new List<object>
                {
                    new Dictionary<string, object>
                    {
                        { "group", "mesh" }, { "name", "panel_id" },
                        { "type", "INT" }, { "domain", "POINT" },
                    },
                };
                secondHeader["definitions"] = new List<object>
                {
                    new Dictionary<string, object> { { "name", "block" } },
                };
                server.Send(Frame(secondHeader, MeshSections()));
                Check("new full request replaces the frame",
                    WaitFor(delegate { return GhLink.FindSlot("k1").Frame.GeometryHash == "sha256:h1b"; }), "");
                Check("old frame stays coherent", firstFrame.Rid == "sha256:r1"
                    && firstFrame.GeometryHash == "sha256:h1"
                    && firstFrame.Attributes.Count == 0
                    && firstFrame.Definitions.Count == 0, "");
                Check("new frame carries its own header",
                    GhLink.FindSlot("k1").Frame.Rid == "sha256:r1b"
                    && GhLink.FindSlot("k1").Frame.Attributes.Count == 1
                    && GhLink.FindSlot("k1").Frame.Definitions.Count == 1, "");

                // A ref keeps the geometry but carries the new rid in a fresh
                // snapshot, so Stage In can label the same output correctly.
                HeldFrame fullFrame = GhLink.FindSlot("k1").Frame;
                var refFrameHeader = Header("request", "k1", "sha256:r1c");
                refFrameHeader["geom"] = Geom("ref", "sha256:h1b");
                server.Send(Frame(refFrameHeader, null));
                Check("ref publishes a new rid",
                    WaitFor(delegate { return GhLink.FindSlot("k1").Frame.Rid == "sha256:r1c"; }), "");
                HeldFrame refFrame = GhLink.FindSlot("k1").Frame;
                Check("ref reuses the geometry snapshot",
                    object.ReferenceEquals(refFrame.Sections, fullFrame.Sections)
                    && object.ReferenceEquals(refFrame.Attributes, fullFrame.Attributes)
                    && object.ReferenceEquals(refFrame.Definitions, fullFrame.Definitions)
                    && refFrame.GeometryHash == "sha256:h1b", "");

                // 7b. the solve pins its rid: a request landing mid-solve must
                //     not relabel the answer, and outside a solve the slot's
                //     own rid is the answer. (The snapshot itself happens on
                //     the GH UI thread; this locks the choice of rid.)
                var pin = new StageSlot();
                pin.Rid = "sha256:latest";
                Check("answer rid falls back to the slot", GhLink.AnswerRid(pin) == "sha256:latest", "");
                pin.SolveRid = "sha256:solving";
                Check("answer rid uses the pinned rid", GhLink.AnswerRid(pin) == "sha256:solving", "");
                // No request context is active here: this is the user-driven
                // solve path, which still answers any stage with a current rid.
                pin.SolveRid = "";
                Check("a user solve answers any stage", GhLink.ShouldAnswer("k1"), "");
                Check("a user solve keeps the slot rid",
                    GhLink.AnswerRid(pin) == "sha256:latest", "rid=" + GhLink.AnswerRid(pin));

                // 8. a standalone error from Blender lands on the stage, and a
                //    later request with a new rid recovers it.
                var error = new Dictionary<string, object>();
                error["type"] = "error";
                error["code"] = "bad_frame";
                error["message"] = "test detail";
                error["stage"] = "k1";
                server.Send(FrameCodec.Encode(error, null));
                Check("inbound error sets the stage",
                    WaitFor(delegate { return GhLink.FindSlot("k1").Status == "error"; }), "");
                Check("inbound error message",
                    GhLink.FindSlot("k1").Message == "bad_frame: test detail",
                    "message=" + GhLink.FindSlot("k1").Message);
                var nextHeader = Header("request", "k1", "sha256:r3");
                nextHeader["geom"] = Geom("full", "sha256:h3");
                server.Send(Frame(nextHeader, MeshSections()));
                Check("request after an error recovers the slot",
                    WaitFor(delegate { return GhLink.FindSlot("k1").Rid == "sha256:r3"
                                              && GhLink.FindSlot("k1").Status == "waiting"; }), "");

                // 9. the receive path answers a version mismatch and an
                //    oversized frame, and notes a bad frame without answering.
                var wrongVersion = new Dictionary<string, object>();
                wrongVersion["type"] = "ping";
                wrongVersion["v"] = 3L;
                server.Send(FrameCodec.Encode(wrongVersion, null));
                Dictionary<string, object> mismatch = ReadUntil(server, "error");
                Check("proto_mismatch code", (string)Json.Get(mismatch, "code") == "proto_mismatch",
                    "code=" + Json.Get(mismatch, "code"));

                long cap = FrameCodec.MaxHeaderBytes + 1;
                var overCap = new byte[4];
                overCap[0] = (byte)(cap & 0xFF);
                overCap[1] = (byte)((cap >> 8) & 0xFF);
                overCap[2] = (byte)((cap >> 16) & 0xFF);
                overCap[3] = (byte)((cap >> 24) & 0xFF);
                server.Send(overCap);
                Dictionary<string, object> tooLarge = ReadUntil(server, "error");
                Check("too_large code", (string)Json.Get(tooLarge, "code") == "too_large",
                    "code=" + Json.Get(tooLarge, "code"));

                server.Send(FrameWithHeader("{\"type\":\"ping\",\"v\":2"));
                Check("bad_frame is noted, not answered", NoErrorFrame(server, 500), "");

                // 10. a foreign key while components exist is provisional; the
                //     next reconcile refuses it with unknown_stage and prunes it.
                //     A second component keeps the link alive across that
                //     reconcile, so the refusal actually goes out.
                var other = new TestComponent();
                GhLink.Register(other);
                GhLink.PublishStage(other, "k-other", true);
                GhLink.Publish(null);
                ReadUntil(server, "hello");

                var foreignHeader = Header("request", "k2", "sha256:r4");
                foreignHeader["geom"] = Geom("full", "sha256:h4");
                server.Send(Frame(foreignHeader, MeshSections()));
                Check("foreign request holds a provisional slot",
                    WaitFor(delegate { return GhLink.FindSlot("k2") != null; }), "");

                GhLink.Unregister(other);
                Dictionary<string, object> unknownStage = ReadUntil(server, "error");
                Check("unknown_stage code", (string)Json.Get(unknownStage, "code") == "unknown_stage",
                    "code=" + Json.Get(unknownStage, "code"));
                Check("unknown_stage stage", (string)Json.Get(unknownStage, "stage") == "k2", "");
                Check("unknown_stage rid", (string)Json.Get(unknownStage, "rid") == "sha256:r4", "");
                Check("refused slot pruned", GhLink.FindSlot("k2") == null, "");
                Check("claimed slot survives a sibling unregister", GhLink.FindSlot("k1") != null, "");

                // 11. the last component leaving stops the worker and closes
                //     the connection.
                GhLink.Unregister(stage);
                Check("last unregister closes the socket", server.DrainClosed(2000), "");
                GhLink.Stop();  // idempotent

                CollectNotes();
                Check("note about the pre-component request",
                    HasNote("no Stageloom component"), string.Join(" | ", Notes));
                Check("note about the held request",
                    HasNote("no active document; held in the slot"), string.Join(" | ", Notes));
                Check("note about the bad frame", HasNote("bad frame (bad_frame)"), "");
                Check("note about the connection", HasNote("connected to"), "");
            }
            catch (Exception ex)
            {
                _failures++;
                System.Console.WriteLine("FAIL link round-trip: " + ex);
            }
            finally
            {
                GhLink.Stop();
                server.Dispose();
            }

            RunSenderLifecycle();
            RunSolveRetry();
            RunRequestContext();
            RunSlotStatus();
            RunDefinitionContentGuard();

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: link round-trip PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: link round-trip FAIL (" + _failures + ")");
            return 1;
        }

        private static void RunSlotStatus()
        {
            var slot = new StageSlot();
            Check("slot starts waiting",
                slot.InStatus == "waiting" && slot.OutStatus == "waiting",
                "in=" + slot.InStatus + " out=" + slot.OutStatus);

            slot.InStatus = "ok";
            GhLink.SetOutStatus(slot, "error", "boom");
            Check("out status reaches the out half",
                slot.OutStatus == "error" && slot.OutMessage == "boom",
                "out=" + slot.OutStatus + " message=" + slot.OutMessage);
            Check("out status leaves the in half alone",
                slot.InStatus == "ok" && slot.InMessage == "",
                "in=" + slot.InStatus + " message=" + slot.InMessage);
            Check("out status updates the aggregate",
                slot.Status == "error" && slot.Message == "boom",
                "status=" + slot.Status + " message=" + slot.Message);

            var empty = new StageSlot();
            Check("answer rid is empty with no request", GhLink.AnswerRid(empty) == "",
                "rid=" + GhLink.AnswerRid(empty));
            empty.Rid = "sha256:latest";
            Check("answer rid falls back to the slot", GhLink.AnswerRid(empty) == "sha256:latest",
                "rid=" + GhLink.AnswerRid(empty));
            empty.SolveRid = "sha256:solving";
            Check("answer rid uses the pinned rid", GhLink.AnswerRid(empty) == "sha256:solving",
                "rid=" + GhLink.AnswerRid(empty));

            // Stage In owns only the In half; a Stage Out failure must survive a
            // successful import on the same slot.
            var split = new StageSlot();
            var inFrame = new HeldFrame("sha256:in", new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 1 }),
            }, new List<Dictionary<string, object>>(), new List<object>(), "sha256:in");
            split.Frame = inFrame;
            int inEpoch = split.StatusEpoch;
            GhLink.SetOutStatus(split, "error", "stage out failed");
            GhLink.SetInStatus(split, inFrame, inEpoch, "ok", "");
            Check("in status reaches the in half",
                split.InStatus == "ok" && split.InMessage == "",
                "in=" + split.InStatus + " message=" + split.InMessage);
            Check("in status leaves the out half alone",
                split.OutStatus == "error" && split.OutMessage == "stage out failed",
                "out=" + split.OutStatus + " message=" + split.OutMessage);
            Check("in status leaves the aggregate with stage out",
                split.Status == "error" && split.Message == "stage out failed",
                "status=" + split.Status + " message=" + split.Message);

            // A request arriving while the frame builds replaces the held
            // frame. The late Stage In verdict must not overwrite the newer
            // request's waiting state.
            var newerFrame = new HeldFrame("sha256:newer", new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 2 }),
            }, new List<Dictionary<string, object>>(), new List<object>(), "sha256:newer");
            split.Frame = newerFrame;
            split.InStatus = "waiting";
            split.InMessage = "";
            split.StatusEpoch++;
            GhLink.SetInStatus(split, inFrame, inEpoch, "error", "late verdict");
            Check("a late in status does not clobber a newer frame",
                split.InStatus == "waiting" && split.InMessage == "",
                "in=" + split.InStatus + " message=" + split.InMessage);

            // An unservable ref bumps the epoch without replacing the frame;
            // the late verdict must still lose to the newer event.
            var epochSlot = new StageSlot();
            epochSlot.Frame = inFrame;
            int epochBefore = epochSlot.StatusEpoch;
            epochSlot.InStatus = "unknown_ref";
            epochSlot.InMessage = "refused";
            epochSlot.StatusEpoch++;
            GhLink.SetInStatus(epochSlot, inFrame, epochBefore, "ok", "");
            Check("a late in status does not clobber a newer epoch",
                epochSlot.InStatus == "unknown_ref" && epochSlot.InMessage == "refused",
                "in=" + epochSlot.InStatus + " message=" + epochSlot.InMessage);

            // L3: saved script components call the old public name.
            GhLink.SetSlotStatus(split, "idle", "alias");
            Check("SetSlotStatus alias reaches the out half",
                split.OutStatus == "idle" && split.OutMessage == "alias",
                "out=" + split.OutStatus + " message=" + split.OutMessage);
            Check("SetSlotStatus alias updates the aggregate",
                split.Status == "idle" && split.Message == "alias",
                "status=" + split.Status + " message=" + split.Message);
            Check("SetSlotStatus alias leaves the in half alone",
                split.InStatus == "waiting" && split.InMessage == "",
                "in=" + split.InStatus + " message=" + split.InMessage);
        }

        /// <summary>A definition whose objects all vanished must not become an
        /// empty block: Stage Out drops the row and warns. The input lists are
        /// all that decides this, so no Rhino/GH native object is needed.</summary>
        private static void RunDefinitionContentGuard()
        {
            System.Console.WriteLine("instance definition content guard");
            var noRows = new List<GH_InstanceReference>();
            Check("all-null definition has no content",
                !Instances.HasDefinitionContent(
                    new List<Mesh>(), new List<Curve>(), new List<Point3d>(),
                    new List<SubD>(), new List<Brep>(), noRows), "");
            Check("a null nested row is not content",
                !Instances.HasDefinitionContent(
                    new List<Mesh>(), new List<Curve>(), new List<Point3d>(),
                    new List<SubD>(), new List<Brep>(),
                    new List<GH_InstanceReference> { null }), "");
        }

        // ------------------------------------------------------------ helpers

        /// <summary>A receive-loop exception must stop the sender with its
        /// connection. Without the finally in GhLink.Loop the old sender stays
        /// alive across the reconnect and can steal the new connection's first
        /// publication.</summary>
        private static void RunSenderLifecycle()
        {
            System.Console.WriteLine("link sender lifecycle");
            Notes.Clear();
            var server = new FakeServer();
            try
            {
                GhLink.Endpoint = "ws://127.0.0.1:" + server.Port + "/";
                GhLink.Start();
                server.Accept();
                ReadUntil(server, "hello");
                Check("one sender after connect",
                    WaitFor(delegate { return GhLink.ActiveSenders == 1; }),
                    "senders=" + GhLink.ActiveSenders);
                // A reset, not a clean close: ReceiveBinary throws and Loop takes
                // its catch path, which must still stop the sender.
                server.ResetClient();
                Check("reset took the exception path",
                    WaitFor(delegate
                    {
                        CollectNotes();
                        return HasNote("connect failed");
                    }),
                    string.Join(" | ", Notes));
                Check("sender stops with the failed connection",
                    WaitFor(delegate { return GhLink.ActiveSenders == 0; }),
                    "senders=" + GhLink.ActiveSenders);
                server.Accept();
                ReadUntil(server, "hello");
                Check("one sender after reconnect",
                    WaitFor(delegate { return GhLink.ActiveSenders == 1; }),
                    "senders=" + GhLink.ActiveSenders);
            }
            catch (Exception ex)
            {
                _failures++;
                System.Console.WriteLine("FAIL sender lifecycle: " + ex);
            }
            finally
            {
                GhLink.Stop();
                server.Dispose();
            }
        }

        /// <summary>A slow solve must not be scheduled a second time by the
        /// 250 ms retry loop; the attempt budget stays for a delegate that never
        /// starts, and a lost SolutionEnd is recovered by the watchdog.</summary>
        private static void RunSolveRetry()
        {
            System.Console.WriteLine("link solve retry");
            StageSlot slot = GhLink.Slot("retry-test");
            slot.Rid = "sha256:retry-test";
            slot.Status = "waiting";
            slot.RequestedAt = DateTime.UtcNow.AddSeconds(-1);
            slot.ResolveAttempts = 0;
            slot.SolveInFlight = true;
            slot.SolveStartedAt = DateTime.UtcNow;
            int retries = GhLink.RetryRequests;
            GhLink.RetryStaleSolves();
            Check("in-flight solve is not retried",
                slot.ResolveAttempts == 0 && GhLink.RetryRequests == retries,
                "attempts=" + slot.ResolveAttempts + " retries=" + GhLink.RetryRequests);

            slot.SolveInFlight = false;
            GhLink.RetryStaleSolves();
            Check("waiting solve is retried once",
                slot.ResolveAttempts == 1 && GhLink.RetryRequests == retries + 1,
                "attempts=" + slot.ResolveAttempts + " retries=" + GhLink.RetryRequests);

            slot.ResolveAttempts = 0;
            slot.RequestedAt = DateTime.UtcNow.AddSeconds(-1);
            slot.SolveInFlight = true;
            slot.SolveStartedAt = DateTime.UtcNow.AddSeconds(-31);
            GhLink.RetryStaleSolves();
            Check("a lost SolutionEnd does not block the retry",
                slot.ResolveAttempts == 1, "attempts=" + slot.ResolveAttempts);

            // A stage with no request (a document-wide solve ran it before any
            // request named it) must not look like a stalled request.
            StageSlot idle = GhLink.Slot("no-request-retry-test");
            idle.Rid = "";
            idle.Status = "waiting";
            idle.RequestedAt = DateTime.UtcNow.AddSeconds(-1);
            idle.ResolveAttempts = 0;
            idle.SolveInFlight = false;
            retries = GhLink.RetryRequests;
            GhLink.RetryStaleSolves();
            Check("a slot without a request is not retried",
                idle.ResolveAttempts == 0 && GhLink.RetryRequests == retries,
                "attempts=" + idle.ResolveAttempts + " retries=" + GhLink.RetryRequests);

            // The retry path queues the waiting (key, rid) pair; the next
            // scheduled delegate consumes it as the active request context.
            Check("a retried request is queued",
                GhLink.BeginRequestSolveForTest(), "no request context started");
            Check("the retried rid is pinned",
                slot.SolveRid == "sha256:retry-test", "solve rid=" + slot.SolveRid);
            Check("the retried stage is request-driven",
                GhLink.ShouldAnswer("retry-test"), "");
            GhLink.EndRequestSolveForTest();
        }

        /// <summary>The scheduled delegate's pending-to-active handoff without
        /// a GH document: requests coalesce, only named keys are pinned and
        /// answer, and finishing the batch clears only those keys.</summary>
        private static void RunRequestContext()
        {
            System.Console.WriteLine("link request context");
            StageSlot first = GhLink.Slot("context-a");
            first.Rid = "sha256:context-a1";
            first.Status = "waiting";
            StageSlot second = GhLink.Slot("context-b");
            second.Rid = "sha256:context-b1";
            second.Status = "waiting";
            StageSlot bystander = GhLink.Slot("context-idle");
            bystander.Rid = "sha256:context-idle";
            bystander.Status = "waiting";

            // Two requests for one key collapse to the latest rid before the
            // delegate starts; the other key joins the same batch. Each
            // RequestStage follows the slot update the receive path makes.
            GhLink.RequestStage("context-a", "sha256:context-a1");
            first.Rid = "sha256:context-a2";
            GhLink.RequestStage("context-a", "sha256:context-a2");
            GhLink.RequestStage("context-b", "sha256:context-b1");

            // A retry captured before the slot moved on must not overwrite the
            // newer rid in the pending map.
            first.Rid = "sha256:context-a3";
            GhLink.RequestStage("context-a", "sha256:context-a2");
            first.Rid = "sha256:context-a2";

            Check("a request batch starts", GhLink.BeginRequestSolveForTest(), "");
            Check("a second delegate finds no batch",
                !GhLink.BeginRequestSolveForTest(), "");
            Check("the requested key is pinned", first.SolveRid == "sha256:context-a2",
                "solve rid=" + first.SolveRid);
            Check("the other requested key is pinned", second.SolveRid == "sha256:context-b1",
                "solve rid=" + second.SolveRid);
            Check("an unrelated key is not pinned",
                string.IsNullOrEmpty(bystander.SolveRid), "solve rid=" + bystander.SolveRid);
            Check("only a requested key answers",
                GhLink.ShouldAnswer("context-a") && GhLink.ShouldAnswer("context-b")
                && !GhLink.ShouldAnswer("context-idle"), "");
            Check("only a requested key is request-driven",
                GhLink.IsRequestDriven("context-a") && !GhLink.IsRequestDriven("context-idle"), "");
            Check("the answer rid is the pinned request",
                GhLink.AnswerRid(first) == "sha256:context-a2", "rid=" + GhLink.AnswerRid(first));

            // A user-driven Stage Out whose answer is unchanged is quiet; a
            // request-driven one always answers.
            first.MarkSent("sha256:context-a2", "sha256:out");
            Check("an unchanged user answer is not re-sent",
                !first.ShouldSend("sha256:context-a2", "sha256:out", false), "");
            Check("a changed user answer is sent",
                first.ShouldSend("sha256:context-a2", "sha256:out2", false), "");
            Check("a request-driven answer is always sent",
                first.ShouldSend("sha256:context-a2", "sha256:out", true), "");

            bystander.SolveInFlight = true;
            bystander.SolveStartedAt = DateTime.UtcNow;
            GhLink.EndRequestSolveForTest();
            Check("finishing clears the first requested key",
                string.IsNullOrEmpty(first.SolveRid) && !first.SolveInFlight, "");
            Check("finishing clears the second requested key",
                string.IsNullOrEmpty(second.SolveRid) && !second.SolveInFlight, "");
            Check("finishing leaves an unrelated key alone",
                bystander.SolveInFlight, "");
            Check("after the batch a user solve answers any stage",
                GhLink.ShouldAnswer("context-idle")
                && GhLink.AnswerRid(bystander) == "sha256:context-idle",
                "rid=" + GhLink.AnswerRid(bystander));
            bystander.SolveInFlight = false;
            bystander.SolveStartedAt = DateTime.MinValue;
        }

        private static Dictionary<string, object> FindStage(Dictionary<string, object> hello, string key)
        {
            var stages = Json.Get(hello, "stages") as List<object>;
            if (stages == null) return null;
            foreach (object entry in stages)
            {
                var row = entry as Dictionary<string, object>;
                if (row != null && (string)Json.Get(row, "key") == key) return row;
            }
            return null;
        }

        private static Dictionary<string, object> Header(string type, string stage, string rid)
        {
            var header = new Dictionary<string, object>();
            header["type"] = type;
            header["stage"] = stage;
            header["rid"] = rid;
            return header;
        }

        private static Dictionary<string, object> Geom(string mode, string hash)
        {
            var geom = new Dictionary<string, object>();
            geom["mode"] = mode;
            geom["hash"] = hash;
            return geom;
        }

        private static byte[] Frame(Dictionary<string, object> header, List<Section> sections)
        {
            return FrameCodec.Encode(header, sections);
        }

        private static List<Section> MeshSections()
        {
            return new List<Section>
            {
                Section.Floats("mesh.verts", new float[] { 0, 0, 0, 1, 0, 0, 0, 1, 0 }),
                Section.U32("mesh.face_counts", new uint[] { 3 }),
                Section.U32("mesh.face_indices", new uint[] { 0, 1, 2 }),
            };
        }

        private static byte[] FrameWithHeader(string text)
        {
            byte[] head = Encoding.UTF8.GetBytes(text);
            var frame = new byte[4 + head.Length];
            frame[0] = (byte)(head.Length & 0xFF);
            frame[1] = (byte)((head.Length >> 8) & 0xFF);
            frame[2] = (byte)((head.Length >> 16) & 0xFF);
            frame[3] = (byte)((head.Length >> 24) & 0xFF);
            Buffer.BlockCopy(head, 0, frame, 4, head.Length);
            return frame;
        }

        private static void Check(string what, bool ok, string detail)
        {
            if (ok)
            {
                System.Console.WriteLine("PASS " + what);
                return;
            }
            _failures++;
            System.Console.WriteLine("FAIL " + what + (detail.Length > 0 ? ": " + detail : ""));
        }

        private static bool WaitFor(Func<bool> condition)
        {
            DateTime end = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < end)
            {
                if (condition()) return true;
                Thread.Sleep(20);
            }
            return condition();
        }

        private static void CollectNotes()
        {
            Notes.AddRange(GhLink.TakeNotes());
        }

        private static bool HasNote(string fragment)
        {
            foreach (string note in Notes)
                if (note.IndexOf(fragment, StringComparison.Ordinal) >= 0) return true;
            return false;
        }

        /// <summary>Reads frames until one of the wanted type arrives, skipping
        /// the sender's heartbeat republishes (hello/schema). Throws when the
        /// deadline passes, so a broken exchange stops the script loudly
        /// instead of cascading null dereferences.</summary>
        private static Dictionary<string, object> ReadUntil(FakeServer server, string type)
        {
            DateTime end = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < end)
            {
                bool closed;
                byte[] frame = server.TryReadFrame(300, out closed);
                if (frame == null)
                {
                    if (closed) break;
                    continue;
                }
                var decoded = FrameCodec.Decode(frame);
                string got = Json.GetString(decoded.Header, "type");
                if (got == type) return decoded.Header;
            }
            throw new Exception("timed out waiting for a " + type + " frame");
        }

        /// <summary>True when no `error` frame arrives within the window; the
        /// heartbeat's hello/schema frames are ignored.</summary>
        private static bool NoErrorFrame(FakeServer server, int windowMs)
        {
            return NoFrameOfType(server, "error", windowMs);
        }

        /// <summary>True when no frame of one type arrives within the window.</summary>
        private static bool NoFrameOfType(FakeServer server, string type, int windowMs)
        {
            DateTime end = DateTime.UtcNow.AddMilliseconds(windowMs);
            while (DateTime.UtcNow < end)
            {
                bool closed;
                byte[] frame = server.TryReadFrame(100, out closed);
                if (frame == null)
                {
                    if (closed) return false;
                    continue;
                }
                var decoded = FrameCodec.Decode(frame);
                if (Json.GetString(decoded.Header, "type") == type) return false;
            }
            return true;
        }

        // -------------------------------------------------------- fake server

        /// <summary>A GH_Component that does nothing, for registration only:
        /// pulling in the product components would pull their whole dependency
        /// set into this exe, and GhLink only needs a component to name as the
        /// solve origin.</summary>
        private sealed class TestComponent : GH_Component
        {
            public TestComponent()
                : base("Link Test", "Link Test", "test double", "Stageloom", "Tests")
            {
            }

            public override Guid ComponentGuid
            {
                get { return new Guid("c5d6e7f8-0000-4000-8000-00000000ffff"); }
            }

            protected override void RegisterInputParams(GH_InputParamManager p) { }

            protected override void RegisterOutputParams(GH_OutputParamManager p) { }

            protected override void SolveInstance(IGH_DataAccess da) { }
        }
    }
}
