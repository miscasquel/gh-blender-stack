// Headless pre-flight for the MiniWsClient inside GhStackSpike.cs: connects to a
// Python echo server, receives a frame, sends one back. Built by build-wstest.sh.

using System;
using System.Text;

namespace GhStack.Spike.S1
{
    public static class WsTest
    {
        public static int Main(string[] args)
        {
            string endpoint = args.Length > 0 ? args[0] : "ws://127.0.0.1:9132/s1test";
            try
            {
                var client = new MiniWsClient(endpoint);
                client.Connect();
                Console.WriteLine("connected to " + endpoint);
                byte[] msg = client.ReceiveBinary();
                if (msg == null)
                {
                    Console.WriteLine("FAIL: no message received");
                    return 1;
                }
                string json = Encoding.UTF8.GetString(msg, 4, msg.Length - 4);
                Console.WriteLine("received " + msg.Length + " bytes: " + json);

                string reply = "{\"type\":\"response\",\"n\":1,\"solves\":1,\"msg\":\"from C#\"}";
                client.SendBinary(Wire.Encode(reply));
                Console.WriteLine("sent reply: " + reply);

                // Second message: over 64 KiB, so websockets uses a 64-bit length.
                msg = client.ReceiveBinary();
                if (msg == null)
                {
                    Console.WriteLine("FAIL: no big message received");
                    return 1;
                }
                Console.WriteLine("received big message: " + msg.Length + " bytes");
                client.SendBinary(Wire.Encode(reply));
                Console.WriteLine("sent big reply");
                client.Dispose();
                Console.WriteLine("RESULT: WsTest PASS");
                return 0;
            }
            catch (Exception ex)
            {
                Console.WriteLine("FAIL: " + ex.GetType().Name + ": " + ex.Message);
                return 1;
            }
        }
    }
}
