# Stageloom compliance record

**Status:** v0.7.0 is the released compliance baseline; the 0.8.0 quality work
is in progress (T1–T9, T10a, the T10b coverage floors, the T10c scale run and
the T10d 1-hour soak complete; T11 remains; see
[`plan-quality-0.7-0.8.md`](plan-quality-0.7-0.8.md)). The release version stays
`0.7.0` until T11. **1.0 planning** continues in [`plan.md`](plan.md). The
extensions.blender.org upload is deferred. Release history is in
[`../CHANGELOG.md`](../CHANGELOG.md) and the GitHub releases.

## Current audit

| Item | State | Evidence |
|---|---|---|
| G1 `network` / `online_access` | **Closed** | `server.py` gates `start()`; the operator re-checks; `blender/tests/offline/run.py` proves the off state and the Start recovery |
| G2 `sys.path` | **Closed** | no code path; the official wheel is the only `websockets` source and Blender installs it into `_libs` |
| G3 `files` | **Closed** | manifest; logs only (the frame cache was removed in 0.7, ADR 0016) |
| G4 `clipboard` | **Closed** | manifest |
| G5 licence | **Closed** | `LICENSE` grant + copyright, manifest `SPDX:GPL-3.0-or-later`, README |
| G6 `bl_info` | **Closed** | shipped `__init__.py` has none; `scripts/install-addon` generates it from the manifest |
| S1 Blender floor | **Settled** | `blender_version_min = "5.2.0"` (5.3 is the 1.0 target); the layered-action API is the reason; 4.2 is a separate compatibility project |
| S2 tags | **Closed** | `tags = ["Geometry Nodes", "Pipeline", "Modeling"]` |
| S3 `log_level` | **Closed** | `prefs.log()` gates `[stageloom]` output |
| S5 ruff | **Closed** | `uvx ruff@0.17.2 check --output-format=concise .` passes |
| S7 dead stubs | **Closed** | none left |
| S8 wheel integrity | **Closed** | the official wheel is the only `websockets` source; its SHA-256 is pinned and verified in `scripts/build-extension` and `scripts/check-docs` |
| S9 data root | **Closed** | `bpy.utils.extension_path_user(...)`; dev fallback; override retained |
| Manifest version | **Closed** | `blender_manifest.toml` declares `0.7.0`; `scripts/version` is the single extractor used by build/install/test, and `scripts/check-docs` checks the C# hello string and protocol example |
| Packaging truthfulness | **Closed** | build/install/test fail on validator/build/install error; version-specific `dist/stageloom-0.7.0.zip`; the normalized zip is validated after rewriting |
| Reproducible artifact | **Closed** | extension zip: sorted entries, fixed `1980-01-01` timestamps/modes, preserved compression; `.gha`: Mono `csc` `/deterministic`; both SHA-256 and two-build checks (`scripts/test-reproducible`, `scripts/test-reproducible-gha`) |
| Wine deploy path | **Closed** | `grasshopper/deploy/deploy.sh` derives the user under `$WINEPREFIX/drive_c/users` (`STAGELOOM_WINE_USER` override, `Public` skipped, zero/multiple users fail); no hardcoded user |
| Listing assets/support | **Deferred** | icon in `docs/listing/`; screenshots pending for a later upload; support URL is listing metadata |
| Platform upload | **Deferred** | v0.7.0 ships on GitHub first; the 1.0 plan owns the platform decision |
| Windows-native acceptance | **Deferred after 0.8.0** | no native-parity claim; tracked in [`plan.md`](plan.md) §5B |
| .NET framework migration | **No-go through 1.0** | C# 5/Mono and MiniWs/Json stay; [ADR 0019](decisions/0019-keep-csharp5-mono.md) |
| CI release gate | **Open for 1.0** | tracked in [`plan.md`](plan.md) §5F; CI runs core/Blender/extension/zip reproducibility, not the reference-machine benchmark, local-only C# or the local `.gha` check |

**Notes.** Manifest schema 1.0.0 has no `support` field; the support URL belongs
in the listing and README. S9 is the extension-user data directory, not the old
Linux path. The vendored `websockets` tree was deleted in 0.8.0 T2; the pinned
wheel is the single source ([ADR 0018](decisions/0018-official-wheel-only.md)).

**Cache removal.** The custom frame cache is gone ([ADR 0016](decisions/0016-live-only-stages.md));
the `files` permission now covers logs only. There is no data-root cache to
migrate: the removed fields in a 0.6 `.blend` are ignored, and Freeze or native
Bake stage is the offline path.

**0.7.0 release verification (2026-11-16).**

```text
blender_manifest.toml    version = "0.7.0"
scripts/test-core        110 tests pass
scripts/test-blender     423 tests pass; offline gate PASS
scripts/test-gha         fourteen codec fixtures + seven console suites pass
scripts/test-extension   installed extension round trip: pass
scripts/check-all        full gate PASS in 116.0 s
scripts/check-docs       ok (version, Python runtime, transport, fixtures, vendor path, stale paths)
uvx ruff@0.17.2 check .  All checks passed
scripts/build-extension  dist/stageloom-0.7.0.zip built and validated
```

**0.8.0 T2 verification (2026-11-16).** The vendored tree and spike tree are
gone; the official wheel is the only `websockets` source. The full gate passed
alone in 175.8 s with the baseline enforced (123 core tests, 427 Blender
headless tests plus the offline gate, the seven C# console suites, and the
installed-extension round trip). Current measurements are in
[`performance-ledger.md`](performance-ledger.md) and
[`plan-quality-0.7-0.8.md`](plan-quality-0.7-0.8.md); this record does not
duplicate them.

**0.8.0 T8/T9a verification (2026-11-18; T9a tree, historical).** Production is
**24,918 physical / 19,945 code lines across 69 files**; the tree has **123
core**, **455 Blender headless** and **687 C#** checks. T8 changed no wire,
fixture or `out_hash` byte and passed 26/26 gated metrics (worst positive p50
+1.81%). T9a makes the compliance claims true without touching production. The
artifact hashes in this block describe the T9a tree; T9b rebuilt both artifacts
on the current tree and records those hashes below:

- one version source: `blender_manifest.toml` + `scripts/version`; the build,
  install and test scripts use it, and `scripts/check-docs` checks the C# hello
  string and protocol example (`Publication.cs` and `docs/protocol.md` stay
  `Stageloom 0.7.0` until T11);
- reproducible extension zip: `scripts/build-extension` normalizes sorted
  entries, fixed `1980-01-01` timestamps/modes and preserved compression, then
  Blender validates the rewritten zip. Two builds produced SHA-256
  `bfb250609feb188a458541c020be8c96049dab53ffb627e7d56a2f128882eb80`;
  `scripts/test-reproducible` is the check. The manifest, wheel digest and
  extracted contents were compared with the source and are unchanged;
- reproducible `.gha`: `grasshopper/build/build-gha.sh` passes Mono `csc`
  `/deterministic` (timestamp, module version GUID and MVID). No `/pathmap` is
  needed because the build emits no debug info, and a copied-tree build
  matched. Three builds produced SHA-256
  `05c904d5bf5cd2012347aa9680edc3d8ade7f3854cc526e19a4cd77879d5efe4`;
  `scripts/test-reproducible-gha` is the local/release check and restores the
  original built `.gha`. The claim is same tree/toolchain, not a different
  Mono/`csc` or Rhino/Grasshopper assembly set;
- deploy: `grasshopper/deploy/deploy.sh` derives the wine user (current prefix:
  `miquel`), and `scripts/check-deployed` still scans every user. No live
  redeploy was performed;
- native Windows/Rhino acceptance is deferred until after 0.8.0; the `.NET`
  migration is a no-go through 1.0 ([ADR 0019](decisions/0019-keep-csharp5-mono.md));
- CI runs docs, ruff, core, Blender headless + offline, extension round trip,
  baseline-gate self-test and the two-build extension-zip reproducibility check.
  It skips the reference-machine benchmark and the local-only C# suite and
  `.gha` check explicitly and claims neither.

**0.8.0 T9b verification (2026-11-18).** The generated protocol tables add
`_protocol_tables.py` and `ProtocolTables.generated.cs` and edit the Python/C#
core. Production is **25,040 physical / 20,035 code lines across 71 files**;
the tree has **132 core**, **455 Blender headless** and **689 C#** checks. No
wire, fixture or `out_hash` byte changed. The T9a hashes above are historical;
the T9b working tree reproduced:

```text
scripts/test-reproducible      two builds PASS; zip sha256 d12b6f504196d24ceaeb8c3896f162f48dbdef36346dd7112100b8e539b79b88
scripts/test-reproducible-gha  three builds PASS; .gha sha256 18c629e3fba03d30b6605ea53b2dd77221533f3f469e9b10a3ff1c57ee8ceafb
scripts/test-extension         installed-extension round trip PASS
scripts/check-all              PASS 177.7 s; 26/26 baseline enforced
```

The full gate's worst gated movement was `pipeline.100k.total_ms` p50 +2.23% /
p95 +3.31%, within the 5% rule; the baseline identity now compares processor,
OS, architecture, Blender and Python rather than the kernel patch (7.3.4 →
7.3.5 is not a different benchmark machine).

The hashes differ from T9a because the zip now ships `core/_protocol_tables.py`
and the edited core modules, and the `.gha` ships
`ProtocolTables.generated.cs` plus the edited C# core; the version stays
`0.7.0`. T11 records the final release-tree hashes.

**0.8.0 T10a verification (2026-11-18).** Tests only; production source and the
release artifacts are unchanged. The tree has **133 core**, **468 Blender
headless** and **689 C#** checks, plus the fixed-seed 10,000-case Python and C#
frame fuzz campaigns. `scripts/test-core` passed in 0.38 s,
`scripts/test-blender` passed 468 tests in 86.9 s plus the offline gate, and
`scripts/test-gha` passed the fixture runner and all eight console suites in
25.5 s. No benchmark or full
`check-all` was run. T10b then added the coverage floors (below), T10c added the
scale run and T10d ran the 1-hour soak.

**0.8.0 T10b coverage verification (2026-11-18).** Tooling/docs only; shipped
code and the release artifacts are unchanged. `scripts/coverage core` measured
**93.50%** (90% floor); `scripts/coverage engine` measured **89.43%** of the
engine/geometry roots (80% floor) by rerunning the 468-test headless suite
inside Blender's bundled Python 3.13 with a private coverage.py 7.17.3 target.
The shipped add-on still has no `sys.path` manipulation. The pure C# core was
measured at **93.36%** (80% floor) with Linux Mono's built-in coverage profiler;
`Attributes.cs` and `Conversion.cs` are excluded because they reference
RhinoCommon and are exercised by the wine suites. No Windows/.NET dependency
was added, and C# coverage stays local-only.

**0.8.0 T10c scale verification (2026-11-19).** Runner/tests/docs only; shipped
code and the release artifacts are unchanged. `scripts/scale` built and
measured **200 stages** (one object with 200 stage records), exactly **100,000
triangles** (250 × 200 quads) and **10,000 instances** (one 2-triangle
definition on a 10,000-point cloud), passed **38/38** structural checks in
**4.58 s** and recorded **524.0 MiB** peak RSS. The focused headless test
`blender/tests/headless/test_scale_shapes.py` asserts the same shapes without
timing and raises the headless count from 468 to **471**. No new timing gate
was added; the 100k in-place and 13-row instance hot paths remain in the
existing benchmark gate, and the 200-stage/10k-row shapes are diagnostic. The
result is
[`../benchmarks/results/2026-11-19-scale.json`](../benchmarks/results/2026-11-19-scale.json).
T10d later ran the soak separately.

**0.8.0 T10d soak verification (2026-11-19).** Runner/tooling/docs plus one
minimal cleanup fix; shipped artifacts, wire, fixtures and `out_hash` are
unchanged. `scripts/soak` completed **10,238 cycles** in **3,600.2 s** of
stage/object/request/playback/bake/reconnect churn with **0 invariant
failures**, **0 unclassified exceptions**, **30/30** native Geometry Nodes bake
cycles, **15** client reconnects and RSS **368.6 → 399.0 MiB**; the final
teardown zeroed every registry, helper and session count. The soak exposed a
whole-object deletion session-state leak (runtime/verify/prefix-memo/playback
report entries survived); `Engine.prune_session_state` in `_rebuild_index`
fixes it, with a regression test. Production is **25,083 physical / 20,072 code
lines across 71 files**; `scripts/test-core` passes 133 tests and
`scripts/test-blender` passes **472** tests in 89.8 s plus the offline gate. The
coverage floors were rechecked after the cleanup fix: core **93.50%**, engine/
geometry **89.46%**, pure C# core **93.36%**, all above their floors. After the
fix the full gate passed **26/26 in 181.7 s** with the baseline enforced (worst
positive +0.31% p50 / +1.47% p95; no metric above +5%); the zip rebuilt to
`fdbf973e710c65ceb37c89520827c5a7f992742b44b9ebf0c3519196bccbb98d`
and the unchanged C# tree still reproduces the `.gha` at
`18c629e3fba03d30b6605ea53b2dd77221533f3f469e9b10a3ff1c57ee8ceafb`. The
result is
[`../benchmarks/results/2026-11-19-soak.json`](../benchmarks/results/2026-11-19-soak.json).
The native bake is Blender's headless Geometry Nodes bake, not native
Windows/Rhino acceptance; that acceptance remains **deferred after 0.8.0** and
no native-parity claim is made. Chain evidence remains the 2026-11-15 live
chain-bake under wine and the headless chain bake/playback suites.

The 1.0 workstreams, platform matrix and release gates are in
[`plan.md`](plan.md). A new listing packet is prepared from the 1.0 release
notes if the platform upload is taken at that point.
