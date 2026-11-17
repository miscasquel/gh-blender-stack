// Socket worker, connection lifecycle and the bounded outbox. One worker per
// Start; a stopped or replaced worker can never revive, clear the next outbox,
// or publish on the next socket. One sender belongs to each connection. The
// outbox is a leaf so other owners can queue without the socket loop.

using System;
using System.Collections.Generic;
using System.Threading;
using Grasshopper.Kernel;
using Stageloom.Core;
using EndpointConfig = Stageloom.Core.Endpoint;

namespace Stageloom
{
    /// <summary>The two mutexes the link shares across owners. State serializes
    /// registries, slots, request context and publication; Lifecycle serializes
    /// Start/Stop/Configure without the lock a worker's exit path needs.</summary>
    internal static class LinkSync
    {
        internal static readonly object State = new object();
        internal static readonly object Lifecycle = new object();
    }

    /// <summary>The generation token of one socket worker. Under State.</summary>
    internal sealed class LinkWorker
    {
        internal volatile bool Running;
        private static LinkWorker _current;

        internal static LinkWorker BeginLocked()
        {
            var worker = new LinkWorker();
            worker.Running = true;
            _current = worker;
            return worker;
        }

        internal static LinkWorker CurrentLocked()
        {
            return _current;
        }

        internal static void ClearLocked()
        {
            _current = null;
        }

        internal static bool IsCurrentLocked(LinkWorker worker)
        {
            return worker != null && object.ReferenceEquals(_current, worker) && worker.Running;
        }

        internal static bool IsCurrent(LinkWorker worker)
        {
            lock (LinkSync.State) { return IsCurrentLocked(worker); }
        }
    }

    internal static class LinkOutbox
    {
        private static readonly Queue<byte[]> Outbox = new Queue<byte[]>();
        private static long OutboxBytes = 0;
        private const long MaxOutboxBytes = 64L * 1024 * 1024;
        private static readonly AutoResetEvent OutboxSignal = new AutoResetEvent(false);

        internal static AutoResetEvent Signal { get { return OutboxSignal; } }

        internal static void Enqueue(byte[] frame)
        {
            lock (LinkSync.State)
            {
                Outbox.Enqueue(frame);
                OutboxBytes += frame.Length;
                // Drop the oldest, but never the newest: a single oversized
                // frame (up to 512 MB) must still be able to go out on a
                // healthy link.
                while (Outbox.Count > 1 && OutboxBytes > MaxOutboxBytes)
                {
                    byte[] dropped = Outbox.Dequeue();
                    OutboxBytes -= dropped.Length;
                }
            }
            OutboxSignal.Set();
        }

        internal static bool TryDequeueLocked(out byte[] frame)
        {
            if (Outbox.Count == 0)
            {
                frame = null;
                return false;
            }
            frame = Outbox.Dequeue();
            OutboxBytes -= frame.Length;
            return true;
        }

        internal static void ClearLocked()
        {
            Outbox.Clear();
            OutboxBytes = 0;
        }

        internal static void Wake()
        {
            OutboxSignal.Set();
        }
    }

    internal static class LinkConnection
    {
        private static volatile string _endpoint;
        private static volatile int _endpointPort;

        internal static string Endpoint { get { return _endpoint; } }
        internal static int Port { get { return _endpointPort; } }

        static LinkConnection()
        {
            int persisted = 0;
            string warning;
            if (!EndpointSettings.TryLoadPort(out persisted, out warning)
                && !string.IsNullOrEmpty(warning))
                LinkNotes.Add(warning);

            EndpointSource source;
            _endpointPort = EndpointConfig.ResolvePort(
                persisted,
                Environment.GetEnvironmentVariable(EndpointConfig.EnvironmentVariable),
                out source, out warning);
            _endpoint = EndpointConfig.Canonical(_endpointPort);
            if (!string.IsNullOrEmpty(warning)) LinkNotes.Add(warning);
            // Keep the startup choice in the diagnostics: "which port did the
            // plugin actually read?" is the first question a mismatch raises.
            LinkNotes.Add("endpoint " + _endpoint + " (" + source + ")");
        }

        private static Thread _thread;       // under State
        private static volatile bool _connected;
        private static MiniWsClient _client; // under State; Stop disposes it
        private static int _activeSenders;

        // Test/diagnostic: one sender loop per connection.
        internal static int ActiveSenders
        {
            get { return Interlocked.Add(ref _activeSenders, 0); }
        }

        internal static bool Connected { get { return _connected; } }

        internal static bool Running
        {
            get
            {
                lock (LinkSync.State) { return _thread != null && _thread.IsAlive; }
            }
        }

        internal static void Start()
        {
            lock (LinkSync.Lifecycle) { StartLocked(); }
        }

        private static void StartLocked()
        {
            lock (LinkSync.State)
            {
                if (_thread != null && _thread.IsAlive) return;
                LinkWorker worker = LinkWorker.BeginLocked();
                _thread = new Thread(delegate() { Loop(worker); });
                _thread.IsBackground = true;
                _thread.Name = "Stageloom-socket";
                _thread.Start();
            }
        }

        internal static void Stop()
        {
            lock (LinkSync.Lifecycle) { StopLocked(); }
        }

        private static void StopLocked()
        {
            LinkWorker worker;
            Thread thread;
            MiniWsClient client;
            GH_Document solveDocument;
            GH_Document.SolutionEndEventHandler solveHandler;
            lock (LinkSync.State)
            {
                worker = LinkWorker.CurrentLocked();
                thread = _thread;
                client = _client;
                LinkWorker.ClearLocked();
                _thread = null;
                _client = null;
                _connected = false;
                RequestScheduler.TakeSolutionHandler(out solveDocument, out solveHandler);
            }
            if (worker != null) worker.Running = false;
            if (client != null) client.Dispose();
            // A late SolutionEnd from a solve that belonged to the stopped
            // worker must not finish a batch the next worker owns.
            if (solveDocument != null && solveHandler != null)
            {
                try { solveDocument.SolutionEnd -= solveHandler; }
                catch (Exception) { }
            }
            LinkOutbox.Wake();
            if (thread != null && thread != Thread.CurrentThread) thread.Join(2000);
            // The worker's finally usually clears these as it exits; if the
            // join timed out, do not carry frames from the dead connection
            // into the next one.
            lock (LinkSync.State)
            {
                LinkOutbox.ClearLocked();
                Publication.ResetConnectionLocked();
                RequestScheduler.ClearAll();
            }
        }

        internal static bool Configure(string host, int port, out string error)
        {
            return Configure(host, port, true, out error);
        }

        internal static bool Configure(string host, int port, bool persist, out string error)
        {
            string canonical;
            if (!EndpointConfig.TryCreate(host, port, out canonical, out error)) return false;
            if (persist && !EndpointSettings.SavePort(port, out error)) return false;

            bool changed;
            bool wasRunning;
            bool hasComponents;
            lock (LinkSync.Lifecycle)
            {
                lock (LinkSync.State)
                {
                    wasRunning = _thread != null && _thread.IsAlive;
                    hasComponents = StageRegistry.HasComponentsLocked();
                    changed = !string.Equals(_endpoint, canonical, StringComparison.Ordinal);
                }
                if (!changed) return true;
                StopLocked();
                _endpoint = canonical;
                _endpointPort = port;
                // A live document keeps its link; a settings component may be
                // configured before its first Stage In/Out registers.
                if (wasRunning || hasComponents) StartLocked();
            }
            LinkNotes.Add("endpoint set to " + canonical);
            return true;
        }

        internal static void Configure(string host, int port, bool persist)
        {
            string error;
            if (!Configure(host, port, persist, out error)) LinkNotes.Add(error);
        }

        private static void Loop(LinkWorker worker)
        {
            int backoffMs = 500;
            while (worker.Running)
            {
                MiniWsClient client = null;
                try
                {
                    client = new MiniWsClient(Endpoint);
                    client.Connect();
                    lock (LinkSync.State)
                    {
                        // A handshake that completes after Stop must not adopt
                        // the stale socket, revive the connection, or clear the
                        // state of the worker that replaced it.
                        if (!LinkWorker.IsCurrentLocked(worker)) return;
                        _client = client;
                        _connected = true;
                        Publication.ConnectionOpenedLocked();
                        // Frames queued for any earlier connection are stale.
                        LinkOutbox.ClearLocked();
                        // The unchanged-answer skip is a fact about the socket
                        // that carried the frame, not about the document.
                        StageRegistry.MarkAllSentLocked("", "");
                    }
                    backoffMs = 500;
                    LinkNotes.Add("connected to " + Endpoint);
                    // Publish from this thread: waiting for the next solve would
                    // delay hello until the user touches the canvas.
                    Publication.PublishIfCurrent(worker);
                    if (!LinkWorker.IsCurrent(worker)) return;
                    var stopSend = new ManualResetEvent(false);
                    var sender = new Thread(delegate() { SendLoop(client, stopSend, worker); });
                    sender.IsBackground = true;
                    sender.Name = "Stageloom-sender";
                    sender.Start();
                    try
                    {
                        ReceiveLoop(client, worker);
                    }
                    finally
                    {
                        // Always stop the sender with the connection.
                        stopSend.Set();
                        sender.Join(500);
                    }
                    LinkNotes.Add("connection closed; retrying");
                }
                catch (Exception ex)
                {
                    LinkNotes.Add("connect failed: " + ErrorText.FirstLine(ex));
                }
                finally
                {
                    if (client != null) client.Dispose();
                    lock (LinkSync.State)
                    {
                        // Only the current worker may tear down the shared
                        // connection state. A stale worker's finally must not
                        // clear the next connection's outbox or capabilities.
                        if (LinkWorker.IsCurrentLocked(worker))
                        {
                            _connected = false;
                            if (object.ReferenceEquals(_client, client)) _client = null;
                            ProtocolHandler.ResetPeerCapsLocked();
                            LinkOutbox.ClearLocked();
                        }
                    }
                }
                int slept = 0;
                while (worker.Running && slept < backoffMs)
                {
                    Thread.Sleep(100);
                    slept += 100;
                }
                if (backoffMs < 5000) backoffMs = Math.Min(5000, backoffMs * 2);
            }
        }

        private static void ReceiveLoop(MiniWsClient client, LinkWorker worker)
        {
            while (worker.Running)
            {
                byte[] frame = client.ReceiveBinary();
                if (frame == null) return;
                // Stop may have replaced this worker while a frame was being
                // read. Nothing decoded by a stopped worker may touch the next
                // session's slots or outbox.
                if (!worker.Running || !LinkWorker.IsCurrent(worker)) return;
                ProtocolHandler.HandleFrame(frame, worker);
            }
        }

        private static void SendLoop(MiniWsClient client, ManualResetEvent stop, LinkWorker worker)
        {
            Interlocked.Increment(ref _activeSenders);
            try
            {
                SendLoopCore(client, stop, worker);
            }
            finally
            {
                Interlocked.Decrement(ref _activeSenders);
            }
        }

        private static void SendLoopCore(MiniWsClient client, ManualResetEvent stop, LinkWorker worker)
        {
            var handles = new WaitHandle[] { LinkOutbox.Signal, stop };
            while (worker.Running)
            {
                int fired = WaitHandle.WaitAny(handles, 250);
                if (fired == 1) return;
                if (!LinkWorker.IsCurrent(worker)) return;
                if (fired == 0) SendFrames(client, worker);
                if (!LinkWorker.IsCurrent(worker)) return;
                RequestScheduler.RetryStaleSolves(worker);
                // Re-publish on a slow heartbeat: a component added while the
                // link was down is picked up without touching the canvas.
                Publication.HeartbeatIfDue(worker);
            }
        }

        private static void SendFrames(MiniWsClient client, LinkWorker worker)
        {
            while (true)
            {
                byte[] frame;
                lock (LinkSync.State)
                {
                    // A stale sender must not take a frame out of the outbox
                    // the current worker owns.
                    if (!LinkWorker.IsCurrentLocked(worker)) return;
                    if (!LinkOutbox.TryDequeueLocked(out frame)) break;
                }
                try
                {
                    client.SendBinary(frame);
                }
                catch (Exception ex)
                {
                    LinkNotes.Add("send failed: " + ErrorText.FirstLine(ex));
                    return;
                }
            }
        }
    }
}
