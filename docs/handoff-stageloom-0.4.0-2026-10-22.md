# Handoff: Stageloom 0.4.0 — rebrand and extensions.blender.org upload

**Date:** 2026-10-22
**Status:** ready to execute. No rebrand or compliance work has been started.
**Current tree:** `5837fd9` (`Add-on/Plugin: carry wire-edge and curve attributes`),
one commit after `v0.3.1`. Working tree clean. Manifest version is `0.3.1`.
`CHANGELOG.md` has an `## [Unreleased]` section for the wire-edge/curve work.
**Target:** **Stageloom 0.4.0**, full rebrand, first upload to
`extensions.blender.org`.
**Companion plugin:** the Grasshopper `.gha` — same repository, same release.

---

## 1. Context

`v0.3.1` was released on 2026-10-21 as a hardening/performance patch. Since then
`5837fd9` added curve and wire-edge attributes; that work is in the `Unreleased`
section and belongs to 0.4.0.

The extensions-platform study in `docs/extensions-compliance.md` is from
2026-10-14 and is stale. At this tree, the hard compliance gaps are closed, but
the study predates the engine split, the 0.3.1 releases and the current ruff
state. The remaining work is:

1. **Rebrand** the product from GH Stack to **Stageloom** (full rename).
2. **Close the compliance audit** and fix the remaining should-fix items:
   ruff, truthful packaging scripts, a negative online-access test, tags, and
   the data-root default.
3. **Prepare the listing** and the G7 self-containment pre-ask.
4. **Ship 0.4.0** on GitHub and upload it to the extensions platform.

The product name and version were deliberately chosen before the first upload
because the platform extension id is permanent. `stageloom` is free in the
public listing (searched 1,542 published extensions); the upload form is the
final check. If it is taken, fall back to **Stagesmith**.

---

## 2. Decisions and assumptions

**Settled:**

- Product name: **Stageloom** (companion: “Stageloom for Grasshopper”).
- Rebrand scope: **full** — package, manifest id/name, UI, C# namespace/assembly,
  scripts, current docs, listing. Historical records keep the old name.
- Version: **0.4.0** (not 1.0.0; the v1 scope is not complete).
- Blender floor: **5.2.0**. Do not widen to 4.2 in this release. `props.py`
  uses the layered-action API exclusively; 4.2 would need a legacy fcurve path.
- Support URL: **https://github.com/miscasquel/stageloom/issues** (or the
  existing repo if it is not renamed). This is listing/README metadata, not a
  manifest field.
- G7 pre-ask: **the repository owner posts it** on Blender Chat
  `#extension-moderators`. Draft in Appendix A.

**Assumptions to confirm before implementation:**

- The data root moves to the extension user data directory in 0.4.0.
- Persisted data keys stay for compatibility (see §3.3).
- The GitHub repo is renamed to `stageloom`; GitHub redirects old links. If
  kept as `gh-blender-stack`, update the README/listing URLs instead.
- Old add-on and old `.gha` are removed before installing 0.4.0.

---

## 3. Workstream A — rebrand

### 3.1 Rule: live references only

A blind global replace would rewrite dated evidence. Rename **live code, UI,
scripts, current docs and tests**. Leave as written:

- `docs/repo-review-2026-10-20.md`
- `docs/live-sync-report-2026-10-19.md`
- `docs/handoff-viewport-stutter-2026-10-21.md`
- `docs/polish-plan.md`
- old `CHANGELOG.md` sections
- `spikes/`

Add one “formerly GH Stack” note in the README and the plan, so the old name is
still discoverable. Historical records can keep the old name without a note.

Leave `protocol/fixtures/hello/expect.json` (and its frame) as a historical v2
sample; its `GhStack 0.3.0` client string is valid test data, not the live
hello. Do not regenerate the golden fixtures just for the name. Update
`docs/protocol.md` and `LinkTests` against the live code.

Current live reference counts (approximate, before edits): 499 × `gh_stack`,
133 × `GhStack`, 64 × `GH Stack`, 39 × `GHSTACK`, 50 × `GHS`, 17 × `GH Stage`.

### 3.2 Rename matrix

| Area | From | To |
|---|---|---|
| Source package | `blender/gh_stack/` | `blender/stageloom/` |
| Python imports/tests | `gh_stack` | `stageloom` |
| Manifest | `id = "gh_stack"`, `name = "GH Stack"` | `id = "stageloom"`, `name = "Stageloom"` |
| Extension module | `bl_ext.user_default.gh_stack` | `bl_ext.user_default.stageloom` |
| Operators | `bl_idname = "gh_stack.*"` | `bl_idname = "stageloom.*"` |
| Python classes | `GHSTACK_*`, `GhStackPreferences`, `GhStack*` | `STAGELOOM_*`, `StageloomPreferences`, `Stageloom*` |
| Panel | `GH Stack` category/label | `Stageloom` |
| C# namespace | `GhStack` | `Stageloom` |
| C# core assembly | `GhStack.Core.dll` | `Stageloom.Core.dll` |
| C# plugin | `GhStack.gha` | `Stageloom.gha` |
| C# test exes | `GhStack*Tests.exe` | `Stageloom*Tests.exe` |
| Deploy folder | `Libraries/GhStack` | `Libraries/Stageloom` |
| Protocol hello | `GhStack 0.3.1` | `Stageloom 0.4.0` |
| Scene / collection | `GH Stage`, `GH Stage Data` | `Stageloom Stage`, `Stageloom Stage Data` |
| Helper / group prefix | `GHS ` | `SL ` |
| Modifier name | `GH · <label>` | `Stageloom · <label>` |
| GitHub repo | `gh-blender-stack` | `stageloom` (assumption; redirects) |

**Version-sync locations** (same list as 0.3.1):

- `blender/gh_stack/blender_manifest.toml` (after the move:
  `blender/stageloom/blender_manifest.toml`)
- `grasshopper/src/gh/GhLink.cs` hello string
- `grasshopper/tests/console/LinkTests.cs` expectation
- `docs/protocol.md` hello example
- `README.md` (including the “v0.3.1 is a working demo” line)
- `CHANGELOG.md`

### 3.3 Compatibility carve-outs — do not rename

These are persisted keys or external identities. Renaming them breaks existing
`.blend` files, keyframes, drivers or Grasshopper documents:

- `Object.gh_stack` — the RNA property group. Keep the attribute name even if
  the Python class becomes `StageloomStack`; the user never sees it.
- `gh_stack_id` — object custom property and cache namespace.
- `gh/<stage>/<param>` — parameter custom properties; keyframes and drivers
  point at these paths.
- `gh_stage_key` — modifier/node-group stage link.
- `gh_stack_format`, `gh_stack_nested` — node-group format markers.
- Stage keys, cache namespaces, wire protocol (`v: 2`) and all section names.
- **C# component GUIDs.** Keep these six values byte-identical:
  - `a1f0c2d4-1b2e-4c3a-9d4f-5e6a7b8c9d01`
  - `b2e1d3c5-2c3f-4d4b-8e5a-6f7b8c9d0e12`
  - `c3f2e4d6-3d4a-4e5c-9f6b-7a8b9c0d1e23`
  - `d4a3b2c1-5e6f-4a7b-8c9d-0e1f2a3b4c56`
  - `e5b4c3d2-6f70-4b81-9d2e-1f2a3b4c5d67`
  - `f6c5d4e3-7081-4c92-ae3f-2a3b4c5d6e78`

Add a code comment at each carve-out explaining that the name is persisted and
must not be renamed without a migration. Do not “fix” these while grepping.

### 3.4 Persisted display-name migration

The scene, collection, helper objects, node groups and modifier names are
user-visible, not animation paths. Rename them on load so existing files look
branded and the engine keeps finding them:

- `helpers.py`: `SCENE_NAME`, `COLLECTION_NAME`, `HELPER_PREFIX`.
- `nodes.py`: `GROUP_PREFIX`.
- Modifier names built in `stack.py`/`ops.py`/wherever `GH ·` is produced.
- On register/load: if the old scene/collection exists and the new name does
  not, rename it in place; walk the helper objects and groups and replace the
  old prefix; rename modifiers. `helpers.adopt_existing()` is the natural place.
- If the migration misses a node group or helper, the engine rebuilds it and
  marks the stage `unbound`; stage records, parameters and keyframes remain.
- Add a layer B test: create a file with the old names (or construct the old
  scene/helper records), load it under 0.4.0, assert the stage/params/keyframes
  survive and the names are migrated.

Keep the `GH Stage`/`GHS` strings in the historical docs; only the live code and
current user-facing strings change.

### 3.5 C# and build specifics

- Rename namespace/usings/classes; keep the `[Guid]` attributes exactly.
- `Stageloom.Core.dll`, `Stageloom.gha`, `Stageloom*Tests.exe`.
- Update resource names (`GhStack.icons.*` → `Stageloom.icons.*`), build scripts,
  deploy script, `scripts/check-deployed`, `scripts/test-gha`.
- Deploy to `Libraries/Stageloom`; remove the old `Libraries/GhStack` copy.
  Two assemblies with the same component GUIDs must not be loaded together.
- The old `.gha` and old add-on must be removed before installing 0.4.0. Put
  this in the release notes and the README upgrade section.

### 3.6 Changelog and upgrade note

Fold the current `## [Unreleased]` into a new `## [0.4.0] — 2026-10-??` section.
Do not lose the wire-edge/curve-attribute entries. Add a short rebrand lead:

> Stageloom is the new name for GH Stack. Remove the old add-on and
> `GhStack.gha` before installing 0.4.0 — the extension id and assembly name
> changed, and the old `.gha` shares component GUIDs. Protocol v2 is unchanged.
> Existing `.blend` stages, parameters and animation are preserved; helper,
> scene and modifier names migrate on load. The old frame cache is not read.

Then the user-facing 0.4.0 changes: wire meshes/curve attributes, the data-root
location, and anything user-visible from compliance. Do not list the package
rename mechanics, ruff, the build-script dedup or other internal churn.

---

## 4. Workstream B — compliance polish

### 4.1 Audit snapshot (at `5837fd9`)

| Item | State |
|---|---|
| G1 `online_access` gate | Code done in `server.py`; no negative test |
| G2 `sys.path` | Done; no code path, only comments/vendor |
| G3/G4 `files` + `clipboard` | Done |
| G5 licence | Done (`LICENSE`, README, manifest) |
| G6 `bl_info` | Done; generated only by `scripts/install-addon` |
| G7 self-containment | Open — pre-ask + listing |
| S1 floor | Settled: keep 5.2.0 |
| S2 tags | Open — add three official tags |
| S3 `log_level` | Done |
| S5 ruff | **Open** — reviewer command reports 100 × F401 |
| S7 dead stubs | Done |
| S8 wheel hash | Done in `scripts/build-extension` |
| S9 data root | Open — move to extension user dir |
| Packaging truthfulness | **Open** — validator/build/install statuses are masked |
| Listing assets/support | Open |
| Compliance doc | Stale; rewrite after the work |

Current ruff result:

```text
uvx ruff check --output-format=concise --ignore=E402,E741,E731 .
# 100 × F401 in blender/gh_stack/blender/engine/__init__.py
```

### 4.2 Ruff

- The 100 F401s are the engine facade re-exports. Fix with an explicit
  `__all__` in `engine/__init__.py` (or redundant `import x as x` aliases).
  No behaviour change.
- Re-run the exact reviewer command; it must be clean.
- Add a CI step (pin a ruff version; `uvx` is available locally). Do not add a
  runtime dependency to the add-on.

### 4.3 Truthful packaging and install scripts

`scripts/build-extension`, `scripts/install-extension` and
`scripts/test-extension` currently pipe Blender commands through
`grep ... || true`, so a failed validation, build or install can be ignored and
an older zip can be picked by `ls -t`.

Required:

- Capture each Blender command’s output and exit status; print the filtered
  output; fail on non-zero. `set -euo pipefail` is already set, but the pipes
  defeat it.
- Read the manifest version and select `dist/stageloom-$VERSION.zip` explicitly;
  do not pick the newest zip by mtime. Delete or ignore stale zips.
- Validate the source **and** the built zip; fail on either.
- Extend the forbidden-file assertion with `__pycache__/` and `.pyo`.
- Fail if the built zip’s version does not match the manifest.
- `install-extension` must not print “installed” when `install-file` failed.
- Add a negative check (e.g. a temp source copy with an invalid manifest or a
  bad wheel digest) proving the script stops.

### 4.4 Offline-access test

The code is correct; the suite has no negative test. Add a second headless
Blender invocation **without** `--online-mode`:

1. Factory settings, enable the add-on.
2. Assert `bpy.app.online_access` is false, no server is running, and
   `bridge.state["notice"]` carries “Online access is disabled”.
3. Set `bpy.context.preferences.system.use_online_access = True`.
4. Call the Start operator; assert the server listens and the notice clears.
5. Stop the server and disable the add-on.

Integrate it into `scripts/test-blender` as a separate process. The main suite
currently runs with `--online-mode` and cannot exercise the off state.

### 4.5 Data root

Default the frame cache/log root to Blender’s per-extension user directory:

```python
bpy.utils.extension_path_user(__package__, path="data", create=True)
```

- `prefs.py`: compute the default from the add-on package (strip `.blender`);
  fall back to `~/.local/share/stageloom` for the development install, where
  the package is not a `bl_ext.*` module.
- Keep the “Data root” preference as an explicit override.
- The old GH Stack cache (`~/.local/share/gh-stack`) is not migrated; the cache
  is disposable. Document the new location.
- Tests: dev fallback, extension path under a throwaway
  `BLENDER_USER_EXTENSIONS`, override honoured, cache writes land there.
- Update the installed test comment (“real data root”) and README/plan wording.

### 4.6 Manifest

- Add `tags = ["Geometry Nodes", "Pipeline", "Modeling"]`.
- Keep `blender_version_min = "5.2.0"`; add the layered-action reason as a
  comment.
- Leave `platforms` omitted (pure-Python wheel).
- Confirm permission strings are under 64 chars with no trailing period.
- Validate source and zip with `blender --command extension validate`.

### 4.7 README and submission packet

- Title: **Stageloom**; mention “formerly GH Stack” once.
- Requirements: Blender 5.2+, Rhino 8 + Grasshopper, Online Access; without
  Rhino the add-on is inspectable via `tools/fake_gh`.
- Add a **Reviewer quick start (no Rhino)** block:
  1. Install the zip / enable.
  2. Enable Online Access.
  3. Add a stage; expected `disconnected` state.
  4. Run `python3 tools/fake_gh/fake_gh.py --stage <key>`; expected `ok`.
- Add a Support line pointing at GitHub Issues.
- Create `docs/submission.md` with paste-ready listing copy:
  name, tagline, short/long description, requirements, limitations, support,
  licence/source, self-containment statement, test-evidence commands, G7 thread
  link, release notes.
- No McNeel/Rhino/Grasshopper logos in the icon or screenshots. Descriptive use
  of “Grasshopper” in the tagline/description is fine.

### 4.8 Listing assets

- Icon: 256×256 PNG, original Stageloom loom/stack motif.
- Three 16:9 static screenshots, under ~2 MB each, no heavy GIFs:
  1. Blender viewport + modifier stack + Stageloom N-panel with parameters.
  2. Rhino/Grasshopper graph: Stage In → definition → Stage Out + Param.
  3. The no-Rhino path: `fake_gh` running and the panel connected.
- Store in `docs/listing/` for reproducibility. Use `DEMO.md`, not private
  FiraTower data.

### 4.9 G7 pre-ask (owner action)

Post the draft in Appendix A on Blender Chat `#extension-moderators` before the
upload. Record the thread URL and answer in `docs/extensions-compliance.md` and
`docs/submission.md`. Do not upload until the answer is clear. If the answer is
negative, keep GitHub-only distribution and ask for the specific change; do not
hide the companion.

### 4.10 Compliance record

Rewrite `docs/extensions-compliance.md` as the current record, not a second
plan:

- G1–G6/S3/S7/S8 closed at 0.3.1/0.4.0, with evidence.
- S1 settled: 5.2.0, with the layered-action reason.
- S2/S5/S9 open with the 0.4.0 fixes.
- G7 and the listing open.
- Correct the stale study: `support` is listing metadata (manifest schema 1.0.0
  has only `website`); G1 line references moved to `server.py` and the engine
  package; S5 is the engine-facade F401 set; S9 is the data-root move.
- Record the G7 thread and answer when it arrives.

---

## 5. Workstream C — release, upload, acceptance

### 5.1 Release 0.4.0

1. Finish the rebrand and compliance work; run the full verification matrix.
2. Bump to 0.4.0 in the six version-sync locations.
3. Fold `Unreleased` into the 0.4.0 changelog section; add the upgrade note.
4. Update `docs/plan.md` status, `docs/backlog.md`, and rewrite
   `docs/extensions-compliance.md` as the current Stageloom record.
5. Build:
   - `scripts/build-extension` → `dist/stageloom-0.4.0.zip`
   - `grasshopper/build/build-gha.sh` → `grasshopper/build/out/Stageloom.gha`
   - `grasshopper/deploy/deploy.sh`
   - `scripts/check-deployed`
6. Validate the zip; inspect its contents; remove the old `GhStack.gha` from
   the wine Libraries before deploying the new one.
7. Commit, tag `v0.4.0`, push, and create the GitHub release with the zip and
   `Stageloom.gha`. Use the 0.4.0 changelog section as the body.

### 5.2 Platform upload

- First upload is **manual** via the profile “Upload Add-on” form on
  `extensions.blender.org`; the CI/CD upload API is for later versions.
- Fill: zip, icon, screenshots, name “Stageloom”, tagline, description, tags,
  support URL, licence/source, release notes, G7 thread link.
- The platform final-checks the `stageloom` id; fallback name is Stagesmith.
- Moderation: answer questions; each revision bumps the version (0.4.1, …).
- On acceptance, install from the platform in a **clean** Blender config (not
  the local zip), run the `fake_gh` round trip, and verify the wheel, the
  online-access gate and the data root.

### 5.3 After acceptance

- Update README with “Install from Blender’s Get Extensions”, the listing URL
  and the `.gha` release link.
- Close `docs/extensions-compliance.md` with the listing URL and outcome.
- Add a tag-triggered CI job that builds/validates the zip; optionally upload
  via the platform API.
- Optional repo hygiene: remove the tracked Linux `.so` under the new
  package’s `vendor/websockets/` from git (it is excluded from the zip but is a
  reviewer question); confirm no `.exe`/`.so`/`.pyc` are tracked.
- The 4.2 LTS compatibility project remains separate.

---

## 6. Verification matrix

```text
scripts/test-core
scripts/test-blender              # includes the rebrand migration + offline gate
scripts/test-gha                  # Stageloom* tests; component GUIDs unchanged
scripts/test-extension            # bl_ext.user_default.stageloom
uvx ruff check --output-format=concise --ignore=E402,E741,E731 .
blender --command extension validate blender/stageloom
scripts/build-extension           # must fail on any validator/build error
blender --command extension validate dist/stageloom-0.4.0.zip
unzip -Z1 dist/stageloom-0.4.0.zip
scripts/check-deployed
```

Expected baseline at the start: core 57, blender 202, gha 12 fixtures + suites,
extension PASS. New tests add to these counts.

Also check:

- No `sys.path` code in `blender/stageloom` (comments only).
- No `bl_info` in the shipped `__init__.py`.
- Zip: no `vendor/`, `.so`, `.exe`, `.pyc`, `.pyo`, `__pycache__`, stray zip.
- Manifest: id/name/tags/licence/copyright/permissions/version.
- Old `.blend`: stages, params, keyframes survive; scene/helper/modifier names
  migrate.
- Old add-on and old `.gha` removed before the new install; no duplicate GUIDs
  or port conflict.
- Ruff clean; build/install scripts fail on a deliberate error.
- Platform clean install + `fake_gh` round trip.

---

## 7. Risks and rollback

- **Rename regressions.** The migration test and the full suite are the gate.
  If the rebrand destabilises the tree, `v0.3.1` remains the public release;
  the rebrand can be reverted before the platform upload. The platform id is
  not reserved until the first upload.
- **Old/new coexistence.** The old add-on can hold the port and the old `.gha`
  has duplicate GUIDs. The release notes must say to remove both.
- **G7.** The only architectural rejection risk. Do not upload before the
  moderator answer; keep GitHub-only as the fallback.
- **Platform id.** `stageloom` looked free; if taken, Stagesmith.
- **Windows-native Rhino** is still untested; the add-on is pure Python and the
  wheel is platform-neutral. State this honestly in the listing.
- **Historical records** keep the old name. Do not sweep `spikes/`, the review,
  the live-sync report or old changelog sections.

---

## 8. Definition of done

- [ ] Stageloom package, manifest id/name, UI, C# namespace/assembly and docs
      renamed; historical records untouched.
- [ ] Component GUIDs unchanged; persisted keys carved out and documented.
- [ ] Old `.blend` migration test passes (stages/params/keyframes preserved,
      display names migrated).
- [ ] Ruff clean under the reviewer command; CI lint step added.
- [ ] Build/install/test scripts fail honestly; no stale-zip fallback.
- [ ] Offline-access test passes in a process without `--online-mode`.
- [ ] Data root uses the extension user directory; override works.
- [ ] Manifest tags/floor/permissions validated.
- [ ] README reviewer quick start + support; `docs/submission.md` ready.
- [ ] Icon and three screenshots ready.
- [ ] G7 answer recorded and linked.
- [ ] `v0.4.0` GitHub release with `stageloom-0.4.0.zip` and
      `Stageloom.gha`.
- [ ] Platform upload accepted; clean install from the platform completes the
      `fake_gh` round trip.
- [ ] Compliance doc closed; README points at the platform listing; CI builds
      and validates the zip on tags.

---

## Appendix A — G7 draft

> Hi #extension-moderators — I’m preparing to submit **Stageloom** (formerly
> GH Stack), a Blender add-on that drives Grasshopper definitions from Blender
> as stackable modifiers.
>
> The Blender add-on is self-contained: it installs from the extension zip,
> checks `bpy.app.online_access`, and shows a clear disconnected state when
> nothing is connected. The Grasshopper companion (Stage In/Out, Param) is
> built from the same GPL-3.0-or-later repository and talks to Blender only
> over a loopback WebSocket (`127.0.0.1`). It is optional for inspecting the
> add-on; the repo bundles `tools/fake_gh`, a stand-in that completes the loop
> without Rhino.
>
> Does this fit the “self-contained / localhost is fine / no external servers”
> guidance? I want to confirm before uploading, since the companion is external
> software even though it is open source and in the same repository.
>
> Repo: https://github.com/miscasquel/stageloom
> No-Rhino path: README → Requirements / Reviewer quick start

---

## Appendix B — key paths

| What | Path |
|---|---|
| Manifest | `blender/stageloom/blender_manifest.toml` |
| Python package | `blender/stageloom/` |
| Engine facade (ruff fix) | `blender/stageloom/blender/engine/__init__.py` |
| Helpers / scene names | `blender/stageloom/blender/helpers.py` |
| Node group prefix | `blender/stageloom/blender/nodes.py` |
| Saved stack records | `blender/stageloom/blender/stack.py` |
| Param custom props | `blender/stageloom/blender/props.py` |
| Operators/panels | `blender/stageloom/blender/ops.py`, `ui.py` |
| Preferences / data root | `blender/stageloom/blender/prefs.py` |
| Online gate | `blender/stageloom/blender/server.py` |
| C# components/GUIDs | `grasshopper/src/gh/Components.cs`, `AttributeComponents.cs` |
| C# build | `grasshopper/build/build-core.sh`, `build-gha.sh` |
| Deploy/check | `grasshopper/deploy/deploy.sh`, `scripts/check-deployed` |
| Extension build/install/test | `scripts/build-extension`, `install-extension`, `test-extension` |
| Headless tests | `blender/tests/headless/` |
| Installed extension test | `blender/tests/installed/run.py` |
| Compliance study | `docs/extensions-compliance.md` |
| Submission packet | `docs/submission.md` (to create) |
| Listing assets | `docs/listing/` (to create) |
