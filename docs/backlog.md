# Backlog

The live list. Resolved work lives in the commits and the CHANGELOG; the
measured evidence behind the bigger items lives in `polish-plan.md`, which is a
record of one review rather than a plan. If something is not here, it is either
done or was judged not worth doing — it is not missing.

Each item says what it is, where, and why it matters. Effort is not estimated:
the last three batches each looked like an afternoon and were not.

---

## Reliability

### Blender engine — the one silence in the epoch design

`engine._bump_epochs` takes its signal from `depsgraph_update_post`, so a change
that never tags the depsgraph is **permanently** invisible rather than caught by
a later pass: a script writing data directly is verified to tag nothing
(`PointCloud.resize()` fires no event at all). The same shape is a **rebake
finishing**, which is unverified. Either measure it and handle it, or write the
accepted risk down next to `_bump_epochs` so nobody re-derives it.

### Protocol and codec

- **`Decode`'s contract is not total.** The `decoder-boundary` group in
  `grasshopper/tests/console/Program.cs` pins the contract, and inputs remain
  that break it (`grasshopper/src/core/Frame.cs`, `Json.cs`).
- **`v` accepts a string, and `int` casting differs between C# and Python.**
  `Frame.cs` vs `core/codec.py`: a protocol that means one thing on each side is
  a bug waiting for a mixed-version pair.
- **`FormatDouble`'s documented claim is false** (`Conversion.cs`).
- **`too_large` and `proto_mismatch` are defined and never sent**
  (`docs/protocol.md` §4). A codec that can only fail in one direction is half a
  codec.
- **Protocol §2 promises a 2 s ping and a 6 s `disconnected` verdict; nothing
  sends either.** The transport keepalive is now explicit (5 s ping, 20 s
  timeout) and covers liveness. Decide: implement the application-level ping, or
  amend §2 and say what liveness is now.

### Plugin (`.gha`)

One wine build + deploy + Rhino restart covers a whole batch here, so these are
worth doing together.

- **`GhLink.Stop()` is dead and ineffective** — no caller, and it does not stop
  the socket thread it names.
- **`slot.Params` is read torn.** The dictionary is published in one assignment
  under the lock, but the UI thread reads the field outside it.
- **`_lastPublishAt` is not volatile, and the `publishedThisConnection` flag
  races** with the sender thread's heartbeat.
- **`Slots` and `StageParams` grow without bound** — `Slot()` creates an entry
  for any key a frame names, including a key from another stack that no component
  owns.
- **A SubD crease that cannot be mapped is dropped silently**
  (`Conversion.cs`). Silent loss is the tier this repository treats as
  unforgivable.
- **The plugin has no behavioural coverage.** Layer C is the codec only, so
  every fix above is verified by hand. One round-trip test against the real
  `GhLink` (not the codec) would pay for itself in the next batch.
- **Error recovery when the client re-sends on its own.** The errored-stage path
  is covered by a scene change; a GH-side fix arriving for the same `rid` is a
  different way through the same code.

### Blender engine — smaller

- **`log_level` is a preference that does nothing.** Defined and drawn in
  `prefs.py`, read by no code. Either implement it or remove it; a knob that
  silently does nothing is worse than no knob.
- **A duplicated lookup and an undefined literal** in `engine`/`nodes`
  (`polish-plan.md` P4-6 has the details).

---

## Polish

- **Cache eviction is quadratic.** `core/cache.py::CacheIndex._evict` calls
  `min()` over all entries and `total_size()` (another full scan) once per
  removal. A heap or an ordered dict makes it linear; the frame cache is the one
  structure every response touches.
- **The C# conversion pass**, all of it cosmetic or warning-shaped:
  every warning carries the code `"conversion"` even when nothing was converted;
  the SubD crease warning fires when there are no creases; `counts["curves"]`
  over-reports; the SubD cage is converted twice per solve; the point-scaling
  loop is duplicated four times; `MeshSections` hardcodes `"mesh.colors"` while
  parameterising the prefix; `Icons.Get` disposes the stream behind the returned
  `Bitmap`; `Program.cs` dereferences unguarded; a hand-built `DecodedFrame` is
  used as an adapter.
- **Build and vendoring:** three build scripts duplicate the same boilerplate
  (`grasshopper/build/*.sh`); the wheel and `vendor/websockets` are two copies of
  the same library, and the vendored speedups build is undecided. `scripts/
  build-extension` is the only one the release path enforces.
- **`spikes/`** stays as evidence; its binaries do not belong in the index (the
  tracked `probe_api.exe` was removed in this batch and is ignored).

---

## Publishing

`docs/extensions-compliance.md` holds the gaps (G1–G7) and an order of work. It
is the gate for submitting to extensions.blender.org, and it is independent of
everything above: G7 is an argument to write, not a patch to land.
