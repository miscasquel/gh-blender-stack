// Layer C, link half: one round trip against the real GhLink.
//
//     GhStackLinkTests.exe
//
// Drives the product GhLink over a scripted fake WebSocket server: hello and
// schema content, slot bookkeeping, the unknown_ref refusal, the error path,
// a same-rid re-send, and the frames the receive path answers for a version
// mismatch and an oversized frame (P5-4). GhLink only needs Grasshopper types
// for its solve trigger; a component that is not on a GH_Document makes
// OnPingDocument() return null, so no GH solve machinery (and no Rhino) is
// exercised here. The connection, decoder, slots, notes and outbox are all the
// product's.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Grasshopper.Kernel;
using GhStack.Core;

namespace GhStack.Tests.Console
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
                Check("hello client", (string)Json.Get(hello, "client") == "GhStack 0.3.0",
                    "client=" + Json.Get(hello, "client"));
                Check("hello doc", (string)Json.Get(hello, "doc") == "(unsaved)",
                    "doc=" + Json.Get(hello, "doc"));
                Check("hello units", !string.IsNullOrEmpty((string)Json.Get(hello, "doc_units")),
                    "doc_units=" + Json.Get(hello, "doc_units"));
                Check("hello stages empty", ((List<object>)Json.Get(hello, "stages")).Count == 0, "");

                // 2. a request before any component: no slot, a note, no growth.
                server.Send(Frame(Header("request", "k0", "r0"), null));
                DateTime noteDeadline = DateTime.UtcNow.AddSeconds(2);
                while (DateTime.UtcNow < noteDeadline && !HasNote("no GH Stack component"))
                {
                    CollectNotes();
                    Thread.Sleep(20);
                }
                Check("foreign request makes no slot", GhLink.FindSlot("k0") == null, "slot exists");

                // 3. components register and publish; hello follows.
                var stage = new TestComponent();
                GhLink.Register(stage);
                GhLink.PublishStage(stage, "k1", true);
                GhLink.Publish(null);
                hello = ReadUntil(server, "hello");
                var stages = (List<object>)Json.Get(hello, "stages");
                bool claimed = false;
                foreach (object entry in stages)
                {
                    var row = (Dictionary<string, object>)entry;
                    if ((string)Json.Get(row, "key") == "k1" && (bool)Json.Get(row, "enabled"))
                        claimed = true;
                }
                Check("hello carries the published stage", claimed, "stages=" + Json.WriteCanonical(stages));

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
                //    come back to waiting and keep answering that rid (P0-4's
                //    GH-side half).
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
                //     the connection (P3-1).
                GhLink.Unregister(stage);
                Check("last unregister closes the socket", server.DrainClosed(2000), "");
                GhLink.Stop();  // idempotent

                CollectNotes();
                Check("note about the pre-component request",
                    HasNote("no GH Stack component"), string.Join(" | ", Notes));
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

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: link round-trip PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: link round-trip FAIL (" + _failures + ")");
            return 1;
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
                // its catch path, which is the path that used to leak the sender.
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
                : base("Link Test", "Link Test", "test double", "GH Stack", "Tests")
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

        /// <summary>A one-client WebSocket server that speaks just enough RFC
        /// 6455 for MiniWsClient: the HTTP upgrade, unmasked text/binary reads,
        /// unmasked replies. Scripted by the test on the main thread.</summary>
        private sealed class FakeServer : IDisposable
        {
            private readonly TcpListener _listener;
            private TcpClient _client;
            private NetworkStream _stream;

            public FakeServer()
            {
                _listener = new TcpListener(IPAddress.Loopback, 0);
                _listener.Start();
            }

            public int Port
            {
                get { return ((IPEndPoint)_listener.LocalEndpoint).Port; }
            }

            public void Accept()
            {
                _client = _listener.AcceptTcpClient();
                _client.NoDelay = true;
                _stream = _client.GetStream();
                string line = ReadLine();
                while (!string.IsNullOrEmpty(line)) line = ReadLine();
                byte[] response = Encoding.ASCII.GetBytes(
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    + "Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n");
                _stream.Write(response, 0, response.Length);
                _stream.Flush();
            }

            public void ResetClient()
            {
                try
                {
                    if (_client != null)
                        _client.Client.LingerState = new LingerOption(true, 0);
                }
                catch (Exception) { }
                try { if (_client != null) _client.Client.Close(); } catch (Exception) { }
                try { if (_stream != null) _stream.Dispose(); } catch (Exception) { }
                _client = null;
                _stream = null;
            }

            public void Send(byte[] payload)
            {
                var ms = new MemoryStream();
                int len = payload.Length;
                ms.WriteByte(0x82);
                if (len < 126)
                {
                    ms.WriteByte((byte)len);
                }
                else if (len <= 0xFFFF)
                {
                    ms.WriteByte(126);
                    ms.WriteByte((byte)((len >> 8) & 0xFF));
                    ms.WriteByte((byte)(len & 0xFF));
                }
                else
                {
                    ms.WriteByte(127);
                    long l = len;
                    for (int i = 7; i >= 0; i--) ms.WriteByte((byte)((l >> (8 * i)) & 0xFF));
                }
                ms.Write(payload, 0, payload.Length);
                byte[] raw = ms.ToArray();
                _stream.Write(raw, 0, raw.Length);
                _stream.Flush();
            }

            /// <summary>One whole message, or null when the window passes. On
            /// EOF `closed` is true; on a quiet socket it stays false.</summary>
            public byte[] TryReadFrame(int timeoutMs, out bool closed)
            {
                closed = false;
                if (!_client.Client.Poll(timeoutMs * 1000, SelectMode.SelectRead)) return null;
                try
                {
                    int b0 = _stream.ReadByte();
                    int b1 = _stream.ReadByte();
                    if (b0 < 0 || b1 < 0) { closed = true; return null; }
                    int opcode = b0 & 0x0F;
                    bool masked = (b1 & 0x80) != 0;
                    long len = b1 & 0x7F;
                    if (len == 126)
                    {
                        len = ((long)_stream.ReadByte() << 8) | (byte)_stream.ReadByte();
                    }
                    else if (len == 127)
                    {
                        len = 0;
                        for (int i = 0; i < 8; i++) len = (len << 8) | (byte)_stream.ReadByte();
                    }
                    byte[] mask = new byte[4];
                    if (masked) for (int i = 0; i < 4; i++) mask[i] = (byte)_stream.ReadByte();
                    var body = new byte[len];
                    int read = 0;
                    while (read < len)
                    {
                        int n = _stream.Read(body, read, (int)(len - read));
                        if (n <= 0) { closed = true; return null; }
                        read += n;
                    }
                    if (masked) for (int i = 0; i < len; i++) body[i] ^= mask[i % 4];
                    if (opcode == 0x8) { closed = true; return null; }
                    if (opcode == 0x9) return null;  // ping: not expected from this client
                    return body;
                }
                catch (IOException)
                {
                    closed = true;
                    return null;
                }
                catch (SocketException)
                {
                    closed = true;
                    return null;
                }
            }

            /// <summary>True when the peer closes within the window.</summary>
            public bool DrainClosed(int timeoutMs)
            {
                DateTime end = DateTime.UtcNow.AddMilliseconds(timeoutMs);
                while (DateTime.UtcNow < end)
                {
                    bool closed;
                    TryReadFrame(200, out closed);
                    if (closed) return true;
                }
                return false;
            }

            private string ReadLine()
            {
                var sb = new StringBuilder();
                while (true)
                {
                    int b = _stream.ReadByte();
                    if (b < 0) return sb.Length > 0 ? sb.ToString() : null;
                    sb.Append((char)b);
                    if (sb.Length >= 2 && sb[sb.Length - 2] == '\r' && sb[sb.Length - 1] == '\n')
                        return sb.ToString(0, sb.Length - 2);
                }
            }

            public void Dispose()
            {
                try { if (_stream != null) _stream.Dispose(); } catch { }
                try { if (_client != null) _client.Close(); } catch { }
                try { _listener.Stop(); } catch { }
            }
        }
    }
}
