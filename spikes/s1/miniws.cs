// The spike wire-frame helpers. The WebSocket client itself lives in the
// product source (grasshopper/src/gh/MiniWsClient.cs); the build scripts compile
// that file into the spike and into the pre-flight, so WsTest.exe exercises the
// real client instead of a copy (a copy once hid a ping-handling bug).

using System;
using System.IO;
using System.Text;

namespace GhStack
{
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
