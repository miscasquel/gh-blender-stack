# Refactor seams — 0.8.0 characterization map (T3, T4)

**Status:** T3, the T4 `geometry_io` split and the T5 engine-object
refactor are complete. This document is the contract for T4–T7: every seam the
four refactor tasks may move, its owner, the behavior that must not change, and
the tests that pin it. T3 changed no production code; T4 moved
`geometry_io.py` into a package behind the same facade; T5 replaced the engine
hub with `ENGINE`/`StageRuntime` without changing behavior. T6–T7 remain.

**Measured at T3 (same machine, 2026-11-16):**

- 123 core tests.
- **454** Blender headless tests plus the offline gate
  (`scripts/test-blender`); 85–87 s.
- **681** passing C# checks in the codec runner and seven console suites
  (`scripts/test-gha`); 22–25 s. `InstanceTests` and `ConversionTests` print an
  explicit `SKIP native …` line under wine: those native Rhino checks are a
  known gap, not coverage.
- `docs/code-health.md` records 454 Blender test functions and 537 declared C#
  `Check(...)` calls; the 681 is the runtime pass count, because some checks run
  inside loops.
- Fourteen golden fixtures; `out_hash`, protocol and fixtures untouched.

**Measured at T4 (same machine, 2026-11-17):**

- **454** Blender headless tests plus the offline gate, unchanged.
- **681** passing C# checks in the codec runner and seven console suites,
  unchanged (native Rhino halves still skip under wine).
- Production: **24,946 physical / 19,394 code lines across 56 files** (flat
  physical, −20 code against the T1 baseline). The new `geometry_io` package is
  **2,151 physical / 1,791 code lines** in 8 files, replacing the 2,151 /
  1,811-line `geometry_io.py`.
- Fourteen golden fixtures; `out_hash`, protocol and fixtures untouched.
- Performance: the T4 verification used the then-current median baseline and
  passed 26/26; T5 replaced it with the 2026-11-17 best-of-three baseline. The
  current gate result is in [`performance-ledger.md`](performance-ledger.md).

**Measured at T5 (same machine, 2026-11-17):**

- **454** Blender headless tests plus the offline gate, unchanged.
- Production: **24,738 physical / 19,376 code lines across 57 files** (T4:
  24,946 / 19,394 across 56; −208 physical, −18 code). The engine package is
  **5,375 physical** against 5,580 at T4; the facade is 42 lines against 852.
- Fourteen golden fixtures; `out_hash`, protocol and fixtures untouched.
- Performance: the final full gate passed **26/26** checks in 174.1 s against
  the 2026-11-17 best-of-three baseline. Decode p50 is +0.80%, 100k in-place
  +0.95%, 1M in-place +0.32% and 13× instance repeat +2.06%; no metric exceeds
  +5%. The earlier median-baseline failure was host CPU power/core-state drift,
  confirmed by a pinned T4/T5 A/B (4.5736 vs 4.5491 ms decode) and replaced by
  the new baseline, not waived.

Notation: a Python test is `file.py::Class.test_name`; a C# check is
`Suite::Check("…")` as printed by the console suite. Where a group of checks is
named as `Suite::CheckGroup`, that means every `Check(...)` inside the group.

---

## 0. Summary matrix

Legend: **E** = existing coverage before T3; **T3** = added by this task;
**G** = known gap the local harness cannot cover (see §5).

T4 is complete: `mesh.py`, `attributes.py`, `curves.py`, `points.py`,
`instances.py` and `dispatch.py` own their kinds; `__init__.py` is the facade.
The T4 rows below keep the original coverage map.

| Task | Seam | Coverage | T3 additions |
|---|---|---|---|
| T4 | mesh read/write, groups, creases, colours | E | malformed groups, colours-only group, corner-colour fold, wrong-domain crease, absent colours/creases |
| T4 | attributes read/write, prune, descriptors | E | unknown type/domain notes, prune keeps native channels |
| T4 | curves read/write | E | short/spanless spline notes, absent group is not "clear" |
| T4 | points read/write | E | incomplete/shape/mismatch classified errors; atomicity gap G |
| T4 | instances/definitions read/write | E + G | — (native Rhino paths skip under wine) |
| T4 | dispatch/facade, surface, empty geometry | E (mesh/curve/points) | surface meshed + note, empty/EMPTY object, duplicate-section guard |
| T5 | hub/facade entry points | E partial | no active object, missing stack object, unique-key resolve, malformed hello |
| T5 | lifecycle/registry, load/undo/redo, scenes | E | active registration report; dormant diagnostics |
| T5 | request/prefix/apply, states, conflicts | E | — |
| T5 | playback gate | E | — |
| T5 | bake and dependent freeze/rollback | E | — |
| T5 | start/stop server, timer dormancy | E partial | bind failure visible + recovery; dormant tick rate |
| T6 | `StageRegistry` claims/conflicts/isolation | E | — |
| T6 | `LinkConnection` start/stop/restart/handshake | E | idempotent Stop, invalid-port Configure |
| T6 | `RequestScheduler` one-in-flight/latest-wins | E | — |
| T6 | `ProtocolHandler` error codes/malformed/caps | E | — |
| T6 | `Publication` dirty flags/hello/schema | E | — |
| T6 | `StageSlot`/`HeldFrame` snapshots | E | null slot, empty-frame sentinel |
| T6 | outbox cap | G (private) | deterministic reflection test (moves with the fields) |
| T7 | `Build` inbound grammar/rows/cycles | E + G | no-table and table-without-rows guards |
| T7 | `Emit` outbound rows/definitions | E + G | null/empty entry guards; real-geometry emit is G |
| T7 | definition writer table/parts/caps/fallback | E | — |
| T7 | Conversion/Attributes per-kind dispatch | E + G | — (native definition members skip under wine) |

---

## 1. T4 — `geometry_io` package split

**Current owner (T4 complete):** `blender/stageloom/blender/geometry_io/`
(2,151 physical / 1,791 code lines in 8 files): `common.py`, `attributes.py`,
`mesh.py`, `curves.py`, `points.py`, `instances.py`, `dispatch.py` and the
`__init__.py` facade. Dependency direction is one-way:
`common` <- `attributes` <- `mesh`/`curves`/`points` <- `instances` <-
`dispatch` <- facade. The facade preserves all 43 non-underscore names; private
test hooks moved to their owning modules. The public facade names are
`mesh_to_sections`, `object_to_sections`, `geometry_set_to_sections`,
`sections_to_mesh`, `sections_to_curve`, `sections_to_points`,
`write_attributes`, `curve_to_sections`, `points_to_sections`. `GeometryIOError`
is the classified failure type; `notes` lists are the visible-warning channel.

### 1.1 Mesh read/write

**Entry points:** `mesh_to_sections`, `read_colors`, `read_creases`,
`loose_edge_indices`, `isolated_vertex_indices`, `wire_mesh_to_sections`,
`select_mesh_group`, `sections_to_mesh`, `_topology_matches`, `write_colors`,
`write_creases`, `_faces_from`.

**Must not change:**

- Group selection is atomic: mesh and SubD are one unit; both complete means
  mesh wins; a partial group (including `mesh.colors` with no vertices) is a
  `GeometryIOError`; no vertices are ever paired with the other group's faces.
- `sections_to_mesh` validates the whole group before touching the mesh:
  verts N×3, `face_indices` length == sum of counts, every count ≥ 3, every
  index < vertex count. A failure is classified and the destination is
  unchanged.
- Same counts are not enough: face order/loop indices must match to take the
  position-only path; otherwise rebuild. The topology token path skips the
  polygon/loop read only when token and previous token match and the cheap
  counts agree. A changed token rebuilds; an absent/malformed token uses the
  verified path and stores no token.
- Native colour is u8 sRGB on the wire: `BYTE_COLOR` direct, `FLOAT_COLOR`
  converted from linear; CORNER folds to one value per vertex, last loop wins.
  Absent `mesh.colors` removes only `Col`, not a user colour attribute.
- Creases: only non-zero values travel, by vertex pair; unmatched edges are
  counted in `notes`; wrong domain/type is warned and skipped; absent creases
  are written as an explicit zeroed channel, never left stale.
- Wire meshes: loose edges become two-point curves and isolated vertices a
  point cloud; crease loss on a face-less mesh is a counted note.

**Existing tests:**

- `test_geometry_io.py::SectionRoundTripTests.test_quad_and_ngon`,
  `test_sections_to_mesh_rebuilds_and_matches`,
  `test_sections_to_mesh_in_place_position_update`,
  `test_topology_change_with_equal_loop_count_rebuilds_faces`,
  `test_quad_diagonal_flip_with_identical_counts_rebuilds_faces`,
  `test_identical_topology_takes_the_position_only_fast_path`,
  `test_token_fast_path_still_writes_colours_and_creases`,
  `test_mesh_and_subd_groups_are_selected_as_units`,
  `test_unmatched_crease_edges_are_noted`, `test_colors_round_trip`,
  `test_float_color_native_reads_scene_linear_as_srgb`,
  `test_byte_color_native_stays_exact`, `test_creases_round_trip`,
  `test_subd_names_and_no_colors`, `test_frame_round_trip`.
- `test_topology_token.py::TopologyTokenApplyTests.*` (same token, changed
  token, attribute-only, malformed token, clearing, definition scopes).
- `test_wire_geometry.py::WireMeshTests.*`; `test_geometry_kinds.py::
  SubDRoundTripTests.test_subd_sections_keep_creases`;
  `test_attributes.py::AttributeIOTests.test_native_channels_travel_once`.
- C# peer boundary: `ConversionTests::Check("mesh round trip: … is
  byte-identical")`, `Check("mesh groups: mesh-only selects mesh")`,
  `Check("mesh groups: both complete chooses the mesh group")`,
  `Check("mesh groups: the SubD cage is never taken from mesh.*")`.

**T3 additions:** `test_geometry_io_edges.py::
SectionGroupTests.test_a_colours_only_group_is_an_incomplete_mesh`,
`test_an_absent_or_foreign_group_selects_nothing`;
`MeshWriteValidationTests.test_malformed_mesh_groups_are_classified_and_leave_the_target_untouched`,
`test_corner_colour_folds_to_one_value_per_vertex`,
`test_a_crease_on_the_wrong_domain_is_warned_and_skipped`,
`test_absent_mesh_colours_remove_only_the_native_channel`,
`test_absent_creases_are_written_as_zero_not_left_stale`.

**T4 moved:** `geometry_io/mesh.py` now owns this, with
`select_mesh_group` shared by the attribute writer. The topology-token branch
and `_topology_matches` stayed together; `test_geometry_io.py` and
`test_topology_token.py` now patch the mesh module.

**Gaps:** duplicate section names at *apply* time are unreachable from a decoded
frame (the codec rejects duplicates and `_apply` receives a dict); the aggregate
writer guard is covered in §1.6. `sections_to_mesh` deliberately ignores extra
`crv.*`/`pts.*` sections; that is the engine's per-helper dispatch, not this
writer.

### 1.2 Attributes read/write

**Entry points:** `read_attributes`, `mesh_attributes`, `points_attributes`,
`wire_attributes`, `_attribute_values`, `_emit_attribute`, `write_attributes`
(including `prune=True`), `_decode_strings`, `_attribute_for`,
`_domain_length`, `describe`; the `_FOREACH` type table.

**Must not change:**

- Every wire type in `core.attributes.WIRE_TYPES` has one Blender read path;
  STRING is the only element-by-element type. Unknown types are skipped on
  read; on write they produce a note and no attribute.
- A wrong element count for the target domain is dropped with a note, never
  truncated or repeated. STRING offsets that run past the value bytes, do not
  cover them, or are absent with non-empty values are one-attribute failures,
  not a frame failure.
- The native colour and crease channels are excluded from generic attributes
  and are never pruned or removed by `prune=True`; a helper with no descriptors
  loses its previous generic attributes (absent means none) while Blender
  internals (`position`, leading-dot names) and the native channels stay.
- Definition prefixes and descriptors (`prefix`, `definition`, `part`,
  `section`, `offsets`) are produced by one emitter; part 0 of a multi-part
  definition cannot inherit part 1's values.

**Existing tests:**

- `test_attributes.py::AttributeIOTests.test_round_trip_every_supported_type`,
  `test_a_wrong_count_is_dropped_with_a_note`,
  `test_malformed_string_offsets_are_skipped_with_a_note`,
  `test_string_offsets_must_cover_the_values_exactly`,
  `test_native_colour_write_is_always_byte_srgb`,
  `test_native_channels_travel_once`,
  `test_a_second_color_attribute_travels_generically`,
  `test_every_wire_type_has_a_foreach_spec`.
- `test_attributes.py::AttributeEngineTests.test_02b_a_response_without_attributes_clears_them`,
  `test_03_an_attribute_value_change_is_new_geometry`.
- `test_wire_geometry.py::WireMeshTests.test_wire_mesh_exports_edges_as_curves_and_points`,
  `CurveComponentTests.test_curve_component_uses_curve_and_curve_point_domains`.
- `test_instances.py::InstanceReadTests.test_a_part_attribute_carries_its_definition_and_part`;
  C# `AttrTests` (all types, string failures, curve/point domains, transforms)
  and `InstanceTests` (`attributes: part descriptors attach only to their part`).

**T3 additions:** `test_geometry_io_edges.py::
AttributeWriteBoundaryTests.test_unknown_type_and_domain_are_noted_and_skipped`,
`test_prune_removes_generic_attributes_but_keeps_native_channels`.

**T4 moved:** `geometry_io/attributes.py` owns one `_FOREACH`/`WIRE_TYPES`
vocabulary and one emitter. `test_every_wire_type_has_a_foreach_spec` now reads
`geometry_io.attributes._FOREACH`.

**Gaps:** the read side silently skips a Blender type absent from
`WIRE_TYPES`; that is Blender's own vocabulary and cannot be produced through
RNA in a test, so the write-side note is the observable contract.

### 1.3 Curves read/write

**Entry points:** `curve_to_sections`, `_bezier_net`,
`curves_component_to_sections`, `curves_component_attributes`,
`_component_bezier_net`, `sections_to_curve`, `_poly_spline`,
`_nurbs_spline`, `_bezier_spline`; curve-type/knot constants.

**Must not change:**

- Poly, NURBS (order, weights, endpoint/uniform, cyclic) and Bezier nets round
  trip; a Bezier travels as a degree-3 NURBS in Bezier knot mode.
- `sections_to_curve` validates the whole group before `splines.clear()`:
  required `crv.points`/`crv.counts`, sum equality, ≥2 points per curve, type,
  order 2..6 and ≤ control points, knot mode, cyclic flag, finite positive
  weights, 3k+1 Bezier nets. A malformed group raises `GeometryIOError` and
  leaves the destination intact. An absent group is an error here; the engine
  clears the helper before calling, which is the public "absent means clear".
- Catmull-Rom, spanless Bezier, short splines, bad NURBS and unknown component
  types are skipped with counted notes; a Bezier control net suppresses
  per-point attributes with a note (no 1:1 mapping).
- `curve_to_sections([])` for an empty curve returns no sections; the engine
  path clears the helper.

**Existing tests:**

- `test_geometry_kinds.py::CurveRoundTripTests.*` (poly, NURBS, cyclic,
  rational, evaluated component, Bezier circle, closed cyclic Bezier).
- `test_geometry_kinds.py::CurveValidationTests.*` (order, lengths, type/mode/
  cyclic/Bezier shape, weights, untouched destination).
- `test_geometry_kinds.py::PointRoundTripTests` is the point neighbour.
- `test_wire_geometry.py::CurveComponentLossNoteTests.*` (Catmull-Rom, missing
  Bezier handles, unsupported NURBS, notes through `geometry_set_to_sections`).
- C# `ConversionTests` managed curve checks (`curves: valid sections
  round-trip byte-identically`, `curves: a null curve is reported as
  curve_skipped`); native Rhino curve round trips are skipped under wine.

**T3 additions:** `test_geometry_io_edges.py::
CurveReadLossTests.test_short_and_spanless_splines_are_counted`,
`test_an_absent_curve_group_is_a_classified_error_not_a_clear`.

**T4 moved:** `geometry_io/curves.py`; `_CurveBatch` and `crv_sections` own the
six-section builder, with the validation order and error texts unchanged.

**Gaps:** the Bezier point-attribute skip note is not directly tested; it is
reached only with a component that has Bezier handles plus a POINT attribute.
Existing tests cover the no-handles skip and the curve/point attribute mapping.

### 1.4 Points read/write

**Entry points:** `points_to_sections`, `read_point_colors`,
`sections_to_points`, `write_point_colors`, `POINT_RADIUS_DEFAULT`.

**Must not change:**

- `pts.radii` is emitted only when at least one radius is non-zero; absent on
  write resets every point to `POINT_RADIUS_DEFAULT` (a previous response's
  radii must not survive).
- Absent `pts.positions` with any other `pts.*` section is an incomplete group
  (`GeometryIOError`); a positions array that is not N×3 is classified; both
  leave the cloud untouched.
- A radii/positions count mismatch is classified.
- An empty section list clears the cloud (`resize(0)`); this is the engine's
  "absent means none" contract. `pts.colors` absent removes only `Col`.
- Native point colour uses the same u8 sRGB mapping as meshes.

**Existing tests:**

- `test_geometry_kinds.py::PointRoundTripTests.test_points_round_trip`,
  `test_absent_radii_clear_a_previous_response`, `test_empty_points_clear`.
- `test_geometry_io.py::SectionRoundTripTests.test_float_color_native_reads_scene_linear_as_srgb`
  (shared colour mapping); `test_attributes.py::AttributeIOTests.test_native_channels_travel_once`.

**T3 additions:** `test_geometry_io_edges.py::
PointWriteValidationTests.test_an_incomplete_point_group_leaves_the_cloud_untouched`,
`test_a_malformed_position_array_leaves_the_cloud_untouched`,
`test_a_radii_count_mismatch_is_classified`;
`MeshWriteValidationTests.test_absent_point_colours_remove_only_the_native_channel`.

**T4 moved:** `geometry_io/points.py`.

**Gap (finding, low severity):** a radii count mismatch is raised *after*
`cloud.resize(n)` and the positions write, so that one malformed group is not
atomic. T4 preserved that behavior; a later task may tighten it. See §5.

### 1.5 Instances and definition table read/write

**Entry points:** `geometry_set_to_sections`, `_serialize_geometry`,
`_serialize_instances`, `_serialize_definition`, `_definition_parts`,
`_reference_parts`, `_member_parts`, `_object_parts`, `_instance_rows`,
`_matrix_wire`, `_definition_prefix`, `_append_definition_rows`,
`_split_part_name`, `_reference_name`, `_is_identity`, `MAX_INSTANCE_DEPTH`.

**Must not change:**

- Single-part definitions keep `idef.<i>.*` and no `parts` field; multi-part
  definitions use `idef.<i>.<p>.*` with definition-level `idef.<i>.inst.*`
  rows; nested rows stay at definition level and index the same flat table.
- A `blocks/2` peer gets one multi-part entry; a peer without it gets one
  single-part definition per part, rows fan out at the same placement, part
  names are distinct and a counted stage note records the fallback. No silent
  collapse.
- Definition names come from the source; content dedupe/name collision is the
  C# outbound side (§4), while the Blender reader keeps names display-only.
- Instance rows are all-or-nothing by reference/transform; out-of-range
  reference indices are counted and skipped; a missing reference/transform
  pair is warned and skipped; recursion depth is capped at
  `MAX_INSTANCE_DEPTH` with a visible note, never a stack overflow.
- `_reject_duplicate_sections` fails the whole aggregate frame on a duplicate
  name instead of letting the codec keep one pool.
- Geometry-set reads cover mesh (including loose remainder), curves, points and
  instance rows in one pass; `object_to_sections` handles MESH, CURVE,
  POINTCLOUD, SURFACE (meshed, noted) and non-geometry objects (skipped, noted).

**Existing tests:**

- `test_instances.py::InstanceReadTests.*` (collection/object definitions,
  transforms, separate children/parts, mixed object+collection, two-level
  nesting, empty part, attributes on parts, kinds, no-`blocks/2` split,
  single-part namespace, own instances, nested row pairs, name hashing, codec
  round trip).
- `test_instances.py::InstanceApplyTests.*` (realized blocks, parts, nested
  rows, multipart fixture, failing multipart frames, clearing definitions,
  Stage scene plumbing).
- `test_instances.py::EnginePrefixTests.*` and `EngineUpdateTests.*`.
- C# `InstanceTests::CheckFixturePlans`, `CheckTableValidation`,
  `CheckNamespaces`, `CheckRows`, `CheckPreflight`, `CheckContentHash`,
  `CheckDescriptorGrammar`, `CheckAttributePartFilter`,
  `CheckOutboundSinglePart/MultiPart/Fallback/ContentKey/Cycle/Attributes`.
- Native `InstanceTests::CheckNativeBuild` and `CheckNativeEmit` are **skipped
  under wine** (`SKIP native instance build: RhinoCore is unavailable`). They
  are the native half of the contract, not covered here.

**T3 additions:** none (existing coverage is already the deepest in the tree).
The native skip is recorded as a gap in §5.

**T4 moved:** `geometry_io/instances.py`; the `_GEOMETRY_PARTS` table,
`geometry_set_to_sections`, the definitions builder, `MAX_INSTANCE_DEPTH` and
the duplicate guard each have one owner.

### 1.6 Dispatch/facade, surface, empty geometry, duplicate guard

**Entry points:** `object_to_sections`, `surface_to_mesh`, `describe`,
`geometry_set_to_sections`, `_reject_duplicate_sections`.

**Must not change:**

- `object_to_sections` dispatches by object type: MESH (wire path when no
  faces), CURVE, POINTCLOUD, SURFACE (evaluated to a temporary mesh, noted,
  datablock removed), everything else returns `([], [])` with a note.
- Empty mesh/cloud is legal and silent: no sections, no note. An EMPTY object
  is a skipped non-geometry object with a note.
- `geometry_set_to_sections` returns `(sections, attributes, definitions)` and
  rejects duplicate names across the whole aggregate.
- `describe` names the owner in notes without changing behaviour.

**Existing tests:**

- `test_geometry_kinds.py::ObjectDispatchTests.test_object_to_sections_by_type`
  (MESH/CURVE/POINTCLOUD).
- `test_wire_geometry.py::WireMeshTests.*` (wire and mixed dispatch).
- `test_geometry_kinds.py::MixedEngineTests.*` (all three helpers in one stage).

**T3 additions:** `test_geometry_io_edges.py::
ObjectDispatchTests.test_a_surface_is_meshed_with_a_note`,
`test_empty_geometry_is_no_sections_and_no_warning`;
`SectionGroupTests.test_duplicate_section_names_are_rejected` (the private guard
is the only deterministic observer and moves with it).

**T4 moved:** `object_to_sections` and `surface_to_mesh` into
`geometry_io/dispatch.py` behind the `_OBJECT_READERS` table;
`geometry_set_to_sections` lives in `instances.py` with the private duplicate
guard, and `test_geometry_io_edges.py` imports the guard from there. The facade
re-exports the public names.

**Gaps:** a duplicate section name cannot be produced through the public
builders today (each kind owns its namespace), so the guard is tested directly.

---

## 2. T5 — Blender engine object (complete)

**Current owner (T5 implementation complete):** `blender/stageloom/blender/engine/` (5,375
physical lines). `engine/runtime.py` owns the session: a single module-level
`ENGINE = Engine()` and one `StageRuntime` record per `(stack_id, stage_key)`.
`engine/__init__.py` is a 42-line facade (`ENGINE`, `register`/`unregister`, the
stable controls and the diagnostics); `apply`, `bake`, `dependencies`,
`diagnostics`, `lifecycle`, `messages`, `playback`, `request`, `state` and
`triggers` own their behavior. Every submodule imports `from .runtime import
ENGINE`, its sibling modules and the core modules it uses; no submodule reaches
back through the package object and there is no `sys.modules[__package__]`.

`StageRuntime` fields: `pending`, `sent`, `last_frame`, `timings`,
`state_since`, `pull_permit`, `pull_deadline`, `last_no_send`. `Engine` owns the
prefix memo/batch/epoch, verification, trace, heartbeat, dirty set, stage
index/claims/owners/conflicts, stack objects/index flags, hot/active/registered
flags, prefix guard (`evaluating`/`guard_owner`/`evaluating_since`/`ticking`),
playback/bake flags/progress/scenes, stats, playback reports and the runtime
records. Timer, hot/idle/soft-retry constants stay module-level.

**Removed:** the 123-name `__all__`; the `sys`, `numpy`, `attributes_mod`,
`codec`, `hashing`, `bridge`, `geometry_io`, `helpers`, `nodes`, `prefs`,
`props`, `server`, `stack_mod` re-exports; the module-level per-stage maps; and
the old 852-line hub. `benchmarks/bench.py`, `ops.py` and `ui.py` use `ENGINE`
or the owning submodule; none reaches `engine.bridge`/`engine.stack_mod`.
Tests were migrated mechanically from the package's private state/functions to
`ENGINE`, `ENGINE.runtime(...)` and their owning modules; assertions and the 454
test count are unchanged, and no test was deleted.

### 2.1 Hub/facade entry points

**Entry points:** `start_server`, `request_again`, `freeze`, `unfreeze`, `push`,
`resolve_duplicate_key`; the diagnostics `registration_report`, `tick_rate`,
`waiting_seconds`, `trigger_health`, `recent_activity`, `stage_timings`; the
registry/lifecycle helpers and the incoming handlers are internal seams.

**Must not change:**

- `start_server` honours the port preference and reports a failure visibly; a
  later start recovers. Current finding: the worker exit path overwrites the
  bind cause with `"server stopped"`; T3 asserts visibility/recovery, not the
  text (§5).
- `push()` with no active object is a no-op; `request_again`/`freeze` on an
  object with no stack record is a no-op; `resolve_duplicate_key` returns None
  and changes nothing unless the key is actually conflicted.
- A malformed client hello (non-list `caps`, non-dict/empty stage entries,
  non-bool `has_stage_in`/`has_stage_out`) is ignored per field: caps reset to
  none and the saved input/output modes survive. A hello itself never sends a
  request; it clears rid/geometry-hash state and arms verification.
- `registration_report` is read-only and names one engine generation; a dormant
  file has no timer, no active handlers and `tick_rate` returns 0.
  `recent_activity` returns a bounded copy; `trigger_health` is empty before an
  event; `waiting_seconds` is 0 without a pending request.

**Existing tests:**

- `test_stage_controls.py::StageControlTests.test_01_freeze_stops_updates`,
  `test_02_pull_refreshes_while_frozen`, `test_03_push_skips_frozen`,
  `test_04_unfreeze_resumes`, `test_07_deleting_the_modifier_removes_the_stage`.
- `test_engine_activity.py::DisconnectedFastPathTests.*`.
- `test_registration.py::RegistrationReportTests.*`,
  `test_adding_and_removing_a_stage_activates_and_deactivates`,
  `test_load_restores_a_missing_timer`.
- `test_stage_state.py::StageStateTests.test_04_unfreeze_settles_to_disabled_or_conflict`,
  `test_05_frozen_error_stays_frozen_and_records_the_error`,
  `test_08_waiting_requires_an_in_flight_request`,
  `test_09_waiting_seconds_clears_on_error_and_idle`,
  `test_10_unknown_states_and_contradictory_flag_targets_are_refused`.
- `test_prefix_memo.py::WaitingAgeTests.*`;
  `test_live_sync.py` diagnostics assertions for `recent_activity`,
  `stage_timings`, `trigger_health`.

**T3 additions:** `test_engine_facade.py::
EngineFacadeTests.test_01_push_with_no_active_object_is_a_noop`,
`test_02_a_missing_stack_object_is_a_noop`,
`test_03_a_unique_key_is_not_resolved`,
`test_04_a_malformed_hello_keeps_the_saved_input_modes`,
`test_05_an_active_engine_reports_one_generation`;
`DormantDiagnosticsTests.test_a_dormant_engine_reports_no_timer_and_a_zero_tick_rate`,
`test_recent_activity_is_a_bounded_copy`,
`test_trigger_health_is_empty_before_an_event`;
`test_registration.py::ServerBindFailureTests.test_a_taken_port_is_visible_and_a_later_start_recovers`.

**T5 may move:** the module-level functions into `Engine` methods (or thin
facade wrappers); `start_server`/diagnostics may move to owned objects. The
tests call the facade names, so a rename is an explicit migration, not a silent
break.

**Known low finding:** `unfreeze(obj, stage)` does not gate on a stack record;
passing a foreign object still writes the stage state and dirties the object.
The public operators pass the owning object, so it is not reachable from the
panel; T5 should decide whether to make it symmetric with `freeze`/`request_again`.

### 2.2 Lifecycle, registry, load/undo/redo, multi-scene

**Entry points:** `register`/`unregister`, `_ensure_index`, `_rebuild_index`,
`_reindex_object`, `_stage_views`, `_claims_for_key`, `_split_duplicate_stages`,
`resolve_duplicate_key`, `_drop_removed_stages`, `_repair_plumbing`,
`_process_object`, `_ordered_stages`, `_mark_dirty_scene`, `_on_load`,
`_on_undo`, `_on_redo`, `_ensure_active`/`_deactivate`.

**Must not change:**

- One index/registry: keys have one proven owner per file; a real duplicate is
  a visible `conflict` and sends/applies nothing until the user resolves one
  claimant. A runtime copy is split deterministically; a saved/undo duplicate
  stays conflicted.
- Stage removal = modifier removal: helpers, definitions, clouds, parameter
  properties, node group and session bookkeeping all go; a missing node group
  is repaired, not treated as stage removal; ambiguous modifier names/uids are
  reported, never guessed.
- Load/undo/redo clear queued frames, pending/sent/verify/pull state and the
  prefix memo; a re-opened stage asks again and can settle. A load with a
  queued stale response must not apply it.
- Multi-scene: a depsgraph/frame event marks only its scene's stacks; a stage
  reads its own scene's frame; the playback gate ignores another scene's
  frame event.
- Registration is idempotent; one enabled engine generation has exactly one
  handler per lifecycle hook and the active handlers only while a stage
  exists. A stage-less file is dormant (no timer, no depsgraph/frame/playback
  handlers).

**Existing tests:**

- `test_lifecycle.py::LifecycleTests.test_01_rename_keeps_the_helper_set_and_group`,
  `test_02_delete_removes_helpers_definitions_clouds_and_group`,
  `test_03_an_object_name_helper_set_migrates_with_its_geometry`,
  `test_04_undo_and_redo_keep_one_identity`,
  `test_05_a_live_stack_with_a_missing_stage_record_is_not_swept`,
  `test_06_remove_helper_removes_every_part_and_cloud`;
  `LegacyFileMigrationTests.test_the_old_file_keeps_its_geometry_under_the_stable_name`.
- `test_duplicates.py::DuplicateStageTests.*`, `RuntimeDuplicateIdentityTests.*`,
  `StrictConflictTests.*`, `SameObjectConflictTests.*`,
  `DuplicateLifecycleTests.*`.
- `test_reload.py::ReopenTests.test_00_the_load_dropped_the_queued_frames`,
  `test_01_engine_hooks_survive_the_load`,
  `test_02_load_resets_waiting_and_marks_the_stack_dirty`,
  `test_03_reopened_stage_talks_to_grasshopper`;
  `RepairTests.*`.
- `test_scenes.py::SceneScopeTests.test_01_each_request_uses_its_objects_scene_frame`,
  `test_02_the_playback_gate_ignores_another_scenes_frame`,
  `test_03_a_depsgraph_event_marks_only_its_scenes_stacks`,
  `test_04_the_prefix_is_read_at_the_objects_scene_frame`.
- `test_registration.py::RegistrationReportTests.*`,
  `test_engine_activity.py::DisconnectedFastPathTests.test_03_an_unrelated_scene_does_not_wake_the_stage`.
- `test_stage_controls.py::StageControlTests.test_06b_deleting_only_the_group_rebuilds_it`,
  `test_06c_a_pre_uid_record_survives_a_deleted_group`.

**T3 additions:** the active-report test in §2.1 (`test_05_an_active_engine…`)
pins one handler per hook and one stack object; the dormant diagnostics class
pins the timer-less state.

**T5 may move:** the index/claims/owners/conflicts and lifecycle handlers into
`Engine`/runtime records. The stage-key conflict and duplicate-split semantics
are one owner; tests stay at `engine.*` facade calls.

**Gaps:** native undo/redo in a GUI session, linked-object editing and a real
file save/reload round trip are not exercised headlessly; the suite drives the
handlers directly and saves/loads through Blender's file API where practical.

### 2.3 Request, prefix, apply, responses, states

**Entry points:** `_process_stage`, `_prefix_sections`, `_build_prefix_batch`,
`_clone_prefix`, `_prefix_revision`, `_same_prefix`, `_timeout`, `_sent`,
`_pending`, `_verify`, `_pull_permits`, `_pull_deadlines`, `_apply`,
`_handle_response`, `_handle_error`, `_handle_schema`, `_handle_incoming`,
`_set_state`.

**Must not change:**

- One request in flight per stage; a newer frame/rid supersedes, a stale answer
  is dropped and traced, a late error cannot clear a newer pending request.
- Latest-wins request ids come from the geometry/params/nonce; a `ref` is
  served only for a held hash; `unknown_ref` forces a full resend; a reconnect
  and a load force verification even when rid/frame are unchanged.
- Prefix reads are evaluated geometry, batched once per pass, memoized by the
  depsgraph generation, and only invalidated below the writing stage; edit mode
  is flushed before a request/pull.
- Apply is per kind: absent group clears its helper; a topology token applies
  only when token and counts match; attributes are written after geometry;
  definitions/rows are validated before any helper is touched.
- State writes go through `_set_state`: conflict > disabled > frozen > transient;
  `waiting` requires a pending request (or a frozen Pull); illegal flag states
  are refused/settled; a frozen error keeps the frozen state and records the
  text; `waiting_seconds` is 0 outside a wait.
- A Pull while frozen is one request with a deadline; a missing answer settles
  back to frozen; a disconnect keeps the last output.
- `_handle_incoming` isolates one bad message: the rest of the batch still
  applies; a response-shaped failure is recovered to a retryable state.

**Existing tests:**

- `test_request_lifecycle.py::OneEvaluationAtATimeTests.*`,
  `PlaybackScrubTests.*`, `OneRequestOutstandingTests.*`,
  `UnknownRefTests.*`, `UnknownRefResponseTests.*`, `SolveFailureTests.*`,
  `CoalescedAnswerTests.*`, `TimeDrivenPrefixTests.*`, `QuietAnswerTests.*`,
  `AnswerRoutingTests.*`.
- `test_prefix.py::PrefixChainTests.*`, `WaitingPrefixTests.*`,
  `ComponentPrefixTests.*`, `NestedGroupPrefixTests.*`, `LateClientTests.*`.
- `test_prefix_memo.py::ModifierInputMemoTests.*`, `ResetRetiresMemoTests.*`,
  `ReferencedObjectTests.*`, `UpstreamOutputTests.*`.
- `test_multistage_link.py::ResponseValidationTests.*` (empty/unknown rid,
  missing out_hash, stale errors, frozen/disabled, load-clears-rid/pull,
  uncorrelated errors, frozen Pull deadline).
- `test_stage_state.py::StageStateTests.*`.
- `test_instances.py::EngineUpdateTests.test_a_lost_answer_is_retried_without_an_edit`,
  `test_a_mixed_frame_still_reapplies_a_cleared_base_helper`.
- `test_attributes.py::AttributeEngineTests.*`; `test_geometry_kinds.py::
  MixedEngineTests.test_03_a_response_without_curves_or_points_clears_them`.

**T3 additions:** none; the malformed-hello case in §2.1 covers the one
uncovered trust-boundary shape.

**T5 may move:** request/prefix/apply and message handlers behind explicit
interfaces; the state table stays the single writer. Tests use
`engine._handle_response`/`_process_stage` and the public facade, not module
layout.

**Gaps:** the `_handle_incoming` per-message isolation is covered by
`test_live_sync` with one injected apply failure; a 10k fuzz corpus is T10.

### 2.4 Playback gate and bake

**Entry points:** `playback.py` (`_on_playback_pre`, `_on_playback_post`,
`_on_frame_change_sync`, `_sync_playback_frame`, `_flush_playback_edits`,
`_reset_reports`, `_playback_deadline`), `bake.py`, `ops.STAGELOOM_OT_bake`,
`ops.STAGELOOM_OT_clear_bake`, `triggers._on_frame_change`.

**Must not change:**

- Play Every Frame (`sync_mode "NONE"`) runs one synchronous stage pass per
  frame, waits for the answer, and suppresses the timer while the gate owns the
  frame. Frame Drop stays asynchronous/latest-wins.
- A repeated frame is not asked twice; a disconnect keeps the last output and
  continues; the default `sync_wait` still has a finite hard cap (ten minutes);
  a driven default is not a declaration change; a stage exception stays in
  that stage; another scene's frame event is ignored; edit mode is flushed
  before the prefix read.
- Bake bakes only the selected stage's native Bake node; it first freezes the
  upstream closure, then triggers each downstream dependent once and freezes
  it on that answer. Only the target has native bake data. A pre-frozen stage
  keeps its own state and gets no marker; a failure rolls back the target bake
  and any freezes this operation created; Clear Bake removes only this bake's
  data and markers.
- Bake orchestration is owned in `engine/bake.py`; the operator is an adapter
  and advertises the irreversible Clear Bake route.

**Existing tests:**

- `test_playback_sync.py::PlaybackSyncTests` (the full numbered matrix: 00–08
  and 10–15, including hard cap, disconnect, scene isolation, edit-mode flush).
- `test_chain_playback.py::ChainPlaybackTests.*`.
- `test_bake_safety.py::BakeSafetyTests.*`.
- `test_chain_bake.py::DependencyTests.*` (upstream/downstream closures,
  cycles, cross-object), `BakeFreezeTests.*` (`test_01`–`test_16`),
  `OfflineBakeAcceptanceTests.test_01_offline_chain_bake_survives_save_reload_and_clear`.
- `test_stage_controls.py::StageControlTests.test_01`–`test_04`.

**T3 additions:** none; the gate and bake matrix is already the release
blocker coverage.

**T5 may move:** gate state into playback-owned records; bake orchestration
already has `engine/bake.py` as its intended owner. The frame-scene and
bake-token rules are one owner each.

**Gaps:** real render/scrub playback in a GUI session and native bake data on a
real Rhino client are not reproducible headless; the suite drives Blender's
handlers and native bake node directly.

### 2.5 T5 implementation record

**Owner:** `engine/runtime.py` (`Engine`, `StageRuntime`, `ENGINE`) plus the
existing behavior modules. `engine/__init__.py` is a facade with no `__all__`,
no module re-exports and no compatibility shim.

**Moved:** every mutable global from the old hub is a field of `ENGINE`; the
eight per-stage maps are `StageRuntime` fields behind `ENGINE.runtime`.
`_pending_of`/`_forget` are `ENGINE.pending_of`/`ENGINE.forget`;
`_ordered_stages`, `_stage_key_conflicted`, `_set_state`, `_trace`,
`_bake_sync`/`_bake_scene`/`_bake_progress`, `stats` and `start_server` have
`ENGINE` methods. Registration/activation (`register`, `unregister`,
`activate`, `deactivate`, `ensure_active`, `settle`) moved onto `Engine`.
Bake orchestration stays in `engine/bake.py`; the operator now uses
`ENGINE.begin_bake`/`set_bake_progress`/`end_bake`.

**Tests:** the Blender suite still runs **454** tests and the offline gate; no
test was removed. Test modules now import owner modules
(`engine.apply`, `engine.lifecycle`, `engine.messages`, `engine.playback`,
`engine.request`, `engine.state`, `engine.triggers`) or `ENGINE`, instead of
the former hub re-exports. `benchmarks/bench.py` was migrated the same way so
the baseline gate still drives the real timer, prefix and apply paths.

**Verification:** `scripts/test-blender` PASS (454 + offline gate); the full
`scripts/check-all` passed 26/26 in 174.1 s against the 2026-11-17
best-of-three baseline. The former median-baseline decode failure was host CPU
power/core-state drift, confirmed by a pinned T4/T5 A/B (4.5736 vs 4.5491 ms)
and replaced by the new baseline, not waived. No protocol, fixture or `out_hash`
change.

**Remaining gaps:** the `unfreeze` asymmetry above is unchanged and documented;
native GUI undo/redo, linked-object editing and real Rhino playback/bake remain
the harness gaps in §5; the `Engine` class is the largest unit (state inventory
plus lifecycle), which is the intended single owner rather than a second hub.

---

## 3. T6 — `GhLink` split

**Current owner:** `grasshopper/src/gh/GhLink.cs` (2,642 physical lines).
`LinkTests` is the integration proof (real `GhLink`, scripted fake WebSocket
server, no Rhino document); `ComponentTests` owns component lifecycle/document
isolation; `MiniWsTests` owns transport; `StageTests` owns the build guard.

### 3.1 `StageRegistry` — claims, conflicts, document isolation

**Entry points:** `Register`/`Unregister`, `RegisterInDocumentForTest`,
`PublishStageIn`/`PublishStageOut`/`PublishParam`, `Forget`, `Slot`,
`FindSlot`, `StageConflict`, `IsStageConflicted`, `RebuildPublicationLocked`,
`ReconcileOwnership`, `RemoveDocumentStateLocked`, `ClearAllLocked`.

**Must not change:**

- One key has one owning document; a key claimed by two documents is a strict
  conflict: no hello row, no schema, no request solve, neither document's slot
  touched; removing one claimant clears the conflict and restores the
  survivor's state.
- One Stage In / one Stage Out / unique parameter id per key; role duplicates
  are a strict conflict, not last-writer merge.
- A removed key is a tombstone: a request is refused with `unknown_stage`, not
  a provisional slot that waits forever. A removed component leaves no claim;
  removing the last component stops the worker and clears all registries.
- Registration is idempotent; a late publish from an unregistered owner leaves
  no claim.

**Existing tests:**

- `ComponentTests` lifecycle: `link component lifecycle` checks
  `AddedToDocument registers each stage component once`, `each stage component
  publishes one claim`, `removing the Param drops its component entry/claim/
  schema declaration/slot`, `a late publish from a removed component leaves no
  claim`, `removing the last component clears the registry/slot`, `removing
  the last component stops the worker`, `a later add restarts the worker`.
- `ComponentTests` document isolation: `each document has its own
  registration`, `two documents publish their own keys`, `a request fills the
  owning document's slot`, `a request creates no slot in the other document`,
  `two documents on one key are a strict conflict`, `a conflicted key is
  withheld from hello`, `a request for a conflicted key is refused`, `the
  conflict names both documents`, `a conflicted request touches neither
  document's slot`, `removing one claimant clears the conflict`, `the
  survivor's schema is restored`, `the later claimant becomes the owner when
  the first leaves`, `duplicate Stage Outs in one document are a strict
  conflict`, `a new document claims its key`, `removing the last component
  releases the key`, `re-adding the component reclaims the key`.
- `LinkTests` integration: `hello carries the published stage`, `a stage in
  republishes hello`, `removing a stage in republishes hello`, `unknown_stage
  code`, `unknown_stage stage`, `unknown_stage rid`, `refused slot pruned`,
  `claimed slot survives a sibling unregister`, `last unregister closes the
  socket`.

**T3 additions:** none; this is the strongest-covered seam. The new
`link transport guards` checks in §3.2–3.6 do not touch registry isolation.

**T6 may move:** into `StageRegistry`; one owner for claims, owners, conflicts,
tombstones and slots. `LinkTests` stays the integration proof.

**Gaps:** a real `GH_Document` cannot be constructed without Rhino, so
document identity is exercised through `RegisterInDocumentForTest` with
synthetic ids and through the component base (`AddedToDocument`). The native
two-document GUI case is a manual acceptance item.

### 3.2 `LinkConnection` — start/stop/restart, worker lifetime, handshake

**Entry points:** `Start`, `Stop`, `Configure`, `Connected`, `Running`,
`ActiveSenders`, `Loop`, `SendLoop`, `ReceiveLoop`, `Endpoint`, `Port`.

**Must not change:**

- One worker per Start; a stopped/replaced worker can never revive, clear the
  next outbox, or publish on the next socket. `Stop` is idempotent, detaches
  the SolutionEnd handler, closes the client and bounds the join.
- Configure validates host/port before moving the live endpoint; a successful
  change stops the old link and starts a new one, whose first connection
  republishes hello and every schema. A rejected configure leaves the endpoint
  untouched.
- A handshake that completes after Stop adopts nothing; a stop during a blocked
  handshake returns and a later configure/start connects.
- One sender per connection; it stops with the connection (including the reset
  path) and does not outlive it.

**Existing tests:**

- `LinkTests`: `connect accepted`, `one sender after connect`, `reset took the
  exception path`, `sender stops with the failed connection`, `one sender after
  reconnect`, `configure accepts the new loopback port`, `the old link closes`,
  `configure updates the endpoint`, `configure updates the port`, `the new link
  connects`, `hello is republished on the new link`, `schema is republished on
  the new link`, `configure rejects a remote host`, `a rejected configure keeps
  the endpoint`, `stop returns while the handshake is blocked`, `configure
  after stop accepts the new endpoint`, `the restarted link publishes hello`,
  `the restarted link has one sender`, `the stale connect publishes no hello`,
  `the stale connect leaves the new link connected`, `the stale socket closes`,
  `the new link still publishes after the stale connect`.
- `ComponentTests`: `connection rejects a port below the range`, `connection
  reports the validation failure`, `connection keeps the current endpoint on
  bad input`, `connection endpoint matches the link`, `connection port is
  saved globally`.
- `MiniWsTests`: handshake/accept, caps, close.
- `test_registration.py::ServerStopTests` and `ServerBindFailureTests` cover
  the Blender server, not `GhLink`; they are the peer-side lifecycle proof.

**T3 additions:** `LinkTests::Check("a null slot reads as the empty frame")`,
`a null slot has no answer rid`, `a null slot is not written by the in-status
writer`, `the empty sentinel is not confused with an empty full frame`,
`configure rejects an out-of-range port`, `a rejected port keeps the endpoint`,
`Stop is idempotent`, and the outbox-cap checks (§3.6).

**T6 may move:** worker/thread/client/outbox/sender/backoff into
`LinkConnection`. `ActiveSenders`/`RetryRequests` are diagnostics and may move
with it; `LinkTests` reads them.

**Gaps:** a true OS-level half-open socket and a GUI Rhino document are not
reproducible in the console harness; `FakeServer` covers the scripted cases.

### 3.3 `RequestScheduler` — one in-flight, latest-wins, retries

**Entry points:** `RequestStage`, `SchedulePendingSolve`, `BeginRequestSolve`,
`RunRequestSolve`, `FinishRequestSolveLocked`, `RetryStaleSolves`,
`ShouldAnswer`, `IsRequestDriven`, `AnswerRid`, `BeginRequestSolveForTest`,
`EndRequestSolveForTest`.

**Must not change:**

- One scheduled delegate owns the queue; requests arriving during a solve are
  the next batch, coalesced per document. A request whose rid the slot already
  replaced is dropped.
- Only the requested keys are pinned (`SolveRid`); an unrelated key is not
  pinned and a key with a newer queued request does not answer the old solve.
  A request-driven answer is always sent; an unchanged user answer is skipped
  (`ShouldSend`/`MarkSent`).
- A slot whose solve is merely slow is not retried; a lost SolutionEnd is
  retried once the watchdog covers it. A one-way key (no answering Stage Out)
  is never retried.
- A stopped worker cannot queue or answer for the next session.

**Existing tests:**

- `LinkTests`: `in-flight solve is not retried`, `waiting solve is retried
  once`, `a lost SolutionEnd does not block the retry`, `a slot without a
  request is not retried`, `a retried request is queued`, `the retried rid is
  pinned`, `the retried stage is request-driven`, `a request batch starts`, `a
  second delegate finds no batch`, `the requested key is pinned`, `an unrelated
  key is not pinned`, `a requested key answers`, `a dependent key with no
  queued request may answer`, `a key with a newer request queued does not
  answer`, `only a requested key is request-driven`, `the answer rid is the
  pinned request`, `an unchanged user answer is not re-sent`, `a changed user
  answer is sent`, `a request-driven answer is always sent`, `finishing clears
  the first requested key`, `finishing clears the second requested key`,
  `finishing leaves an unrelated key alone`, `after the batch a user solve
  answers any stage`, `the queued request starts the next batch`.
- `ComponentTests`: `a dependent stage out sends its changed answer`, `a stage
  with a queued request does not answer this solve`, `an idle stage out is not
  retried`, `a Stage In only key is not retried`, `a disabled Stage Out does
  not answer`, `a Param only request stays waiting without a retry storm`.
- `MiniWsTests`: `oversized response header rejected before it is read in
  full`, `stalled handshake times out`.

**T3 additions:** none; the scheduler matrix is already complete.

**T6 may move:** into `RequestScheduler`; `PendingRequests`/`_activeRequests`/
`_solveQueued` and their locks move together. `BeginRequestSolveForTest` is a
test hook; if T6 keeps it, it must keep its semantics, otherwise move the
LinkTests checks with the scheduler.

**Gaps:** a real Grasshopper `ScheduleSolution` callback is not available
without a document; the console suite drives the same context code through the
hooks.

### 3.4 `ProtocolHandler` — frame decode, caps, refusals, error codes

**Entry points:** `Handle`, `ReceiveLoop` decode path, `ReadCaps`,
`AttributeList`, `HeaderList`, `HeaderObjects`, `ErrorFrame`.

**Must not change:**

- Bad frames never tear down a healthy connection: `bad_frame` is a local note
  (no answer the sender can act on); `proto_mismatch` and `too_large` are
  answered with an `error` frame carrying the code. No unclassified exception
  escapes the receive loop.
- `unknown_ref` for a ref the slot cannot serve sets the slot status and sends
  `unknown_ref`; the next request must be full.
- `unknown_stage` for a removed key; `duplicate_stage` for a conflicted key
  request; a request with no stage key is noted and ignored; a non-request type
  is noted and ignored.
- `caps` is the peer's last request advertisement; a missing/malformed list is
  empty, never inferred, and a new connection resets it.
- Full requests replace the frame atomically; refs reuse the held geometry and
  publish a new rid; only the shared `HeldFrame.Empty` means "no frame", a
  zero-section full frame is real.

**Existing tests:**

- `LinkTests`: `hello advertises blocks/2`, `the peer's blocks/2 capability is
  retained`, `a request without caps clears the peer capability`, `slot rid`,
  `slot waiting`, `slot geometry hash`, `slot params snapshot`, `unknown_ref
  code`, `unknown_ref stage`, `unknown_ref rid`, `refusal status on the slot`,
  `same-rid re-send returns to waiting`, `new full request replaces the frame`,
  `old frame stays coherent`, `ref publishes a new rid`, `ref reuses the
  geometry snapshot`, `inbound error sets the stage`, `proto_mismatch code`,
  `too_large code`, `bad_frame is noted, not answered`, `unknown_stage code`,
  `unknown_stage stage`, `unknown_stage rid`, notes for pre-component
  requests, held requests, bad frames and connection.
- `ComponentTests`: `a zero-section full request is a held frame`, `a ref for a
  held empty frame is accepted`, `clearing the key drops its slot`, `a request
  for a removed key is refused`, `a removed key does not become provisional`,
  `re-claiming a removed key accepts requests`.
- Core codec: `blender/tests/core/test_codec_boundaries.py` and C#
  `Program.cs` fixture/boundary checks own the byte-level codes; `MiniWsTests`
  owns transport framing.

**T3 additions:** none; the malformed/oversize paths are already covered.

**T6 may move:** into `ProtocolHandler`; `ReadCaps`/header list helpers and
`ErrorFrame` move with it. Do not add a second frame validator or a second
`HeldFrame` owner.

**Gaps:** fuzzing 10k mutated frames is T10, not T3.

### 3.5 `Publication` — hello/schema dirty flags and document snapshot

**Entry points:** `Publish`, `SendPublication`, `StagesChangedLocked`,
`CaptureDocumentSnapshot`, `ReadDocumentUnits`, `SetUnitsSnapshotForTest`,
`DocumentScale`, `UnitsName`.

**Must not change:**

- Hello is published once per connection and again when the stage set,
  `has_stage_in`/`has_stage_out`, or doc units change; it is not republished
  for an unchanged stage set. A schema is sent only when its canonical
  declaration changes; an unchanged schema is quiet. Removing a stage clears
  its schema signature so a later reappearance cannot be suppressed.
- Publication touches only registries and the UI-thread-captured units
  snapshot; it never reads `RhinoDoc`/`GH_Document` from the socket or sender.
- A schema for a conflicted key is withheld; a schema for an unknown stage is
  logged and ignored.

**Existing tests:**

- `LinkTests`: `hello client`, `hello doc`, `hello units`, `hello stages
  empty`, `hello carries the
  published stage`, `a stage out alone has no stage in`, `a stage out alone has
  a stage out`, `a stage in republishes hello`, `removing a stage in
  republishes hello`, `hello reports has_stage_in per key`, `schema stage`,
  `schema declaration`, `unchanged schema is quiet`, `changed schema is sent`,
  `removing a Param drops its schema declaration`, `registration captures the
  UI thread`, `a socket-like thread is not the UI thread`, `hello publishes the
  cached unit name`, `a unit change republishes hello`.
- `ComponentTests`: `a conflicted key has no schema declaration`, `a
  conflicted key is withheld from hello`, `the survivor's schema is restored`,
  `the survivor's schema is republished alone`, `a new document claims its
  key`, `adding a Param registers it`, `the Param publishes its declaration`,
  `removing the Param drops its schema declaration`.
- `test_registration.py` covers the Blender-side schema application and
  parameter UI, not the link publication.

**T3 additions:** none; dirty-flag behavior is fully covered.

**T6 may move:** into `Publication`; keep one canonical JSON signature and one
last-published snapshot. `_lastSchema`/`_publishedThisConnection` move with it.

**Gaps:** the heartbeat republish at 2 s is timing-based; LinkTests observes
that an unchanged schema is quiet within a 600 ms window, not a long soak.

### 3.6 `StageSlot`/`HeldFrame` — snapshots, status halves, frame identity

**Entry points:** `StageSlot` (`Rid`, `SolveRid`, `Params`, `Frame`, `Status`,
`Message`, `InStatus`/`InMessage`, `OutStatus`/`OutMessage`, `ShouldSend`,
`MarkSent`), `HeldFrame`, `AnswerRid`, `ShouldAnswer`, `IsRequestDriven`,
`SetOutStatus`, `SetInStatus`, `ReadFrame`, `HasFrame`.

**Must not change:**

- A frame is one immutable snapshot: sections, attributes, definitions and
  geometry hash are read together; a full request replaces it atomically, a
  ref reuses it and publishes a new rid.
- `StageSlot.Empty` is the only "no frame"; a zero-section frame is real and
  can be served by a ref.
- Stage In and Stage Out own separate verdicts; a late Stage In write is
  dropped if the frame/epoch changed; Stage Out writes the aggregate; a
  one-way key's Stage In owns the terminal status.
- `AnswerRid` prefers a pinned `SolveRid`, otherwise the slot rid; `ShouldSend`
  skips an unchanged user answer and always sends a request-driven one.

**Existing tests:**

- `LinkTests`: `slot starts waiting`, `out status reaches the out half`, `out
  status leaves the in half alone`, `out status updates the aggregate`, `answer
  rid is empty with no request`, `answer rid falls back to the slot`, `answer
  rid uses the pinned rid`, `in status reaches the in half`, `in status leaves
  the out half alone`, `in status leaves the aggregate with stage out`, `a late
  in status does not clobber a newer frame`, `a late in status does not clobber
  a newer epoch`, `SetOutStatus reaches the out half`, `SetOutStatus updates
  the aggregate`, `SetOutStatus leaves the in half alone`, `an unchanged user
  answer is not re-sent`, `a changed user answer is sent`, `a request-driven
  answer is always sent`.
- `ComponentTests`: `stage in success reports ok`, `stage in success clears its
  message`, `stage in failure reports error`, `a protocol error is not reported
  as waiting`, `a zero-section full request is a held frame`, `the empty frame
  keeps the full rid`, `a ref for a held empty frame is accepted`.
- `test_instances.py` and `test_topology_token.py` cover the Blender-side
  frame/answer application, not the C# snapshot.

**T3 additions:** `LinkTests::Check("a null slot reads as the empty frame")`,
`a null slot has no answer rid`, `a null slot is not written by the in-status
writer`, `the empty sentinel is not confused with an empty full frame`,
`the outbox cap has a deterministic observer`, `the outbox drops the oldest
frame over its byte cap`.

**T6 may move:** `StageSlot`/`HeldFrame` into their own owner; `_peerCaps`,
outbox and publication state must not drift into the slot. `_activeSenders`
stays a connection diagnostic.

**Gaps:** `Enqueue`'s cap and `OutboxBytes` have no public observer while
disconnected; the T3 test uses reflection and must move with the fields. The
`StageSlot` null-key path is not part of the product contract (components always
pass a key), so it is not tested.

---

## 4. T7 — `Instances` split / table-driven conversion

**Current owner:** `grasshopper/src/gh/Instances.cs` (2,185 physical lines:
`Instances`, `DefinitionBuilder`, `DefinitionPlan`/`PartPlan`,
`DefinitionTableWriter`, `DefinitionWriter`, `GeometryPart`,
`DefinitionContent`, `TableEntry`, `DefinitionSection`, `NestedRow`), plus
`core/Conversion.cs` (1,609) and `core/Attributes.cs` (1,114).

### 4.1 Build (inbound) and definition plan

**Entry points:** `Instances.Build`, `Instances.ReadRows`,
`Instances.DefinitionBuilder` (`Plan`, `Definition`, `Preflight`,
`ContentHash`, `ReferenceId`), `TableEntry`, `DefinitionSection`.

**Must not change:**

- No `definitions` table is a plain no-op (a plain frame may share the header);
  a definitions table with no readable row pair is noted ("definitions arrived
  without instance rows"), never a silent empty stage.
- The table is validated before geometry: name string, positive integer
  `parts` (boolean/fraction/string/zero/null/over-cap rejected), section
  names parse inside the declared namespace, part indices in range, no
  duplicate definition/part namespaces, unknown `idef.` names noted.
- A definition is built children-first; a cycle is cut with a visible note, not
  a stack overflow; an out-of-table nested row is noted and cut.
- Rows are all-or-nothing: `ref_def`/`transforms` lengths agree exactly;
  transform count must be a multiple of 16; a non-u32 ref_def or short pair is
  a note, not a truncated pair. An absent pair is silent; an empty pair reads
  no instances.
- Attribute descriptors are validated against the definition/part table; a
  descriptor from another part cannot attach.
- Empty parts are legal and keep their index; a multi-part definition with no
  geometry is still a definition.

**Existing tests:**

- `InstanceTests::CheckFixturePlans` (single/multi fixtures, namespaces),
  `CheckTableValidation` (every malformed `parts` form, cap, legal unnamed),
  `CheckNamespaces` (unparsed names, outside names, empty parts),
  `CheckRows` (matching pair, extra indices, transform count, non-u32, half
  pair, absent/empty pair, f32 alignment), `CheckPreflight` (two-definition
  cycle, self-cycle, out-of-table row, cyclic hash), `CheckContentHash`
  (single/multi/nested/part/attribute/part-count), `CheckDescriptorGrammar`
  and `CheckAttributePartFilter` (definition/part/offsets/boolean/null).
- Native `CheckNativeBuild` (fixtures, part attributes, nested row, empty
  parts, empty definition) is **skipped under wine**.

**T3 additions:** `InstanceTests::Check("build: a definitions table without
rows is noted")`, `Check("build: no definitions table is a no-op")`.

**T7 may move:** `DefinitionBuilder` and its plan/section types into an inbound
builder module. The validation order and note text are the contract;
`CheckFixturePlans`/`CheckTableValidation`/`CheckRows`/`CheckPreflight` move
with the builder.

**Gaps:** native build under wine is skipped (see §5). The managed grammar
tests still cover the full inbound matrix; they do not prove Rhino object
construction.

### 4.2 Emit (outbound), `DefinitionWriter`, `DefinitionTableWriter`

**Entry points:** `Instances.Emit`, `Instances.HasDefinitionContent`,
`DefinitionWriter.Definition`, `DefinitionTableWriter.Write/TryGet/
AttributeCount/PeerSupportsParts`, `NestedRow`.

**Must not change:**

- `Emit(null)` and `Emit(empty)` are no-ops (no sections, descriptors,
  definitions, warnings or counts). A null instance reference is an
  `instance_skipped` warning and no row.
- A single-part definition keeps the exact legacy bytes/namespace
  (`idef.<i>.*`, no `parts`), including its attribute indices and output hash.
  A multi-part definition writes one table entry with `parts: N` and
  `idef.<i>.<p>.*`; nested rows stay at `idef.<i>.inst.*`.
- Without `blocks/2`, every part becomes a single-part definition with a
  distinct part-suffixed name; each referring row fans out at the same
  placement; an `instance_split` warning names the definition and count. An
  empty part still gets a definition; unnamed parts use the content-hash
  fallback name.
- Definitions dedupe by content key; changing a part, a part attribute, the
  part count or a nested placement changes the key/name; a completed
  definition is cached, not rehashed; a graph-side cycle warns and stops.
- Attribute descriptors carry the part prefix/field only when the peer supports
  parts; a fallback split loses the part field and keeps the part's own
  attribute values.
- `ReadRows`/transform conversion is metres→document units for inbound and
  document units→metres for outbound; row order is preserved.

**Existing tests:**

- `InstanceTests::CheckOutboundSinglePart` (legacy table entry, sections,
  definitions, descriptors, output hash, nested row at definition level),
  `CheckOutboundMultiPart` (one entry with `parts`, part namespaces,
  descriptors, nested rows, no duplicate sections, empty part, dedupe),
  `CheckOutboundFallback` (one definition per part, names, namespaces,
  descriptors, first-part nested rows, warning, dedupe, empty part, unnamed
  fallback), `CheckOutboundContentKey` (identical multi-part dedupe, unnamed
  deterministic name, part/attribute/part-count/nested changes, single-part
  legacy identity), `CheckOutboundCycle` (self-nesting warns, cached
  definition), `CheckOutboundAttributes` (part prefix/field, single-part
  legacy bytes, curve/point descriptors).
- Native `CheckNativeEmit` (two-object multi-part, fallback fan-out, warning,
  graph-side cycle) is **skipped under wine**.
- `AttrTests`: `every type: names and types survive`, `every type: section
  bytes survive`, `curve: per-curve value is one item`, `curve: per-point
  values are one list per curve`, `transforms: wire translation applies`,
  `transforms: round trip is byte-stable`, `descriptor: absent components uses
  the type's shape`, `malformed string is skipped with a note`.
- `ConversionTests`: managed mesh/curve section boundaries and null-curve
  classification; native round trips skipped under wine.

**T3 additions:** `InstanceTests::Check("emit: a null list is a no-op")`,
`an empty list is a no-op`, `a null reference is warned and skipped`. These
cover the `Emit` entry guard the managed `DefinitionTableWriter` tests do not
reach.

**T7 may move:** `Emit`/`DefinitionWriter` into an outbound module;
`DefinitionTableWriter` remains the grammar/table owner. `Conversion`/
`Attributes` per-kind dispatch may become tables, but the section names,
dtypes, descriptor fields and warning codes are the contract.

**Gaps:** `Emit`'s real-geometry rows (a constructed `GH_InstanceReference`
and paired definition), single-part byte compatibility through `Emit` itself,
multi-part/fallback through `Emit`, and graph-side cycles are only covered by
`CheckNativeEmit`, which is skipped under wine. The table writer half is
covered; T7 must keep the native probe green where RhinoCore is available and
must not claim it locally.

### 4.3 Conversion/Attributes per-kind dispatch

**Entry points:** `Conversion.SectionsFromGeometry`, `SectionsFromMesh`,
`SectionsFromCurves`, `MeshFromSections`, `CurveFromSections`,
`SelectMeshGroup`, `ReadMeshData`, `ReadCurveData`, `TransformToWire`,
`PrefixSections`, `AttributeStore.Collect/GroupOf/Set/TryGet/Has`.

**Must not change:**

- One conversion table per kind: mesh, SubD, curve, point, Brep (meshed with a
  warning); section names/dtypes/order must stay byte-identical. A null curve
  is `curve_skipped`, not `curve_sampled`; an n-gon with holes is four quads
  plus an `ngon_holes` warning.
- Attribute groups are `mesh`, `subd`, `crv`, `pts`; domain/name/type/count
  validation is one choke point; STRING offsets cover the bytes exactly; an
  unknown type does not count as an attribute.
- Definition prefixes and part filters apply the same rules as the frame's own
  geometry; a single-part definition keeps the old attribute indices.
- Transforms are row-major on the wire, translation in the fourth column;
  inbound applies document units; outbound writes metres.

**Existing tests:**

- `ConversionTests`: `mesh: triangle/quad/n-gon/empty` decode, colours,
  `mesh round trip: … is byte-identical`, `mesh groups: mesh-only/subd-only/
  both complete/definition prefixes`, curve counts/types/order/weights/
  cyclic/empty/byte-identical, `curves: a null curve is reported as
  curve_skipped` and not sampled.
- `AttrTests`: every type round trip, legacy root key, labels/domains,
  definition/curve/point descriptors, transforms, malformed strings,
  descriptors components, `has`/`tryget` behaviour.
- `InstanceTests::CheckAttributePartFilter` and `CheckOutboundAttributes`.

**T3 additions:** none; the type corpus and descriptor grammar are already the
strongest coverage in the C# suite.

**T7 may move:** the per-kind switches into tables; keep one `WIRE_TYPES`
vocabulary and one descriptor emitter. `Attributes.cs`/`Conversion.cs` keep
their public names so the component tests and live Stage In/Out do not change.

**Gaps:** native mesh/curve/point construction is skipped under wine; the
managed section-boundary half and the golden fixtures are the local proof. A
real Rhino bake of a point/curve definition member is a T7 native acceptance
item, not a local test.

---

## 5. Known gaps and findings (not fixed in T3/T4)

1. **Native Rhino geometry checks skip under wine (coverage gap).**
   `ConversionTests` prints `SKIP native Rhino geometry round trip: RhinoCore is
   unavailable in this harness`; `InstanceTests` prints `SKIP native instance
   build`. Those checks are not executed locally. Do not count them as T3/T4
   coverage; T7's native acceptance must run where RhinoCore loads.
2. **A bind failure's cause is overwritten (low/medium, report only).**
   `GhServer._serve` sets `bridge.state["error"] = "cannot listen on …"`; the
   worker's `_run` finally then calls `set_disconnected("server stopped")`,
   which replaces it. The failure is still visible and a later start recovers;
   the panel cannot distinguish a taken port from a normal stop.
   `ServerBindFailureTests` asserts visibility/recovery, not the text.
3. **`sections_to_points` is not atomic on a radii count mismatch (low).**
   It resizes and writes positions before validating `pts.radii`, then raises
   `GeometryIOError`. Incomplete/no-positions and malformed positions are
   atomic. The T3 test pins the classified error only; T4 preserved it, and a
   later task may tighten it.
4. **`engine.unfreeze(obj, stage)` does not gate on a stack record (low).**
   Unlike `freeze`/`request_again`, it writes the stage state and dirties the
   object even when `obj` has no stage record. Not reachable from the panel,
   which passes the owning object. T5 preserved the asymmetry deliberately: it
   is a behavior change that needs its own regression test, so it stays a
   documented gap rather than riding along with the object migration.
5. **`GhLink.Enqueue`'s cap has no public observer (test-only reflection).**
   The T3 check reflects `MaxOutboxBytes`/`OutboxBytes`/`Outbox` while the link
   is stopped; T6 must move the test with `Publication`/`LinkConnection`.
6. **A real Grasshopper document / Rhino GUI is not available (harness gap).**
   Document ids are synthetic (`RegisterInDocumentForTest`), `ScheduleSolution`
   is driven through the request-context hooks, and undo/redo in the plugin is
   not a live session. `LinkTests`/`ComponentTests` cover the product code paths;
   they do not claim a native two-document run.
7. **A real Blender GUI session is not available (harness gap).**
   Undo/redo, linked-object refusal, real playback/render and the extension
   install path are covered through Blender's handlers/file API and the offline
   gate, not through a window; native render/bake acceptance remains external.

---

## 6. T3/T4 verification commands

```bash
scripts/test-blender     # 454 tests + offline gate, PASS
scripts/test-gha         # codec runner + 7 console suites, 681 passing checks; native skips printed
scripts/check-all        # T4 full gate: PASS 179.5 s, 26/26; bench 63.1 s
git diff --check
uvx ruff@0.17.2 check .
scripts/check-docs
scripts/code-health --check
```

T4's full gate passed alone in 179.5 s with the baseline enforced. The new
package is 2,151 physical / 1,791 code lines versus the old 2,151 / 1,811;
production is 24,946 physical / 19,394 code across 56 files (flat physical,
−20 code). The topology-token path is unchanged and the in-place gates are
green. No protocol, fixture or `out_hash` change.
