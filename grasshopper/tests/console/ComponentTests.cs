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
using Grasshopper.Kernel.Types;
using Stageloom.Core;

namespace Stageloom.Tests.Console
{
    public static class ComponentTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            System.Console.WriteLine("stage component status");
            var server = new FakeServer();
            try
            {
                GhLink.Endpoint = "ws://127.0.0.1:" + server.Port + "/";
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

        // ------------------------------------------------------------ helpers

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
        }

        private sealed class StageOutHarness : StageOutComponent
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
