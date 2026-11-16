# Stageloom compliance record

**Status:** v0.7.0 is the compliance baseline; **1.0 planning** continues in
[`plan.md`](plan.md). The extensions.blender.org upload is deferred. Release
history is in [`../CHANGELOG.md`](../CHANGELOG.md) and the GitHub releases.

## Current audit

| Item | State | Evidence |
|---|---|---|
| G1 `network` / `online_access` | **Closed** | `server.py` gates `start()`; the operator re-checks; `blender/tests/offline/run.py` proves the off state and the Start recovery |
| G2 `sys.path` | **Closed** | no code path; only comments/vendor; the zip excludes `vendor/` |
| G3 `files` | **Closed** | manifest; logs only (the frame cache was removed in 0.7, ADR 0016) |
| G4 `clipboard` | **Closed** | manifest |
| G5 licence | **Closed** | `LICENSE` grant + copyright, manifest `SPDX:GPL-3.0-or-later`, README |
| G6 `bl_info` | **Closed** | shipped `__init__.py` has none; `scripts/install-addon` generates it from the manifest |
| S1 Blender floor | **Settled** | `blender_version_min = "5.2.0"` (5.3 is the 1.0 target); the layered-action API is the reason; 4.2 is a separate compatibility project |
| S2 tags | **Closed** | `tags = ["Geometry Nodes", "Pipeline", "Modeling"]` |
| S3 `log_level` | **Closed** | `prefs.log()` gates `[stageloom]` output |
| S5 ruff | **Closed** | `uvx ruff@0.17.2 check --output-format=concise .` passes |
| S7 dead stubs | **Closed** | none left |
| S8 wheel integrity | **Closed** | digest pinned and verified in `scripts/build-extension` |
| S9 data root | **Closed** | `bpy.utils.extension_path_user(...)`; dev fallback; override retained |
| Manifest version | **Closed** | `blender_manifest.toml` declares `0.7.0`; the build, install and validation paths read that one value |
| Packaging truthfulness | **Closed** | build/install/test fail on validator/build/install error; version-specific `dist/stageloom-0.7.0.zip` |
| Listing assets/support | **Deferred** | icon in `docs/listing/`; screenshots pending for a later upload; support URL is listing metadata |
| Platform upload | **Deferred** | v0.7.0 ships on GitHub first; the 1.0 plan owns the platform decision |
| Windows-native acceptance | **Open for 1.0** | tracked in [`plan.md`](plan.md) §5B |
| CI release gate | **Open for 1.0** | tracked in [`plan.md`](plan.md) §5F |

**Notes.** Manifest schema 1.0.0 has no `support` field; the support URL belongs
in the listing and README. S9 is the extension-user data directory, not the old
Linux path.

**Cache removal.** The custom frame cache is gone ([ADR 0016](decisions/0016-live-only-stages.md));
the `files` permission now covers logs only. There is no data-root cache to
migrate: the removed fields in a 0.6 `.blend` are ignored, and Freeze or native
Bake stage is the offline path.

**0.7.0 release verification (2026-11-16).**

```text
blender_manifest.toml    version = "0.7.0"
scripts/test-core        110 tests pass
scripts/test-blender     413 tests pass; offline gate PASS
scripts/test-gha         fourteen codec fixtures + seven console suites pass
scripts/test-extension   installed extension round trip: pass
scripts/check-all        full gate PASS in 113.0 s
scripts/check-docs       ok (version, Python runtime, transport, fixtures, vendor path, stale paths)
uvx ruff@0.17.2 check .  All checks passed
scripts/build-extension  dist/stageloom-0.7.0.zip built and validated
```

The 1.0 workstreams, platform matrix and release gates are in
[`plan.md`](plan.md). A new listing packet is prepared from the 1.0 release
notes if the platform upload is taken at that point.
