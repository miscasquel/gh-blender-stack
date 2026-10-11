# S1: the transport gate

**Question (plan §15):** does a C# `GhStack.gha` under wine connect, trigger a solve
from a background message via `ScheduleSolution`, send a response back, and save and
reopen the document with no dialog?

## What is already verified headlessly

| Piece | Result | How |
|---|---|---|
| `websockets` 17.2 on a Blender background thread | pass | `spikes/s2_server_thread.py`, `results/s2-background.txt` |
| Queue handoff + timer drain + 2 MB load | pass | `results/s2-gui.txt`, `results/s2_timer.txt` |
| Python S1 peer (server.py) | pass | `python3 spikes/s1/server.py --selftest` |
| C# `MiniWsClient` handshake, masking, framing | pass | `spikes/s1/WsTest.exe` against `wsecho.py` |

So only the **Grasshopper integration** under wine is left, exactly as the plan says
S1 needs a live session.

## What the spike proves

One component, "S1 Spike", on tab "GH Stack S1", GUID `9c2f0a1e-…`:

- a background thread connects to `ws://127.0.0.1:9131/s1` with a hand-rolled
  WebSocket client (see the Mono findings in `GhStackSpike.cs`);
- every message from Blender lands in a per-stage slot on that thread;
- the thread asks the UI thread to solve via `OnPingDocument().ScheduleSolution(0, …)`,
  which expires the component;
- `SolveInstance` reads the slot and queues a reply that the socket thread sends;
- failures are runtime messages only — no dialogs.

## Live-run log (2026-10-11)

Three rounds of debugging, all on the component side; the transport itself worked
from the first attempt.

| Run | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | "Input parameter index [-1] too low for Component S1 Spike"; no outputs | `da.GetData("On")` — the nickname. `IGH_DataAccess` matches the parameter **name**, which is "Enabled" | `GetData("Enabled", …)` |
| 2 | Outputs `n` and `msg` populated, `solves` `<null>`, same index error | `da.SetData("s")` — the nickname of "solves". Same class of bug, on the output side | `SetData("solves", …)`; output nickname set to "solves" to match the name |
| 3 | Rounds 2 and 3 stalled 30 s and connections dropped with keepalive ping timeouts | `NoteSolved()` queued the reply during `SolveInstance`, but `SendQueued` only ran after `ReceiveBinary()` returned, and the peer waits for the reply before sending the next request — a deadlock by design | replies go out from a dedicated sender thread driven by an `AutoResetEvent` |
| 4 | Every round answered at exactly 1000 ms with `solves: 1` | the watchdog replying to every request within 1 s, so it consumed the nonce before the solution ran, and the reply stopped proving the solve | watchdog backed off to 10 s so the solution wins the race normally |

Findings worth carrying into M4: `GetData`/`SetData` match on parameter name, not
nickname; and the reply path must be able to send while the read loop is blocked,
per plan §10 ("Stage Out queues its response at the end of the solve, and the
socket thread sends it").

Confirmed working across runs 2–3: the component connects under wine, a runtime
message really does trigger `ScheduleSolution` and `SolveInstance` on the UI
thread (the `solves` count climbed 1 → 4 → 7 → 12 in run 2, and the slot value
reached the `n` and `msg` outputs), and the reply reaches the peer with the right
nonce and message.

## Result (2026-10-11)

**The gate passes**, including the save-and-reopen check. The cleanest evidence is
the reopened document, where `solves` restarts at 1 and climbs one per round:

```
connection from ('127.0.0.1', 34964)
  round 1: reply in 63.5 ms: n=1, solves=1
  round 2: reply in 21.5 ms: n=2, solves=2
  round 3: reply in 33.6 ms: n=3, solves=3
  round 4: reply in 33.6 ms: n=4, solves=4
  round 5: reply in 34.1 ms: n=5, solves=5
```

The full evidence, and the earlier identical pass, is in `spikes/results/s1.txt`.
The canvas log in `spikes/results/s1-gui.png` shows the lifecycle: the peer went
away, the component backed off, reconnected on its own, received a message, and
expired itself on the UI thread — all as runtime messages, no dialog.

So, under wine: the `.gha` connects; a message received on the background socket
thread raises a solution on the UI thread through `OnPingDocument().ScheduleSolution`
+ `ExpireSolution`; `SolveInstance` runs for every message (the `solves` counter
climbing one per round is what proves the solve really ran); the reply, queued at
the end of the solve, reaches the peer in ~20–65 ms; and save and reopen bring the
component back, reconnected, with no manual step and no dialog.

Three socket-side details mattered, all recorded for M4:

- `ScheduleSolution(0, …)` coalesces and the delegate is silently dropped, so only
  the first message ever solves. A 10 ms delay works reliably.
- the reply must be able to leave while the read loop is blocked on the next frame,
  or the peer's next request never comes and the connection times out. A dedicated
  sender thread handles that; it is retired before its socket is disposed, so a
  stale sender cannot dequeue and drop a reply.
- the spike's nonce counter must be reset per connection, or a reconnect's first
  round looks "already answered" and never gets a reply. The real protocol uses
  request IDs, which never repeat, so this is a spike-only artefact.

## What is still open

Nothing for S1's own gate. The geometry round trip is S3, and the real components
are M4.

## The live run (for reproducing the spike)

The server must be running before Grasshopper starts, because Grasshopper only scans
`Libraries/` at launch. Keep it running: stopping it makes the component report
`connect failed: Connection refused` and retry on its own, which is correct behaviour
but noisy.

1. **Start the server** (from the repo root):
   ```
   PYTHONPATH=blender/gh_stack/vendor python3 spikes/s1/server.py --rounds 5
   ```
   It prints `listening on ws://127.0.0.1:9131/s1` and waits.

2. **Start Rhino** (it scans `Libraries/GhStackSpike/` on launch):
   ```
   WINEPREFIX=~/.local/share/wineprefixes/rhino8 ~/.local/share/wine-rhino/bin/wine \
     "C:\Program Files\Rhino 8\System\Rhino.exe"
   ```
   Then in Rhino: type `Grasshopper`, and on the canvas drag the **S1 Spike**
   component from the **GH Stack S1** tab.

3. **Watch the server output.** Each round prints
   `round N: reply in … ms: {"msg":…,"n":N,"solves":…}`. The `solves` value climbing
   one per round is what proves the solve ran; a round answered at exactly
   10000 ms comes from the spike's watchdog instead.

4. **Save and reopen.** In Grasshopper: File → Save As, close the editor, and reopen
   the file. The component comes back, reconnects on its own, and `solves`
   restarts at 1. The pass in `spikes/results/s1.txt` was recorded this way.

## Building and deploying

```
spikes/s1/build.sh        # GhStackSpike.gha via wine csc.exe
spikes/s1/deploy.sh       # into Libraries/GhStackSpike/
spikes/s1/build-wstest.sh # WsTest.exe, the headless client pre-flight
python3 spikes/s1/server.py --selftest   # verifies the Python peer alone
```

The headless pre-flight runs `WsTest.exe` against a tiny echo server
(`/tmp` scratch; see the WsTest note in this directory). It is worth running before
any live session, because it catches client-side framing bugs without Rhino.
