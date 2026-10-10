# Spikes

Scripts and results from the feasibility checks behind `docs/plan.md`. They are evidence, not product code.

Run a Blender script headless:

```
blender -b --factory-startup --python spikes/<script>.py
```

Several scripts write scratch files to `/tmp/opencode/probe/`. Create that folder first (`mkdir -p /tmp/opencode/probe`). Results of the original runs are in `results/`.

## Scripts and what they back

| Script | Question | Plan reference |
|---|---|---|
| `probe_bl.py` | Can Python add a modifier type? (Inconclusive. The built-in type list is fixed.) | §5, ADR context |
| `probe_gn.py` | First GN modifier probe. Superseded by the scripts below. Uses object inputs that hang headless. | history |
| `probe_dbg.py`, `probe_dbg2.py`, `probe_dbg3.py`, `probe_dbg4.py`, `probe_dbg5.py` | Discovering how Blender 5.2 exposes modifier inputs (`bl_system_properties_get`) | §5 |
| `probe_hang.py`, `probe_hang2.py`, `probe_hang3.py` | Isolating the headless hang: object input via modifier vs. Object Info default | §5, §17 |
| `probe_gn_steps.py` | Step-by-step instrumentation of the hang | history |
| `probe_live.py` | In-place `bmesh` edit propagates through Object Info; a PLY import case | §5, §13 |
| `probe_ply.py` | GN PLY import caches by path | §5 |
| `probe_arch.py` | Object Info Original vs. Relative; `persistent_uid` stability; `stack.copy()`; hidden helpers | §5 |
| `probe_excl.py` | Helper in an excluded collection still resolves | §5 |
| `probe_data.py` | Custom-prop keyframes; crease attributes; Subsurf; GN Subdivision node; spline attributes; driver | §6, §7 |
| `probe_drv.py` | Driver on a custom prop; layered action API | §7 |
| `probe_geo.py` | `evaluated_geometry()` components; mixed output | §6 |
| `probe_nurbs.py` | GN keeps NURBS weight, order, and knots mode | §6, ADR 0003 |
| `bench_geo.py` | Array read and write timings, JSON size, at about 100k triangles | §13 |
| `ws_rtt.py` | `websockets` round-trip timings. Needs a venv: `uv venv venv --python 3.14 && uv pip install --python venv/bin/python websockets==17.2` | §13 |

Do not run `probe_gn.py` or `probe_hang*.py` without a timeout. They are the cases that hang.
