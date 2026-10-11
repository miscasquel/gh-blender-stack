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

## The live run

Three steps, in order. The server must run first, because Grasshopper only scans
`Libraries/` at startup.

1. **Start the server** (from the repo root):
   ```
   PYTHONPATH=blender/gh_stack/vendor python3 spikes/s1/server.py --rounds 3
   ```
   It prints `listening on ws://127.0.0.1:9131/s1` and waits.

2. **Start Rhino** (it scans `Libraries/GhStackSpike/` on launch):
   ```
   WINEPREFIX=~/.local/share/wineprefixes/rhino8 ~/.local/share/wine-rhino/bin/wine \
     "C:\Program Files\Rhino 8\System\Rhino.exe"
   ```
   Then in Rhino: type `Grasshopper`, and on the canvas drag the **S1 Spike**
   component from the **GH Stack S1** tab.

3. **Watch the server output.** Each round should print
   `round N: reply in … ms: {"msg":…,"n":N,"solves":…}`.

4. **Save and reopen.** In Grasshopper: File → Save As, to e.g. `C:\users\miquel\Documents\s1.gh`.
   Then close the editor, delete the component from the canvas, and reopen the same
   file. The component comes back, the server shows a **new connection**, and no
   dialog appears. That is the whole gate.

## What I need from you

The save-and-reopen part is the operator check the plan names. Tell me:

1. When the server logged the first `reply in … ms`, what it said.
2. Whether reopening the saved file reconnected without a dialog.
3. Whether the "no server running" case stays silent (open Rhino with the component
   before starting any server: the component should show "connecting…", a runtime
   warning, and nothing else).

I will transcribe what you report into `spikes/results/s1.txt` and mark S1 passed or
failed in the plan.
