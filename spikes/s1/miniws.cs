// A minimal WebSocket client for the S1 spike: one binary message in each
// direction, small payloads, loopback. RFC 6455 framing with client-side masking.
//
// This file has no Grasshopper references, so it can be built and run headlessly
// (spikes/s1/build-wstest.sh) without Rhino. Findings about Mono and
// ClientWebSocket are recorded in GhStackSpike.cs.

using System;
using System.IO;
using System.Net.Sockets;
using System.Text;

namespace GhStack.Spike.S1
{
    public class MiniWsClient : IDisposable
    {
        private readonly string _host;
        private readonly int _port;
        private readonly string _path;
        private TcpClient _tcp;
        private NetworkStream _stream;

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
                // Headers such as Sec-WebSocket-Accept are not verified by the spike.
            }
        }

        public bool Connected
        {
            get { return _tcp != null && _tcp.Connected; }
        }

        /// <summary>Sends one binary (opcode 2) WebSocket message.</summary>
        public void SendBinary(byte[] payload)
        {
            var ms = new MemoryStream();
            int len = payload.Length;
            ms.WriteByte(0x82); // FIN + binary
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
                for (int i = 7; i >= 0; i--) ms.WriteByte((byte)((len >> (8 * i)) & 0xFF));
            }
            byte[] mask = new byte[4];
            new Random().NextBytes(mask);
            ms.Write(mask, 0, 4);
            for (int i = 0; i < len; i++) ms.WriteByte((byte)(payload[i] ^ mask[i % 4]));
            byte[] frame = ms.ToArray();
            _stream.Write(frame, 0, frame.Length);
            _stream.Flush();
        }

        /// <summary>Returns one binary message, or null on close/error.</summary>
        public byte[] ReceiveBinary()
        {
            int b0 = _stream.ReadByte();
            if (b0 < 0) return null;
            int b1 = _stream.ReadByte();
            if (b1 < 0) return null;
            int opcode = b0 & 0x0F;
            bool masked = (b1 & 0x80) != 0;
            int len = b1 & 0x7F;
            if (len == 126)
                len = (_stream.ReadByte() << 8) | _stream.ReadByte();
            else if (len == 127)
                throw new NotSupportedException("spike client: 64-bit lengths not handled");
            byte[] mask = new byte[4];
            if (masked)
                for (int i = 0; i < 4; i++) mask[i] = (byte)_stream.ReadByte();
            var body = new byte[len];
            int read = 0;
            while (read < len)
            {
                int n = _stream.Read(body, read, len - read);
                if (n == 0) return null;
                read += n;
            }
            if (masked)
                for (int i = 0; i < len; i++) body[i] ^= mask[i % 4];
            if (opcode == 0x8) return null; // close
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

    // ------------------------------------------------ spike wire frame helpers

    /// <summary>The spike frame: [u32 little-endian JSON length][UTF-8 JSON].</summary>
    public static class Wire
    {
        public static byte[] Encode(string json)
        {
            byte[] body = Encoding.UTF8.GetBytes(json);
            byte[] head = BitConverter.GetBytes(body.Length);
            var ms = new MemoryStream(4 + body.Length);
            ms.Write(head, 0, 4);
            ms.Write(body, 0, body.Length);
            return ms.ToArray();
        }

        public static string DecodeJson(byte[] frame)
        {
            if (frame.Length < 4) throw new FormatException("frame too short");
            int len = BitConverter.ToInt32(frame, 0);
            if (len <= 0 || 4 + len > frame.Length) throw new FormatException("bad frame length");
            return Encoding.UTF8.GetString(frame, 4, len);
        }
    }
}
