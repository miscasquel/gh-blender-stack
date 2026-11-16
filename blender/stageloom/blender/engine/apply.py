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


def _definition_helper(obj, stage, index, kind, part=None):
    return engine.bpy.data.objects.get(
        engine.helpers.definition_name(obj, stage.key, index, kind, part=part)
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
    """Whether a frame names geometry that an empty helper is not displaying.

    Each section group is checked against the helper it writes: base geometry,
    `idef.<i>.*`, a multi-part `idef.<i>.<p>.*` part and `inst.*` rows
    separately. A cleared helper must be repaired even when the applied hash
    still matches.
    """
    for name in sections:
        index, part, rest = engine._definition_split(name)
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

        if part is None and rest.startswith("inst."):
            refs = sections.get("idef.%d.inst.ref_def" % index)
            if refs is not None and len(refs):
                if not engine._cloud_has_rows(obj, stage, index, nested=True):
                    return True
            continue
        prefix = (
            "idef.%d." % index if part is None
            else "idef.%d.%d." % (index, part)
        )
        kind = engine._kind_of(rest)
        if kind is not None and engine._group_has_geometry(sections, prefix, kind):
            if not engine._helper_has_data(
                engine._definition_helper(obj, stage, index, kind, part=part), kind
            ):
                return True
    return False


def _invalidate_prefix_batch(obj, stage):
    """Drop the prefix batch entries an apply made stale.

    A prefix ends just before its stage's modifier, so a written output only
    changes the prefixes *below* the writing stage in the modifier stack.
    Entries above it — and the stage's own entry, whose prefix does not contain
    its output — stay valid for the rest of the pass. Stack order is
    authoritative: the stage record keeps creation order and a dragged modifier
    must not make an upstream stage look downstream (or the reverse).
    """
    record = engine.stack_mod.stack_of(obj)
    if record is None:
        engine._prefix_batch.pop((obj.as_pointer(), stage.key), None)
        return
    ordered = engine._ordered_stages(obj, record)
    try:
        position = ordered.index(stage)
    except ValueError:
        # A stale response for a stage no longer in the record: only its own
        # entry can be named safely.
        engine._prefix_batch.pop((obj.as_pointer(), stage.key), None)
        return
    pointer = obj.as_pointer()
    for candidate in ordered[position + 1:]:
        engine._prefix_batch.pop((pointer, candidate.key), None)


_LEGACY_DEFINITIONS = object()


def _apply(obj, stage, sections, attributes=None, definitions=_LEGACY_DEFINITIONS):
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

    A multi-part definition (`definitions[i].parts > 1`) is one namespace per
    part: `idef.<i>.<p>.*`, with one per-kind helper per part inside the same
    definition collection. The whole frame is validated before anything is
    written, so a mixed namespace or a part outside `0..N-1` rejects the frame
    instead of pairing one part's vertices with another part's faces.
    """
    # Resolve and validate the definition table first: a bad entry must reject
    # the frame before any helper is touched (atomicity), and legacy direct
    # callers that omit the table infer single-part definitions from names.
    table, strict = _definition_table(definitions, sections)
    _validate_attribute_descriptors(attributes, table)
    top_rows = engine._rows_of(sections, "")
    nested_rows = engine._nested_rows_of(sections)
    if strict:
        _validate_row_targets(table, top_rows, nested_rows)

    # A prefix batch built earlier in this pass evaluated the helpers before
    # this write. Only the entries *downstream* of this stage in the modifier
    # stack are stale: an upstream prefix cannot contain this stage's output,
    # and neither can this stage's own prefix (which ends before its modifier).
    # Clearing the whole batch on every apply rebuilt all N prefixes for every
    # stage of a multi-stage pass, which is quadratic on playback.
    _invalidate_prefix_batch(obj, stage)
    # The per-helper writes stamp their own geometry, but instance rows,
    # definition collections and cloud removals only touch helper data the memo
    # does not watch. Stamp the apply itself so every real apply — including
    # an instance-only or definition-only frame — retires every prefix memo
    # downstream of it.
    engine._bump_id(stage)

    base = {"mesh": [], "curves": [], "points": []}
    groups = {}

    for name, values in sections.items():
        index, part, rest = engine._definition_split(name)
        if index is not None:
            if part is None and rest.startswith("inst."):
                continue  # handled with the other row pairs below
            bucket = engine._kind_of(rest)
            if bucket is not None:
                entry = groups.setdefault(
                    (index, part if part is not None else 0),
                    {"mesh": [], "curves": [], "points": []},
                )
                entry[bucket].append((rest, values))
            continue
        if name.startswith("inst."):
            continue
        bucket = engine._kind_of(name)
        if bucket is not None:
            base[bucket].append((name, values))

    keep = set(table) | set(nested_rows)
    for index, _matrix in top_rows:
        keep.add(index)
    for index in sorted(nested_rows):
        for target, _matrix in nested_rows[index]:
            if target in table or target in nested_rows:
                keep.add(target)
            else:
                # The response names a nested placement whose definition group
                # is absent: keep it visible instead of building an empty block.
                engine.prefs.log(
                    "[stageloom] %s: block %d nests definition %d, which the response does not carry"
                    % (stage.key, index, target),
                    flush=True,
                )
    parts_by_index = {index: table.get(index, 1) for index in keep}

    # Definitions the response no longer carries go away whole: collection,
    # helpers, nested empties and row cloud.
    for index in list(engine.helpers.definition_collections(obj, stage.key)):
        if index not in keep:
            engine.helpers.remove_definition(obj, stage.key, index)

    # Data first, wiring after: the node group reads the final definition set.
    for index in sorted(keep):
        _collection, part_objects = engine.helpers.ensure_definition_parts(
            obj, stage.key, index, parts_by_index[index]
        )
        for part, objects in enumerate(part_objects):
            entry = groups.get((index, part))
            if entry is None:
                entry = {"mesh": [], "curves": [], "points": []}
            engine._apply_mesh(objects["mesh"], entry["mesh"])
            engine._apply_curves(objects["curves"], entry["curves"])
            engine._apply_points(objects["points"], entry["points"])

    engine._apply_mesh(engine.helpers.ensure_helper(obj, stage.key, "mesh"), base["mesh"])
    engine._apply_curves(engine.helpers.ensure_helper(obj, stage.key, "curves"), base["curves"])
    engine._apply_points(engine.helpers.ensure_helper(obj, stage.key, "points"), base["points"])
    engine._apply_attributes(obj, stage, sections, attributes, parts_by_index)
    engine._apply_instance_rows(obj, stage, keep, top_rows, nested_rows)

    engine.nodes.ensure_group(obj, stage.key, nested_rows)
    # The frame is fully on screen: the heartbeat's "applied" stamp. Written
    # here rather than at the callers so every apply path is counted once.
    engine._heartbeat["applied_at"] = engine.time.monotonic()


# A part count is an object-allocation factor on the apply side: a malformed
# frame naming billions of empty parts must be rejected, not looped over.
MAX_DEFINITION_PARTS = 4096

_DEFINITION_GEOMETRY_GROUPS = ("mesh", "subd", "crv", "pts")


def _definition_split(name):
    """``(index, part, rest)`` when a section name is inside a definition
    group, else ``(None, None, name)``.

    ``idef.<i>.mesh.verts`` is a single-part fragment: ``part`` is None.
    ``idef.<i>.<p>.mesh.verts`` is a part fragment. ``rest`` is the remainder
    after the namespace, so a definition-level ``inst.*`` row of a multi-part
    definition keeps ``part`` None (§5.2).
    """
    if not isinstance(name, str) or not name.startswith("idef."):
        return None, None, name
    parts = name.split(".", 2)
    if len(parts) != 3 or not parts[1].isascii() or not parts[1].isdigit():
        return None, None, name
    index = int(parts[1])
    rest = parts[2]
    head, separator, inner = rest.partition(".")
    if separator and inner and head.isascii() and head.isdigit():
        return index, int(head), inner
    return index, None, rest


def _definition_table(definitions, sections):
    """Validate and return ``(parts by index, strict)``.

    ``_LEGACY_DEFINITIONS`` is the legacy direct-apply form (and a
    pre-instances caller): single-part definitions are inferred from the
    section names, as the path did before multi-part definitions. A list is
    authoritative: every `idef.*` section must fit it, and an entry's `parts`
    must be a positive integer. ``None`` is not a table and rejects the frame.

    Raises :class:`GeometryIOError` on anything the apply path must not guess:
    a malformed entry, a definition index outside the table, a part outside
    ``0..N-1``, or a section that mixes the single- and multi-part namespaces.
    """
    if definitions is _LEGACY_DEFINITIONS:
        table = {}
        for name in sections:
            index, part, rest = _definition_split(name)
            if index is None:
                _reject_unparsed_definition_name(name)
                continue
            if part is not None:
                raise engine.geometry_io.GeometryIOError(
                    "section %r uses a part namespace but the frame has no definitions table"
                    % (name,)
                )
            _validate_definition_rest(name, rest, multi=False, definition_level=True)
            table[index] = 1
        return table, False

    if not isinstance(definitions, list):
        raise engine.geometry_io.GeometryIOError("definitions is not an array")
    table = {}
    for index, entry in enumerate(definitions):
        if not isinstance(entry, dict):
            raise engine.geometry_io.GeometryIOError(
                "definitions[%d] is not an object" % index
            )
        if not isinstance(entry.get("name", ""), str):
            raise engine.geometry_io.GeometryIOError(
                "definitions[%d].name is not a string" % index
            )
        parts = entry.get("parts", 1)
        if isinstance(parts, bool) or not isinstance(parts, int) or parts < 1:
            raise engine.geometry_io.GeometryIOError(
                "definitions[%d].parts is not a positive integer" % index
            )
        if parts > MAX_DEFINITION_PARTS:
            raise engine.geometry_io.GeometryIOError(
                "definitions[%d].parts is %d, over the %d-part cap"
                % (index, parts, MAX_DEFINITION_PARTS)
            )
        table[index] = parts

    for name in sections:
        index, part, rest = _definition_split(name)
        if index is None:
            _reject_unparsed_definition_name(name)
            continue
        if index >= len(table):
            raise engine.geometry_io.GeometryIOError(
                "section %r names definition %d outside the definitions table"
                % (name, index)
            )
        parts = table[index]
        if part is not None:
            if parts == 1:
                raise engine.geometry_io.GeometryIOError(
                    "section %r uses the multi-part namespace on single-part definition %d"
                    % (name, index)
                )
            if part >= parts:
                raise engine.geometry_io.GeometryIOError(
                    "section %r names part %d outside 0..%d"
                    % (name, part, parts - 1)
                )
        elif parts > 1 and not rest.startswith("inst."):
            raise engine.geometry_io.GeometryIOError(
                "section %r is definition-level in multi-part definition %d"
                % (name, index)
            )
        _validate_definition_rest(
            name, rest, multi=parts > 1, definition_level=part is None
        )
    return table, True


def _reject_unparsed_definition_name(name):
    """A name in the `idef.` namespace that does not parse is not skipped.

    `_definition_split` returns no index both for a base section and for a
    malformed `idef.` name; only the latter is an error here.
    """
    if isinstance(name, str) and name.startswith("idef."):
        raise engine.geometry_io.GeometryIOError(
            "section %r does not parse as a definition section" % (name,)
        )


def _validate_definition_rest(name, rest, multi, definition_level):
    """Reject a definition section outside the §5.2 namespaces.

    Single-part definitions accept every geometry group, `attr.*` and `inst.*`.
    Multi-part definitions only accept a definition-level `inst.*` pair; their
    geometry and attributes live under a part. An unparsed name is rejected
    rather than silently ignored.
    """
    head, separator, _inner = rest.partition(".")
    if definition_level:
        allowed = ("inst",) if multi else (
            _DEFINITION_GEOMETRY_GROUPS + ("attr", "inst")
        )
    else:
        # A part holds geometry and attributes; nested rows stay at definition
        # level, so `idef.<i>.<p>.inst.*` is never legal (§5.2).
        allowed = _DEFINITION_GEOMETRY_GROUPS + ("attr",)
    if not separator or head not in allowed:
        where = "multi-part" if multi else "single-part"
        raise engine.geometry_io.GeometryIOError(
            "section %r does not parse as a %s definition section" % (name, where)
        )


def _validate_attribute_descriptors(attributes, table):
    """Check every descriptor's ``definition``/``part`` against the table and
    the section it names (§5.2). A descriptor is part of the frame's meaning,
    so a mismatch rejects the frame instead of writing one part's values onto
    another part's helper."""
    for position, descriptor in enumerate(attributes or []):
        if not isinstance(descriptor, dict):
            raise engine.geometry_io.GeometryIOError(
                "attribute descriptor %d is not an object" % position
            )
        definition = descriptor.get("definition")
        part = descriptor.get("part")
        if definition is None:
            if part is not None:
                raise engine.geometry_io.GeometryIOError(
                    "attribute descriptor %d has a part but no definition" % position
                )
            section = descriptor.get("section")
            if isinstance(section, str) and section.startswith("idef."):
                raise engine.geometry_io.GeometryIOError(
                    "attribute descriptor %d names definition section %r without "
                    "a definition index" % (position, section)
                )
            offsets = descriptor.get("offsets")
            if isinstance(offsets, str) and offsets.startswith("idef."):
                raise engine.geometry_io.GeometryIOError(
                    "attribute descriptor %d names definition offsets %r without "
                    "a definition index" % (position, offsets)
                )
            continue
        if (
            isinstance(definition, bool)
            or not isinstance(definition, int)
            or definition not in table
        ):
            raise engine.geometry_io.GeometryIOError(
                "attribute descriptor %d names definition %r outside the table"
                % (position, definition)
            )
        parts = table[definition]
        if parts == 1:
            if part is not None:
                raise engine.geometry_io.GeometryIOError(
                    "attribute descriptor %d has a part on single-part definition %d"
                    % (position, definition)
                )
        elif (
            isinstance(part, bool)
            or not isinstance(part, int)
            or part < 0
            or part >= parts
        ):
            raise engine.geometry_io.GeometryIOError(
                "attribute descriptor %d names part %r outside 0..%d"
                % (position, part, parts - 1)
            )
        section = descriptor.get("section")
        if not isinstance(section, str):
            raise engine.geometry_io.GeometryIOError(
                "attribute descriptor %d has no section name" % position
            )
        index, section_part, _rest = _definition_split(section)
        expected_part = None if parts == 1 else part
        if index != definition or section_part != expected_part:
            raise engine.geometry_io.GeometryIOError(
                "attribute descriptor %d section %r does not match definition %d part %r"
                % (position, section, definition, expected_part)
            )
        offsets = descriptor.get("offsets")
        if offsets is not None:
            offset_index, offset_part, _offset_rest = _definition_split(offsets)
            if offset_index != definition or offset_part != expected_part:
                raise engine.geometry_io.GeometryIOError(
                    "attribute descriptor %d offsets %r do not match its definition/part"
                    % (position, offsets)
                )


def _validate_row_targets(table, top_rows, nested_rows):
    """Every row target must index the definition table (§5.2)."""
    for target, _matrix in top_rows:
        if target not in table:
            raise engine.geometry_io.GeometryIOError(
                "instance row references definition %d outside the table" % target
            )
    for scope, rows in nested_rows.items():
        if scope not in table:
            raise engine.geometry_io.GeometryIOError(
                "nested row scope names definition %d outside the table" % scope
            )
        for target, _matrix in rows:
            if target not in table:
                raise engine.geometry_io.GeometryIOError(
                    "definition %d nests definition %d outside the table"
                    % (scope, target)
                )


def _nested_rows_of(sections):
    """Every definition's nested placements, ``{index: [(target, matrix)]}``.

    One source of truth for the apply path and for the same-hash repair path
    (a frame already applied still has to be able to rebuild the node group)."""
    scopes = {}
    for name in sections:
        index, part, rest = engine._definition_split(name)
        if index is not None and part is None and rest.startswith("inst."):
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


def _selected_mesh_group(sections, definition=None, part=None, parts=1):
    """The topology group the frame selects for one scope, or None.

    The attribute path uses the same ``select_mesh_group`` owner as the mesh
    writer, so attributes can never be attached from the group whose vertices
    and faces were not written. A multi-part definition's scope is its part
    prefix, never the whole definition.
    """
    if definition is None:
        prefix = ""
    elif parts > 1:
        prefix = "idef.%d.%d." % (int(definition), int(part))
    else:
        prefix = "idef.%d." % int(definition)
    by_name = {
        name[len(prefix):]: values
        for name, values in sections.items()
        if name.startswith(prefix)
    }
    try:
        return engine.geometry_io.select_mesh_group(by_name)
    except engine.geometry_io.GeometryIOError:
        # The mesh writer raises the classified error; the attribute pass must
        # not turn it into a second, different failure.
        return None


def _rows_of(sections, prefix):
    """The decoded row pairs of one scope as ``[(definition index, matrix)]``.

    Both halves are all-or-nothing; a missing or mismatched pair is reported.
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
    # Earlier builds used an Empty with a collection instance for a nested
    # placement; the node graph carries it now, so remove any such object.
    for index in sorted(definitions):
        collection = engine.bpy.data.collections.get(
            engine.helpers.definition_collection_name(obj, stage.key, index)
        )
        if collection is None:
            continue
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


def _apply_attributes(obj, stage, sections, attributes, parts_by_index):
    """Reconcile the frame's attribute sections with the helpers they belong to.

    mesh and subd share the mesh helper (a SubD cage is a mesh); points go to
    the point cloud helper. A descriptor with a ``definition`` index targets
    that definition's helper instead (protocol §5.2), and for a multi-part
    definition its ``part`` selects that part's helper inside the same
    collection. Curve attributes are not transferred yet: Blender's curve
    helper is a Curve datablock, which cannot store attributes.

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
        part = descriptor.get("part")
        grouped.setdefault((definition, part, kind), []).append(
            (descriptor, values, offsets)
        )

    targets = {(None, None, "mesh"), (None, None, "points")}
    for index, parts in sorted(parts_by_index.items()):
        for part in range(parts):
            key_part = part if parts > 1 else None
            targets.add((int(index), key_part, "mesh"))
            targets.add((int(index), key_part, "points"))
    targets.update(grouped)

    def order(target):
        definition, part, kind = target
        if definition is None:
            return (0, -1, -1, kind)
        return (
            1,
            int(definition),
            -1 if part is None else int(part),
            kind,
        )

    selected_mesh = {}

    def selected_group(definition, part):
        key = (definition, part)
        if key not in selected_mesh:
            parts = parts_by_index.get(int(definition), 1) if definition is not None else 1
            selected_mesh[key] = _selected_mesh_group(
                sections, definition, part=part, parts=parts
            )
        return selected_mesh[key]

    for definition, part, kind in sorted(targets, key=order):
        if definition is None:
            helper = engine.helpers.helper_for(obj, stage.key, kind)
        else:
            parts = parts_by_index.get(int(definition), 1)
            _collection, part_objects = engine.helpers.ensure_definition_parts(
                obj, stage.key, int(definition), parts
            )
            helper = part_objects[part if part is not None else 0][kind]
        if helper is None:
            continue
        entries = grouped.get((definition, part, kind), [])
        if kind == "mesh" and entries:
            chosen = selected_group(definition, part)
            if chosen is not None:
                entries = [
                    entry
                    for entry in entries
                    if (entry[0].get("group") or "") == chosen
                ]
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
            engine._bump_id(helper)
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
            engine._bump_id(helper)
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
            engine._bump_id(helper)
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

