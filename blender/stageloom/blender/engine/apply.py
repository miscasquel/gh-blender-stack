"""Response sections to hidden helpers: geometry, attributes and instances."""

import sys

engine = sys.modules[__package__]


def _warnings_text(header):
    """The response's warnings as one line each (protocol §4.4)."""
    lines = []
    for warning in header.get("warnings") or []:
        if isinstance(warning, dict):
            text = warning.get("message") or warning.get("code")
        else:
            text = str(warning)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _group_has_geometry(sections, prefix, kind):
    """Whether one section group carries geometry that a helper must hold."""
    if kind == "mesh":
        for name in ("mesh.verts", "subd.verts"):
            values = sections.get(prefix + name)
            if values is not None and len(values) >= 3:
                return True
        return False
    if kind == "curves":
        values = sections.get(prefix + "crv.points")
        return values is not None and len(values) >= 4
    values = sections.get(prefix + "pts.positions")
    return values is not None and len(values) >= 3


def _helper_has_data(helper, kind):
    if helper is None:
        return False
    data = helper.data
    if kind == "mesh":
        return len(data.vertices) > 0
    if kind == "curves":
        return len(data.splines) > 0
    return len(data.points) > 0


def _definition_helper(obj, stage, index, kind):
    return engine.bpy.data.objects.get(
        engine.helpers.definition_name(obj, stage.key, index, kind)
    )


def _cloud_has_rows(obj, stage, index, nested=False):
    name = (
        engine.helpers.nested_name(obj, stage.key, index)
        if nested
        else engine.helpers.instance_name(obj, stage.key, index)
    )
    cloud = engine.bpy.data.objects.get(name)
    return cloud is not None and len(cloud.data.points) > 0


def _payload_missing(obj, stage, sections):
    """Whether a frame that names geometry is displayed over empty helpers.

    The one case the applied hash cannot see: the helpers were emptied while
    the hash stayed, so the skip rule would refuse every response — and Push
    too. A frame that carries geometry over empty helpers is re-applied
    instead, which is idempotent when the output really is empty.

    Every section group is checked against the helper it actually writes:
    top-level base geometry against the top-level helper, `idef.<i>.*` against
    definition i's helper, and `inst.*` rows against the row clouds. A
    definition-only output has empty top-level helpers and used to look
    "missing" on every pass, so it was re-applied forever.
    """
    for name in sections:
        index, rest = engine._definition_split(name)
        if index is None:
            if rest.startswith("inst."):
                refs = sections.get("inst.ref_def")
                if refs is not None and len(refs):
                    for target in set(int(value) for value in refs):
                        if not engine._cloud_has_rows(obj, stage, target):
                            return True
                continue
            kind = engine._kind_of(rest)
            if kind is not None and engine._group_has_geometry(sections, "", kind):
                if not engine._helper_has_data(
                    engine.helpers.helper_for(obj, stage.key, kind), kind
                ):
                    return True
            continue

        prefix = "idef.%d." % index
        if rest.startswith("inst."):
            refs = sections.get(prefix + "inst.ref_def")
            if refs is not None and len(refs):
                if not engine._cloud_has_rows(obj, stage, index, nested=True):
                    return True
            continue
        kind = engine._kind_of(rest)
        if kind is not None and engine._group_has_geometry(sections, prefix, kind):
            if not engine._helper_has_data(
                engine._definition_helper(obj, stage, index, kind), kind
            ):
                return True
    return False


def _apply(obj, stage, sections, attributes=None):
    """Write the response geometry into the stage's helpers, one per kind, then
    its attributes into the helpers that can hold them.

    A section group that is absent means "none": that helper is cleared, so a
    definition that stops returning curves stops showing them. Attributes are
    written after the geometry, because the geometry write is what fixes the
    element counts they have to match.

    Instance definitions (protocol §5.2) are the same rule one level up: each
    `idef.<i>.*` group fills definition i's prototype collection (created on
    first sight, removed when the response stops carrying it), the top-level
    `inst.*` rows fill each definition's row cloud, and a definition's own
    `idef.<i>.inst.*` rows fill its nested placement cloud, which the node
    group instances from.
    """
    base = {"mesh": [], "curves": [], "points": []}
    definitions = {}

    for name, values in sections.items():
        index, rest = engine._definition_split(name)
        if index is not None:
            if rest.startswith("inst."):
                continue  # handled with the other row pairs below
            bucket = engine._kind_of(rest)
            if bucket is not None:
                entry = definitions.setdefault(index, {"mesh": [], "curves": [], "points": []})
                entry[bucket].append((rest, values))
            continue
        if name.startswith("inst."):
            continue
        bucket = engine._kind_of(name)
        if bucket is not None:
            base[bucket].append((name, values))

    top_rows = engine._rows_of(sections, "")
    nested_rows = engine._nested_rows_of(sections)
    keep = set(definitions) | set(nested_rows)
    for index, _matrix in top_rows:
        keep.add(index)
    for index in sorted(nested_rows):
        for target, _matrix in nested_rows[index]:
            if target in definitions or target in nested_rows:
                keep.add(target)
            else:
                # The response names a nested placement whose definition group
                # is absent: keep it visible instead of building an empty block.
                engine.prefs.log(
                    "[stageloom] %s: block %d nests definition %d, which the response does not carry"
                    % (stage.key, index, target),
                    flush=True,
                )
    for index in keep:
        definitions.setdefault(index, {"mesh": [], "curves": [], "points": []})

    # Definitions the response no longer carries go away whole: collection,
    # helpers, nested empties and row cloud.
    for index in list(engine.helpers.definition_collections(obj, stage.key)):
        if index not in keep:
            engine.helpers.remove_definition(obj, stage.key, index)

    # Data first, wiring after: the node group reads the final definition set.
    for index in sorted(definitions):
        _collection, objects = engine.helpers.ensure_definition(obj, stage.key, index)
        engine._apply_mesh(objects["mesh"], definitions[index]["mesh"])
        engine._apply_curves(objects["curves"], definitions[index]["curves"])
        engine._apply_points(objects["points"], definitions[index]["points"])

    engine._apply_mesh(engine.helpers.ensure_helper(obj, stage.key, "mesh"), base["mesh"])
    engine._apply_curves(engine.helpers.ensure_helper(obj, stage.key, "curves"), base["curves"])
    engine._apply_points(engine.helpers.ensure_helper(obj, stage.key, "points"), base["points"])
    engine._apply_attributes(obj, stage, sections, attributes)
    engine._apply_instance_rows(obj, stage, definitions, top_rows, nested_rows)

    engine.nodes.ensure_group(obj, stage.key, nested_rows)
    # The frame is fully on screen: the heartbeat's "applied" stamp. Written
    # here rather than at the callers so every apply path is counted once.
    engine._heartbeat["applied_at"] = engine.time.monotonic()


def _definition_split(name):
    """``(index, rest)`` when a section name is inside a definition group
    (``idef.<i>.mesh.verts``), else ``(None, name)``."""
    if not name.startswith("idef."):
        return None, name
    parts = name.split(".", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None, name
    return int(parts[1]), parts[2]


def _nested_rows_of(sections):
    """Every definition's nested placements, ``{index: [(target, matrix)]}``.

    One source of truth for the apply path and for the same-hash repair path
    (a frame already applied still has to be able to rebuild the node group)."""
    scopes = {}
    for name in sections:
        index, rest = engine._definition_split(name)
        if index is not None and rest.startswith("inst."):
            scopes.setdefault(index, None)
    return {index: engine._rows_of(sections, "idef.%d." % index) for index in scopes}


def _kind_of(name):
    if name.startswith("mesh.") or name.startswith("subd."):
        return "mesh"
    if name.startswith("crv."):
        return "curves"
    if name.startswith("pts."):
        return "points"
    return None


def _rows_of(sections, prefix):
    """The decoded row pairs of one scope as ``[(definition index, matrix 4x4)]``.

    Both halves are all-or-nothing (protocol §5.2). A missing or mismatched
    pair is reported rather than silently degraded: a truncated pair used to
    keep the shorter side and leave no trace.
    """
    refs = sections.get(prefix + "inst.ref_def")
    mats = sections.get(prefix + "inst.transforms")
    if refs is None and mats is None:
        return []
    if refs is None or mats is None or len(mats) < 16:
        engine.prefs.log(
            "[stageloom] %sinst.* rows are incomplete (ref_def=%s, transforms=%s); skipped"
            % (prefix, refs is not None, mats is not None),
            flush=True,
        )
        return []
    matrices = engine.np.asarray(mats, dtype=engine.np.float32).reshape(-1, 16)
    if len(refs) != len(matrices):
        engine.prefs.log(
            "[stageloom] %sinst.* row pair lengths differ (%d rows vs %d matrices); the shorter side is used"
            % (prefix, len(refs), len(matrices)),
            flush=True,
        )
    count = min(len(refs), len(matrices))
    return [(int(refs[row]), matrices[row]) for row in range(count)]


def _apply_instance_rows(obj, stage, definitions, top_rows, nested_rows):
    """The row pairs: each definition's top-level rows fill its row cloud, and
    its own rows fill its nested placement cloud (position/rotation/scale, read
    by the node group). Both are the same channel; nested clouds carry the row
    target as an INT ``child`` attribute, which the group's per-child instance
    selections compare against. Nothing bakes: the node group instances the
    child's prototype chain at these points (see nodes.ensure_group)."""
    # Legacy leftovers: earlier builds represented a nested placement as an
    # Empty with a collection instance; the node graph carries them now, so any
    # such object is removed on sight.
    for index in sorted(definitions):
        collection, _objects = engine.helpers.ensure_definition(obj, stage.key, index)
        for child in list(collection.objects):
            if child.type == "EMPTY":
                engine.bpy.data.collections[collection.name].objects.unlink(child)
                engine.bpy.data.objects.remove(child, do_unlink=True)
    for index, rows in sorted(nested_rows.items()):
        if not rows:
            continue
        known = engine.helpers.definition_collections(obj, stage.key)
        missing = [target for target, _matrix in rows if target not in known]
        if missing:
            engine.prefs.log(
                "[stageloom] %s: block %d nests definition %d, which the response does not carry"
                % (stage.key, index, missing[0]),
                flush=True,
            )

    # Top-level rows: one cloud per definition, or none when it has no rows.
    clouds = {}
    for target, matrix in top_rows:
        clouds.setdefault(target, []).append((target, matrix))
    for index, existing in sorted(engine.helpers.instance_clouds(obj, stage.key).items()):
        if not clouds.get(index):
            engine.helpers.remove_cloud(existing)
    for index, rows in sorted(clouds.items()):
        if rows:
            engine._write_rows_cloud(
                engine.helpers.ensure_instance_cloud(obj, stage.key, index), rows, stage.key
            )

    # Nested placements: one cloud per definition that nests something, or
    # none when its own rows are gone.
    for index, existing in sorted(engine.helpers.nested_clouds(obj, stage.key).items()):
        if not nested_rows.get(index):
            engine.helpers.remove_cloud(existing)
    for index, rows in sorted(nested_rows.items()):
        if rows:
            engine._write_rows_cloud(
                engine.helpers.ensure_nested_cloud(obj, stage.key, index),
                rows,
                stage.key,
                with_child=True,
            )


def _write_rows_cloud(cloud, rows, stage_key, with_child=False):
    """One row cloud's data: a point per ``(target, matrix)`` row with
    ``rotation`` / ``scale`` attributes the node group reads by name. A nested
    cloud also carries the target definition index per point, so one
    Instance-on-Points per child can select its own placements."""
    data = cloud.data
    n = len(rows)
    if len(data.points) != n:
        data.resize(n)
    positions = engine.np.empty(n * 3, dtype=engine.np.float32)
    rotation = data.attributes.get("rotation")
    if rotation is None:
        rotation = data.attributes.new("rotation", "QUATERNION", "POINT")
    scale = data.attributes.get("scale")
    if scale is None:
        scale = data.attributes.new("scale", "FLOAT_VECTOR", "POINT")
    child = None
    if with_child:
        child = data.attributes.get("child")
        if child is None:
            child = data.attributes.new("child", "INT", "POINT")
    warned_shear = False
    for row, (target, matrix) in enumerate(rows):
        local = engine.helpers.matrix_of(matrix)
        position = local.to_translation()
        positions[row * 3:row * 3 + 3] = position
        quaternion = local.to_quaternion()
        scale_value = local.to_scale()
        rotation.data[row].value = quaternion
        scale.data[row].vector = scale_value
        if child is not None:
            child.data[row].value = int(target)
        if not warned_shear and not engine.helpers.decomposes_cleanly(
            local, position, quaternion, scale_value
        ):
            # A shear (or a non-uniform basis Blender cannot rebuild from
            # position/rotation/scale) is the one shape the point-cloud
            # channel cannot carry; the transform is applied lossily.
            engine.prefs.log(
                "[stageloom] %s: instance transform %d has shear; "
                "Blender can only carry position, rotation and scale" % (stage_key, row),
                flush=True,
            )
            warned_shear = True
    data.points.foreach_set("co", positions)
    data.points.update()
    cloud.update_tag()


def _has_generic_attributes(owner):
    """Whether the owner still carries a stage-owned generic attribute."""
    for attr in owner.attributes:
        name = attr.name
        if name.startswith(".") or name in engine.attributes_mod.SKIP_NAMES:
            continue
        if name in (engine.geometry_io.CREASE_ATTRIBUTE, engine.geometry_io.COLOR_ATTRIBUTE):
            continue
        return True
    return False


def _apply_attributes(obj, stage, sections, attributes):
    """Reconcile the frame's attribute sections with the helpers they belong to.

    mesh and subd share the mesh helper (a SubD cage is a mesh); points go to
    the point cloud helper. A descriptor with a ``definition`` index targets
    that definition's helper instead (protocol §5.2). Curve attributes are not
    transferred yet — Blender's curve helper is a legacy Curve datablock, which
    cannot store attributes.

    Absent means none: a helper the frame gives no descriptors is pruned of
    its previous generic attributes. The native colour channel is handled by
    the geometry write path, and the crease attribute by ``write_creases``.
    """
    kinds = {"mesh": "mesh", "subd": "mesh", "pts": "points"}
    grouped = {}
    for descriptor in attributes or []:
        kind = kinds.get(descriptor.get("group"))
        if kind is None:
            continue
        values = sections.get(descriptor.get("section"))
        if values is None:
            continue
        offsets = sections.get(descriptor.get("offsets")) if descriptor.get("offsets") else None
        definition = descriptor.get("definition")
        grouped.setdefault((definition, kind), []).append((descriptor, values, offsets))

    targets = {(None, "mesh"), (None, "points")}
    for index in engine.helpers.definition_collections(obj, stage.key):
        targets.add((int(index), "mesh"))
        targets.add((int(index), "points"))
    targets.update(grouped)

    def order(target):
        definition, kind = target
        return (definition is not None, -1 if definition is None else int(definition), kind)

    for definition, kind in sorted(targets, key=order):
        if definition is None:
            helper = engine.helpers.helper_for(obj, stage.key, kind)
        else:
            _collection, objects = engine.helpers.ensure_definition(
                obj, stage.key, int(definition)
            )
            helper = objects[kind]
        if helper is None:
            continue
        entries = grouped.get((definition, kind), [])
        if not entries and not engine._has_generic_attributes(helper.data):
            continue
        engine._bump_id(helper)
        for note in engine.geometry_io.write_attributes(helper.data, entries, prune=True):
            engine.prefs.log("[stageloom] %s: %s" % (stage.key, note), flush=True)
        helper.update_tag()


def _apply_mesh(helper, entries):
    mesh = helper.data
    if not entries:
        if len(mesh.vertices) or len(mesh.polygons):
            mesh.clear_geometry()
            helper.update_tag()
        return
    engine._bump_id(helper)
    prepared = []
    for name, values in entries:
        arr = engine.np.asarray(values)
        if name.endswith(".verts"):
            arr = arr.reshape(-1, 3)
        elif name.endswith(".colors"):
            arr = arr.reshape(-1, 4)
        elif name.endswith(".crease_edges"):
            arr = arr.reshape(-1, 2)
        prepared.append((name, engine._dtype_of(name), arr))
    engine.geometry_io.sections_to_mesh(mesh, prepared)
    helper.update_tag()


def _apply_curves(helper, entries):
    if not entries:
        if len(helper.data.splines):
            helper.data.splines.clear()
            helper.update_tag()
        return
    engine._bump_id(helper)
    prepared = []
    for name, values in entries:
        arr = engine.np.asarray(values)
        if name.endswith(".points"):
            arr = arr.reshape(-1, 4)
        prepared.append((name, engine._dtype_of(name), arr))
    engine.geometry_io.sections_to_curve(helper.data, prepared)
    helper.update_tag()


def _apply_points(helper, entries):
    if not entries:
        if len(helper.data.points):
            helper.data.resize(0)
            helper.update_tag()
        return
    engine._bump_id(helper)
    prepared = []
    for name, values in entries:
        arr = engine.np.asarray(values)
        if name.endswith(".positions"):
            arr = arr.reshape(-1, 3)
        elif name.endswith(".colors"):
            arr = arr.reshape(-1, 4)
        prepared.append((name, engine._dtype_of(name), arr))
    engine.geometry_io.sections_to_points(helper.data, prepared)
    helper.update_tag()


def _dtype_of(name):
    if name.endswith(".colors") or name in ("crv.types", "crv.orders", "crv.knots_mode", "crv.cyclic"):
        return "u8"
    if (
        name.endswith(".verts")
        or name.endswith(".crease_values")
        or name.endswith(".points")
        or name.endswith(".positions")
        or name.endswith(".radii")
        or name.endswith("inst.transforms")
    ):
        return "f32"
    return "u32"

