// Outbound instances (Stage Out): GH_InstanceReference -> definition table +
// rows. DefinitionTableWriter owns the grammar and dedupe.

using System;
using System.Collections.Generic;
using Grasshopper.Kernel.Types;
using Grasshopper.Rhinoceros.Model;
using Rhino;
using Rhino.DocObjects;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom
{
    internal static class InstanceEmit
    {
        /// <summary>Write the instance rows and the definitions they reference.
        /// Without `blocks/2` a multi-part definition splits into single-part
        /// ones and every referring row fans out at the same placement.</summary>
        public static void Emit(IList<GH_InstanceReference> instances, double scale,
                                List<Section> sections,
                                List<Dictionary<string, object>> attributes,
                                List<Dictionary<string, object>> definitions,
                                List<WireWarning> warnings,
                                bool peerSupportsParts,
                                Dictionary<string, object> counts = null)
        {
            if (instances == null || instances.Count == 0) return;
            var writer = new DefinitionWriter(scale, sections, attributes, definitions, warnings,
                                              peerSupportsParts);
            var refDef = new List<uint>();
            var transforms = new List<float>();
            foreach (GH_InstanceReference instance in instances)
            {
                if (instance == null || instance.Value == null)
                {
                    warnings.Add(new WireWarning(WarningCodes.InstanceSkipped,
                        "An instance reference without a value was ignored"));
                    continue;
                }
                int[] indices = writer.Definition(instance.InstanceDefinition,
                                                  instance.Value.ParentIdefId);
                if (indices == null) continue;
                foreach (int index in indices)
                {
                    if (index < 0) continue;
                    refDef.Add((uint)index);
                    transforms.AddRange(Conversion.TransformToWire(instance.Value.Xform, scale));
                }
            }
            if (refDef.Count == 0) return;
            sections.Add(Section.U32("inst.ref_def", refDef.ToArray()));
            sections.Add(Section.Floats("inst.transforms", transforms.ToArray()));
            if (counts != null)
            {
                counts["definitions"] = (long)definitions.Count;
                counts["instances"] = (long)refDef.Count;
            }
        }

        /// <summary>Whether any list carries a transportable kind.</summary>
        private static bool HasGeometry(params System.Collections.IList[] lists)
        {
            foreach (System.Collections.IList list in lists)
                if (list.Count > 0) return true;
            return false;
        }

        /// <summary>One definition part: every non-reference model object is
        /// one part, and never flattened into another kind.</summary>
        internal sealed class GeometryPart
        {
            public readonly List<Mesh> Meshes = new List<Mesh>();
            public readonly List<Curve> Curves = new List<Curve>();
            public readonly List<Point3d> Points = new List<Point3d>();
            public readonly List<SubD> Subds = new List<SubD>();
            public readonly List<Brep> Breps = new List<Brep>();
            public readonly List<GeometryBase> Sources = new List<GeometryBase>();

            public bool HasGeometry
            {
                get
                {
                    return HasGeometry(Meshes, Curves, Points, Subds, Breps);
                }
            }
        }

        /// <summary>A gathered definition: parts in object order plus nested rows.</summary>
        internal sealed class DefinitionContent
        {
            public readonly List<GeometryPart> Parts = new List<GeometryPart>();
            public readonly List<DefinitionTableWriter.NestedRow> Nested =
                new List<DefinitionTableWriter.NestedRow>();

            public bool HasContent
            {
                get
                {
                    foreach (GeometryPart part in Parts)
                        if (part.HasGeometry) return true;
                    foreach (DefinitionTableWriter.NestedRow row in Nested)
                        if (row != null) return true;
                    return false;
                }
            }
        }

        /// <summary>Gathers and writes one definition: paired graph-side
        /// definitions preferred, document blocks by id otherwise.</summary>
        internal sealed class DefinitionWriter
        {
            private readonly double _scale;
            private readonly List<WireWarning> _warnings;
            private readonly DefinitionTableWriter _table;
            private readonly Dictionary<string, DefinitionContent> _contents =
                new Dictionary<string, DefinitionContent>(StringComparer.Ordinal);

            public DefinitionWriter(double scale, List<Section> sections,
                                    List<Dictionary<string, object>> attributes,
                                    List<Dictionary<string, object>> definitions,
                                    List<WireWarning> warnings,
                                    bool peerSupportsParts)
            {
                _scale = scale;
                _warnings = warnings;
                _table = new DefinitionTableWriter(sections, attributes, definitions,
                                                   warnings, peerSupportsParts);
            }

            /// <summary>Every table index for one instance's definition, writing
            /// it on first use; a fallback split returns one per part.</summary>
            public int[] Definition(ModelInstanceDefinition paired, Guid referenceId)
            {
                string key = Key(paired, referenceId);
                int[] known;
                if (_table.TryGet(key, out known)) return known;

                if (paired != null && paired.Objects != null && paired.Objects.Count > 0)
                {
                    DefinitionContent content = Content(key, paired, null);
                    return Write(DisplayName(paired), key, content);
                }

                if (referenceId != Guid.Empty)
                {
                    RhinoDoc document = RhinoDoc.ActiveDoc;
                    InstanceDefinition docDef = document == null
                        ? null : document.InstanceDefinitions.Find(referenceId, true);
                    if (docDef == null)
                    {
                        // A document block the id names is gone (deleted,
                        // another document): an empty table entry would be a
                        // phantom block, so the row is skipped loudly instead.
                        Warn("An instance reference to a document block (" + referenceId.ToString("N") + ") could not be resolved; the row was skipped");
                        return null;
                    }
                    DefinitionContent content = Content(key, null, docDef);
                    return Write(docDef.Name ?? "", key, content);
                }

                Warn("An instance reference without a definition was skipped");
                return null;
            }

            private void Warn(string message)
            {
                _warnings.Add(new WireWarning(WarningCodes.InstanceSkipped, message));
            }

            private int[] Write(string name, string key, DefinitionContent content)
            {
                if (!content.HasContent)
                {
                    // All the definition's objects were null (or none survived):
                    // writing the table entry would give the row an empty block,
                    // which Blender would draw as nothing. Drop the row instead.
                    Warn("Block definition \"" + DefinitionIdentity.Label(name) + "\" has no objects and was skipped");
                    return null;
                }

                List<DefinitionTableWriter.Part> parts = BuildParts(content);
                return _table.Write(key, name, parts, content.Nested);
            }

            /// <summary>Convert gathered parts to neutral buffers. A single-part
            /// definition keeps the legacy global attribute indices.</summary>
            private List<DefinitionTableWriter.Part> BuildParts(DefinitionContent content)
            {
                var result = new List<DefinitionTableWriter.Part>(content.Parts.Count);
                bool single = content.Parts.Count <= 1;
                int attributeBase = _table.AttributeCount;
                for (int part = 0; part < content.Parts.Count; part++)
                {
                    int index0 = single ? attributeBase : 0;
                    int? partIndex = content.Parts.Count > 1 && _table.PeerSupportsParts
                        ? (int?)part : null;
                    result.Add(BuildPart(content.Parts[part], index0, partIndex));
                }
                return result;
            }

            private DefinitionTableWriter.Part BuildPart(GeometryPart part, int index0,
                                                          int? partIndex)
            {
                var buffer = new DefinitionTableWriter.Part();
                if (!part.HasGeometry) return buffer;

                Dictionary<string, object> counts;
                List<Section> geometry = Conversion.SectionsFromGeometry(
                    part.Meshes, part.Curves, part.Points, part.Subds, part.Breps,
                    _scale, _warnings, out counts);
                buffer.Sections.AddRange(geometry);

                foreach (GeometryBase source in part.Sources)
                {
                    string group = AttributeStore.GroupOf(source);
                    if (group == null) continue;
                    // index0 continues across several sources of one part.
                    // A multi-part part starts at zero so its content key is
                    // index-free; a single-part definition starts at the
                    // frame's committed count so its legacy bytes are exact.
                    AttributeStore.Collect(source, group, buffer.Sections,
                                           buffer.Descriptors, "", 0L, partIndex,
                                           index0 + buffer.Descriptors.Count);
                }
                return buffer;
            }

            private DefinitionContent Content(string key, ModelInstanceDefinition paired,
                                              InstanceDefinition docDef)
            {
                DefinitionContent content;
                if (key != null && _contents.TryGetValue(key, out content)) return content;
                content = paired != null ? GatherPaired(paired) : GatherDocument(docDef);
                if (key != null) _contents[key] = content;
                return content;
            }

            private DefinitionContent GatherPaired(ModelInstanceDefinition paired)
            {
                var content = new DefinitionContent();
                if (paired.Objects != null)
                    foreach (ModelObject obj in paired.Objects)
                        GatherModelObject(obj, content);
                return content;
            }

            private DefinitionContent GatherDocument(InstanceDefinition docDef)
            {
                var content = new DefinitionContent();
                RhinoObject[] objects = docDef.GetObjects();
                if (objects != null)
                {
                    foreach (RhinoObject obj in objects)
                    {
                        if (obj == null)
                        {
                            Warn("A null object in block definition \"" + DefinitionIdentity.Label(docDef.Name) + "\" was skipped");
                            continue;
                        }
                        GatherDocumentObject(obj, content);
                    }
                }
                return content;
            }

            private void GatherModelObject(ModelObject obj, DefinitionContent content)
            {
                if (obj == null)
                {
                    Warn("A null object in a block definition was skipped");
                    return;
                }

                // A block instance is the one definitive way to reach the child
                // definition and its transform.
                GH_InstanceReference goo;
                if (obj.CastTo<GH_InstanceReference>(out goo))
                {
                    if (goo != null && goo.Value != null)
                    {
                        content.Nested.Add(Row(goo));
                        return;
                    }
                    if (obj.ObjectType == ObjectType.InstanceReference)
                    {
                        Warn("An instance reference in a block definition has no wire form" + " and was skipped");
                        return;
                    }
                }
                if (obj.ObjectType == ObjectType.InstanceReference)
                {
                    InstanceReferenceGeometry reference;
                    if (obj.CastTo<InstanceReferenceGeometry>(out reference) && reference != null)
                    {
                        content.Nested.Add(Row(new GH_InstanceReference(reference)));
                        return;
                    }
                    Warn("An instance reference in a block definition has no wire form" + " and was skipped");
                    return;
                }

                var part = new GeometryPart();
                GeometryBase geometry = GeometryOf(obj);
                if (geometry != null) CollectGeometry(geometry, part, content.Nested);
                else
                    Warn("An object in a block definition (" + obj.TypeName + ") has no wire form and was skipped");
                // It stays a part even when empty: dropping it would renumber
                // every later part. A null list entry is not an object and is
                // skipped above, so it never consumes a part index.
                content.Parts.Add(part);
            }

            private void GatherDocumentObject(RhinoObject obj, DefinitionContent content)
            {
                GeometryBase geometry = obj.Geometry;
                if (geometry == null)
                {
                    Warn("An object in a block definition (" + obj.ObjectType + ") has no geometry and was skipped");
                    return;
                }

                var part = new GeometryPart();
                CollectGeometry(geometry, part, content.Nested);
                if (geometry is InstanceReferenceGeometry) return;
                content.Parts.Add(part);
            }

            /// <summary>One geometry out of a ModelObject, ObjectType first, so
            /// a cast cannot mesh a Brep or edge-ify a mesh.</summary>
            private static readonly Dictionary<ObjectType, Func<ModelObject, GeometryBase>> ObjectCasts =
                new Dictionary<ObjectType, Func<ModelObject, GeometryBase>>
            {
                { ObjectType.Mesh, delegate(ModelObject obj)
                    { Mesh mesh; return obj.CastTo<Mesh>(out mesh) ? mesh : null; } },
                { ObjectType.Brep, delegate(ModelObject obj)
                    { Brep brep; return obj.CastTo<Brep>(out brep) ? brep : null; } },
                { ObjectType.Surface, delegate(ModelObject obj)
                    { Brep brep; return obj.CastTo<Brep>(out brep) ? brep : null; } },
                { ObjectType.Curve, delegate(ModelObject obj)
                    { Curve curve; return obj.CastTo<Curve>(out curve) ? curve : null; } },
                { ObjectType.Point, delegate(ModelObject obj)
                    { Point point; return obj.CastTo<Point>(out point) ? point : null; } },
                { ObjectType.PointSet, delegate(ModelObject obj)
                    { PointCloud cloud; return obj.CastTo<PointCloud>(out cloud) ? cloud : null; } },
                { ObjectType.SubD, delegate(ModelObject obj)
                    { SubD subd; return obj.CastTo<SubD>(out subd) ? subd : null; } },
            };

            private GeometryBase GeometryOf(ModelObject obj)
            {
                Func<ModelObject, GeometryBase> cast;
                if (ObjectCasts.TryGetValue(obj.ObjectType, out cast))
                {
                    GeometryBase geometry = cast(obj);
                    if (geometry != null) return geometry;
                }
                // Last resort: whichever of the transportable kinds it becomes.
                Mesh fallbackMesh;
                if (obj.CastTo<Mesh>(out fallbackMesh) && fallbackMesh != null)
                    return fallbackMesh;
                Curve fallbackCurve;
                if (obj.CastTo<Curve>(out fallbackCurve) && fallbackCurve != null)
                    return fallbackCurve;
                Point fallbackPoint;
                if (obj.CastTo<Point>(out fallbackPoint) && fallbackPoint != null)
                    return fallbackPoint;
                PointCloud fallbackCloud;
                if (obj.CastTo<PointCloud>(out fallbackCloud) && fallbackCloud != null)
                    return fallbackCloud;
                return null;
            }

            /// <summary>The outbound geometry-kind table: cast, part slot and
            /// attribute-source flag per transportable kind.</summary>
            private sealed class GeometryKind
            {
                public readonly Type Type;
                public readonly bool Source;
                public readonly Action<GeometryPart, GeometryBase> Add;

                public GeometryKind(Type type, bool source, Action<GeometryPart, GeometryBase> add)
                {
                    Type = type;
                    Source = source;
                    Add = add;
                }
            }

            private static readonly GeometryKind[] GeometryKinds =
            {
                new GeometryKind(typeof(Mesh), true, delegate(GeometryPart part, GeometryBase geometry)
                { part.Meshes.Add((Mesh)geometry); }),
                new GeometryKind(typeof(SubD), true, delegate(GeometryPart part, GeometryBase geometry)
                { part.Subds.Add((SubD)geometry); }),
                new GeometryKind(typeof(PointCloud), true, delegate(GeometryPart part, GeometryBase geometry)
                { part.Points.AddRange(((PointCloud)geometry).GetPoints()); }),
                new GeometryKind(typeof(Brep), false, delegate(GeometryPart part, GeometryBase geometry)
                { part.Breps.Add((Brep)geometry); }),
                new GeometryKind(typeof(Curve), false, delegate(GeometryPart part, GeometryBase geometry)
                { part.Curves.Add((Curve)geometry); }),
                new GeometryKind(typeof(Point), false, delegate(GeometryPart part, GeometryBase geometry)
                { part.Points.Add(((Point)geometry).Location); }),
            };

            private void CollectGeometry(GeometryBase geometry, GeometryPart part,
                                         List<DefinitionTableWriter.NestedRow> nested)
            {
                if (geometry == null) return;
                InstanceReferenceGeometry reference = geometry as InstanceReferenceGeometry;
                if (reference != null)
                {
                    var goo = new GH_InstanceReference(reference);
                    if (goo.Value != null) nested.Add(Row(goo));
                    else
                        Warn("An instance reference in a block definition has no wire form" + " and was skipped");
                    return;
                }

                foreach (GeometryKind kind in GeometryKinds)
                {
                    if (!kind.Type.IsInstanceOfType(geometry)) continue;
                    kind.Add(part, geometry);
                    if (kind.Source) part.Sources.Add(geometry);
                    return;
                }
                Warn("A block definition object (" + geometry.ObjectType + ") has no wire form and was skipped");
            }

            private DefinitionTableWriter.NestedRow Row(GH_InstanceReference goo)
            {
                float[] wire = Conversion.TransformToWire(goo.Value.Xform, _scale);
                string signature = Key(goo.InstanceDefinition, goo.Value.ParentIdefId);
                return new DefinitionTableWriter.NestedRow(wire, signature,
                    delegate
                    {
                        return Definition(goo.InstanceDefinition, goo.Value.ParentIdefId);
                    });
            }

            private static string Key(ModelInstanceDefinition paired, Guid referenceId)
            {
                if (paired != null && paired.Id.HasValue) return "id:" + paired.Id.Value.ToString("N");
                if (referenceId != Guid.Empty) return "id:" + referenceId.ToString("N");
                if (paired != null) return "obj:" + System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(paired);
                return null;
            }

            private static string DisplayName(ModelInstanceDefinition paired)
            {
                if (paired == null) return "";
                string display = paired.DisplayName;
                if (!string.IsNullOrEmpty(display)) return display;
                object rawName = paired.Name;
                if (rawName != null)
                {
                    string name = rawName.ToString();
                    if (!string.IsNullOrEmpty(name)) return name;
                }
                return "";
            }
        }
    }
}
