// A minimal WebSocket client for the GH Stack link: one binary message in each
// direction, loopback. RFC 6455 framing with client-side masking.
//
// This file has no Grasshopper references, so it can be built and run headlessly
// (spikes/s1/build-wstest.sh) without Rhino. Findings about Mono and
// ClientWebSocket are recorded in GhStackSpike.cs.

using System;
using System.IO;
using System.Net.Sockets;
using System.Text;

namespace GhStack
{
    public class MiniWsClient : IDisposable
    {
        private readonly string _host;
        private readonly int _port;
        private readonly string _path;
        private TcpClient _tcp;
        private NetworkStream _stream;
        // Serialises frame writes: SendFrame is called by the sender thread
        // (responses) and by the socket thread (pongs).
        private readonly object _writeLock = new object();

        public MiniWsClient(string endpoint)
        {
            Uri uri = new Uri(endpoint);
            if (uri.Scheme != "ws")
                throw new ArgumentException("only ws:// is supported by this spike client");
            _host = uri.Host;
            _port = uri.Port;
            _path = uri.PathAndQuery;
        }

        public void Connect()
        {
            _tcp = new TcpClient();
            _tcp.NoDelay = true;
            _tcp.Connect(_host, _port);
            _stream = _tcp.GetStream();

            byte[] key = new byte[16];
            new Random().NextBytes(key);
            string b64 = Convert.ToBase64String(key);
            string request =
                "GET " + _path + " HTTP/1.1\r\n" +
                "Host: " + _host + ":" + _port + "\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Key: " + b64 + "\r\n" +
                "Sec-WebSocket-Version: 13\r\n\r\n";
            byte[] head = Encoding.ASCII.GetBytes(request);
            _stream.Write(head, 0, head.Length);
            _stream.Flush();

            string response = ReadLine();
            if (response == null || response.IndexOf("101", StringComparison.Ordinal) < 0)
                throw new IOException("handshake failed: " + (response ?? "no response"));

            string line;
            while ((line = ReadLine()) != null && line.Length > 0)
            {
                // Headers such as Sec-WebSocket-Accept are not verified by this client.
            }
        }

        /// <summary>Sends one binary (opcode 2) WebSocket message.</summary>
        public void SendBinary(byte[] payload)
        {
            SendFrame(0x2, payload);
        }

        /// <summary>One masked frame, client-to-server.</summary>
        /// <remarks>Written from two threads: the sender thread for responses,
        /// and the socket thread for pongs. NetworkStream is not thread safe, so
        /// a pong written while a large frame was half-sent would interleave
        /// with it and corrupt the stream for the rest of the connection — the
        /// server would then see a malformed frame and close. The lock serialises
        /// the two; a pong waits at most for the frame currently being written.</remarks>
        private void SendFrame(int opcode, byte[] payload)
        {
            var ms = new MemoryStream();
            int len = payload.Length;
            ms.WriteByte((byte)(0x80 | (opcode & 0x0F))); // FIN + opcode
            if (len < 126)
            {
                ms.WriteByte((byte)(0x80 | len));
            }
            else if (len <= 0xFFFF)
            {
                ms.WriteByte((byte)(0x80 | 126));
                ms.WriteByte((byte)((len >> 8) & 0xFF));
                ms.WriteByte((byte)(len & 0xFF));
            }
            else
            {
                ms.WriteByte((byte)(0x80 | 127));
                // Shift a long, not an int: C# masks int shift counts to 5 bits,
                // so (len >> 32) on an int would silently be (len >> 0) and the
                // 8-byte length would repeat its low half.
                long l = len;
                for (int i = 7; i >= 0; i--) ms.WriteByte((byte)((l >> (8 * i)) & 0xFF));
            }
            byte[] mask = new byte[4];
            new Random().NextBytes(mask);
            ms.Write(mask, 0, 4);
            for (int i = 0; i < len; i++) ms.WriteByte((byte)(payload[i] ^ mask[i % 4]));
            byte[] frame = ms.ToArray();
            lock (_writeLock)
            {
                _stream.Write(frame, 0, frame.Length);
                _stream.Flush();
            }
        }

        /// <summary>Returns one binary message, or null on close/error.
        ///
        /// Control frames (ping, pong, close) and fragmentation are handled here:
        /// Blender pings every 2 s (protocol §2), and the ping payload must never
        /// reach the frame decoder — a ping body decoded as a protocol frame is
        /// exactly the "header length over the cap" failure. Pings are answered
        /// with pongs so the server's keepalive stays satisfied.</summary>
        public byte[] ReceiveBinary()
        {
            var message = new MemoryStream();
            bool fragmented = false;
            while (true)
            {
                int b0 = _stream.ReadByte();
                if (b0 < 0) return null;
                int b1 = _stream.ReadByte();
                if (b1 < 0) return null;
                bool fin = (b0 & 0x80) != 0;
                int opcode = b0 & 0x0F;
                bool masked = (b1 & 0x80) != 0;
                long len = b1 & 0x7F;
                if (len == 126)
                {
                    int hi = _stream.ReadByte(), lo = _stream.ReadByte();
                    if (hi < 0 || lo < 0) return null;
                    len = ((long)hi << 8) | (byte)lo;
                }
                else if (len == 127)
                {
                    // 64-bit length, big-endian. Python's websockets uses this for
                    // any payload over 64 KiB, which the 10k+ geometry frames exceed.
                    len = 0;
                    for (int i = 0; i < 8; i++)
                    {
                        int b = _stream.ReadByte();
                        if (b < 0) return null;
                        len = (len << 8) | (byte)b;
                    }
                }
                byte[] mask = new byte[4];
                if (masked)
                    for (int i = 0; i < 4; i++) mask[i] = (byte)_stream.ReadByte();
                byte[] body = ReadExactly(len);
                if (body == null) return null;
                if (masked)
                    for (int i = 0; i < len; i++) body[i] ^= mask[i % 4];

                if (opcode == 0x8) return null; // close
                if (opcode == 0x9) // ping: answer and keep reading
                {
                    SendFrame(0xA, body);
                    continue;
                }
                if (opcode == 0xA) continue; // pong

                if (opcode == 0x0) // continuation of a fragmented message
                {
                    if (!fragmented) continue; // stray continuation
                    message.Write(body, 0, body.Length);
                    if (fin) return message.ToArray();
                    continue;
                }

                // 0x1 text, 0x2 binary: the protocol is binary-only, but a text
                // frame is still a complete message; the decoder rejects it.
                if (fin) return body;
                fragmented = true;
                message = new MemoryStream();
                message.Write(body, 0, body.Length);
            }
        }

        private byte[] ReadExactly(long len)
        {
            var body = new byte[len];
            int read = 0;
            while (read < len)
            {
                int n = _stream.Read(body, read, (int)(len - read));
                if (n <= 0) return null;
                read += n;
            }
            return body;
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
            try { if (_tcp != null) _tcp.Close(); } catch { }
        }
    }
}
