// Instances facade: the table/rows <-> Rhino block instances entry points.

using System;
using System.Collections.Generic;
using Grasshopper.Kernel.Types;
using Rhino;
using Rhino.Geometry;
using Stageloom.Core;

namespace Stageloom
{
    public static class Instances
    {
        /// <summary>The name given to definitions Blender did not name.</summary>
        public const string DefinitionPrefix = DefinitionIdentity.Prefix;

        /// <summary>The protocol's definition-part cap (4096).</summary>
        internal const int MaxDefinitionParts = DefinitionTable.MaxParts;

        /// <summary>Definition rows and transforms, decoded (protocol §5.2).</summary>
        public static bool ReadRows(DecodedFrame frame, string prefix, double scale,
                                    out long[] refDef, out List<Transform> transforms,
                                    Action<string> note = null)
        {
            return InstanceBuild.ReadRows(frame, prefix, scale, out refDef, out transforms, note);
        }

        /// <summary>Append the frame's instances as GH_InstanceReference items.</summary>
        public static void Build(DecodedFrame frame, double scale,
                                 List<Dictionary<string, object>> attributes,
                                 List<IGH_Goo> geometry, Action<string> note)
        {
            InstanceBuild.Build(frame, scale, attributes, geometry, note);
        }

        /// <summary>Write GH_InstanceReference items into a response.</summary>
        public static void Emit(IList<GH_InstanceReference> instances, double scale,
                                List<Section> sections,
                                List<Dictionary<string, object>> attributes,
                                List<Dictionary<string, object>> definitions,
                                List<WireWarning> warnings,
                                bool peerSupportsParts,
                                Dictionary<string, object> counts = null,
                                RhinoDoc document = null)
        {
            InstanceEmit.Emit(instances, scale, sections, attributes, definitions, warnings,
                              peerSupportsParts, counts, document);
        }
    }
}
