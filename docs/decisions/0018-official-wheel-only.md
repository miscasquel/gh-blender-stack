# 0018 — The official websockets wheel is the only source

**Status:** Accepted, 2026-11-16 (0.8.0 T2). Decides against the `vendor/`
fallback in [0006](0006-carried-decisions.md).

## Context

The add-on needs one runtime dependency: `websockets`. It already shipped the
official pure-Python wheel for the extension
(`blender/stageloom/wheels/websockets-17.2-py3-none-any.whl`, declared in
`blender_manifest.toml`), while the development/test path used a second copy
under `blender/stageloom/vendor/` (63 tracked files, 21,968 tracked lines). Two
copies of the same package can drift, double the license/size surface, and make
"what ships" ambiguous. The extension build already excluded the vendored tree
from the zip, so only development and test tooling consumed it.

## Decision

- **Delete the vendored tree.** The pinned official wheel is the single
  `websockets` source for the extension, development installs, tests, tools and
  benchmarks.
- **Keep the wheel pinned.** `scripts/build-extension` verifies the SHA-256
  before validating/building the extension; `scripts/check-docs` verifies the
  same digest and the manifest declaration.
- **Development install.** `scripts/install-addon` extracts the wheel's
  `websockets` package into the addons directory, which Blender already has on
  `sys.path`; shipped code still never manipulates `sys.path` (G2).
- **Tests and tools.** `blender/tests/headless/run.py`, the fake-GH harness and
  runner, `blender/tests/offline/run.py`, `blender/tests/installed/run.py`,
  `tools/fake_gh/fake_gh.py` and `benchmarks/bench.py` put the wheel itself on
  `sys.path` (Blender 5.3's CPython supports zipimport) or pass it through
  `PYTHONPATH`. No checkout setup and no vendored copy is needed.

## License and digest

- Version: `websockets 17.2`, wheel
  `websockets-17.2-py3-none-any.whl`.
- License: BSD-3-Clause (Aymeric Augustin and contributors), included inside the
  wheel at `websockets-17.2.dist-info/licenses/LICENSE`.
- SHA-256:
  `6aa59f0ef92e796b2db6f5f26550c4713c0e4036899fadf02f55e2ed4db0b7ae`.

## Consequences

- One package version, one license copy and one upgrade point; the tracked tree
  is 63 files and 21,968 lines smaller.
- The extension zip still carries only the declared wheel (the Blender extension
  installer puts it in `_libs`); the build no longer needs a vendor exclusion.
- Development and CI need the wheel file in the checkout (it is tracked, not
  downloaded) or `scripts/install-addon`/the test runners cannot import
  `websockets`. This is deliberate: a missing wheel fails loudly.
- The build, docs check and extension validator fail on a modified or missing
  wheel instead of silently falling back to a second copy.
