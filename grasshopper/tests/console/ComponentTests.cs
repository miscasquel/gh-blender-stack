// Layer C, component half: Stage In writes its own status half and Stage Out
// gates a solve that has no request.
//
//     StageloomComponentTests.exe
//
// The real StageInComponent/StageOutComponent SolveInstance paths run with a
// scripted IGH_DataAccess and a fake WebSocket server, so the test sees the
// same slot bookkeeping the live components use. No Rhino document is needed;
// the success case builds an empty frame (the converters tolerate a frame with
// no known geometry group) and the failure case is a malformed mesh frame.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Threading;
using Grasshopper.Kernel;
using Grasshopper.Kernel.Data;
using Grasshopper.Kernel.Parameters;
using Grasshopper.Kernel.Types;
using Stageloom.Core;
using Rhino.Geometry;

namespace Stageloom.Tests.Console
{
    public static class ComponentTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            // Keep the suite out of the user's persisted endpoint. This must
            // happen before the first GhLink access, which resolves settings.
            EndpointSettings.DatabaseName = "StageloomComponentTests";

            System.Console.WriteLine("stage component status");
            var server = new FakeServer();
            try
            {
                GhLink.Configure("127.0.0.1", server.Port, false);
                GhLink.Start();
                server.Accept();
                ReadUntil(server, "hello");

                var stageIn = new StageInHarness();
                var stageOut = new StageOutHarness();
                GhLink.Register(stageIn);
                GhLink.Register(stageOut);

                RunStageInSuccess(server, stageIn);
                RunStageInFailure(server, stageIn);
                RunStageInProtocolError(stageIn);
                RunStageInLateWrite(server);
                RunStageOutNoRequest(server, stageOut);
                RunStageOutDependentAnswer(server, stageOut);
                RunStageOutTopologyToken(server, stageOut);
                RunEmptyFrame(server, stageIn);
                RunOneWayTests(server);
                RunRemovedKeyTests(server);
                RunUnitTransforms();
                RunParamVisibilityTests();
                RunLifecycleTests(server, stageIn, stageOut);
                RunDocumentIsolationTests();
                RunConnectionComponent();
            }
            catch (Exception ex)
            {
                _failures++;
                System.Console.WriteLine("FAIL stage component status: " + ex);
            }
            finally
            {
                GhLink.Stop();
                server.Dispose();
            }

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: stage component status PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: stage component status FAIL (" + _failures + ")");
            return 1;
        }

        private static void RunStageInSuccess(FakeServer server, StageInHarness component)
        {
            const string key = "component-in-ok";
            const string rid = "sha256:component-in-ok";
            SendRequest(server, key, rid, new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 1 }),
            });
            StageSlot slot = WaitSlot(key, rid);

            // A failed export on the same stage must not leak into the import.
            GhLink.SetOutStatus(slot, "error", "stage out failed");

            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            component.Run(da);

            Check("stage in success reports ok", Output(da, "Status") == "ok",
                "status=" + Output(da, "Status"));
            Check("stage in success clears its message", Output(da, "Message") == "",
                "message=" + Output(da, "Message"));
            Check("stage in success writes the in half",
                slot.InStatus == "ok" && slot.InMessage == "",
                "in=" + slot.InStatus + " message=" + slot.InMessage);
            Check("stage in success leaves the out failure alone",
                slot.OutStatus == "error" && slot.OutMessage == "stage out failed",
                "out=" + slot.OutStatus + " message=" + slot.OutMessage);
            Quiet(slot);
        }

        private static void RunStageInFailure(FakeServer server, StageInHarness component)
        {
            const string key = "component-in-bad";
            const string rid = "sha256:component-in-bad";
            SendRequest(server, key, rid, new List<Section>
            {
                Section.Floats("mesh.verts", new float[] { 0, 0, 0, 1, 0, 0 }),
                Section.U32("mesh.face_counts", new uint[] { 2 }),
                Section.U32("mesh.face_indices", new uint[] { 0, 1 }),
            });
            StageSlot slot = WaitSlot(key, rid);
            GhLink.SetOutStatus(slot, "ok", "");

            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            component.Run(da);

            string message = Output(da, "Message");
            Check("stage in failure reports error", Output(da, "Status") == "error",
                "status=" + Output(da, "Status"));
            Check("stage in failure has a message", !string.IsNullOrEmpty(message), "message empty");
            Check("stage in failure writes the in half",
                slot.InStatus == "error" && slot.InMessage == message,
                "in=" + slot.InStatus + " message=" + slot.InMessage);
            Check("stage in failure leaves the out half alone",
                slot.OutStatus == "ok" && slot.OutMessage == "",
                "out=" + slot.OutStatus + " message=" + slot.OutMessage);
            Quiet(slot);
        }

        private static void RunStageInProtocolError(StageInHarness component)
        {
            const string key = "component-in-protocol-error";
            StageSlot slot = GhLink.Slot(key);
            slot.InStatus = "error";
            slot.InMessage = "bad_frame: keep me";

            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            component.Run(da);

            Check("a protocol error is not reported as waiting",
                Output(da, "Status") == "error", "status=" + Output(da, "Status"));
            Check("a protocol error keeps its message",
                Output(da, "Message") == "bad_frame: keep me",
                "message=" + Output(da, "Message"));
        }

        private static void RunStageInLateWrite(FakeServer server)
        {
            const string key = "component-in-stale";
            const string oldRid = "sha256:component-in-old";
            const string newRid = "sha256:component-in-new";
            SendRequest(server, key, oldRid, new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 1 }),
            });
            StageSlot slot = WaitSlot(key, oldRid);
            HeldFrame oldFrame = slot.Frame;
            int oldEpoch = slot.StatusEpoch;

            SendRequest(server, key, newRid, new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 2 }),
            });
            WaitSlot(key, newRid);

            GhLink.SetInStatus(slot, oldFrame, oldEpoch, "error", "late verdict");
            Check("a late stage in write is dropped after a newer request",
                slot.InStatus == "waiting" && slot.InMessage == "",
                "in=" + slot.InStatus + " message=" + slot.InMessage);

            // A refused ref is a newer request that leaves the held frame in
            // place. Its status must survive a late Stage In verdict too.
            const string refusedRid = "sha256:component-in-refused";
            HeldFrame currentFrame = slot.Frame;
            int currentEpoch = slot.StatusEpoch;
            var refHeader = new Dictionary<string, object>();
            refHeader["type"] = "request";
            refHeader["stage"] = key;
            refHeader["rid"] = refusedRid;
            var refGeom = new Dictionary<string, object>();
            refGeom["mode"] = "ref";
            refGeom["hash"] = "sha256:not-held";
            refHeader["geom"] = refGeom;
            server.Send(FrameCodec.Encode(refHeader, null));
            WaitStatus(key, refusedRid, "unknown_ref");

            GhLink.SetInStatus(slot, currentFrame, currentEpoch, "ok", "");
            Check("a late stage in write is dropped after a refused ref",
                slot.InStatus == "unknown_ref" && !string.IsNullOrEmpty(slot.InMessage),
                "in=" + slot.InStatus + " message=" + slot.InMessage);
            Quiet(slot);
        }

        private static void RunStageOutNoRequest(FakeServer server, StageOutHarness component)
        {
            const string key = "component-out-idle";
            StageSlot slot = GhLink.Slot(key);
            slot.InStatus = "error";
            slot.InMessage = "import failed";

            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            da.Inputs["Geometry"] = new List<IGH_Goo>();
            component.Run(da);

            Check("stage out without a request stays idle",
                Output(da, "Status") == "idle", "status=" + Output(da, "Status"));
            Check("stage out without a request sends nothing",
                !(bool)OutputValue(da, "Sent") && (string)OutputValue(da, "Output hash") == "",
                "sent=" + OutputValue(da, "Sent") + " hash=" + OutputValue(da, "Output hash"));
            Check("stage out without a request writes its own half",
                slot.OutStatus == "idle" && slot.OutMessage == "",
                "out=" + slot.OutStatus + " message=" + slot.OutMessage);
            Check("stage out idle leaves the in half alone",
                slot.InStatus == "error" && slot.InMessage == "import failed",
                "in=" + slot.InStatus + " message=" + slot.InMessage);
            Check("stage out idle updates the aggregate",
                slot.Status == "idle" && slot.Message == "",
                "status=" + slot.Status + " message=" + slot.Message);
            Check("stage out without a request emits no response",
                NoFrameOfType(server, "response", 400), "a response frame arrived");

            slot.RequestedAt = DateTime.UtcNow.AddSeconds(-1);
            int attempts = slot.ResolveAttempts;
            GhLink.RetryStaleSolves();
            Check("an idle stage out is not retried",
                slot.ResolveAttempts == attempts,
                "attempts=" + slot.ResolveAttempts);
        }

        /// <summary>A parameter shared by two Stage Out branches recomputes both
        /// during a request solve aimed at one stage. The other stage's answer
        /// must still reach Blender when its output changed: otherwise
        /// Grasshopper shows both results while only one Blender object updates.
        /// A stage with a newer request queued must stay quiet, because this
        /// solve would label older inputs with that request's rid.</summary>
        private static void RunStageOutDependentAnswer(FakeServer server, StageOutHarness component)
        {
            const string requested = "component-out-requested";
            const string requestedRid = "sha256:component-out-requested";
            const string dependent = "component-out-dependent";
            const string dependentRid = "sha256:component-out-dependent";
            const string queued = "component-out-queued";
            const string queuedRid = "sha256:component-out-queued";

            StageSlot requestedSlot = GhLink.Slot(requested);
            requestedSlot.Rid = requestedRid;
            requestedSlot.Status = "ok";
            Quiet(requestedSlot);
            StageSlot dependentSlot = GhLink.Slot(dependent);
            dependentSlot.Rid = dependentRid;
            dependentSlot.Status = "ok";
            Quiet(dependentSlot);

            // A request solve owns one key; the other branch is a dependent
            // that this solve happens to recompute.
            GhLink.RequestStage(requested, requestedRid);
            Check("a dependent answer test starts a request context",
                GhLink.BeginRequestSolveForTest(), "");
            Check("the requested key answers", GhLink.ShouldAnswer(requested), "");
            Check("a dependent stage with no queued request may answer",
                GhLink.ShouldAnswer(dependent), "");
            Check("a dependent stage is not request-driven",
                !GhLink.IsRequestDriven(dependent), "");
            Check("a dependent stage answers its own rid",
                GhLink.AnswerRid(dependentSlot) == dependentRid,
                "rid=" + GhLink.AnswerRid(dependentSlot));

            // Its changed output is a real response for Blender, with its own
            // rid (never the requested stage's pinned rid).
            var da = new FakeDa(component);
            da.Inputs["Key"] = dependent;
            da.Inputs["Enabled"] = true;
            da.Inputs["Geometry"] = new List<IGH_Goo> { new GH_Point(new Point3d(0, 0, 0)) };
            component.Run(da);
            Check("the dependent stage out sends its changed answer",
                (bool)OutputValue(da, "Sent"),
                "sent=" + OutputValue(da, "Sent") + " status=" + Output(da, "Status"));
            Dictionary<string, object> response = ReadUntil(server, "response");
            Check("the dependent answer names its stage",
                (string)Json.Get(response, "stage") == dependent,
                "stage=" + Json.Get(response, "stage"));
            Check("the dependent answer carries its own rid",
                (string)Json.Get(response, "rid") == dependentRid,
                "rid=" + Json.Get(response, "rid"));

            // A stage whose newer request is queued for the next solve must not
            // answer this one: its inputs for that rid have not been read yet.
            StageSlot queuedSlot = GhLink.Slot(queued);
            queuedSlot.Rid = queuedRid;
            queuedSlot.Status = "ok";
            Quiet(queuedSlot);
            GhLink.RequestStage(queued, queuedRid);
            Check("a stage with a queued request does not answer this solve",
                !GhLink.ShouldAnswer(queued), "");
            var queuedDa = new FakeDa(component);
            queuedDa.Inputs["Key"] = queued;
            queuedDa.Inputs["Enabled"] = true;
            queuedDa.Inputs["Geometry"] = new List<IGH_Goo> { new GH_Point(new Point3d(0, 0, 0)) };
            component.Run(queuedDa);
            Check("the queued stage out stays quiet",
                !(bool)OutputValue(queuedDa, "Sent"),
                "sent=" + OutputValue(queuedDa, "Sent"));
            Check("the queued stage out emits no response",
                NoFrameOfType(server, "response", 400), "");

            // The queued request is the next batch, where it is requested and
            // may answer normally.
            GhLink.EndRequestSolveForTest();
            Check("the queued request starts the next batch",
                GhLink.BeginRequestSolveForTest(), "");
            Check("the queued key answers in its own batch",
                GhLink.ShouldAnswer(queued), "");
            GhLink.EndRequestSolveForTest();
        }

        /// <summary>The ok response carries the additive base-topology token
        /// (§4.4) and it tracks topology only: a moved vertex keeps it, a
        /// flipped quad changes it. Curves/points carry no token, and an error
        /// response carries no `geom` at all.</summary>
        private static void RunStageOutTopologyToken(FakeServer server, StageOutHarness component)
        {
            const string key = "component-out-topology";
            StageSlot slot = GhLink.Slot(key);

            List<Section> quad = TopologySections(new uint[] { 0, 1, 2, 3 });
            Dictionary<string, object> first = StageOutComponent.TopologyHeader(quad);
            string firstToken = first == null ? null : Json.GetString(first, "topology");
            Check("the ok header carries a topology token",
                firstToken != null && firstToken.StartsWith("sha256:")
                && firstToken.Length == 71,
                "token=" + (firstToken ?? "null"));

            List<Section> moved = TopologySections(new uint[] { 0, 1, 2, 3 });
            moved[0] = Section.Floats("mesh.verts",
                new float[] { 10, 0, 0, 11, 0, 0, 11, 1, 0, 10, 1, 0 });
            Dictionary<string, object> movedHeader = StageOutComponent.TopologyHeader(moved);
            Check("a position-only answer keeps the topology token",
                movedHeader != null
                && Json.GetString(movedHeader, "topology") == firstToken,
                "first=" + firstToken + " moved="
                + (movedHeader == null ? "null" : Json.GetString(movedHeader, "topology")));

            Dictionary<string, object> flipped =
                StageOutComponent.TopologyHeader(TopologySections(new uint[] { 0, 1, 3, 2 }));
            Check("a changed face order changes the topology token",
                flipped != null
                && Json.GetString(flipped, "topology") != firstToken,
                "first=" + firstToken + " flipped="
                + (flipped == null ? "null" : Json.GetString(flipped, "topology")));

            Check("a curve-only response carries no topology token",
                StageOutComponent.TopologyHeader(new Section[]
                {
                    Section.Floats("crv.points", new float[] { 0, 0, 0, 1 }),
                }) == null, "");

            // A goo that throws during dispatch reaches Stage Out's own error
            // answer: that response must stay shaped like every error (no geom).
            const string errorRid = "sha256:component-out-topology-error";
            slot.Rid = errorRid;
            slot.Status = "ok";
            Quiet(slot);
            GhLink.RequestStage(key, errorRid);
            Check("the error token test starts a request context",
                GhLink.BeginRequestSolveForTest(), "");
            var errorDa = StageOutData(component, key);
            errorDa.Inputs["Geometry"] = new List<IGH_Goo> { new ThrowingGoo() };
            component.Run(errorDa);
            Dictionary<string, object> errorResponse = ReadUntil(server, "response");
            Check("a throwing solve emits an error response",
                Json.GetString(errorResponse, "status") == "error",
                "status=" + Json.GetString(errorResponse, "status"));
            Check("an error response carries no topology token",
                Json.Get(errorResponse, "geom") == null,
                "geom=" + Json.WriteCanonical(Json.Get(errorResponse, "geom")));
            GhLink.EndRequestSolveForTest();
        }

        private static List<Section> TopologySections(uint[] indices)
        {
            var sections = new List<Section>();
            sections.Add(Section.Floats("mesh.verts",
                new float[] { 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0 }));
            sections.Add(Section.U32("mesh.face_counts", new uint[] { 4 }));
            sections.Add(Section.U32("mesh.face_indices", indices));
            return sections;
        }

        /// <summary>A goo whose dispatch cast fails, so Stage Out's own error
        /// path answers exactly as it would for a conversion failure.</summary>
        private sealed class ThrowingGoo : GH_Goo<object>
        {
            public override IGH_Goo Duplicate() { return new ThrowingGoo(); }
            public override bool IsValid { get { return true; } }
            public override string ToString() { return "throwing"; }
            public override string TypeName { get { return "Throwing"; } }
            public override string TypeDescription { get { return "A goo that fails dispatch"; } }
            public override bool CastTo<Q>(ref Q target)
            {
                throw new InvalidOperationException("topology error test");
            }
        }

        // ------------------------------------------------------- empty frame

        /// <summary>A full request with zero sections is a real frame, not the
        /// "no frame" sentinel: a later ref for it is accepted, and Stage In
        /// reports an empty geometry list with `ok`.</summary>
        private static void RunEmptyFrame(FakeServer server, StageInHarness component)
        {
            System.Console.WriteLine();
            System.Console.WriteLine("empty frame");

            const string key = "component-in-empty";
            const string fullRid = "sha256:component-in-empty-full";
            SendRequest(server, key, fullRid, new List<Section>());
            StageSlot slot = WaitSlot(key, fullRid);
            Check("a zero-section full request is a held frame",
                GhLink.HasFrame(slot.Frame) && slot.Frame.Sections.Count == 0,
                "frame=" + (GhLink.HasFrame(slot.Frame) ? "held" : "empty")
                + " sections=" + slot.Frame.Sections.Count);
            Check("the empty frame keeps the full rid", slot.Frame.Rid == fullRid,
                "rid=" + slot.Frame.Rid);

            const string refRid = "sha256:component-in-empty-ref";
            var refHeader = new Dictionary<string, object>();
            refHeader["type"] = "request";
            refHeader["stage"] = key;
            refHeader["rid"] = refRid;
            var refGeom = new Dictionary<string, object>();
            refGeom["mode"] = "ref";
            refGeom["hash"] = "sha256:hash-" + fullRid;
            refHeader["geom"] = refGeom;
            server.Send(FrameCodec.Encode(refHeader, null));

            StageSlot refSlot = WaitSlot(key, refRid);
            Check("a ref for a held empty frame is accepted",
                GhLink.HasFrame(refSlot.Frame) && refSlot.Frame.Sections.Count == 0
                && refSlot.Status == "waiting",
                "status=" + refSlot.Status + " sections=" + refSlot.Frame.Sections.Count);

            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            component.Run(da);
            Check("stage in reports ok for an empty frame",
                Output(da, "Status") == "ok", "status=" + Output(da, "Status"));
            Check("stage in outputs an empty geometry list",
                da.Outputs["Geometry"] is List<IGH_Goo>
                && ((List<IGH_Goo>)da.Outputs["Geometry"]).Count == 0,
                "geometry=" + (da.Outputs["Geometry"] == null
                    ? "(null)" : da.Outputs["Geometry"].GetType().Name));
            Check("stage in writes the empty frame verdict",
                slot.InStatus == "ok" && slot.InMessage == "",
                "in=" + slot.InStatus + " message=" + slot.InMessage);
            Check("the empty frame carries its request id",
                Output(da, "Request ID") == refRid,
                "rid=" + Output(da, "Request ID"));
            Quiet(slot);
        }

        // ---------------------------------------------------------- one-way

        /// <summary>A key with no enabled Stage Out is one-way: Stage In's
        /// verdict is terminal, and RetryStaleSolves never re-asks for it. A
        /// Param-only key has no stage at all and is equally never retried.</summary>
        private static void RunOneWayTests(FakeServer server)
        {
            System.Console.WriteLine();
            System.Console.WriteLine("one-way stages");

            var inOnly = new StageInHarness();
            var outDisabled = new StageOutHarness();
            var inForDisabled = new StageInHarness();
            var paramOnly = new ParamHarness();
            try
            {
                // 1. Stage In only: no Stage Out claim at all.
                const string inKey = "one-way-in";
                inOnly.Add(null);
                inOnly.Run(StageInData(inOnly, inKey));
                SendRequest(server, inKey, "sha256:one-way-in", Sections());
                StageSlot inSlot = WaitSlot(inKey, "sha256:one-way-in");
                Check("a Stage In only key has no answering Stage Out",
                    !GhLink.StageAnswers(inKey), "");
                RetryNoRequest(inSlot);
                Check("a Stage In only key is not retried",
                    inSlot.ResolveAttempts == 0,
                    "attempts=" + inSlot.ResolveAttempts);

                FakeDa inDa = StageInData(inOnly, inKey);
                inOnly.Run(inDa);
                Check("a Stage In only key reaches a terminal status",
                    Output(inDa, "Status") == "ok" && inSlot.Status == "ok",
                    "output=" + Output(inDa, "Status") + " slot=" + inSlot.Status);
                RetryNoRequest(inSlot);
                Check("the delivered one-way key is not retried",
                    inSlot.ResolveAttempts == 0,
                    "attempts=" + inSlot.ResolveAttempts);
                Quiet(inSlot);

                // 2. A Stage Out present but disabled cannot answer either.
                const string disabledKey = "one-way-disabled-out";
                outDisabled.Add(null);
                inForDisabled.Add(null);
                FakeDa disabledOut = StageOutData(outDisabled, disabledKey);
                disabledOut.Inputs["Enabled"] = false;
                outDisabled.Run(disabledOut);
                SendRequest(server, disabledKey, "sha256:one-way-disabled", Sections());
                StageSlot disabledSlot = WaitSlot(disabledKey, "sha256:one-way-disabled");
                Check("a disabled Stage Out does not answer",
                    !GhLink.StageAnswers(disabledKey), "");
                RetryNoRequest(disabledSlot);
                Check("a disabled Stage Out key is not retried",
                    disabledSlot.ResolveAttempts == 0,
                    "attempts=" + disabledSlot.ResolveAttempts);
                FakeDa disabledIn = StageInData(inForDisabled, disabledKey);
                inForDisabled.Run(disabledIn);
                Check("Stage In owns the status when Stage Out is disabled",
                    disabledSlot.Status == "ok" && disabledSlot.OutStatus == "waiting",
                    "status=" + disabledSlot.Status + " out=" + disabledSlot.OutStatus);
                Quiet(disabledSlot);

                // 3. Param only: the key has a schema, but no stage to answer.
                const string paramKey = "one-way-param";
                paramOnly.Add(null);
                paramOnly.Run(ParamData(paramOnly, paramKey));
                SendRequest(server, paramKey, "sha256:one-way-param", Sections());
                StageSlot paramSlot = WaitSlot(paramKey, "sha256:one-way-param");
                Check("a Param only key has no answering Stage Out",
                    !GhLink.StageAnswers(paramKey), "");
                RetryNoRequest(paramSlot);
                Check("a Param only key is not retried",
                    paramSlot.ResolveAttempts == 0,
                    "attempts=" + paramSlot.ResolveAttempts);
                Check("a Param only request stays waiting without a retry storm",
                    paramSlot.Status == "waiting" && paramSlot.ResolveAttempts == 0,
                    "status=" + paramSlot.Status + " attempts=" + paramSlot.ResolveAttempts);
                Quiet(paramSlot);
            }
            finally
            {
                // Consume any pending request these cases queued so the next
                // case does not inherit their document id.
                if (GhLink.BeginRequestSolveForTest()) GhLink.EndRequestSolveForTest();
                GhLink.Unregister(inOnly);
                GhLink.Unregister(outDisabled);
                GhLink.Unregister(inForDisabled);
                GhLink.Unregister(paramOnly);
            }
        }

        /// <summary>Make a slot look like a request that has waited much longer
        /// than the retry threshold, then run one retry pass.</summary>
        private static void RetryNoRequest(StageSlot slot)
        {
            slot.RequestedAt = DateTime.UtcNow.AddSeconds(-2);
            slot.ResolveAttempts = 0;
            slot.SolveInFlight = false;
            GhLink.RetryStaleSolves();
        }

        // ------------------------------------------------------ removed keys

        /// <summary>A key cleared from its component leaves the published view
        /// at once and is refused with unknown_stage, not kept as a provisional
        /// slot forever. Claiming the key again clears the tombstone.</summary>
        private static void RunRemovedKeyTests(FakeServer server)
        {
            System.Console.WriteLine();
            System.Console.WriteLine("removed keys");

            const string key = "component-removed";
            var component = new StageInHarness();
            try
            {
                component.Add(null);
                component.Run(StageInData(component, key));
                SendRequest(server, key, "sha256:removed-1", Sections());
                StageSlot slot = WaitSlot(key, "sha256:removed-1");
                Check("the claimed key holds a request",
                    slot.Rid == "sha256:removed-1", "rid=" + slot.Rid);
                GhLink.BeginRequestSolveForTest();
                GhLink.EndRequestSolveForTest();

                // Clear the key exactly as SolveInstance does when the Key
                // input is emptied.
                var cleared = new FakeDa(component);
                cleared.Inputs["Key"] = "";
                cleared.Inputs["Enabled"] = true;
                component.Run(cleared);
                Check("clearing the key drops its slot",
                    GhLink.FindSlot(key) == null, "slot still exists");

                SendRequest(server, key, "sha256:removed-2", Sections());
                Dictionary<string, object> refusal = ReadErrorFor(server, key, "sha256:removed-2");
                Check("a request for a removed key is refused",
                    Json.GetString(refusal, "code") == "unknown_stage"
                    && Json.GetString(refusal, "stage") == key
                    && Json.GetString(refusal, "rid") == "sha256:removed-2",
                    "error=" + Json.WriteCanonical(refusal));
                Check("a removed key does not become provisional",
                    GhLink.FindSlot(key) == null, "slot exists");

                // Re-claiming the key makes it live again.
                component.Run(StageInData(component, key));
                SendRequest(server, key, "sha256:removed-3", Sections());
                StageSlot restored = WaitSlot(key, "sha256:removed-3");
                Check("re-claiming a removed key accepts requests",
                    restored.Rid == "sha256:removed-3", "rid=" + restored.Rid);
                Quiet(restored);
                GhLink.BeginRequestSolveForTest();
                GhLink.EndRequestSolveForTest();
            }
            catch (Exception ex)
            {
                _failures++;
                System.Console.WriteLine("FAIL removed keys: " + ex);
            }
            finally
            {
                GhLink.Unregister(component);
            }
        }

        // --------------------------------------------------------- unit rows

        /// <summary>A placement transform's translation is a length. The wire
        /// is metres; a millimetre document's rows must come out 1000× larger,
        /// and Stage Out must divide them back. Instance geometry was already
        /// scaled in both directions, which is why a metres document hid this.</summary>
        private static void RunUnitTransforms()
        {
            System.Console.WriteLine();
            System.Console.WriteLine("unit transforms");

            var header = new Dictionary<string, object>();
            var frame = DecodedFrame.FromSections(header, new List<Section>
            {
                Section.U32("inst.ref_def", new uint[] { 0 }),
                Section.Floats("inst.transforms", new float[]
                {
                    1, 0, 0, 5,
                    0, 1, 0, 4,
                    0, 0, 1, 3,
                    0, 0, 0, 1,
                }),
            });

            long[] refs;
            List<Transform> transforms;
            bool read = Instances.ReadRows(frame, "", 1000.0, out refs, out transforms);
            bool oneRow = read && refs.Length == 1 && transforms != null && transforms.Count == 1;
            Check("unit rows: a mm document scales the placement",
                oneRow
                && Near(transforms[0][0, 3], 5000) && Near(transforms[0][1, 3], 4000)
                && Near(transforms[0][2, 3], 3000),
                oneRow ? transforms[0].ToString() : "no rows");

            Transform millimetres = oneRow ? transforms[0] : new Transform(1.0);
            float[] wire = Conversion.TransformToWire(millimetres, 1000.0);
            Check("unit rows: Stage Out divides the placement back to metres",
                Near(wire[3], 5) && Near(wire[7], 4) && Near(wire[11], 3),
                wire.Length == 16 ? wire[3] + "," + wire[7] + "," + wire[11] : "bad wire");

            Instances.ReadRows(frame, "", 1.0, out refs, out transforms);
            bool metres = transforms != null && transforms.Count == 1;
            Check("unit rows: a metre document keeps the placement",
                metres && Near(transforms[0][0, 3], 5)
                && Near(transforms[0][1, 3], 4) && Near(transforms[0][2, 3], 3),
                metres ? transforms[0].ToString() : "no rows");
        }

        private static bool Near(double value, double expected)
        {
            double tolerance = Math.Max(1e-5, Math.Abs(expected) * 1e-5);
            return Math.Abs(value - expected) <= tolerance;
        }

        // ------------------------------------------------------ param visibility

        /// <summary>The Param node hides the optional inputs that carry no
        /// data and have no wire, the same "hide unused" rule as Rhino 8's
        /// native components. A value or a wire keeps an input visible.</summary>
        private static void RunParamVisibilityTests()
        {
            System.Console.WriteLine();
            System.Console.WriteLine("param input visibility");

            var param = new ParamComponent();
            Check("param starts with every input before placement",
                param.Params.Input.Count == 9, "count=" + param.Params.Input.Count);

            param.ApplyInitialVisibility();
            Check("a new param node hides its unused inputs",
                InputNames(param) == "Key,Label,Type", InputNames(param));

            param.SetInputsCollapsed(false);
            Check("show all restores every input",
                InputNames(param) == "Key,Label,Type,Default,Min,Max,Step,Values,Group",
                InputNames(param));

            SetStringInput(param, "Default", "2.5");
            SetNumberInput(param, "Min", -1.0);
            param.SetInputsCollapsed(true);
            Check("inputs with data stay visible",
                InputNames(param) == "Key,Label,Type,Default,Min", InputNames(param));
            Check("empty optional inputs are hidden",
                FindInput(param, "Max") == null && FindInput(param, "Step") == null,
                InputNames(param));

            ClearStringInput(param, "Default");
            ClearNumberInput(param, "Min");
            param.SetInputsCollapsed(true);
            Check("cleared inputs hide again",
                InputNames(param) == "Key,Label,Type", InputNames(param));

            // A wire keeps an input visible even when it is empty.
            param.SetInputsCollapsed(false);
            var source = new Param_Number();
            IGH_Param maxInput = FindInput(param, "Max");
            maxInput.AddSource(source);
            param.SetInputsCollapsed(true);
            Check("a wired input stays visible",
                FindInput(param, "Max") != null, InputNames(param));
            param.SetInputsCollapsed(false);
            Check("show all still restores the rest",
                param.Params.Input.Count == 9, InputNames(param));

            // A loaded document keeps its saved parameter list; placement must
            // not hide an expanded node.
            var loaded = new ParamComponent();
            loaded.MarkLoaded();
            loaded.ApplyInitialVisibility();
            Check("a loaded document keeps its expanded inputs",
                loaded.Params.Input.Count == 9, InputNames(loaded));
        }

        // ------------------------------------------------------------ lifecycle

        /// <summary>Registration and deregistration are one pair. A removed
        /// Param drops its claim, schema declaration, slot and component entry;
        /// a remaining Stage In still answers. Removing the last component stops
        /// the worker and clears the slots; the next add starts it again.
        ///
        /// The console suite has no Rhino process, so the document is null:
        /// GH_Component's lifecycle hook tolerates it, exactly as
        /// OnPingDocument() returns null throughout these tests.</summary>
        private static void RunLifecycleTests(FakeServer server, StageInHarness stageIn,
                                              StageOutHarness stageOut)
        {
            System.Console.WriteLine();
            System.Console.WriteLine("component lifecycle");

            // The earlier cases registered these two directly; start from an
            // empty registry so the counts below describe only this case.
            GhLink.Unregister(stageIn);
            GhLink.Unregister(stageOut);
            Check("the last earlier unregister leaves an empty registry",
                GhLink.ComponentCount == 0 && GhLink.ClaimCount == 0 && !GhLink.Running,
                "components=" + GhLink.ComponentCount + " claims=" + GhLink.ClaimCount
                + " running=" + GhLink.Running);

            const string inKey = "lifecycle-in";
            const string outKey = "lifecycle-out";
            const string paramKey = "lifecycle-param";

            var inComponent = new StageInHarness();
            var outComponent = new StageOutHarness();
            // The last unregister stopped the worker; the next register starts a
            // fresh connection, so the scripted server accepts it again.
            server.ResetClient();
            inComponent.Add(null);
            outComponent.Add(null);
            server.Accept();
            ReadUntil(server, "hello");
            Check("AddedToDocument registers each stage component once",
                GhLink.ComponentCount == 2, "components=" + GhLink.ComponentCount);

            GhLink.Note("lifecycle note");
            inComponent.ClearRuntimeMessages();
            inComponent.Run(StageInData(inComponent, inKey));
            Check("the base drains link notes into runtime remarks",
                RuntimeText(inComponent, GH_RuntimeMessageLevel.Remark).Contains("lifecycle note"),
                RuntimeText(inComponent, GH_RuntimeMessageLevel.Remark));
            outComponent.Run(StageOutData(outComponent, outKey));
            Check("each stage component publishes one claim",
                GhLink.ClaimCount == 2, "claims=" + GhLink.ClaimCount);

            var param = new ParamHarness();
            param.Add(null);
            Check("adding a Param registers it",
                GhLink.ComponentCount == 3, "components=" + GhLink.ComponentCount);
            param.Run(ParamData(param, paramKey));
            Check("the Param publishes its declaration",
                GhLink.ClaimCount == 3 && GhLink.StageParamCount(paramKey) == 1,
                "claims=" + GhLink.ClaimCount + " params=" + GhLink.StageParamCount(paramKey));

            param.Remove(null);
            Check("removing the Param drops its component entry",
                GhLink.ComponentCount == 2, "components=" + GhLink.ComponentCount);
            Check("removing the Param drops its claim",
                GhLink.ClaimCount == 2, "claims=" + GhLink.ClaimCount);
            Check("removing the Param drops its schema declaration",
                GhLink.StageParamCount(paramKey) == 0,
                "params=" + GhLink.StageParamCount(paramKey));
            Check("removing the Param drops its slot",
                GhLink.FindSlot(paramKey) == null, "slot still exists");

            // A late publish from the removed component must not resurrect a
            // claim nothing can serve or remove. PublishParam is the exact path
            // SolveInstance uses; no slot is involved here.
            var stale = new Dictionary<string, object>();
            stale["id"] = Guid.NewGuid().ToString();
            stale["label"] = "late";
            stale["type"] = "float";
            stale["default"] = 1.0;
            stale["values"] = new List<object>();
            GhLink.PublishParam(param, paramKey, stale);
            Check("a late publish from a removed component leaves no claim",
                GhLink.ClaimCount == 2 && GhLink.StageParamCount(paramKey) == 0,
                "claims=" + GhLink.ClaimCount + " params=" + GhLink.StageParamCount(paramKey));

            // The remaining Stage In still works after the Param goes.
            const string inRid = "sha256:lifecycle-in";
            SendRequest(server, inKey, inRid, new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 1 }),
            });
            StageSlot inSlot = WaitSlot(inKey, inRid);
            FakeDa inDa = StageInData(inComponent, inKey);
            inComponent.Run(inDa);
            Check("the remaining Stage In still answers",
                Output(inDa, "Status") == "ok", "status=" + Output(inDa, "Status"));
            Quiet(inSlot);

            outComponent.Remove(null);
            Check("removing a Stage Out leaves the other component registered",
                GhLink.ComponentCount == 1 && GhLink.ClaimCount == 1,
                "components=" + GhLink.ComponentCount + " claims=" + GhLink.ClaimCount);
            Check("removing a Stage Out clears its slot",
                GhLink.FindSlot(outKey) == null, "slot still exists");

            inComponent.Remove(null);
            Check("removing the last component clears the registry",
                GhLink.ComponentCount == 0 && GhLink.ClaimCount == 0,
                "components=" + GhLink.ComponentCount + " claims=" + GhLink.ClaimCount);
            Check("removing the last component clears its slot",
                GhLink.FindSlot(inKey) == null, "slot still exists");
            Check("removing the last component stops the worker",
                !GhLink.Running && !GhLink.Connected,
                "running=" + GhLink.Running + " connected=" + GhLink.Connected);

            // The next add starts a fresh worker and can claim again.
            server.ResetClient();
            var restarted = new StageInHarness();
            restarted.Add(null);
            Check("a later add restarts the worker",
                GhLink.ComponentCount == 1 && GhLink.Running,
                "components=" + GhLink.ComponentCount + " running=" + GhLink.Running);
            server.Accept();
            ReadUntil(server, "hello");

            const string restartKey = "lifecycle-restart";
            const string restartRid = "sha256:lifecycle-restart";
            SendRequest(server, restartKey, restartRid, new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 1 }),
            });
            StageSlot restartSlot = WaitSlot(restartKey, restartRid);
            FakeDa restartDa = StageInData(restarted, restartKey);
            restarted.Run(restartDa);
            Check("the restarted component answers and claims again",
                Output(restartDa, "Status") == "ok" && GhLink.ClaimCount == 1,
                "status=" + Output(restartDa, "Status") + " claims=" + GhLink.ClaimCount);
            Quiet(restartSlot);

            // The original defect: a Param removed while it is the last
            // component must still unregister and stop the worker.
            restarted.Remove(null);
            const string lastParamKey = "lifecycle-param-last";
            var lastParam = new ParamHarness();
            lastParam.Add(null);
            lastParam.Run(ParamData(lastParam, lastParamKey));
            Check("a Param can be the only registered component",
                GhLink.ComponentCount == 1 && GhLink.StageParamCount(lastParamKey) == 1,
                "components=" + GhLink.ComponentCount
                + " params=" + GhLink.StageParamCount(lastParamKey));
            lastParam.Remove(null);
            Check("a removed last Param leaves nothing behind",
                GhLink.ComponentCount == 0 && GhLink.ClaimCount == 0
                && GhLink.StageParamCount(lastParamKey) == 0
                && GhLink.FindSlot(lastParamKey) == null && !GhLink.Running,
                "components=" + GhLink.ComponentCount + " claims=" + GhLink.ClaimCount
                + " params=" + GhLink.StageParamCount(lastParamKey)
                + " slot=" + (GhLink.FindSlot(lastParamKey) == null ? "gone" : "present")
                + " running=" + GhLink.Running);
        }

        private static FakeDa StageInData(StageInHarness component, string key)
        {
            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            return da;
        }

        private static FakeDa StageOutData(StageOutHarness component, string key)
        {
            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Enabled"] = true;
            da.Inputs["Geometry"] = new List<IGH_Goo>();
            return da;
        }

        private static FakeDa ParamData(ParamHarness component, string key)
        {
            var da = new FakeDa(component);
            da.Inputs["Key"] = key;
            da.Inputs["Label"] = "Radius";
            da.Inputs["Type"] = 0;
            return da;
        }

        // ------------------------------------------------------ document scope

        /// <summary>Ownership is per document: a request for one document's
        /// key fills and pins only that document's slot, a key two documents
        /// claim is a strict conflict with no hello row, no schema and no
        /// answer, and removing one claimant restores the survivor. The
        /// console has no Rhino process, so documents are represented by their
        /// stable ids (the identity a real GH_Document would supply).</summary>
        private static void RunDocumentIsolationTests()
        {
            System.Console.WriteLine();
            System.Console.WriteLine("document isolation");

            var server = new FakeServer();
            var docA = new Guid("11111111-1111-4111-8111-111111111111");
            var docB = new Guid("22222222-2222-4222-8222-222222222222");
            var docC = new Guid("33333333-3333-4333-8333-333333333333");
            var docD = new Guid("44444444-4444-4444-8444-444444444444");
            var docE = new Guid("55555555-5555-4555-8555-555555555555");
            var docF = new Guid("66666666-6666-4666-8666-666666666666");

            var inA = new StageInHarness();
            var outA = new StageOutHarness();
            var inB = new StageInHarness();
            var outB = new StageOutHarness();
            var outC = new StageOutHarness();
            var paramC = new ParamHarness();
            var outD = new StageOutHarness();
            var paramD = new ParamHarness();
            var outE1 = new StageOutHarness();
            var outE2 = new StageOutHarness();
            var paramE = new ParamHarness();
            var outF = new StageOutHarness();
            var outF2 = new StageOutHarness();
            var outG = new StageOutHarness();
            var outH = new StageOutHarness();

            try
            {
                GhLink.Configure("127.0.0.1", server.Port, false);
                GhLink.Start();
                server.Accept();
                ReadUntil(server, "hello");

                // Two documents with different keys publish and solve apart.
                GhLink.RegisterInDocumentForTest(inA, docA);
                GhLink.RegisterInDocumentForTest(outA, docA);
                GhLink.RegisterInDocumentForTest(inB, docB);
                GhLink.RegisterInDocumentForTest(outB, docB);
                Check("each document has its own registration",
                    GhLink.DocumentCount == 2, "documents=" + GhLink.DocumentCount);
                GhLink.PublishStageIn(inA, "doc-a", true);
                GhLink.PublishStageOut(outA, "doc-a", true);
                GhLink.PublishStageIn(inB, "doc-b", true);
                GhLink.PublishStageOut(outB, "doc-b", true);
                GhLink.Publish(null);
                Dictionary<string, object> hello = ReadUntil(server, "hello");
                Check("two documents publish their own keys",
                    FindStage(hello, "doc-a") != null && FindStage(hello, "doc-b") != null,
                    "stages=" + Json.WriteCanonical(Json.Get(hello, "stages")));

                SendRequest(server, "doc-a", "sha256:doc-a", Sections());
                StageSlot slotA = WaitSlotIn(docA, "doc-a", "sha256:doc-a");
                Check("a request fills the owning document's slot",
                    slotA != null && slotA.Rid == "sha256:doc-a",
                    "rid=" + (slotA == null ? "(none)" : slotA.Rid));
                Check("a request creates no slot in the other document",
                    GhLink.FindSlot(docB, "doc-a") == null, "slot exists");
                Guid requested;
                Check("the request solve names the owning document",
                    GhLink.BeginRequestSolveForTest(out requested) && requested == docA,
                    "document=" + requested);
                Check("only the requested document's key is request-driven",
                    GhLink.IsRequestDriven("doc-a") && !GhLink.IsRequestDriven("doc-b"), "");
                Quiet(slotA);
                GhLink.EndRequestSolveForTest();

                // The same key in two documents is a strict conflict.
                GhLink.RegisterInDocumentForTest(outC, docC);
                GhLink.RegisterInDocumentForTest(paramC, docC);
                GhLink.RegisterInDocumentForTest(outD, docD);
                GhLink.RegisterInDocumentForTest(paramD, docD);
                GhLink.PublishStageOut(outC, "doc-dup", true);
                GhLink.PublishParam(paramC, "doc-dup", ParamDeclaration("c"));
                GhLink.Publish(null);
                hello = ReadHello(server, "doc-dup", true);
                Check("one document can publish the key before the conflict",
                    hello != null, "no hello");
                GhLink.PublishStageOut(outD, "doc-dup", true);
                GhLink.PublishParam(paramD, "doc-dup", ParamDeclaration("d"));
                Check("two documents on one key are a strict conflict",
                    GhLink.IsStageConflicted("doc-dup")
                    && GhLink.StageConflict("doc-dup").IndexOf("duplicate stage key",
                        StringComparison.Ordinal) >= 0,
                    GhLink.StageConflict("doc-dup"));
                Check("a conflicted key has no schema declaration",
                    GhLink.StageParamCount("doc-dup") == 0,
                    "params=" + GhLink.StageParamCount("doc-dup"));

                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                Check("a conflicted key is withheld from hello",
                    FindStage(hello, "doc-dup") == null,
                    "stages=" + Json.WriteCanonical(Json.Get(hello, "stages")));

                SendRequest(server, "doc-dup", "sha256:doc-dup", Sections());
                Dictionary<string, object> duplicate = ReadUntil(server, "error");
                Check("a request for a conflicted key is refused",
                    Json.GetString(duplicate, "code") == "duplicate_stage"
                    && Json.GetString(duplicate, "stage") == "doc-dup",
                    "error=" + Json.WriteCanonical(duplicate));
                Check("the conflict names both documents",
                    Json.GetString(duplicate, "message") != null
                    && Json.GetString(duplicate, "message").IndexOf("claimed by",
                        StringComparison.Ordinal) >= 0,
                    Json.GetString(duplicate, "message"));
                Check("a conflicted request touches neither document's slot",
                    GhLink.FindSlot(docC, "doc-dup") == null
                    && GhLink.FindSlot(docD, "doc-dup") == null, "slot exists");

                // A component on a conflicted key reports the duplication and
                // sends no response.
                outC.ClearRuntimeMessages();
                FakeDa duplicateDa = StageOutData(outC, "doc-dup");
                duplicateDa.Inputs["Geometry"] = new List<IGH_Goo> { new GH_Point(new Point3d(0, 0, 0)) };
                outC.Run(duplicateDa);
                Check("a conflicted Stage Out explains the duplicate",
                    RuntimeText(outC, GH_RuntimeMessageLevel.Error).IndexOf("duplicate stage key",
                        StringComparison.Ordinal) >= 0,
                    RuntimeText(outC, GH_RuntimeMessageLevel.Error));
                Check("a conflicted Stage Out emits no response",
                    !(bool)OutputValue(duplicateDa, "Sent")
                    && NoFrameOfType(server, "response", 400), "");

                // Removing one duplicate document restores the survivor's stage
                // and schema.
                GhLink.Unregister(outD);
                GhLink.Unregister(paramD);
                Check("removing one claimant clears the conflict",
                    !GhLink.IsStageConflicted("doc-dup") && GhLink.ConflictCount == 0,
                    "conflicts=" + GhLink.ConflictCount);
                Check("the survivor's schema is restored",
                    GhLink.StageParamCount("doc-dup") == 1,
                    "params=" + GhLink.StageParamCount("doc-dup"));
                hello = ReadHello(server, "doc-dup", true);
                Check("the survivor's stage is republished",
                    hello != null && FindStage(hello, "doc-dup") != null,
                    hello == null ? "no hello" : Json.WriteCanonical(Json.Get(hello, "stages")));
                Dictionary<string, object> restoredSchema = ReadUntil(server, "schema");
                Check("the survivor's schema is republished alone",
                    Json.GetString(restoredSchema, "stage") == "doc-dup"
                    && ((List<object>)Json.Get(restoredSchema, "params")).Count == 1,
                    "schema=" + Json.WriteCanonical(restoredSchema));

                SendRequest(server, "doc-dup", "sha256:doc-dup-2", Sections());
                StageSlot survivor = WaitSlotIn(docC, "doc-dup", "sha256:doc-dup-2");
                Check("the survivor answers its own request",
                    survivor != null && GhLink.FindSlot(docD, "doc-dup") == null,
                    survivor == null ? "no slot" : "rid=" + survivor.Rid);
                FakeDa survivorDa = StageOutData(outC, "doc-dup");
                survivorDa.Inputs["Geometry"] = new List<IGH_Goo> { new GH_Point(new Point3d(0, 0, 0)) };
                Check("the survivor starts a request batch",
                    GhLink.BeginRequestSolveForTest(), "");
                outC.Run(survivorDa);
                Dictionary<string, object> answer = ReadUntil(server, "response");
                Check("the survivor's answer carries the request",
                    Json.GetString(answer, "stage") == "doc-dup"
                    && Json.GetString(answer, "rid") == "sha256:doc-dup-2",
                    "response=" + Json.WriteCanonical(answer));
                Quiet(survivor);
                GhLink.EndRequestSolveForTest();

                // If the earlier owner leaves, the later claimant becomes the
                // owner with a fresh slot: it must not inherit the departed
                // document's held frame.
                var docG = new Guid("77777777-7777-4777-8777-777777777777");
                var docH = new Guid("88888888-8888-4888-8888-888888888888");
                GhLink.RegisterInDocumentForTest(outG, docG);
                GhLink.PublishStageOut(outG, "doc-owner-drop", true);
                GhLink.Publish(null);
                ReadHello(server, "doc-owner-drop", true);
                SendRequest(server, "doc-owner-drop", "sha256:owner-g", Sections());
                StageSlot oldOwner = WaitSlotIn(docG, "doc-owner-drop", "sha256:owner-g");
                Check("the first owner holds its own frame",
                    oldOwner != null && oldOwner.Frame.GeometryHash == "sha256:hash-sha256:owner-g",
                    oldOwner == null ? "no slot" : oldOwner.Frame.GeometryHash);

                GhLink.RegisterInDocumentForTest(outH, docH);
                GhLink.PublishStageOut(outH, "doc-owner-drop", true);
                Check("the later claimant creates a conflict",
                    GhLink.IsStageConflicted("doc-owner-drop"), GhLink.StageConflict("doc-owner-drop"));
                GhLink.Unregister(outG);
                Check("the later claimant becomes the owner when the first leaves",
                    !GhLink.IsStageConflicted("doc-owner-drop")
                    && GhLink.FindSlot(docG, "doc-owner-drop") == null
                    && GhLink.FindSlot(docH, "doc-owner-drop") == null,
                    "conflict=" + GhLink.StageConflict("doc-owner-drop"));

                SendRequest(server, "doc-owner-drop", "sha256:owner-h", Sections());
                StageSlot newOwner = WaitSlotIn(docH, "doc-owner-drop", "sha256:owner-h");
                Check("the surviving owner answers with its own fresh frame",
                    newOwner != null && newOwner.Frame.GeometryHash == "sha256:hash-sha256:owner-h"
                    && GhLink.FindSlot(docG, "doc-owner-drop") == null,
                    newOwner == null ? "no slot" : newOwner.Frame.GeometryHash);
                Guid newOwnerDocument;
                Check("the surviving owner's request is routed to it",
                    GhLink.BeginRequestSolveForTest(out newOwnerDocument)
                    && newOwnerDocument == docH, "document=" + newOwnerDocument);
                Quiet(newOwner);
                GhLink.EndRequestSolveForTest();
                GhLink.Unregister(outH);

                // Duplicate roles within one document stay a strict conflict:
                // hello/schema must not keep the last writer's values.
                GhLink.RegisterInDocumentForTest(outE1, docE);
                GhLink.RegisterInDocumentForTest(outE2, docE);
                GhLink.RegisterInDocumentForTest(paramE, docE);
                GhLink.PublishStageOut(outE1, "doc-local-dup", true);
                GhLink.PublishParam(paramE, "doc-local-dup", ParamDeclaration("e"));
                GhLink.Publish(null);
                hello = ReadHello(server, "doc-local-dup", true);
                Check("one Stage Out can publish the key before the duplicate",
                    hello != null, "no hello");
                GhLink.PublishStageOut(outE2, "doc-local-dup", false);
                Check("duplicate Stage Outs in one document are a strict conflict",
                    GhLink.IsStageConflicted("doc-local-dup")
                    && GhLink.StageParamCount("doc-local-dup") == 0,
                    GhLink.StageConflict("doc-local-dup"));
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                Check("an in-document duplicate is withheld from hello",
                    FindStage(hello, "doc-local-dup") == null,
                    "stages=" + Json.WriteCanonical(Json.Get(hello, "stages")));

                GhLink.Unregister(outE2);
                Check("removing the duplicate role clears the local conflict",
                    !GhLink.IsStageConflicted("doc-local-dup")
                    && GhLink.StageParamCount("doc-local-dup") == 1,
                    "conflict=" + GhLink.StageConflict("doc-local-dup")
                    + " params=" + GhLink.StageParamCount("doc-local-dup"));
                hello = ReadHello(server, "doc-local-dup", true);
                Dictionary<string, object> survivorRow = hello == null ? null : FindStage(hello, "doc-local-dup");
                Check("the remaining Stage Out republishes its stage",
                    survivorRow != null, hello == null ? "no hello" : Json.WriteCanonical(Json.Get(hello, "stages")));
                Check("the survivor restores its own enabled value",
                    survivorRow != null && (bool)Json.Get(survivorRow, "enabled"),
                    survivorRow == null ? "no row" : Json.WriteCanonical(survivorRow));

                // Removal removes the document's ownership; re-adding a
                // component recomputes it from the live claims.
                GhLink.RegisterInDocumentForTest(outF, docF);
                GhLink.PublishStageOut(outF, "doc-readd", true);
                GhLink.Publish(null);
                hello = ReadHello(server, "doc-readd", true);
                Check("a new document claims its key", hello != null, "no hello");

                GhLink.Unregister(outF);
                hello = ReadHello(server, "doc-readd", false);
                Check("removing the last component releases the key",
                    hello != null && FindStage(hello, "doc-readd") == null,
                    hello == null ? "no hello" : Json.WriteCanonical(Json.Get(hello, "stages")));

                GhLink.RegisterInDocumentForTest(outF2, docF);
                GhLink.PublishStageOut(outF2, "doc-readd", true);
                GhLink.Publish(null);
                hello = ReadHello(server, "doc-readd", true);
                Check("re-adding the component reclaims the key",
                    hello != null && FindStage(hello, "doc-readd") != null,
                    hello == null ? "no hello" : Json.WriteCanonical(Json.Get(hello, "stages")));
            }
            catch (Exception ex)
            {
                _failures++;
                System.Console.WriteLine("FAIL document isolation: " + ex);
            }
            finally
            {
                GhLink.Unregister(outA);
                GhLink.Unregister(inA);
                GhLink.Unregister(outB);
                GhLink.Unregister(inB);
                GhLink.Unregister(outC);
                GhLink.Unregister(paramC);
                GhLink.Unregister(outE1);
                GhLink.Unregister(outE2);
                GhLink.Unregister(paramE);
                GhLink.Unregister(outF);
                GhLink.Unregister(outF2);
                GhLink.Unregister(outG);
                GhLink.Unregister(outH);
                GhLink.Stop();
                server.Dispose();
            }
        }

        private static Dictionary<string, object> ParamDeclaration(string id)
        {
            var declaration = new Dictionary<string, object>();
            declaration["id"] = id;
            declaration["label"] = "Value";
            declaration["type"] = "float";
            declaration["default"] = 1.0;
            declaration["min"] = null;
            declaration["max"] = null;
            declaration["step"] = 0.1;
            declaration["values"] = new List<object>();
            declaration["group"] = "";
            return declaration;
        }

        private static List<Section> Sections()
        {
            return new List<Section>
            {
                Section.U32("test.unknown", new uint[] { 1 }),
            };
        }

        private static StageSlot WaitSlotIn(Guid documentId, string key, string rid)
        {
            DateTime end = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < end)
            {
                StageSlot slot = GhLink.FindSlot(documentId, key);
                if (slot != null && slot.Frame.Rid == rid) return slot;
                Thread.Sleep(20);
            }
            throw new Exception("timed out waiting for stage " + key + " in document " + documentId);
        }

        /// <summary>Read hellos until one carries (or does not carry) a key.
        /// Reconcile publishes immediately after a removal; an older heartbeat
        /// may still be ahead of it in the server's receive queue.</summary>
        private static Dictionary<string, object> ReadHello(FakeServer server, string key, bool present)
        {
            DateTime end = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < end)
            {
                bool closed;
                byte[] frame = server.TryReadFrame(250, out closed);
                if (frame == null)
                {
                    if (closed) break;
                    continue;
                }
                var decoded = FrameCodec.Decode(frame);
                if (Json.GetString(decoded.Header, "type") != "hello") continue;
                bool found = FindStage(decoded.Header, key) != null;
                if (found == present) return decoded.Header;
            }
            return null;
        }

        // ------------------------------------------------------ connection node

        /// <summary>The Stageloom Connection component: bad input is a runtime
        /// message (never a dialog), a valid port moves the link and is saved
        /// globally, and the outputs show the canonical endpoint and state.</summary>
        private static void RunConnectionComponent()
        {
            System.Console.WriteLine();
            System.Console.WriteLine("connection component");
            var server = new FakeServer();
            var component = new ConnectionHarness();
            try
            {
                string error;
                Check("connection settings save", EndpointSettings.SavePort(61234, out error),
                    error ?? "");
                int stored;
                Check("connection settings reload",
                    EndpointSettings.TryLoadPort(out stored, out error) && stored == 61234,
                    "stored=" + stored + " " + (error ?? ""));
                Check("connection settings clear", EndpointSettings.ClearPort(out error),
                    error ?? "");

                var da = new FakeDa(component);
                da.Inputs["Port"] = 80;
                component.ClearRuntimeMessages();
                component.Run(da);
                Check("connection rejects a port below the range",
                    Output(da, "Status") == "invalid port",
                    "status=" + Output(da, "Status"));
                Check("connection reports the validation failure",
                    RuntimeText(component, GH_RuntimeMessageLevel.Error).Contains("1024"),
                    RuntimeText(component, GH_RuntimeMessageLevel.Error));
                Check("connection keeps the current endpoint on bad input",
                    Output(da, "Endpoint") == GhLink.Endpoint,
                    "endpoint=" + Output(da, "Endpoint"));
                Check("connection host output is loopback",
                    Output(da, "Host") == "127.0.0.1", "host=" + Output(da, "Host"));

                da = new FakeDa(component);
                da.Inputs["Port"] = server.Port;
                component.ClearRuntimeMessages();
                component.Run(da);
                string expected = "ws://127.0.0.1:" + server.Port + "/";
                Check("connection accepts a valid port",
                    Output(da, "Endpoint") == expected, "endpoint=" + Output(da, "Endpoint"));
                Check("connection reports connecting",
                    Output(da, "Status") == "connecting", "status=" + Output(da, "Status"));
                Check("connection has no runtime error on success",
                    RuntimeText(component, GH_RuntimeMessageLevel.Error) == "",
                    RuntimeText(component, GH_RuntimeMessageLevel.Error));

                server.Accept();
                ReadUntil(server, "hello");
                da = new FakeDa(component);
                da.Inputs["Port"] = server.Port;
                component.ClearRuntimeMessages();
                component.Run(da);
                Check("connection reports connected",
                    Output(da, "Status") == "connected", "status=" + Output(da, "Status"));
                Check("connection endpoint matches the link",
                    Output(da, "Endpoint") == GhLink.Endpoint,
                    "endpoint=" + Output(da, "Endpoint"));
                Check("connection port is saved globally",
                    EndpointSettings.TryLoadPort(out stored, out error) && stored == server.Port,
                    "stored=" + stored + " " + (error ?? ""));
            }
            catch (Exception ex)
            {
                _failures++;
                System.Console.WriteLine("FAIL connection component: " + ex);
            }
            finally
            {
                GhLink.Unregister(component);
                server.Dispose();
            }
        }

        private static string RuntimeText(GH_Component component, GH_RuntimeMessageLevel level)
        {
            var parts = new List<string>();
            foreach (string message in component.RuntimeMessages(level)) parts.Add(message);
            return string.Join(" | ", parts.ToArray());
        }

        private static IGH_Param FindInput(IGH_Component component, string name)
        {
            int index = component.Params.IndexOfInputParam(name);
            return index < 0 ? null : component.Params.Input[index];
        }

        private static string InputNames(IGH_Component component)
        {
            var names = new List<string>();
            foreach (IGH_Param param in component.Params.Input) names.Add(param.Name);
            return string.Join(",", names.ToArray());
        }

        private static void SetStringInput(IGH_Component component, string name, string value)
        {
            var param = (Param_String)FindInput(component, name);
            param.PersistentData.Clear();
            param.PersistentData.Append(new GH_String(value));
        }

        private static void SetNumberInput(IGH_Component component, string name, double value)
        {
            var param = (Param_Number)FindInput(component, name);
            param.PersistentData.Clear();
            param.PersistentData.Append(new GH_Number(value));
        }

        private static void ClearStringInput(IGH_Component component, string name)
        {
            ((Param_String)FindInput(component, name)).PersistentData.Clear();
        }

        private static void ClearNumberInput(IGH_Component component, string name)
        {
            ((Param_Number)FindInput(component, name)).PersistentData.Clear();
        }

        // ------------------------------------------------------------ helpers

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

        private static void SendRequest(FakeServer server, string key, string rid,
                                        List<Section> sections)
        {
            var header = new Dictionary<string, object>();
            header["type"] = "request";
            header["stage"] = key;
            header["rid"] = rid;
            var geom = new Dictionary<string, object>();
            geom["mode"] = "full";
            geom["hash"] = "sha256:hash-" + rid;
            header["geom"] = geom;
            server.Send(FrameCodec.Encode(header, sections));
        }

        private static StageSlot WaitSlot(string key, string rid)
        {
            DateTime end = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < end)
            {
                StageSlot slot = GhLink.FindSlot(key);
                if (slot != null && slot.Frame.Rid == rid) return slot;
                Thread.Sleep(20);
            }
            throw new Exception("timed out waiting for stage " + key + " rid " + rid);
        }

        private static StageSlot WaitStatus(string key, string rid, string status)
        {
            DateTime end = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < end)
            {
                StageSlot slot = GhLink.FindSlot(key);
                if (slot != null && slot.Rid == rid && slot.Status == status) return slot;
                Thread.Sleep(20);
            }
            throw new Exception("timed out waiting for stage " + key + " status " + status);
        }

        private static object OutputValue(FakeDa da, string name)
        {
            object value;
            return da.Outputs.TryGetValue(name, out value) ? value : null;
        }

        private static string Output(FakeDa da, string name)
        {
            return OutputValue(da, name) as string;
        }

        /// <summary>Stop the retry loop re-asking for a slot the test is done
        /// with, without changing the status fields under assertion.</summary>
        private static void Quiet(StageSlot slot)
        {
            slot.ResolveAttempts = 8;
        }

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
                if (Json.GetString(decoded.Header, "type") == type) return decoded.Header;
            }
            throw new Exception("timed out waiting for a " + type + " frame");
        }

        /// <summary>Reads until the error for one exact stage/rid arrives,
        /// skipping older errors other cases left in the stream.</summary>
        private static Dictionary<string, object> ReadErrorFor(
            FakeServer server, string stage, string rid)
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
                if (Json.GetString(decoded.Header, "type") != "error") continue;
                if (Json.GetString(decoded.Header, "stage") == stage
                    && Json.GetString(decoded.Header, "rid") == rid)
                    return decoded.Header;
            }
            throw new Exception("timed out waiting for an error for " + stage + " " + rid);
        }

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

        private sealed class StageInHarness : StageInComponent
        {
            public void Run(IGH_DataAccess da) { SolveInstance(da); }
            public void Add(GH_Document document) { AddedToDocument(document); }
            public void Remove(GH_Document document) { RemovedFromDocument(document); }
        }

        private sealed class StageOutHarness : StageOutComponent
        {
            public void Run(IGH_DataAccess da) { SolveInstance(da); }
            public void Add(GH_Document document) { AddedToDocument(document); }
            public void Remove(GH_Document document) { RemovedFromDocument(document); }
        }

        private sealed class ParamHarness : ParamComponent
        {
            public void Run(IGH_DataAccess da) { SolveInstance(da); }
            public void Add(GH_Document document) { AddedToDocument(document); }
            public void Remove(GH_Document document) { RemovedFromDocument(document); }
        }

        private sealed class ConnectionHarness : ConnectionComponent
        {
            public void Run(IGH_DataAccess da) { SolveInstance(da); }
        }

        /// <summary>The IGH_DataAccess calls the two components make, backed by
        /// dictionaries. Inputs use the parameter names the product passes.</summary>
        private sealed class FakeDa : IGH_DataAccess
        {
            public readonly Dictionary<string, object> Inputs = new Dictionary<string, object>();
            public readonly Dictionary<string, object> Outputs = new Dictionary<string, object>();
            private readonly List<string> _inputNames = new List<string>();

            public FakeDa(IGH_Component component)
            {
                foreach (IGH_Param p in component.Params.Input) _inputNames.Add(p.Name);
            }

            public int Iteration { get { return 0; } }
            public void IncrementIteration() { }
            public void DisableGapLogic() { }
            public void DisableGapLogic(int paramIndex) { }
            public GH_Path ParameterTargetPath(int paramIndex) { return null; }
            public int ParameterTargetIndex(int paramIndex) { return paramIndex; }
            public void AbortComponentSolution() { }
            public List<T> Util_RemoveNullRefs<T>(List<T> L) { return L; }
            public int Util_CountNullRefs<T>(List<T> L) { return 0; }
            public int Util_CountNonNullRefs<T>(List<T> L) { return L == null ? 0 : L.Count; }
            public bool Util_EnsureNonNullCount<T>(List<T> L, int N) { return true; }
            public int Util_FirstNonNullItem<T>(List<T> L) { return -1; }
            public bool SetData(int paramIndex, object data)
            {
                return SetData(_inputNames.Count > paramIndex ? _inputNames[paramIndex] : "out" + paramIndex, data);
            }
            public bool SetData(int paramIndex, object data, int itemIndexOverride)
            {
                return SetData(paramIndex, data);
            }
            public bool SetData(string paramName, object data)
            {
                Outputs[paramName] = data;
                return true;
            }
            public bool SetDataList(int paramIndex, IEnumerable data)
            {
                return SetDataList(_inputNames.Count > paramIndex ? _inputNames[paramIndex] : "out" + paramIndex, data);
            }
            public bool SetDataList(int paramIndex, IEnumerable data, int listIndexOverride)
            {
                return SetDataList(paramIndex, data);
            }
            public bool SetDataList(string paramName, IEnumerable data)
            {
                Outputs[paramName] = data;
                return true;
            }
            public bool SetDataTree(int paramIndex, IGH_DataTree tree) { return false; }
            public bool SetDataTree(int paramIndex, IGH_Structure tree) { return false; }
            public bool BlitData<T>(int paramIndex, GH_Structure<T> tree, bool overwrite)
                where T : IGH_Goo
            {
                return false;
            }
            public bool GetData<T>(int index, ref T destination)
            {
                return GetData(_inputNames.Count > index ? _inputNames[index] : "", ref destination);
            }
            public bool GetData<T>(string name, ref T destination)
            {
                object value;
                if (!Inputs.TryGetValue(name, out value)) return false;
                if (value is T)
                {
                    destination = (T)value;
                    return true;
                }
                return false;
            }
            public bool GetDataList<T>(int index, List<T> list)
            {
                return GetDataList(_inputNames.Count > index ? _inputNames[index] : "", list);
            }
            public bool GetDataList<T>(string name, List<T> list)
            {
                object value;
                if (!Inputs.TryGetValue(name, out value)) return false;
                var source = value as IEnumerable;
                if (source == null) return false;
                foreach (object item in source)
                    if (item is T) list.Add((T)item);
                return true;
            }
            public bool GetDataTree<T>(int index, out GH_Structure<T> tree)
                where T : IGH_Goo
            {
                tree = null;
                return false;
            }
            public bool GetDataTree<T>(string name, out GH_Structure<T> tree)
                where T : IGH_Goo
            {
                tree = null;
                return false;
            }
        }
    }
}
