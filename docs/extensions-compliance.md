# Stageloom compliance record

**Status:** v0.6.0 is the compliance baseline; **1.0 planning** continues in
[`plan.md`](plan.md). The extensions.blender.org upload is deferred. Release
history is in [`../CHANGELOG.md`](../CHANGELOG.md) and the GitHub releases.

## Current audit

| Item | State | Evidence |
|---|---|---|
| G1 `network` / `online_access` | **Closed** | `server.py` gates `start()`; the operator re-checks; `blender/tests/offline/run.py` proves the off state and the Start recovery |
| G2 `sys.path` | **Closed** | no code path; only comments/vendor; the zip excludes `vendor/` |
| G3 `files` | **Closed** | manifest |
| G4 `clipboard` | **Closed** | manifest |
| G5 licence | **Closed** | `LICENSE` grant + copyright, manifest `SPDX:GPL-3.0-or-later`, README |
| G6 `bl_info` | **Closed** | shipped `__init__.py` has none; `scripts/install-addon` generates it from the manifest |
| S1 Blender floor | **Settled** | `blender_version_min = "5.2.0"` (5.3 is the 1.0 target); the layered-action API is the reason; 4.2 is a separate compatibility project |
| S2 tags | **Closed** | `tags = ["Geometry Nodes", "Pipeline", "Modeling"]` |
| S3 `log_level` | **Closed** | `prefs.log()` gates `[stageloom]` output |
| S5 ruff | **Closed** | `uvx ruff check --output-format=concise --ignore=E402,E741,E731 .` passes |
| S7 dead stubs | **Closed** | none left |
| S8 wheel integrity | **Closed** | digest pinned and verified in `scripts/build-extension` |
| S9 data root | **Closed** | `bpy.utils.extension_path_user(...)`; dev fallback; override retained |
| Packaging truthfulness | **Closed** | build/install/test fail on validator/build/install error; version-specific zip |
| Listing assets/support | **Deferred** | icon in `docs/listing/`; screenshots pending for a later upload; support URL is listing metadata |
| Platform upload | **Deferred** | v0.6.0 ships on GitHub first; the 1.0 plan owns the platform decision |
| Windows-native acceptance | **Open for 1.0** | tracked in [`plan.md`](plan.md) §5B |
| CI release gate | **Open for 1.0** | tracked in [`plan.md`](plan.md) §5F |

**Notes.** Manifest schema 1.0.0 has no `support` field; the support URL belongs
in the listing and README. S9 is the extension-user data directory, not the old
Linux path.

**0.6.0 release verification (2026-11-14).**

```text
scripts/test-core        57 tests pass
scripts/test-blender     268 tests pass (including the offline gate)
scripts/test-gha         fixtures + attribute store + link + component status + stage guard pass
scripts/test-extension   installed extension round trip: pass
scripts/bench            all budgets pass (100k round trip 14.9 ms / 50 ms,
                         13x instance prefix+encode 10.3 ms / 100 ms)
uvx ruff check ...       All checks passed
blender --command extension validate blender/stageloom
blender --command extension validate dist/stageloom-0.6.0.zip
```

The 1.0 workstreams, platform matrix and release gates are in
[`plan.md`](plan.md). A new listing packet is prepared from the 1.0 release
notes if the platform upload is taken at that point.
