# Extensions Platform compliance

**Status:** study, 2026-10-14. Written from a full read of the tree at `cfcab50`
(branch state: `blender_manifest.toml` v0.1.2, the `websockets` wheel, the
build/install/test scripts, `LICENSE`, `CHANGELOG.md`, `docs/polish-plan.md`,
`docs/plan.md`), cross-checked against the official rules:

- [Add-on Guidelines](https://developer.blender.org/docs/handbook/extensions/addon_guidelines)
- [Moderation Guidelines](https://developer.blender.org/docs/features/extensions/moderation/guidelines)
- [Getting started / Manifest](https://docs.blender.org/manual/en/latest/advanced/extensions/getting_started.html)
- [Internet Access](https://docs.blender.org/manual/en/latest/advanced/extensions/addons.html)
- [Python Wheels](https://docs.blender.org/manual/en/latest/advanced/extensions/python_wheels.html)
- [Extension Licenses](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html)
- [Extensions Tags](https://docs.blender.org/manual/en/latest/advanced/extensions/tags.html)
- [CI/CD upload API](https://developer.blender.org/docs/features/extensions/ci_cd/)
- PyPI: `websockets-17.2-py3-none-any.whl` confirmed present on the simple index.

**Scope:** what has to change for `gh_stack` to be accepted on
`extensions.blender.org`. No protocol changes, no new features.

**Items** are tagged `G` (hard gap — fix before upload) and `S` (should-fix —
reviewer friction and quality). Effort follows the polish plan: **S** ≈ under an
hour, **M** ≈ half a day, **L** ≈ a day or more.

**Overlaps:** `S5`/`S7` cover `polish-plan.md` P6-3/P6 dead code and P8-4 (the
`bl_info` version sync); `S8`/packaging touch P7-1 and P7-2. Do them in one pass
so both documents close together.

**Progress (0.2.1, 2026-10-17).** Phase 0 landed: `bpy.app.online_access` gates
the server (with a panel notice), the `sys.path` fallback is gone (the dev
install places `websockets` on Blender's addons path), `files`/`clipboard` are
declared, the licence/copyright texts agree, and `bl_info` is generated from the
manifest by `scripts/install-addon` (P8-4 retired — the version lives in the
manifest, the plugin's hello string, the protocol example, the README and the
CHANGELOG). `log_level` works, ruff is green under the pinned `ruff.toml`
(classic `E4`/`E7`/`E9`/`F`; newer ruff defaults swept in style rules this tree
does not follow), the wheel digest is pinned and verified, `build-extension`
validates the built zip and refuses platform-specific files, and the README's
Requirements section now answers "what does a reviewer with only Blender see"
with the `fake_gh` path. S4/S6's remaining bits and the Phase 1 items (G7
pre-ask, S1 floor, S2 tags, S9 data root, support URL, screenshots) are the
upload batch.

---

## 0. Verdict

The add-on is **~80 % of the way**. It already has a manifest, a genuine
pure-Python wheel, extension build/install/round-trip tooling, and the right
licence family. What is missing is a small set of declaration and hygiene items
that moderators are explicitly instructed to grep for, plus one **architectural
judgement risk** (`G7`) that has to be argued in the submission, not patched in
the code.

---

## 1. Already compliant (verified, not assumed)

- **Manifest shape.** `blender/gh_stack/blender_manifest.toml` carries
  `schema_version`, `id`, `version`, `name`, `tagline`, `maintainer`, `type`,
  `license`, `blender_version_min`, `website`. All required fields are present.
- **Licence family.** `SPDX:GPL-3.0-or-later` is exactly what the platform
  requires for add-ons. (The file-vs-manifest mismatch is `G5`.)
- **Wheel.** `wheels/websockets-17.2-py3-none-any.whl` is a genuine PyPI
  artifact (verified against the simple index) and **pure Python**, so one zip
  covers all five platforms and `platforms` stays omitted. Do **not** move to the
  `cp314` wheel: that forces per-platform zips and loses the single artifact.
- **Dependencies.** `websockets` is BSD-3-Clause (GPL-compatible). `numpy`
  (`engine.py:25`, `geometry_io.py:21`) is imported but ships with Blender, so it
  is not a dependency to declare.
- **No violations in code.** No runtime pip install, no auto-updater, no
  `bl_ext` / `user_repository` literals, no keymap manipulation, no
  `sys.modules` writes. The only `sys` use is `G2`.
- **Loopback only.** `server.py:130` binds `"127.0.0.1"`.
- **Nothing writes inside the extension directory.** The frame cache goes to
  `prefs.data_root()` (`cache.py:184,268,277`), default
  `~/.local/share/gh-stack` (`prefs.py:8`).
- **Extension round trip exists.** `scripts/test-extension` builds, installs
  into a throwaway config and completes a `fake_gh` round trip. This is the
  strongest evidence to put in the submission.

---

## 2. Hard gaps

### G1 · `network` permission declared, `bpy.app.online_access` never checked

**Rule.** Moderation Guidelines: *"Python: If using `network` permission, the
add-on is expected to check for `bpy.app.online_access`."* Manual → Internet
Access: *"it must check for the (read-only) property `bpy.app.online_access`."*

**Where.** `blender/gh_stack/blender/server.py:58-69` (`GhServer.start`),
reached from `engine.py:1194` (`register()` → `start_server()`) and from
`ops.py:103-111` (`gh_stack.start_server`).

**Fix.** Gate `start()`:

```python
import bpy
if not bpy.app.online_access:
    bridge.state["notice"] = (
        "Online access is disabled — enable it in Preferences > System"
        + (" (locked by the command line)" if bpy.app.online_access_override else "")
    )
    return False
```

Use `notice`, not `error`: an offline run is a legitimate state, not a failure.
Re-check in the operator (`gh_stack.start_server`) so flipping the preference and
pressing Start works without a Blender restart. A listening socket on 127.0.0.1
is arguably not "internet access" — but the *permission contract* is what
reviewers check, and this is one line.

**Effort:** S.

### G2 · `sys.path.insert(0, VENDOR)` — a direct "Bundle Modules" violation

**Rule.** Add-on Guidelines → **Bundle Modules**: *"Manipulating Blender's module
loading such as changing the module search path or inserting modules directly
into the global module dictionary is forbidden."* Violations in Code: all `sys`
uses are presumed read-only.

**Where.** `blender/gh_stack/blender/server.py:17` computes `VENDOR` from
`__file__`; `server.py:34-35` does `sys.path.insert(0, VENDOR)`.

**Context.** The fallback exists **only** for the development symlink install
(`scripts/install-addon`), which has no wheel. The extension zip always gets the
wheel, so the fallback is dead weight in the shipped artifact — and it is the
single most greppable offence in the tree. The `sys.path[0]` position also means
the vendored copy would shadow anything else named `websockets`.

**Options, best first.**

1. **Delete the fallback from the add-on.** `_load_websockets()` becomes a plain
   `import websockets` with a clear error string on `ImportError`. Then make the
   *dev script* provide the module without touching `sys.path`: copy
   `gh_stack/vendor/websockets` into `$BLENDER_USER_SCRIPTS/addons/websockets`,
   which Blender already has on `sys.path`. Zero path manipulation in the
   extension.
2. Have `scripts/install-addon` install the wheel through Blender's own
   extension installer instead of symlinking the tree.
3. If it must stay, put the fallback in a module the build excludes and keep the
   shipped path a plain import. Ugly; only if 1 and 2 are rejected.

**Verify:** `grep -rn "sys.path" blender/gh_stack` returns only matches under
`vendor/` (which the zip excludes).

**Effort:** S-M.

### G3 · `files` permission missing

**Rule.** Moderation Guidelines: *"Permissions are not correct (`files` is
expected for most I/O add-ons)."*

**Where.** Reads and writes at `core/cache.py:184,268,277`; directory creation in
`ResponseStore`; user-settable root at `prefs.py:28-33`.

**Fix.**

```toml
files = "Store cached Grasshopper frames under the data root"
```

47 characters, no trailing period, within the 64-character limit.

**Effort:** S.

### G4 · `clipboard` permission missing

**Where.** `ops.py:98` — `context.window_manager.clipboard = key` in
`GHSTACK_OT_copy_key`.

**Fix.** Declare it, or drop the operator:

```toml
clipboard = "Copy the stage key to paste into Grasshopper"
```

**Effort:** S.

### G5 · The declared licence is not the licence the tree grants

**Where.** `LICENSE` is verbatim GPLv3 with no copyright holder and no "or any
later version" grant. `README.md:135` says "GPL-3.0".
`blender_manifest.toml:11` says `SPDX:GPL-3.0-or-later`. The platform requires
GPL-3.0-or-later for add-ons, so the *manifest* is the side that is right and
the *files* are the side that is wrong.

**Fix.** Make all three agree. Recommended:

1. Add SPDX headers at the top of `blender/gh_stack/__init__.py`:

   ```
   SPDX-FileCopyrightText: 2026 Miquel Oller
   SPDX-License-Identifier: GPL-3.0-or-later
   ```

2. Add the standard grant sentence to `LICENSE` (the meaning the manifest
   already declares): *"This program is free software: you can redistribute it
   and/or modify it under the terms of the GNU General Public License as
   published by the Free Software Foundation, either version 3 of the License,
   or (at your option) any later version."* and name the copyright holder.

3. Add `copyright = ["2026 Miquel Oller"]` to the manifest. Optional for GPL, but
   the validator defines the exact form ("Year Name" / "Year-Year Name") and it
   removes a reviewer question at zero cost.

4. Update `README.md:135` to say GPL-3.0-or-later.

**Effort:** S.

### G6 · `bl_info` is still in `__init__.py`

**Rule.** Manual → Converting a Legacy Add-on into an Extension, step 2:
*"Remove the `bl_info` information (this is now in the manifest)."*

**Where.** `blender/gh_stack/__init__.py:14-22`. It exists only so the dev
symlink install reports a name and version, and it is kept in sync with the
manifest by hand — the drift risk recorded as P8-4.

**Fix.** Delete it from the shipped file; the manifest is authoritative. If the
legacy Add-ons dialog must stay pretty in dev, generate the `bl_info` from the
manifest inside `scripts/install-addon`, which makes the manifest a single
source of truth and retires P8-4. Deleting it also removes the `(0, 1, 2)` tuple
that will silently rot behind the manifest.

**Effort:** S.

### G7 · Self-containment — the architectural judgement risk

**Rule.** Moderation Guidelines → General: *"Make sure the add-on is
self-contained. This is the most delicate one… It should not have functionality
which is extended by external software. It should not depend on external servers
(`localhost` is fine)."*

GH Stack's Blender half drives Rhino/Grasshopper through a companion `.gha`. A
moderator who installs the zip, adds a stage, and sees "disconnected" with
nothing to connect to may read this as the forbidden case. The facts that argue
against that reading, and that the submission must state up front:

1. The companion is built from **this same GPL-3.0 repository**
   (`grasshopper/`), so no closed component is required.
2. The transport is **loopback only** — the guidelines explicitly carve out
   `localhost`.
3. The add-on **degrades predictably and inspectably**: the state machine
   (`waiting`/`ok`/`error`/`disconnected`/`disabled`), the Diagnostics panel, and
   cache replay with Grasshopper closed.
4. `tools/fake_gh` proves the Blender half end to end against a stand-in, so a
   reviewer *can* exercise the loop without Rhino.

**Deliverables before upload.**

- A "Requirements / What you need" section in the listing and README that names
  Rhino 8 + Grasshopper, links the `.gha` build steps, and states the
  `fake_gh` path for a dry run.
- A concrete answer to "what does a reviewer with only Blender see?" — install,
  add a stage, get a clear action-required state, and a documented one-command
  way to start `fake_gh` from the repository.
- **Ask first.** The guidelines invite it: *"if in doubt ask on the
  `#extension-moderators` room."* Post the localhost-bridge architecture and the
  `localhost` carve-out before uploading, and link the answer in the submission.

**Effort:** M (writing, plus the wait on an answer).

---

## 3. Should-fix

| # | Item | Where | Fix |
|---|---|---|---|
| S1 | `blender_version_min = "5.2.0"` limits the audience to 5.2+ only | `blender_manifest.toml:12` | Deliberate? The platform floor is `4.2.0`. `bpy.data.pointclouds` is 4.1+, so a 4.2 LTS test pass is cheap and would reach 4.2/4.5/5.0/5.1. Decide and record. |
| S2 | No `tags`, so no discovery surface | manifest | `tags = ["Geometry Nodes", "Pipeline", "Modeling"]` — all three are official add-on tags. |
| S3 | `log_level` preference does nothing | `prefs.py:40-43, 51` | Wire it to the `print("[gh-stack] …")` calls or delete it. A setting that does nothing reads as dead code in review. |
| S4 | `bpy.data.objects` used 8× | `engine.py:94,142,171,229,253,289,389,403`; `helpers.py:40,61` | Code Quality flags this explicitly ("a rare case where the current scene's objects should be accessed instead"). Most are intentional lookups of engine-owned names: comment each with why, or move to `bpy.context.scene.objects`. |
| S5 | Lint has not been run against the reviewer's command | whole tree | `find "." -iname "*.py" \| xargs ruff check --output-format=concise --ignore=E402,E741,E731`. Function-local imports are already on the P6-3 list. |
| S6 | Three hidden helper objects per stage, always | `helpers.py:55-57` | `ensure_helpers` builds mesh + curves + points for every stage. Cheap, but it is the first thing a reviewer sees in the outliner. Optional. |
| S7 | Dead `register(): pass` stubs | `server.py:200-201`, `helpers.py:90-95`, `nodes.py:131-136`, `props.py:311-316` | Remove; the checklist calls out unused/dead code. |
| S8 | Wheel integrity is unchecked | `blender/gh_stack/wheels/` | Record the PyPI SHA-256 and verify it in `scripts/build-extension`. A modified wheel is rejected platform-side and invisible locally. |
| S9 | Data root default is a Linux-ism | `prefs.py:8, 64-70` | `~/.local/share/gh-stack` resolves everywhere via `expanduser` but is not conventional on Windows/macOS and does not use the documented location. Default to `bpy.utils.extension_path_user(__package__, create=True)`, keep `data_root` as an override. This is where the "read-only System repository" guidance bites. |

---

## 4. Packaging and release mechanics

**Validate the artifact, not just the source.** `scripts/build-extension:16`
runs `blender --command extension validate "$SOURCE"` on the source directory.
Add the post-build zip validation, which runs the same code the platform runs:

```
blender --command extension validate dist/gh_stack-<version>.zip
```

It is what catches "wheel declared but missing", malformed permission strings and
path-separator mistakes — the failure modes that are invisible locally.

**Zip contract.** `build-extension:21-31` stages with `tar` excluding `vendor/`,
`__pycache__`, `*.pyc`; `blender --command extension build` adds the default
excludes (`__pycache__/`, `/.git/`, `/*.zip`). Add an assertion that lists the
built zip and fails on `vendor/`, `.so`, `.exe`, `.pyc` or a stray `*.zip`. The
committed `spikes/s6/probe_api.exe` (P7-1) and
`vendor/websockets/speedups.cpython-314-x86_64-linux-gnu.so` (P7-2) are outside
the staged tree today — **P7-2 option (a), "build it in `install-addon` and
gitignore it", is now the compliance answer, not just hygiene**: nothing
platform-specific may ever enter the wheel path.

**Version and metadata.** Version must increase on every upload and follow
semver; `0.1.2` is fine. With `G6` done, `__init__.py` no longer carries a
competing version and the P8-4 sync problem disappears.

**Listing assets** (platform side, not the repo): icon, screenshots (heavy GIFs
are called out), a description, and a **support URL** — add
`support = "https://github.com/miscasquel/gh-blender-stack/issues"`. `website`
is already set.

**Automation.** The platform exposes
`POST /api/v1/extensions/<id>/versions/upload/` with `version_file`,
`release_notes` and a profile-generated token. `.github/workflows/tests.yml`
already runs layer A on push/PR; a release job can reuse the same Blender
download, run `scripts/build-extension`, validate the zip, and upload with
release notes derived from `CHANGELOG.md` — which also makes `dist/`
reproducible on the platform's Blender rather than a local one.

---

## 5. Order of work

**Phase 0 — before uploading (~half a day).**
`G1` online-access gate · `G2` remove the `sys.path` fallback · `G3`/`G4`
permissions · `G5` licence + copyright · `G6` drop `bl_info` · `S3` `log_level` ·
`S5` ruff · `S8` wheel hash · validate the built zip · README/listing
"Requirements" section.

**Phase 1 — at upload.**
`G7` pre-ask `#extension-moderators` and link the answer · `S1` version-floor
decision · `S2` tags · `S9` data-root default · `support` URL · screenshots ·
release notes.

**Phase 2 — after acceptance.**
P7-1/P7-2 repo hygiene · `S4` comments · `S6` helper creation · a 4.2 LTS test
pass if the floor widens · CI upload automation.

---

## 6. Not problems — do not over-correct

- **`numpy`.** Ships with Blender; not a declared dependency.
- **The `.gha` is not in the zip.** It is an external application, not an add-on
  dependency. The submission explains it (`G7`).
- **`vendor/` exists in git.** Only the zip contents matter, and the zip
  excludes it.
- **Writing the frame cache to the data root.** The guidance is only "do not
  write in your own directory", which is already satisfied. `S9` is a
  convention improvement, not a violation.
- **`blender_version_min = "5.2.0"`.** Allowed; the platform floor is `4.2.0`.
  `S1` is about reach, not compliance.

---

## 7. Open decisions

1. **`blender_version_min`.** Stay 5.2-only, or test down to 4.2 LTS and widen?
   Widening is the biggest reach win and the biggest unknown (untested API
   surface).
2. **The vendor fallback (`G2`).** Delete it and move the dev install to an
   out-of-package copy / wheel install, or wrap it so it is excluded from the
   zip? The former is cleaner for review; the latter is a smaller change.
