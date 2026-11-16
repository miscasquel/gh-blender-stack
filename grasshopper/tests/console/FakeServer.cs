// A scripted one-client WebSocket server shared by the console tests.

using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;

namespace Stageloom.Tests.Console
{
    /// <summary>A one-client WebSocket server that speaks just enough RFC
    /// 6455 for MiniWsClient: the HTTP upgrade, unmasked text/binary reads,
    /// unmasked replies. Scripted by the test on the main thread.</summary>
    internal sealed class FakeServer : IDisposable
    {
        private const string WebSocketGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

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
            AcceptHandshakeOnly();
            CompleteHandshake();
        }

        /// <summary>Accept the TCP connection but leave the HTTP upgrade
        /// unanswered, so MiniWsClient.Connect blocks in the handshake.</summary>
        public void AcceptHandshakeOnly()
        {
            _client = _listener.AcceptTcpClient();
            _client.NoDelay = true;
            _stream = _client.GetStream();
        }

        /// <summary>Read the HTTP upgrade and answer 101 with the key's accept,
        /// completing a handshake deferred by <see cref="AcceptHandshakeOnly"/>.</summary>
        public void CompleteHandshake()
        {
            string key = ReadHandshakeRequest();
            WriteHandshake("HTTP/1.1 101 Switching Protocols", AcceptFor(key));
        }

        /// <summary>Read and discard the upgrade request, returning its
        /// Sec-WebSocket-Key (or null when the client sent none).</summary>
        public string ReadHandshakeRequest()
        {
            string key = null;
            string line = ReadLine();
            while (!string.IsNullOrEmpty(line))
            {
                int colon = line.IndexOf(':');
                if (colon > 0)
                {
                    string name = line.Substring(0, colon).Trim();
                    if (string.Equals(name, "Sec-WebSocket-Key", StringComparison.OrdinalIgnoreCase))
                        key = line.Substring(colon + 1).Trim();
                }
                line = ReadLine();
            }
            return key;
        }

        /// <summary>The RFC 6455 accept value for a client key.</summary>
        public static string AcceptFor(string key)
        {
            byte[] hash;
            using (SHA1 sha1 = SHA1.Create())
                hash = sha1.ComputeHash(Encoding.ASCII.GetBytes(key + WebSocketGuid));
            return Convert.ToBase64String(hash);
        }

        /// <summary>Answer an already-read upgrade with a custom status line and
        /// accept value (null omits the header). Extra headers are added as-is
        /// so a test can prove case-insensitive names and extra fields work.</summary>
        public void WriteHandshake(string statusLine, string accept)
        {
            WriteHandshake(statusLine, accept, null);
        }

        public void WriteHandshake(string statusLine, string accept, string extraHeaders)
        {
            var sb = new StringBuilder();
            sb.Append(statusLine).Append("\r\n");
            sb.Append("Upgrade: websocket\r\n");
            sb.Append("Connection: Upgrade\r\n");
            if (accept != null)
                sb.Append("Sec-WebSocket-Accept: ").Append(accept).Append("\r\n");
            if (extraHeaders != null)
                sb.Append(extraHeaders);
            sb.Append("\r\n");
            SendRaw(Encoding.ASCII.GetBytes(sb.ToString()));
        }

        public void WriteRaw(byte[] data)
        {
            _stream.Write(data, 0, data.Length);
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
            SendFrame(0x2, true, 0, false, payload.Length, payload);
        }

        public void SendRaw(byte[] data)
        {
            WriteRaw(data);
        }

        /// <summary>Writes one scripted frame. <paramref name="declaredLength"/>
        /// is encoded into the header, even when no payload follows it, so a
        /// test can declare an oversized frame without allocating one.</summary>
        public void SendFrame(int opcode, bool fin, int rsv, bool masked,
            long declaredLength, byte[] payload)
        {
            SendRaw(BuildFrame(opcode, fin, rsv, masked, declaredLength, payload));
        }

        public static byte[] BuildFrame(int opcode, bool fin, int rsv, bool masked,
            long declaredLength, byte[] payload)
        {
            var ms = new MemoryStream();
            byte b0 = (byte)((fin ? 0x80 : 0) | ((rsv & 0x07) << 4) | (opcode & 0x0F));
            ms.WriteByte(b0);
            if (declaredLength < 126)
            {
                ms.WriteByte((byte)((masked ? 0x80 : 0) | (int)declaredLength));
            }
            else if (declaredLength <= 0xFFFF)
            {
                ms.WriteByte((byte)((masked ? 0x80 : 0) | 126));
                ms.WriteByte((byte)((declaredLength >> 8) & 0xFF));
                ms.WriteByte((byte)(declaredLength & 0xFF));
            }
            else
            {
                ms.WriteByte((byte)((masked ? 0x80 : 0) | 127));
                long l = declaredLength;
                for (int i = 7; i >= 0; i--)
                    ms.WriteByte((byte)((l >> (8 * i)) & 0xFF));
            }

            byte[] body = payload ?? new byte[0];
            if (masked)
            {
                byte[] mask = new byte[] { 0x11, 0x22, 0x33, 0x44 };
                ms.Write(mask, 0, 4);
                for (int i = 0; i < body.Length; i++)
                    ms.WriteByte((byte)(body[i] ^ mask[i & 3]));
            }
            else
            {
                ms.Write(body, 0, body.Length);
            }
            return ms.ToArray();
        }

        /// <summary>One whole message, or null when the window passes. On
        /// EOF `closed` is true; on a quiet socket it stays false.</summary>
        public byte[] TryReadFrame(int timeoutMs, out bool closed)
        {
            int opcode;
            bool fin, masked;
            byte[] body = TryReadFrameRaw(timeoutMs, out opcode, out fin, out masked, out closed);
            if (body == null) return null;
            if (opcode == 0x8)
            {
                closed = true;
                return null;
            }
            if (opcode == 0x9 || opcode == 0xA) return null;
            return body;
        }

        /// <summary>One whole client frame, including control frames, with its
        /// opcode and mask bit. A zero-length body returns an empty array.</summary>
        public byte[] TryReadFrameRaw(int timeoutMs, out int opcode, out bool fin,
            out bool masked, out bool closed)
        {
            opcode = 0;
            fin = false;
            masked = false;
            closed = false;
            if (!_client.Client.Poll(timeoutMs * 1000, SelectMode.SelectRead))
                return null;
            try
            {
                int b0 = _stream.ReadByte();
                int b1 = _stream.ReadByte();
                if (b0 < 0 || b1 < 0)
                {
                    closed = true;
                    return null;
                }
                fin = (b0 & 0x80) != 0;
                opcode = b0 & 0x0F;
                masked = (b1 & 0x80) != 0;
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
                    if (n <= 0)
                    {
                        closed = true;
                        return null;
                    }
                    read += n;
                }
                if (masked) for (int i = 0; i < len; i++) body[i] ^= mask[i % 4];
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
