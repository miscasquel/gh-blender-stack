# 0017 — Compatibility shims and their removal versions

**Status:** Accepted, 2026-11-16 (0.8.0 T2)

## Context

The quality plan (D3, §2.1) requires every compatibility shim to name why it
exists, the test that exercises the old form, and the version after which it is
removed. The 0.8.0 T2 subtraction pass reviewed every legacy branch and every
public alias. Test-only aliases and overloads were deleted; branches that read a
saved 0.6/0.7 form were kept only where the old form can still load.

The 1.0 plan promises that opening an earlier project preserves stages,
parameters, keyframes, helpers and animation. These shims therefore stay through
1.0.0 and are removed in **1.1.0**, when the in-memory compatibility surface can
be dropped without breaking that promise.

## Kept shims

| Shim | Why it exists | Test that exercises the old form | Removed after |
|---|---|---|---|
| Legacy port migration `9130 → 59130` (`prefs._migrate_old_port`, `OLD_DEFAULT_PORT`) | Blender saved 9130 as the old default; 9130 is also BlenderKit's port, so leaving it would silently point at another add-on. | `blender/tests/headless/test_registration.py::test_saved_old_default_port_is_migrated` | 1.1.0 |
| Legacy data-root migration `~/.local/share/gh-stack →` the extension data directory (`prefs._migrate_old_data_root`, `OLD_DATA_ROOT`) | A preference saved before the rebrand still names the old directory; moving it keeps logs with the add-on. | `blender/tests/headless/test_registration.py::test_saved_old_data_root_is_migrated` | 1.1.0 |
| Pre-rebrand scene, collection, helper and modifier names (`helpers.OLD_SCENE_NAME`, `OLD_COLLECTION_NAME`, `OLD_HELPER_PREFIX`; `nodes.OLD_GROUP_PREFIX`, `OLD_MODIFIER_PREFIX`; `helpers.adopt_existing`, `nodes.migrate_names`) | 0.6 projects save `GH Stage`, `GH Stage Data`, `GHS …` helpers and `GH · …` modifiers; renaming them on load is cheaper than a document rewrite and keeps helpers owned. | `blender/tests/headless/test_rebrand.py::test_old_names_are_migrated_and_persisted_keys_stay` | 1.1.0 |
| Object-name-based helper identity (`helpers._migrate_identity_names`) | 0.6/early-0.7 helpers were named after the object (`SL <object name>.<key>`); an object rename then created a second empty set. The stable stack-id name adopts the old set and its data. | `blender/tests/headless/test_rebrand.py::test_object_name_helpers_move_to_the_stable_identity` | 1.1.0 |
| Missing or malformed definition part count (`helpers.DEFINITION_PARTS_KEY = "gh_stack_parts"`, default `1`) | A definition collection saved before multi-part definitions has no key (or a corrupt value); reading it as one part preserves the original single-part shape instead of rejecting the frame. | `blender/tests/headless/test_instances.py::test_a_definition_collection_without_the_parts_key_is_one_part` | 1.1.0 |
| Surface-object prefix read (`engine/request._prefix_sections` SURFACE branch, `geometry_io.object_to_sections` SURFACE branch, `geometry_io.surface_to_mesh`) | A document saved before 0.4.1 can contain a stage on a surface object. Add Stage now refuses surfaces, but the old stage must still read its prefix by meshing the evaluated surface rather than dropping the geometry. | `blender/tests/headless/test_stage_addressing.py::SurfaceStageRejectionTests::test_02_surface_object_sections_still_mesh_for_an_old_document` | 1.1.0 |
| Attributes under the pre-rebrand dictionary key (`AttributeStore.RootKey = "GHStack.Attributes"`) | Rhino geometry saved by a pre-Stageloom component stores its attributes under the literal old key; reading it keeps those attributes alive through the upgrade. | `grasshopper/tests/console/AttrTests.cs::CheckLegacyRootKey` pins the literal; `InstanceTests.cs::CheckLegacyRootKey` reads a geometry dictionary saved under it (runs where RhinoCore exists) | 1.1.0 |
| `StageInComponent.HasOutput` | A Grasshopper document saved before the `Attributes` output was added loads the file's own output list. Writing a newly declared output by name throws; the guard skips only that write, so the old document still solves. | `grasshopper/tests/console/ComponentTests.cs::RunStageInLegacyOutputs` | 1.1.0 |
| Document-less slot lookup (`GhLink.Slot(string key)`) | A script or console check that has no `GH_Component` owner needs a slot without a document; product components use `Slot(owner, key)`, which preserves document isolation. The overload never bypasses ownership for a key that has an owner. | Every console slot setup (`LinkTests`, `ComponentTests`) and the scripted checks | 1.1.0 |

The `gh_stack_id`, `gh_stack_format` and `gh_stack_nested` keys are current
format stamps, not legacy reads: they are written on every new helper/group and
are part of the present identity/continuity contract.

## Deleted test-only shims (0.8.0 T2)

These had no owner, no external consumer and no old persisted form. They were
removed rather than inventoried:

- `GhLink.PublishStage` — tests now call `PublishStageOut`.
- `GhLink.SetSlotStatus` — tests now call `SetOutStatus`.
- `Instances.Emit` text-only warning overloads and the non-capability
  convenience overload only they used — callers use the rich `WireWarning`,
  capability-aware overload.
- `Conversion` text-only warning overloads (`SectionsFromCurves`,
  `SubDFromSections`, `SectionsFromSubD`, `SectionsFromGeometry`) — the live
  Rhino check now builds the same `List<WireWarning>` the product uses.

## Consequences

- No shim is un-owned: every remaining branch is listed above with a test and a
  removal release.
- 1.0 keeps reading 0.6/0.7 documents; 1.1 is the first release allowed to break
  them.
- A new shim without a row here, a migration test, and a removal version is
  rejected in review.
