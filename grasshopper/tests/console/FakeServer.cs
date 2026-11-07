// A scripted one-client WebSocket server shared by the console tests.

using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Stageloom.Tests.Console
{
    /// <summary>A one-client WebSocket server that speaks just enough RFC
    /// 6455 for MiniWsClient: the HTTP upgrade, unmasked text/binary reads,
    /// unmasked replies. Scripted by the test on the main thread.</summary>
    internal sealed class FakeServer : IDisposable
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
