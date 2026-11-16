// Layer C, MiniWs transport: handshake verification, frame caps, fragmentation,
// control frames and close handling against an in-process scripted server.
//
//     StageloomMiniWsTests.exe
//
// No Rhino and no Grasshopper: the transport is pure managed code, so this
// suite runs under the wine runtime without a Rhino process. It is the direct
// test of the hardening that GhLink relies on: a bad or hostile frame must
// return null (after a best-effort close) rather than allocate, throw or leave
// the socket thread reading garbage.
//
// Exit code 0 all pass, 1 any failure.

using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

namespace Stageloom.Tests.Console
{
    public static class MiniWsTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            System.Console.WriteLine("mini websocket transport");

            HandshakeTests();
            FrameTests();
            LengthTests();
            FragmentationTests();
            ControlTests();
            ViolationTests();

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: mini websocket PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: mini websocket FAIL (" + _failures + ")");
            return 1;
        }

        // ------------------------------------------------------------ handshake

        private static void HandshakeTests()
        {
            // RFC 6455 §1.3's example key and accept value.
            Check("RFC accept example",
                FakeServer.AcceptFor("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
                FakeServer.AcceptFor("dGhlIHNhbXBsZSBub25jZQ=="));

            using (var server = new FakeServer())
            {
                string seenKey = null;
                ConnectAttempt attempt = Connect(server, 2000, delegate
                {
                    server.AcceptHandshakeOnly();
                    seenKey = server.ReadHandshakeRequest();
                    string accept = FakeServer.AcceptFor(seenKey);
                    // Lower-case header names, a second accept field in a
                    // different case and an unrelated extra header: all legal.
                    server.WriteHandshake(
                        "HTTP/1.1 101 Switching Protocols",
                        accept,
                        "server: stageloom-test\r\nSEC-WEBSOCKET-ACCEPT: " + accept + "\r\n");
                });
                Check("valid handshake connects",
                    attempt.Finished && attempt.Error == null && attempt.Client != null,
                    Describe(attempt));
                Check("client sent a key", !string.IsNullOrEmpty(seenKey), "key=" + seenKey);

                if (attempt.Client != null)
                {
                    // A frame after a handshake with mixed-case headers still
                    // travels both ways.
                    byte[] payload = Encoding.ASCII.GetBytes("after-handshake");
                    attempt.Client.SendBinary(payload);
                    int opcode;
                    bool fin, masked, closed;
                    byte[] got = server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed);
                    Check("send after handshake", Same(got, payload), "got=" + got);
                    attempt.Client.Dispose();
                }
            }

            // A status line whose reason phrase contains "101" must not pass:
            // the check is the status token, not a substring.
            using (var server = new FakeServer())
            {
                ConnectAttempt attempt = Connect(server, 2000, delegate
                {
                    server.AcceptHandshakeOnly();
                    string key = server.ReadHandshakeRequest();
                    server.WriteHandshake("HTTP/1.1 201 101 Switching Protocols",
                        FakeServer.AcceptFor(key));
                });
                Check("wrong status rejected despite 101 in reason",
                    FailedWith(attempt, "handshake failed"), Describe(attempt));
                DisposeAttempt(attempt);
            }

            using (var server = new FakeServer())
            {
                ConnectAttempt attempt = Connect(server, 2000, delegate
                {
                    server.AcceptHandshakeOnly();
                    server.ReadHandshakeRequest();
                    server.WriteHandshake("HTTP/1.1 101 Switching Protocols", null);
                });
                Check("missing accept rejected",
                    FailedWith(attempt, "Sec-WebSocket-Accept"), Describe(attempt));
                DisposeAttempt(attempt);
            }

            using (var server = new FakeServer())
            {
                ConnectAttempt attempt = Connect(server, 2000, delegate
                {
                    server.AcceptHandshakeOnly();
                    server.ReadHandshakeRequest();
                    server.WriteHandshake("HTTP/1.1 101 Switching Protocols",
                        FakeServer.AcceptFor("a-different-key"));
                });
                Check("wrong accept rejected",
                    FailedWith(attempt, "bad Sec-WebSocket-Accept"), Describe(attempt));
                DisposeAttempt(attempt);
            }

            using (var server = new FakeServer())
            {
                ConnectAttempt attempt = Connect(server, 2000, delegate
                {
                    server.AcceptHandshakeOnly();
                    string key = server.ReadHandshakeRequest();
                    server.WriteHandshake(
                        "HTTP/1.1 101 Switching Protocols",
                        FakeServer.AcceptFor(key),
                        "X-Fill: " + new string('a', 20 * 1024) + "\r\n");
                });
                Check("oversized response header rejected before it is read in full",
                    FailedWith(attempt, "response header over"), Describe(attempt));
                DisposeAttempt(attempt);
            }

            using (var server = new FakeServer())
            {
                var watch = Stopwatch.StartNew();
                ConnectAttempt attempt = Connect(server, 250, delegate
                {
                    server.AcceptHandshakeOnly();
                });
                watch.Stop();
                Check("stalled handshake times out",
                    attempt.Finished && attempt.Client == null && attempt.Error is IOException
                    && watch.ElapsedMilliseconds < 3000,
                    Describe(attempt) + " elapsed=" + watch.ElapsedMilliseconds + "ms");
                DisposeAttempt(attempt);
            }
        }

        // ---------------------------------------------------------------- frames

        private static void FrameTests()
        {
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;

                byte[] empty = new byte[0];
                server.SendFrame(0x2, true, 0, false, 0, empty);
                Check("zero-length binary message", Same(client.ReceiveBinary(), empty), "");

                byte[] small = Encoding.ASCII.GetBytes("one frame");
                server.SendFrame(0x2, true, 0, false, small.Length, small);
                Check("small binary message", Same(client.ReceiveBinary(), small), "");

                // Client-to-server masking and lengths. The server unmasks in
                // TryReadFrameRaw, so equal bodies prove both the mask bit and
                // the mask application.
                byte[] masked = Encoding.ASCII.GetBytes("client masked payload");
                client.SendBinary(masked);
                int opcode;
                bool fin, wasMasked, closed;
                byte[] got = server.TryReadFrameRaw(2000, out opcode, out fin, out wasMasked, out closed);
                Check("client frame is masked", wasMasked, "masked=" + wasMasked);
                Check("client payload survives masking", Same(got, masked), "got=" + got);
                Check("client data opcode", opcode == 0x2 && fin, "opcode=" + opcode + " fin=" + fin);
            }

            // The handshake's short read timeout must not leak into frame
            // reads: ReceiveBinary has to block for an idle peer, not wake up
            // as a broken connection every handshake-budget interval.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server, 250))
            {
                if (client == null) return;
                Thread.Sleep(600);
                byte[] afterIdle = Encoding.ASCII.GetBytes("after-idle");
                server.SendFrame(0x2, true, 0, false, afterIdle.Length, afterIdle);
                byte[] got = null;
                Exception error = null;
                try { got = client.ReceiveBinary(); }
                catch (Exception ex) { error = ex; }
                Check("handshake timeout does not leak into frame reads",
                    error == null && Same(got, afterIdle),
                    error == null ? "got=" + got
                        : error.GetType().Name + ": " + error.Message);
            }
        }

        private static void LengthTests()
        {
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] medium = Filled(300, 0x5A);       // 16-bit length
                byte[] large = Filled(70000, 0xC3);      // 64-bit length
                server.SendFrame(0x2, true, 0, false, medium.Length, medium);
                Check("16-bit server length", Same(client.ReceiveBinary(), medium), "");
                server.SendFrame(0x2, true, 0, false, large.Length, large);
                Check("64-bit server length", Same(client.ReceiveBinary(), large), "");
            }

            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] medium = Filled(300, 0x11);
                byte[] large = Filled(70000, 0x22);
                client.SendBinary(medium);
                int opcode;
                bool fin, masked, closed;
                Check("client 16-bit length",
                    Same(server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed), medium), "");
                client.SendBinary(large);
                Check("client 64-bit length",
                    Same(server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed), large), "");
            }
        }

        private static void FragmentationTests()
        {
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] first = Encoding.ASCII.GetBytes("frag-");
                byte[] second = Encoding.ASCII.GetBytes("mented");
                server.SendFrame(0x2, false, 0, false, first.Length, first);
                server.SendFrame(0x0, true, 0, false, second.Length, second);
                Check("fragmented binary joins",
                    Same(client.ReceiveBinary(), Encoding.ASCII.GetBytes("frag-mented")), "");
            }

            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] first = Encoding.ASCII.GetBytes("hel");
                byte[] second = Encoding.ASCII.GetBytes("lo");
                server.SendFrame(0x1, false, 0, false, first.Length, first);
                server.SendFrame(0x0, true, 0, false, second.Length, second);
                Check("fragmented text joins",
                    Same(client.ReceiveBinary(), Encoding.ASCII.GetBytes("hello")), "");
            }

            // A ping in the middle of a fragmented message is allowed and its
            // pong must not disturb the continuation state.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] first = Encoding.ASCII.GetBytes("a");
                byte[] ping = Encoding.ASCII.GetBytes("hi");
                byte[] second = Encoding.ASCII.GetBytes("b");
                byte[] all = Concat(
                    FakeServer.BuildFrame(0x1, false, 0, false, first.Length, first),
                    FakeServer.BuildFrame(0x9, true, 0, false, ping.Length, ping),
                    FakeServer.BuildFrame(0x0, true, 0, false, second.Length, second));
                server.SendRaw(all);
                Check("ping during fragmentation is answered and the message joins",
                    Same(client.ReceiveBinary(), Encoding.ASCII.GetBytes("ab")), "");

                int opcode;
                bool fin, masked, closed;
                byte[] pong = server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed);
                Check("interleaved ping pong", opcode == 0xA && Same(pong, ping),
                    "opcode=" + opcode + " body=" + pong);
            }

            // More than one continuation is a normal fragmented message.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x2, false, 0, false, 1, new byte[] { (byte)'x' });
                server.SendFrame(0x0, false, 0, false, 1, new byte[] { (byte)'y' });
                server.SendFrame(0x0, true, 0, false, 1, new byte[] { (byte)'z' });
                Check("three-part fragmented message",
                    Same(client.ReceiveBinary(), Encoding.ASCII.GetBytes("xyz")), "");
            }
        }

        private static void ControlTests()
        {
            // Ping gets a pong, then the following data message is returned.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] ping = Encoding.ASCII.GetBytes("ping-body");
                byte[] message = Encoding.ASCII.GetBytes("after-ping");
                server.SendRaw(Concat(
                    FakeServer.BuildFrame(0x9, true, 0, false, ping.Length, ping),
                    FakeServer.BuildFrame(0x2, true, 0, false, message.Length, message)));
                Check("ping then message", Same(client.ReceiveBinary(), message), "");

                int opcode;
                bool fin, masked, closed;
                byte[] pong = server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed);
                Check("pong echoes the ping body", opcode == 0xA && Same(pong, ping),
                    "opcode=" + opcode + " body=" + pong);
            }

            // An unsolicited pong is ignored.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] message = Encoding.ASCII.GetBytes("after-pong");
                server.SendFrame(0xA, true, 0, false, 4, Encoding.ASCII.GetBytes("pong"));
                server.SendFrame(0x2, true, 0, false, message.Length, message);
                byte[] got = client.ReceiveBinary();
                Check("unsolicited pong ignored", Same(got, message),
                    "expected=" + Encoding.ASCII.GetString(message)
                    + " got=" + (got == null ? "(null)" : Encoding.ASCII.GetString(got)));
            }

            // Close is echoed with the same body and yields a clean null.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] closeBody = new byte[] { 0x03, 0xE8, (byte)'b', (byte)'y', (byte)'e' };
                server.SendFrame(0x8, true, 0, false, closeBody.Length, closeBody);
                Check("close returns null", client.ReceiveBinary() == null, "");

                int opcode;
                bool fin, masked, closed;
                byte[] echo = server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed);
                Check("close echo carries the peer body",
                    opcode == 0x8 && Same(echo, closeBody), "opcode=" + opcode + " body=" + echo);
                Check("receive after close is null", client.ReceiveBinary() == null, "");
            }

            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x8, true, 0, false, 0, new byte[0]);
                Check("empty close returns null", client.ReceiveBinary() == null, "");
                int opcode;
                bool fin, masked, closed;
                byte[] echo = server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed);
                Check("empty close echoed", opcode == 0x8 && echo != null && echo.Length == 0,
                    "opcode=" + opcode + " body=" + echo);
            }
        }

        // ------------------------------------------------------------- failures

        private static void ViolationTests()
        {
            // Declared length over the frame cap: rejected before any payload
            // allocation. The test does not send the body; the old code would
            // have tried to allocate it and blocked or thrown.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                var watch = Stopwatch.StartNew();
                server.SendFrame(0x2, true, 0, false, 600L * 1024 * 1024, null);
                byte[] got = client.ReceiveBinary();
                watch.Stop();
                Check("oversized declared frame rejected without reading it",
                    got == null && watch.ElapsedMilliseconds < 2000,
                    "got=" + got + " elapsed=" + watch.ElapsedMilliseconds + "ms");
                Check("oversized frame closes with 1009", ReadsCloseCode(server, 1009), "");
            }

            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x2, false, 0, false, 4, Encoding.ASCII.GetBytes("part"));
                server.SendFrame(0x0, true, 0, false, 600L * 1024 * 1024, null);
                byte[] got = client.ReceiveBinary();
                Check("fragmented message over the cap rejected", got == null, "got=" + got);
                Check("fragmented cap closes with 1009", ReadsCloseCode(server, 1009), "");
            }

            // A 64-bit length with the top bit set is not a valid length.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                byte[] raw = new byte[] { 0x82, 127, 0x80, 0, 0, 0, 0, 0, 0, 0 };
                server.SendRaw(raw);
                Check("64-bit length with the high bit set rejected",
                    client.ReceiveBinary() == null, "");
                Check("bad 64-bit length closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // Stray continuation.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x0, true, 0, false, 1, new byte[] { 1 });
                Check("stray continuation rejected", client.ReceiveBinary() == null, "");
                Check("stray continuation closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // New data frame while a fragmented message is open.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x2, false, 0, false, 1, new byte[] { 1 });
                server.SendFrame(0x2, true, 0, false, 1, new byte[] { 2 });
                Check("new data frame inside fragmentation rejected",
                    client.ReceiveBinary() == null, "");
                Check("replacement data frame closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // RSV bits without a negotiated extension.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x2, true, 0x4, false, 1, new byte[] { 1 });
                Check("RSV bit rejected", client.ReceiveBinary() == null, "");
                Check("RSV closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // Reserved opcodes.
            foreach (int opcode in new int[] { 0x3, 0xB })
            {
                using (var server = new FakeServer())
                using (MiniWsClient client = Connected(server))
                {
                    if (client == null) continue;
                    server.SendFrame(opcode, true, 0, false, 0, new byte[0]);
                    Check("reserved opcode 0x" + opcode.ToString("X") + " rejected",
                        client.ReceiveBinary() == null, "");
                    Check("reserved opcode closes with 1002", ReadsCloseCode(server, 1002), "");
                }
            }

            // Fragmented control frame.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x9, false, 0, false, 1, new byte[] { 1 });
                Check("fragmented ping rejected", client.ReceiveBinary() == null, "");
                Check("fragmented control closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // Control frames cannot use a 16/64-bit length.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x9, true, 0, false, 126, null);
                Check("oversized control length rejected", client.ReceiveBinary() == null, "");
                Check("control length closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // One-byte close body is malformed.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x8, true, 0, false, 1, new byte[] { 0x03 });
                Check("one-byte close body rejected", client.ReceiveBinary() == null, "");
                Check("bad close closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            // Server frames must be unmasked.
            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x2, true, 0, true, 1, new byte[] { 1 });
                Check("masked server data frame rejected", client.ReceiveBinary() == null, "");
                Check("masked server frame closes with 1002", ReadsCloseCode(server, 1002), "");
            }

            using (var server = new FakeServer())
            using (MiniWsClient client = Connected(server))
            {
                if (client == null) return;
                server.SendFrame(0x9, true, 0, true, 1, new byte[] { 1 });
                Check("masked server ping rejected", client.ReceiveBinary() == null, "");
                Check("masked ping closes with 1002", ReadsCloseCode(server, 1002), "");
            }
        }

        // ------------------------------------------------------------- helpers

        private sealed class ConnectAttempt
        {
            public MiniWsClient Client;
            public Exception Error;
            public bool Finished;
        }

        /// <summary>Starts Connect on its own thread and runs the scripted
        /// server half on this one, since Connect would otherwise block the
        /// test thread before the server can answer.</summary>
        private static ConnectAttempt Connect(FakeServer server, int timeoutMs, Action handshake)
        {
            var attempt = new ConnectAttempt();
            var thread = new Thread(delegate()
            {
                var client = new MiniWsClient(Url(server.Port), timeoutMs);
                try
                {
                    client.Connect();
                    attempt.Client = client;
                }
                catch (Exception ex)
                {
                    attempt.Error = ex;
                    try { client.Dispose(); } catch (Exception) { }
                }
            });
            thread.IsBackground = true;
            thread.Start();
            try
            {
                handshake();
            }
            catch (Exception ex)
            {
                if (attempt.Error == null) attempt.Error = ex;
            }
            attempt.Finished = thread.Join(5000);
            return attempt;
        }

        private static MiniWsClient Connected(FakeServer server)
        {
            return Connected(server, 3000);
        }

        private static MiniWsClient Connected(FakeServer server, int timeoutMs)
        {
            ConnectAttempt attempt = Connect(server, timeoutMs, delegate { server.Accept(); });
            if (attempt.Client != null && attempt.Error == null) return attempt.Client;
            Check("connect for frame test", false, Describe(attempt));
            DisposeAttempt(attempt);
            return null;
        }

        private static bool FailedWith(ConnectAttempt attempt, string fragment)
        {
            return attempt.Finished
                && attempt.Client == null
                && attempt.Error is IOException
                && attempt.Error.Message.IndexOf(fragment, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static void DisposeAttempt(ConnectAttempt attempt)
        {
            if (attempt.Client != null)
            {
                try { attempt.Client.Dispose(); } catch (Exception) { }
            }
        }

        private static bool ReadsCloseCode(FakeServer server, int expected)
        {
            int opcode;
            bool fin, masked, closed;
            byte[] body = server.TryReadFrameRaw(2000, out opcode, out fin, out masked, out closed);
            if (body == null || opcode != 0x8) return false;
            if (expected < 0) return body.Length == 0;
            if (body.Length < 2) return false;
            return ((body[0] << 8) | body[1]) == expected;
        }

        private static string Url(int port)
        {
            return "ws://127.0.0.1:" + port + "/";
        }

        private static string Describe(ConnectAttempt attempt)
        {
            return "finished=" + attempt.Finished
                + " connected=" + (attempt.Client != null)
                + " error=" + (attempt.Error == null ? "(none)" : attempt.Error.GetType().Name + ": " + attempt.Error.Message);
        }

        private static bool Same(byte[] a, byte[] b)
        {
            if (a == null || b == null) return a == b;
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++)
                if (a[i] != b[i]) return false;
            return true;
        }

        private static byte[] Concat(params byte[][] parts)
        {
            int total = 0;
            foreach (byte[] part in parts) total += part.Length;
            var all = new byte[total];
            int p = 0;
            foreach (byte[] part in parts)
            {
                Buffer.BlockCopy(part, 0, all, p, part.Length);
                p += part.Length;
            }
            return all;
        }

        private static byte[] Filled(int length, byte value)
        {
            var data = new byte[length];
            for (int i = 0; i < length; i++) data[i] = value;
            return data;
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
    }
}
