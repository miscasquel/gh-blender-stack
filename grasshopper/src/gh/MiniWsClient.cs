// A minimal WebSocket client for the Stageloom link: one binary message in each
// direction, loopback. RFC 6455 framing with client-side masking.
//
// The peer is trusted only in the sense that it runs on loopback; it is still
// the Blender add-on, so a malformed or oversized frame must never allocate an
// unbounded buffer, wedge the socket thread, or escape as an exception. The
// handshake verifies the HTTP 101 status and Sec-WebSocket-Accept, the header
// read is bounded, and every frame is checked against the protocol caps before
// its payload is allocated.
//
// This file has no Grasshopper references, so it can be built and run headlessly
// (spikes/s1/build-wstest.sh and the MiniWs console suite) without Rhino.
// Findings about Mono and ClientWebSocket are recorded in StageloomSpike.cs.

using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using Stageloom.Core;

namespace Stageloom
{
    public class MiniWsClient : IDisposable
    {
        // The whole handshake (TCP connect, request write, status and headers)
        // must finish inside this window. Production uses the default; tests
        // pass a short value through the internal constructor.
        internal const int DefaultHandshakeTimeoutMs = 10000;

        // The HTTP response (status line plus headers) is small. Bound it so a
        // server that never sends a blank line cannot grow a StringBuilder
        // forever.
        internal const int MaxHandshakeHeaderBytes = 16 * 1024;

        private const string WebSocketGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
        private static readonly byte[] Empty = new byte[0];

        private readonly string _host;
        private readonly int _port;
        private readonly string _path;
        private readonly int _handshakeTimeoutMs;
        private TcpClient _tcp;
        private NetworkStream _stream;
        // Serialises frame writes: SendFrame is called by the sender thread
        // (responses) and by the socket thread (pongs and close echoes).
        private readonly object _writeLock = new object();
        // One RandomNumberGenerator for every mask and handshake key. A new
        // Random per frame can repeat the same seed (and therefore mask) when
        // frames are sent close together; crypto also removes the shared
        // Random thread-safety question.
        private readonly object _randomLock = new object();
        private readonly RandomNumberGenerator _random = RandomNumberGenerator.Create();
        private bool _disposed;
        // Set once this side has sent or received a close, or the stream died.
        // Reads happen on the socket thread; writes are under _writeLock where
        // they race with a sender, so volatile keeps the flag visible.
        private volatile bool _closed;

        public MiniWsClient(string endpoint)
            : this(endpoint, DefaultHandshakeTimeoutMs)
        {
        }

        /// <summary>Test seam: a short handshake timeout without changing the
        /// production default. Not part of the plugin's transport surface.</summary>
        internal MiniWsClient(string endpoint, int handshakeTimeoutMs)
        {
            if (endpoint == null) throw new ArgumentNullException("endpoint");
            if (handshakeTimeoutMs <= 0)
                throw new ArgumentOutOfRangeException("handshakeTimeoutMs");

            Uri uri = new Uri(endpoint);
            if (uri.Scheme != "ws")
                throw new ArgumentException("only ws:// is supported by this spike client");
            _host = uri.Host;
            _port = uri.Port;
            _path = uri.PathAndQuery;
            _handshakeTimeoutMs = handshakeTimeoutMs;
        }

        public void Connect()
        {
            if (_stream != null) throw new InvalidOperationException("already connected");

            _tcp = new TcpClient();
            _tcp.NoDelay = true;
            // Connect has no timeout of its own. Wait on the async result so a
            // stalled peer (or a vanished loopback listener) cannot hold the
            // socket thread forever; Dispose from Stop closes the socket and
            // releases this wait early.
            IAsyncResult pending = _tcp.BeginConnect(_host, _port, null, null);
            try
            {
                if (!pending.AsyncWaitHandle.WaitOne(_handshakeTimeoutMs))
                {
                    try { _tcp.Close(); } catch (Exception) { }
                    throw new IOException("connect to " + _host + ":" + _port + " timed out");
                }
                _tcp.EndConnect(pending);
            }
            finally
            {
                try { pending.AsyncWaitHandle.Close(); } catch (Exception) { }
            }
            _stream = _tcp.GetStream();
            // Bound writes too: a peer that stops reading must not wedge the
            // sender or the socket thread inside a pong. Reads use the same
            // budget only for the handshake; ReceiveBinary blocks normally.
            try { _stream.WriteTimeout = _handshakeTimeoutMs; } catch (Exception) { }

            DateTime deadline = DateTime.UtcNow.AddMilliseconds(_handshakeTimeoutMs);
            int headerBytes = 0;

            byte[] key = new byte[16];
            FillRandom(key);
            string keyText = Convert.ToBase64String(key);
            string request =
                "GET " + _path + " HTTP/1.1\r\n" +
                "Host: " + _host + ":" + _port + "\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Key: " + keyText + "\r\n" +
                "Sec-WebSocket-Version: 13\r\n\r\n";
            byte[] head = Encoding.ASCII.GetBytes(request);
            _stream.Write(head, 0, head.Length);
            _stream.Flush();

            string status = ReadHandshakeLine(deadline, ref headerBytes);
            if (status == null)
                throw new IOException("handshake failed: no response");
            if (!IsSwitchingProtocols(status))
                throw new IOException("handshake failed: " + status);

            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            while (true)
            {
                string line = ReadHandshakeLine(deadline, ref headerBytes);
                if (line == null)
                    throw new IOException("handshake failed: truncated headers");
                if (line.Length == 0) break;

                int colon = line.IndexOf(':');
                if (colon <= 0)
                    throw new IOException("handshake failed: malformed header");
                string name = line.Substring(0, colon).Trim();
                string value = line.Substring(colon + 1).Trim();
                if (name.Length == 0)
                    throw new IOException("handshake failed: malformed header");
                // Last value wins for duplicates; the peer sends one of each.
                headers[name] = value;
            }

            string upgrade;
            if (!headers.TryGetValue("Upgrade", out upgrade) || !HasToken(upgrade, "websocket"))
                throw new IOException("handshake failed: missing Upgrade: websocket");

            string connection;
            if (!headers.TryGetValue("Connection", out connection) || !HasToken(connection, "Upgrade"))
                throw new IOException("handshake failed: missing Connection: Upgrade");

            string accept;
            if (!headers.TryGetValue("Sec-WebSocket-Accept", out accept))
                throw new IOException("handshake failed: missing Sec-WebSocket-Accept");
            string expected = ComputeAccept(keyText);
            if (!string.Equals(accept, expected, StringComparison.Ordinal))
                throw new IOException("handshake failed: bad Sec-WebSocket-Accept");

            // From here on reads block until a frame arrives or the socket is
            // closed; only the handshake has a deadline.
            try { _stream.ReadTimeout = System.Threading.Timeout.Infinite; } catch (Exception) { }
        }

        /// <summary>Sends one binary (opcode 2) WebSocket message.</summary>
        public void SendBinary(byte[] payload)
        {
            if (payload == null) throw new ArgumentNullException("payload");
            SendFrame(0x2, payload);
        }

        /// <summary>One masked frame, client-to-server.</summary>
        /// <remarks>Written from two threads: the sender thread for responses,
        /// and the socket thread for pongs/close echoes. NetworkStream is not
        /// thread safe, so the whole wire write is serialised; a pong waits at
        /// most for the frame currently being written and cannot interleave
        /// with it.</remarks>
        private void SendFrame(int opcode, byte[] payload)
        {
            byte[] frame = BuildFrame(opcode, payload);
            lock (_writeLock)
            {
                if (_closed) throw new IOException("websocket is closed");
                WriteAll(frame);
            }
        }

        /// <summary>Builds one complete client frame in a single buffer. The
        /// payload is copied exactly once, into the masked frame; nothing
        /// mutates or holds a second full copy of the caller's array.</summary>
        private byte[] BuildFrame(int opcode, byte[] payload)
        {
            if (payload == null) payload = Empty;
            int len = payload.Length;
            if ((long)len > FrameCodec.MaxMessageBytes)
                throw new IOException(
                    "frame is " + len + " bytes, over the " + FrameCodec.MaxMessageBytes + " cap");

            int extra = 0;
            if (len >= 126 && len <= 0xFFFF) extra = 2;
            else if (len > 0xFFFF) extra = 8;
            byte[] frame = new byte[2 + extra + 4 + len];
            int p = 0;
            frame[p++] = (byte)(0x80 | (opcode & 0x0F)); // FIN + opcode
            if (len < 126)
            {
                frame[p++] = (byte)(0x80 | len);
            }
            else if (len <= 0xFFFF)
            {
                frame[p++] = (byte)(0x80 | 126);
                frame[p++] = (byte)((len >> 8) & 0xFF);
                frame[p++] = (byte)(len & 0xFF);
            }
            else
            {
                frame[p++] = (byte)(0x80 | 127);
                // Shift a long, not an int: C# masks int shift counts to 5 bits,
                // so (len >> 32) on an int would silently be (len >> 0) and the
                // 8-byte length would repeat its low half.
                long l = len;
                for (int i = 7; i >= 0; i--) frame[p++] = (byte)((l >> (8 * i)) & 0xFF);
            }

            byte[] mask = new byte[4];
            FillRandom(mask);
            frame[p++] = mask[0];
            frame[p++] = mask[1];
            frame[p++] = mask[2];
            frame[p++] = mask[3];
            for (int i = 0; i < len; i++)
                frame[p + i] = (byte)(payload[i] ^ mask[i & 3]);
            return frame;
        }

        /// <summary>Returns one binary message, or null on close/error.
        ///
        /// Control frames (ping, pong, close) and fragmentation are handled here:
        /// Blender pings every 5 s (protocol §2), and the ping payload must never
        /// reach the frame decoder — a ping body decoded as a protocol frame is
        /// exactly the "header length over the cap" failure. Pings are answered
        /// with pongs so the server's keepalive stays satisfied. Every malformed
        /// or oversized frame is answered with a close frame and returns null;
        /// no protocol failure escapes as an exception.</summary>
        public byte[] ReceiveBinary()
        {
            if (_closed) return null;
            try
            {
                return ReceiveCore();
            }
            catch (ObjectDisposedException)
            {
                // Stop disposes the client to unblock this thread.
                return FinishClosed();
            }
            catch (IOException)
            {
                // A close/EOF is handled inside ReceiveCore. An I/O failure
                // while the socket is still open is a broken connection; keep
                // the old contract and let GhLink note and reconnect. Only a
                // local close is swallowed.
                if (_closed) return FinishClosed();
                throw;
            }
            catch (SocketException)
            {
                if (_closed) return FinishClosed();
                throw;
            }
        }

        private byte[] ReceiveCore()
        {
            var message = new MemoryStream();
            bool fragmented = false;
            while (true)
            {
                if (_closed) return null;
                int b0 = _stream.ReadByte();
                if (b0 < 0) return FinishClosed();
                int b1 = _stream.ReadByte();
                if (b1 < 0) return FinishClosed();

                bool fin = (b0 & 0x80) != 0;
                int opcode = b0 & 0x0F;
                bool masked = (b1 & 0x80) != 0;
                int lenCode = b1 & 0x7F;

                if ((b0 & 0x70) != 0)
                    return Fail(1002, "reserved bits set");
                if (!IsValidOpcode(opcode))
                    return Fail(1002, "unknown opcode " + opcode);
                if (masked)
                    return Fail(1002, "server frame is masked");

                bool control = opcode >= 0x8;
                if (control)
                {
                    if (!fin)
                        return Fail(1002, "fragmented control frame");
                    if (lenCode > 125)
                        return Fail(1002, "control frame over 125 bytes");
                }
                else if (opcode == 0x0)
                {
                    if (!fragmented)
                        return Fail(1002, "continuation without a fragmented message");
                }
                else if (fragmented)
                {
                    return Fail(1002, "new data frame inside a fragmented message");
                }

                long len;
                if (lenCode < 126)
                {
                    len = lenCode;
                }
                else if (lenCode == 126)
                {
                    int hi = _stream.ReadByte();
                    int lo = _stream.ReadByte();
                    if (hi < 0 || lo < 0) return FinishClosed();
                    len = ((long)hi << 8) | (byte)lo;
                }
                else
                {
                    // 64-bit length, big-endian. The most significant bit must
                    // be zero (RFC 6455 §5.2); otherwise the value does not fit
                    // a signed 64-bit length and is rejected before any cast.
                    len = 0;
                    for (int i = 0; i < 8; i++)
                    {
                        int b = _stream.ReadByte();
                        if (b < 0) return FinishClosed();
                        if (i == 0 && (b & 0x80) != 0)
                            return Fail(1002, "invalid 64-bit length");
                        len = (len << 8) | (byte)b;
                    }
                }

                // The cap is checked against the declared length, before
                // ReadExactly allocates a buffer of that size. A frame over the
                // cap never reaches an array allocation.
                if (!control)
                {
                    if (len > FrameCodec.MaxMessageBytes)
                        return Fail(1009, "frame over the " + FrameCodec.MaxMessageBytes + "-byte cap");
                    if (opcode == 0x0 && (long)message.Length + len > FrameCodec.MaxMessageBytes)
                        return Fail(1009, "fragmented message over the " + FrameCodec.MaxMessageBytes + "-byte cap");
                    if (len > int.MaxValue)
                        return Fail(1009, "frame length does not fit an int");
                }

                byte[] body;
                if (!ReadExactly((int)len, out body)) return FinishClosed();

                if (opcode == 0x8)
                {
                    // A close body is either empty or a 2-byte code plus a
                    // UTF-8 reason; one byte is malformed. Echo the peer's body
                    // (the RFC close handshake) and report a clean end.
                    if (body.Length == 1)
                        return Fail(1002, "invalid close frame");
                    return EchoClose(body);
                }
                if (opcode == 0x9)
                {
                    // Ping: answer with the same payload and keep reading. The
                    // write lock keeps the pong from interleaving with a frame
                    // the sender thread is writing.
                    if (!TrySend(0xA, body)) return FinishClosed();
                    continue;
                }
                if (opcode == 0xA) continue; // pong

                if (opcode == 0x0)
                {
                    message.Write(body, 0, body.Length);
                    if (fin) return message.ToArray();
                    continue;
                }

                // 0x1 text, 0x2 binary: the protocol is binary-only, but a text
                // frame is still a complete message; the decoder rejects it.
                if (fin) return body;
                message.Write(body, 0, body.Length);
                fragmented = true;
            }
        }

        private static bool IsValidOpcode(int opcode)
        {
            return opcode == 0x0 || opcode == 0x1 || opcode == 0x2
                || opcode == 0x8 || opcode == 0x9 || opcode == 0xA;
        }

        private bool ReadExactly(int len, out byte[] body)
        {
            body = new byte[len];
            int read = 0;
            while (read < len)
            {
                int n = _stream.Read(body, read, len - read);
                if (n <= 0)
                {
                    body = null;
                    return false;
                }
                read += n;
            }
            return true;
        }

        /// <summary>Best-effort pong/close write from the receive path.</summary>
        private bool TrySend(int opcode, byte[] payload)
        {
            byte[] frame;
            try { frame = BuildFrame(opcode, payload); }
            catch (Exception) { return false; }

            lock (_writeLock)
            {
                if (_closed) return false;
                try
                {
                    WriteAll(frame);
                    return true;
                }
                catch (Exception)
                {
                    _closed = true;
                    return false;
                }
            }
        }

        /// <summary>Send a close frame with a code/reason and end the message
        /// stream. Failures are swallowed: the connection is already being
        /// abandoned, and the caller only needs the null return.</summary>
        private byte[] Fail(ushort code, string reason)
        {
            byte[] payload = ClosePayload(code, reason);
            byte[] frame = null;
            try { frame = BuildFrame(0x8, payload); }
            catch (Exception) { }

            lock (_writeLock)
            {
                if (_closed) return null;
                _closed = true;
                if (frame != null)
                {
                    try { WriteAll(frame); } catch (Exception) { }
                }
            }
            return null;
        }

        /// <summary>Echo the peer's close body, then return null. No exception
        /// escapes when the peer closes the connection.</summary>
        private byte[] EchoClose(byte[] body)
        {
            byte[] frame = null;
            try { frame = BuildFrame(0x8, body); }
            catch (Exception) { }

            lock (_writeLock)
            {
                if (_closed) return null;
                _closed = true;
                if (frame != null)
                {
                    try { WriteAll(frame); } catch (Exception) { }
                }
            }
            return null;
        }

        private static byte[] ClosePayload(ushort code, string reason)
        {
            byte[] reasonBytes = Empty;
            if (!string.IsNullOrEmpty(reason))
            {
                byte[] all = Encoding.UTF8.GetBytes(reason);
                int n = Math.Min(all.Length, 123);
                reasonBytes = new byte[n];
                Buffer.BlockCopy(all, 0, reasonBytes, 0, n);
            }
            byte[] payload = new byte[2 + reasonBytes.Length];
            payload[0] = (byte)(code >> 8);
            payload[1] = (byte)(code & 0xFF);
            Buffer.BlockCopy(reasonBytes, 0, payload, 2, reasonBytes.Length);
            return payload;
        }

        private byte[] FinishClosed()
        {
            _closed = true;
            return null;
        }

        private void WriteAll(byte[] frame)
        {
            if (_stream == null) throw new IOException("not connected");
            _stream.Write(frame, 0, frame.Length);
            _stream.Flush();
        }

        private void FillRandom(byte[] buffer)
        {
            lock (_randomLock)
            {
                if (_disposed) throw new ObjectDisposedException("MiniWsClient");
                _random.GetBytes(buffer);
            }
        }

        // ------------------------------------------------------- handshake I/O

        private static bool IsSwitchingProtocols(string line)
        {
            string[] parts = line.Split(new char[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length < 2) return false;
            if (!parts[0].StartsWith("HTTP/", StringComparison.Ordinal)) return false;
            return parts[1] == "101";
        }

        private static bool HasToken(string value, string token)
        {
            if (value == null) return false;
            string[] parts = value.Split(',');
            foreach (string part in parts)
            {
                if (string.Equals(part.Trim(), token, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            return false;
        }

        private static string ComputeAccept(string key)
        {
            byte[] hash;
            using (SHA1 sha1 = SHA1.Create())
                hash = sha1.ComputeHash(Encoding.ASCII.GetBytes(key + WebSocketGuid));
            return Convert.ToBase64String(hash);
        }

        private string ReadHandshakeLine(DateTime deadline, ref int used)
        {
            var sb = new StringBuilder(128);
            while (true)
            {
                int b = ReadHandshakeByte(deadline, ref used);
                if (b < 0) return sb.Length > 0 ? sb.ToString() : null;
                if (b == '\n')
                {
                    int n = sb.Length;
                    if (n > 0 && sb[n - 1] == '\r') n--;
                    return sb.ToString(0, n);
                }
                sb.Append((char)b);
            }
        }

        private int ReadHandshakeByte(DateTime deadline, ref int used)
        {
            if (used >= MaxHandshakeHeaderBytes)
                throw new IOException(
                    "handshake failed: response header over " + MaxHandshakeHeaderBytes + " bytes");
            used++;

            int remaining = (int)(deadline - DateTime.UtcNow).TotalMilliseconds;
            if (remaining <= 0)
                throw new IOException("handshake timed out");

            // Set the timeout to the time left, not a fresh full budget for
            // every byte: a slow-drip peer cannot extend the handshake past the
            // deadline one byte at a time.
            try { _stream.ReadTimeout = remaining; } catch (InvalidOperationException) { }
            return _stream.ReadByte();
        }

        public void Dispose()
        {
            // Do not take _writeLock here: Stop runs on the UI thread and a
            // sender may be blocked inside WriteAll. Closing the stream aborts
            // that write and unblocks the receive thread.
            _closed = true;
            try { if (_stream != null) _stream.Dispose(); } catch (Exception) { }
            try { if (_tcp != null) _tcp.Close(); } catch (Exception) { }
            lock (_randomLock)
            {
                if (!_disposed)
                {
                    _disposed = true;
                    try { _random.Dispose(); } catch (Exception) { }
                }
            }
        }
    }
}
