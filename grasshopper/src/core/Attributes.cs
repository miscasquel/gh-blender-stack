// Attributes ride on the geometry, natively, on both platforms.
//
// Blender stores named typed arrays on its datablocks, so Rhino's twin is the
// geometry's own UserDictionary (ArchivableDictionary): it is part of the
// geometry, it is duplicated and transformed with it, and it serializes into
// the document. This file is the one place that maps the wire's attribute
// vocabulary (Blender's data types) onto dictionary values, for both directions
// and for the Attribute Get/Set components.
//
// Wire shape (protocol §5): one `attr.<i>.values` section per attribute plus a
// header descriptor carrying name, domain, type and shape. STRING adds an
// `attr.<i>.offsets` section with each string's byte length.
//
// Compound values are stored as flat arrays of their elements, so the dictionary
// holds only array types it can serialize natively: double[], int[], bool[],
// byte[] and string[]. `components` says how many entries make one element.
//
// The mechanism lives on the root ArchivableDictionary, not on GeometryBase:
// that is the part worth testing, and it runs under the console suite without
// Rhino's native half (the geometry methods below are thin wrappers).

using System;
using System.Collections.Generic;
using System.Text;
using Rhino.Collections;
using Rhino.Geometry;

namespace Stageloom.Core
{
    public static class AttributeStore
    {
        // Persisted ArchivableDictionary key: keep it across the rebrand so
        // attributes stored by older components still read back.
        public const string RootKey = "GHStack.Attributes";

        // Blender data type -> storage kind, components, wire dtype and UI label.
        // Each kind carries its own check/decode/encode/slice behaviour.
        private sealed class StorageKind
        {
            public readonly string Name;
            public readonly Func<object, int> Length;
            public readonly Func<object, string> ItemProblem;
            public readonly Func<Section, object> Decode;
            public readonly Func<string, object, Section> Encode;
            public readonly Action<ArchivableDictionary, object> Store;
            public readonly Func<object, int, int, int, object> Slice;

            public StorageKind(string name, Func<object, int> length,
                               Func<object, string> itemProblem,
                               Func<Section, object> decode,
                               Func<string, object, Section> encode,
                               Action<ArchivableDictionary, object> store,
                               Func<object, int, int, int, object> slice)
            {
                Name = name;
                Length = length;
                ItemProblem = itemProblem;
                Decode = decode;
                Encode = encode;
                Store = store;
                Slice = slice;
            }
        }

        private sealed class TypeInfo
        {
            public readonly StorageKind Kind;
            public readonly int Components;
            public readonly string Label;
            public readonly string Dtype;

            public TypeInfo(StorageKind kind, int components, string label, string dtype)
            {
                Kind = kind;
                Components = components;
                Label = label;
                Dtype = dtype;
            }
        }

        private static int LengthOf<T>(object values)
        {
            T[] array = values as T[];
            return array == null ? -1 : array.Length;
        }

        private static string StringItems(object values)
        {
            foreach (string item in (string[])values)
                if (item == null) return "STRING values must not be null";
            return null;
        }

        private static readonly StorageKind DoubleKind = new StorageKind(
            "double", LengthOf<double>, null,
            delegate(Section values)
            {
                float[] raw = values.ToFloats();
                var target = new double[raw.Length];
                for (int i = 0; i < raw.Length; i++) target[i] = raw[i];
                return target;
            },
            delegate(string name, object values)
            {
                double[] raw = (double[])values;
                var target = new float[raw.Length];
                for (int i = 0; i < raw.Length; i++) target[i] = (float)raw[i];
                return Section.Floats(name, target);
            },
            delegate(ArchivableDictionary record, object values)
            { record.Set("values", (double[])values); },
            delegate(object flat, int start, int count, int components)
            { return SliceArray((double[])flat, start, count, components); });

        private static readonly StorageKind IntKind = new StorageKind(
            "int", LengthOf<int>, null,
            delegate(Section values) { return values.ToI32(); },
            delegate(string name, object values) { return Section.Ints(name, (int[])values); },
            delegate(ArchivableDictionary record, object values)
            { record.Set("values", (int[])values); },
            delegate(object flat, int start, int count, int components)
            { return SliceArray((int[])flat, start, count, components); });

        private static readonly StorageKind BoolKind = new StorageKind(
            "bool", LengthOf<bool>, null,
            delegate(Section values)
            {
                byte[] raw = values.Raw;
                var target = new bool[raw.Length];
                for (int i = 0; i < raw.Length; i++) target[i] = raw[i] != 0;
                return target;
            },
            delegate(string name, object values)
            {
                bool[] raw = (bool[])values;
                var target = new byte[raw.Length];
                for (int i = 0; i < raw.Length; i++) target[i] = raw[i] ? (byte)1 : (byte)0;
                return Section.Bytes(name, target);
            },
            delegate(ArchivableDictionary record, object values)
            { record.Set("values", (bool[])values); },
            delegate(object flat, int start, int count, int components)
            { return SliceArray((bool[])flat, start, count, components); });

        private static readonly StorageKind ByteKind = new StorageKind(
            "byte", LengthOf<byte>, null,
            delegate(Section values) { return values.Raw; },
            delegate(string name, object values) { return Section.Bytes(name, (byte[])values); },
            delegate(ArchivableDictionary record, object values)
            { record.Set("values", (byte[])values); },
            delegate(object flat, int start, int count, int components)
            { return SliceArray((byte[])flat, start, count, components); });

        private static readonly StorageKind StringKind = new StorageKind(
            "string", LengthOf<string>, StringItems,
            delegate(Section values) { return null; }, // DecodeValues splits STRING
            delegate(string name, object values)
            {
                var lines = (string[])values;
                var bytes = new List<byte>();
                foreach (string item in lines)
                    bytes.AddRange(Encoding.UTF8.GetBytes(item));
                return Section.Bytes(name, bytes.ToArray());
            },
            delegate(ArchivableDictionary record, object values)
            { record.Set("values", (string[])values); },
            delegate(object flat, int start, int count, int components)
            { return SliceArray((string[])flat, start, count, components); });

        private static readonly Dictionary<string, TypeInfo> Types = new Dictionary<string, TypeInfo>
        {
            { "FLOAT", new TypeInfo(DoubleKind, 1, "Float", "f32") },
            { "INT", new TypeInfo(IntKind, 1, "Integer", "i32") },
            { "BOOLEAN", new TypeInfo(BoolKind, 1, "Boolean", "u8") },
            { "FLOAT_VECTOR", new TypeInfo(DoubleKind, 3, "Vector", "f32") },
            { "FLOAT2", new TypeInfo(DoubleKind, 2, "2D Vector", "f32") },
            { "FLOAT4", new TypeInfo(DoubleKind, 4, "4D Vector", "f32") },
            { "FLOAT_COLOR", new TypeInfo(DoubleKind, 4, "Color", "f32") },
            { "BYTE_COLOR", new TypeInfo(ByteKind, 4, "Byte Color", "u8") },
            { "QUATERNION", new TypeInfo(DoubleKind, 4, "Quaternion", "f32") },
            { "FLOAT4X4", new TypeInfo(DoubleKind, 16, "4x4 Matrix", "f32") },
            { "INT8", new TypeInfo(IntKind, 1, "8-Bit Integer", "i32") },
            { "INT16_2D", new TypeInfo(IntKind, 2, "2D Integer Vector", "i32") },
            { "INT32_2D", new TypeInfo(IntKind, 2, "2D Integer Vector", "i32") },
            { "STRING", new TypeInfo(StringKind, 1, "String", "u8") },
        };

        /// <summary>The Blender UI name of a wire type id ("FLOAT" -> "Float").</summary>
        public static string Label(string type)
        {
            TypeInfo info;
            return Types.TryGetValue(type ?? "", out info) ? info.Label : type;
        }

        /// <summary>The domain as a display word ("POINT" -> "Point"). The wire
        /// and the dictionary keep the protocol's upper-case names.</summary>
        public static string DomainLabel(string domain)
        {
            if (string.IsNullOrEmpty(domain)) return "";
            // Two POINT domains exist on the wire: group crv means per curve
            // point, group pts means per point-cloud point. The curve-local
            // dictionary keeps the distinction so Attribute Get can say which.
            if (domain == "CURVE_POINT") return "Curve Point";
            return char.ToUpperInvariant(domain[0]) + domain.Substring(1).ToLowerInvariant();
        }

        /// <summary>The protocol pool a geometry object belongs to, or null when
        /// attributes cannot be attached to it.</summary>
        private static readonly KeyValuePair<Type, string>[] Groups =
        {
            new KeyValuePair<Type, string>(typeof(Mesh), "mesh"),
            new KeyValuePair<Type, string>(typeof(SubD), "subd"),
            new KeyValuePair<Type, string>(typeof(PointCloud), "pts"),
        };

        public static string GroupOf(GeometryBase geometry)
        {
            foreach (KeyValuePair<Type, string> group in Groups)
                if (group.Key.IsInstanceOfType(geometry)) return group.Value;
            return null;
        }

        // ------------------------------------------------------------- storage

        /// <summary>True when the root holds at least one well-formed attribute
        /// record. Non-attribute entries (another plugin's dictionary, a stale
        /// value of the wrong shape) do not count.</summary>
        public static bool Has(ArchivableDictionary root)
        {
            if (root == null) return false;
            foreach (string name in root.Keys)
            {
                ArchivableDictionary record;
                if (!root.TryGetDictionary(name, out record) || record == null) continue;
                string type, domain, problem;
                object values;
                if (!TryRecord(record, out type, out domain, out values, out problem)) continue;
                int count;
                if (!TryValues(type, values, out count, out problem)) continue;
                return true;
            }
            return false;
        }

        public static string[] Names(ArchivableDictionary root)
        {
            return root == null ? new string[0] : root.Keys;
        }

        /// <summary>One attribute's metadata and flat values, or false. Field
        /// reads are typed so a corrupted record cannot throw a cast exception
        /// into Attribute Get.</summary>
        public static bool TryGet(ArchivableDictionary root, string name,
                                  out string type, out string domain, out object values)
        {
            type = null;
            domain = "";
            values = null;
            if (root == null) return false;
            ArchivableDictionary record;
            if (!root.TryGetDictionary(name, out record) || record == null) return false;
            object raw;
            if (record.TryGetValue("type", out raw)) type = raw as string;
            if (record.TryGetValue("domain", out raw) && raw != null)
                domain = raw as string ?? "";
            if (string.IsNullOrEmpty(type) || !Types.ContainsKey(type)) return false;
            if (!record.TryGetValue("values", out values) || values == null) return false;
            int count;
            string problem;
            return TryValues(type, values, out count, out problem);
        }

        /// <summary>Store one attribute (values as a flat native array). This is
        /// the outbound owner: an unknown type, a wrong array kind or a length
        /// that is not a whole number of elements is a typed bad_attribute, not
        /// an InvalidCastException later in Collect.</summary>
        public static void Set(ArchivableDictionary root, string name, string type, string domain, object values)
        {
            TypeInfo info;
            if (type == null || !Types.TryGetValue(type, out info))
                throw new FrameException("bad_attribute",
                    "unknown attribute type " + (type ?? "(missing)"));
            if (string.IsNullOrEmpty(name))
                throw new FrameException("bad_attribute", "attribute name must be a non-empty string");
            if (string.IsNullOrEmpty(domain))
                throw new FrameException("bad_attribute",
                    "attribute " + name + ": domain must be a non-empty string");
            string problem;
            int count;
            if (!TryValues(type, values, out count, out problem))
                throw new FrameException("bad_attribute", "attribute " + name + ": " + problem);
            if (root == null)
                throw new FrameException("bad_attribute",
                    "attribute " + name + ": no dictionary to store it in");
            Store(root, name, type, domain, values);
        }

        /// <summary>Write an already-validated attribute record. The inbound
        /// path uses this so a truncated descriptor cannot raise from Set after
        /// its values have been decoded and checked.</summary>
        private static void Store(ArchivableDictionary root, string name, string type,
                                  string domain, object values)
        {
            var record = new ArchivableDictionary();
            record.Set("type", type);
            record.Set("domain", domain ?? "");
            record.Set("components", Types[type].Components);
            Types[type].Kind.Store(record, values);
            root.Set(name, record);
        }

        /// <summary>The one check that a dictionary value has the storage kind
        /// and element shape its attribute type names.</summary>
        private static bool TryValues(string type, object values, out int count, out string problem)
        {
            count = 0;
            problem = null;
            if (values == null) { problem = "values are missing"; return false; }
            TypeInfo info = Types[type];
            int length = info.Kind.Length(values);
            if (length < 0)
            {
                problem = "values must be " + info.Kind.Name + "[] for " + type;
                return false;
            }
            if (info.Kind.ItemProblem != null)
            {
                problem = info.Kind.ItemProblem(values);
                if (problem != null) return false;
            }
            if (length % info.Components != 0)
            {
                problem = length + " values are not a multiple of " + info.Components + " components";
                return false;
            }
            count = length / info.Components;
            return true;
        }

        /// <summary>Read a stored record's type, domain and values without a
        /// throwing GetString/GetLong cast.</summary>
        private static bool TryRecord(ArchivableDictionary record, out string type, out string domain,
                                      out object values, out string problem)
        {
            type = null;
            domain = "";
            values = null;
            problem = null;
            if (record == null) { problem = "record is not a dictionary"; return false; }
            object raw;
            if (!record.TryGetValue("type", out raw) || raw == null)
            { problem = "type is missing"; return false; }
            type = raw as string;
            if (string.IsNullOrEmpty(type))
            { problem = "type must be a non-empty string"; return false; }
            if (!Types.ContainsKey(type))
            { problem = "unknown attribute type " + type; return false; }
            if (!record.TryGetValue("domain", out raw) || raw == null)
            { problem = "domain is missing"; return false; }
            domain = raw as string;
            if (string.IsNullOrEmpty(domain))
            { problem = "domain must be a non-empty string"; return false; }
            if (!record.TryGetValue("values", out values) || values == null)
            { problem = "values are missing"; return false; }
            return true;
        }

        // ------------------------------------------------- wire -> dictionary

        /// <summary>One descriptor's fields, read with typed access.</summary>
        private sealed class DescriptorFields
        {
            public string Name;
            public string Type;
            public string Domain;
            public string Section;
            public string Offsets;
            public long Count;
            public int Components;
            public bool HasComponents;
        }

        /// <summary>Attach every descriptor of one group from a decoded frame.
        /// A malformed descriptor or a descriptor naming a section the frame
        /// does not carry is skipped with a note: a truncated frame must not
        /// take the stage down. When ``definition`` is given, only that
        /// definition's descriptors attach; otherwise only the frame's own
        /// (the ones without a definition marker). When ``part`` is given, only
        /// descriptors carrying that part attach; a descriptor without a part
        /// is the single-part/legacy form and never lands on a part.</summary>
        public static void Attach(ArchivableDictionary root, string group,
                                  List<Dictionary<string, object>> descriptors, List<Section> sections,
                                  long? definition = null, Action<string> note = null,
                                  long? part = null)
        {
            if (root == null || descriptors == null || sections == null) return;
            Dictionary<string, Section> byName = SectionMap(sections);

            foreach (Dictionary<string, object> descriptor in descriptors)
            {
                DescriptorFields fields;
                if (!SelectDescriptor(descriptor, group, definition, part, note, out fields))
                    continue;

                object flat;
                if (!DecodeSelected(fields, group, byName, note, out flat)) continue;
                Store(root, fields.Name, fields.Type, fields.Domain, flat);
            }
        }

        /// <summary>Decode a selected descriptor with the one note path both
        /// attachments use.</summary>
        private static bool DecodeSelected(DescriptorFields fields, string group,
                                           Dictionary<string, Section> byName,
                                           Action<string> note, out object flat)
        {
            flat = null;
            string problem = DomainProblem(group, fields.Domain);
            if (problem != null)
            {
                AddNote(note, fields.Name, problem);
                return false;
            }
            problem = DecodeValues(fields, byName, out flat);
            if (problem != null)
            {
                AddNote(note, fields.Name, problem);
                return false;
            }
            return true;
        }

        /// <summary>The one descriptor filter for mesh/subd/pts and curves.</summary>
        private static bool SelectDescriptor(Dictionary<string, object> descriptor, string group,
                                             long? definition, long? part, Action<string> note,
                                             out DescriptorFields fields)
        {
            fields = null;
            if (descriptor == null)
            {
                AddNote(note, null, "descriptor is null");
                return false;
            }

            string descriptorGroup, groupProblem;
            if (!TryGroup(descriptor, out descriptorGroup, out groupProblem))
            {
                AddNote(note, NameText(descriptor), groupProblem);
                return false;
            }
            if (descriptorGroup != group) return false;

            long marker;
            string definitionProblem;
            if (!TryDefinition(descriptor, out marker, out definitionProblem))
            {
                AddNote(note, NameText(descriptor), definitionProblem);
                return false;
            }
            if (marker != (definition ?? -1)) return false;

            long descriptorPart;
            bool hasPart;
            string partProblem;
            if (!TryPart(descriptor, out descriptorPart, out hasPart, out partProblem))
            {
                AddNote(note, NameText(descriptor), partProblem);
                return false;
            }
            if (hasPart && !definition.HasValue)
            {
                AddNote(note, NameText(descriptor),
                    "part " + descriptorPart + " has no definition");
                return false;
            }
            if (part.HasValue)
            {
                if (!hasPart)
                {
                    AddNote(note, NameText(descriptor), "has no part; definition "
                        + definition + " part " + part + " was skipped");
                    return false;
                }
                if (descriptorPart != part.Value) return false;
            }
            else if (hasPart)
            {
                AddNote(note, NameText(descriptor),
                    "part " + descriptorPart + " is not valid on single-part definition "
                    + marker);
                return false;
            }

            string problem = ReadDescriptor(descriptor, out fields);
            if (problem != null)
            {
                AddNote(note, fields.Name, problem);
                return false;
            }
            return true;
        }

        private static Dictionary<string, Section> SectionMap(List<Section> sections)
        {
            var byName = new Dictionary<string, Section>();
            foreach (Section section in sections)
                if (section != null && section.Name != null)
                    byName[section.Name] = section;
            return byName;
        }

        private static void AddNote(Action<string> note, string name, string problem)
        {
            if (note == null) return;
            note("attribute " + (string.IsNullOrEmpty(name) ? "(unnamed)" : name) + ": " + problem);
        }

        private static string NameText(Dictionary<string, object> descriptor)
        {
            object raw;
            if (descriptor.TryGetValue("name", out raw))
            {
                string name = raw as string;
                if (!string.IsNullOrEmpty(name)) return name;
            }
            return null;
        }

        private static bool TryGroup(Dictionary<string, object> descriptor,
                                     out string group, out string problem)
        {
            group = null;
            problem = null;
            object raw;
            if (!descriptor.TryGetValue("group", out raw) || raw == null)
            { problem = "group is missing"; return false; }
            group = raw as string;
            if (string.IsNullOrEmpty(group))
            { problem = "group must be a non-empty string"; return false; }
            return true;
        }

        /// <summary>The definition index, or -1 for the frame's own geometry.
        /// A marker that is not a non-negative integer is malformed; treating
        /// -1 as "the frame's own" would attach a definition's data silently.</summary>
        private static bool TryDefinition(Dictionary<string, object> descriptor,
                                          out long definition, out string problem)
        {
            definition = -1;
            problem = null;
            object marker;
            if (!descriptor.TryGetValue("definition", out marker) || marker == null) return true;
            if (!TryInteger(marker, out definition) || definition < 0)
            { problem = "definition must be a non-negative integer"; return false; }
            return true;
        }

        private static bool TryInteger(object raw, out long value)
        {
            if (raw is long) { value = (long)raw; return true; }
            if (raw is int) { value = (int)raw; return true; }
            value = 0;
            return false;
        }

        /// <summary>The part index of a descriptor, and whether it carries one.
        /// Single-part definitions and the frame's own geometry omit it; a
        /// multi-part descriptor must carry a non-negative integer (a boolean
        /// or a fractional number is malformed).</summary>
        private static bool TryPart(Dictionary<string, object> descriptor,
                                    out long part, out bool hasPart, out string problem)
        {
            part = -1;
            hasPart = false;
            problem = null;
            object raw;
            if (!descriptor.TryGetValue("part", out raw)) return true;
            hasPart = true;
            if (raw == null || !TryInteger(raw, out part) || part < 0)
            {
                problem = "part must be a non-negative integer";
                return false;
            }
            return true;
        }

        /// <summary>Read the descriptor fields that follow the group and
        /// definition filters. Count is required: a descriptor without it
        /// cannot prove its values are whole.</summary>
        private static string ReadDescriptor(Dictionary<string, object> descriptor,
                                             out DescriptorFields fields)
        {
            fields = new DescriptorFields();
            object raw;

            string problem = RequiredString(descriptor, "name", out fields.Name);
            if (problem != null) return problem;
            problem = RequiredString(descriptor, "type", out fields.Type);
            if (problem != null) return problem;
            if (!Types.ContainsKey(fields.Type)) return "unknown attribute type " + fields.Type;
            problem = RequiredString(descriptor, "domain", out fields.Domain);
            if (problem != null) return problem;
            problem = RequiredString(descriptor, "section", out fields.Section);
            if (problem != null) return problem;

            if (descriptor.TryGetValue("offsets", out raw) && raw != null)
            {
                fields.Offsets = raw as string;
                if (string.IsNullOrEmpty(fields.Offsets))
                    return "offsets must be a non-empty string";
            }

            if (!descriptor.TryGetValue("count", out raw) || raw == null)
                return "count is missing";
            long count;
            if (!TryInteger(raw, out count) || count < 0)
                return "count must be a non-negative integer";
            fields.Count = count;

            if (descriptor.TryGetValue("components", out raw) && raw != null)
            {
                long components;
                if (!TryInteger(raw, out components) || components <= 0
                    || components > int.MaxValue)
                    return "components must be a positive integer";
                fields.Components = (int)components;
                fields.HasComponents = true;
            }
            return null;
        }

        private static string RequiredString(Dictionary<string, object> descriptor,
                                             string key, out string value)
        {
            value = null;
            object raw;
            if (!descriptor.TryGetValue(key, out raw) || raw == null)
                return key + " is missing";
            value = raw as string;
            if (string.IsNullOrEmpty(value))
                return key + " must be a non-empty string";
            return null;
        }

        /// <summary>The protocol domains a group's attributes can name.</summary>
        private sealed class DomainSet
        {
            public readonly HashSet<string> Domains;
            public readonly string Description;

            public DomainSet(string description, params string[] domains)
            {
                Description = description;
                Domains = new HashSet<string>(domains);
            }
        }

        private static readonly Dictionary<string, DomainSet> Domains =
            new Dictionary<string, DomainSet>
        {
            { "mesh", new DomainSet("mesh", "POINT", "EDGE", "FACE", "CORNER") },
            { "subd", new DomainSet("subd", "POINT", "EDGE", "FACE", "CORNER") },
            { "pts", new DomainSet("point-cloud", "POINT") },
            { "crv", new DomainSet("curve", "CURVE", "EDGE", "POINT", "CURVE_POINT") },
        };

        private static string DomainProblem(string group, string domain)
        {
            DomainSet set;
            if (!Domains.TryGetValue(group, out set))
                return "unsupported attribute group " + group;
            // CURVE_POINT is the curve-local storage name for a per-point
            // list; a peer that sends it means the same slicing as POINT.
            if (set.Domains.Contains(domain)) return null;
            return "\"" + domain + "\" is not a " + set.Description + " attribute domain";
        }

        /// <summary>Check a section's dtype and byte length before any decoder
        /// reads it as an array.</summary>
        private static string SectionProblem(Section section, string expected)
        {
            if (section.Dtype != expected)
                return section.Name + " must be " + expected + ", not "
                    + (section.Dtype ?? "(missing)");
            if (section.Raw == null)
                return section.Name + " has no bytes";
            int itemSize = expected == "u8" ? 1 : 4;
            if (section.Raw.Length % itemSize != 0)
                return section.Name + " length " + section.Raw.Length
                    + " is not a multiple of " + itemSize;
            return null;
        }

        /// <summary>Resolve one descriptor's section and decode it only when
        /// its dtype, count, components and byte length agree. This is the one
        /// inbound validation owner, shared by mesh/subd/pts and curves.</summary>
        private static string DecodeValues(DescriptorFields fields,
                                           Dictionary<string, Section> byName,
                                           out object flat)
        {
            flat = null;
            Section values;
            if (!byName.TryGetValue(fields.Section, out values) || values == null)
                return "section " + fields.Section + " is missing";

            int components = Types[fields.Type].Components;
            if (fields.HasComponents && fields.Components != components)
                return "components must be " + components + ", not " + fields.Components;

            if (fields.Type == "STRING")
            {
                string stringProblem = SectionProblem(values, "u8");
                if (stringProblem != null) return stringProblem;
                if (string.IsNullOrEmpty(fields.Offsets))
                    return "STRING values have no offsets section";
                Section offsets;
                if (!byName.TryGetValue(fields.Offsets, out offsets) || offsets == null)
                    return "offsets section " + fields.Offsets + " is missing";
                stringProblem = SectionProblem(offsets, "u32");
                if (stringProblem != null) return stringProblem;
                string[] strings = SplitStrings(values.Raw, offsets, out stringProblem);
                if (strings == null) return stringProblem;
                if (fields.Count != strings.Length)
                    return CountProblem(fields.Count, strings.Length, "strings");
                flat = strings;
                return null;
            }

            string dtypeProblem = SectionProblem(values, Types[fields.Type].Dtype);
            if (dtypeProblem != null) return dtypeProblem;

            flat = Types[fields.Type].Kind.Decode(values);
            long length = Types[fields.Type].Kind.Length(flat);
            long expected;
            if (!TryMultiply(fields.Count, components, out expected))
                return "count " + fields.Count + " is too large";
            if (length != expected)
                return "count " + fields.Count + " does not match " + length
                    + " values (" + components + " per element)";
            return null;
        }

        private static string CountProblem(long declared, long actual, string unit)
        {
            return "count " + declared + " does not match " + actual + " " + unit;
        }

        private static bool TryMultiply(long count, int components, out long product)
        {
            product = 0;
            if (count < 0 || components <= 0) return false;
            if (count > long.MaxValue / components) return false;
            product = count * components;
            return true;
        }

        // A STRING value that is not valid UTF-8 cannot become a .NET string
        // without inventing U+FFFD; the protocol promises UTF-8 bytes.
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        /// <summary>The wire's concatenated UTF-8 bytes plus per-string lengths.
        /// Returns null with a reason when the lengths are out of range, do not
        /// describe exactly the value bytes, or the bytes are not UTF-8; a
        /// short slice would otherwise pass as a valid string and resurrect
        /// corrupted data.</summary>
        private static string[] SplitStrings(byte[] raw, Section offsets, out string problem)
        {
            problem = "";
            if (offsets == null)
            {
                if (raw.Length == 0) return new string[0];
                problem = "STRING values have no offsets section";
                return null;
            }
            uint[] lengths = offsets.ToU32();
            var strings = new string[lengths.Length];
            long at = 0;
            for (int i = 0; i < lengths.Length; i++)
            {
                uint length = lengths[i];
                if (length > int.MaxValue)
                {
                    problem = "length " + length + " at offset " + at + " exceeds int.MaxValue";
                    return null;
                }
                if (length > raw.Length - at)
                {
                    problem = "length " + length + " at offset " + at
                              + " runs past the " + raw.Length + " value bytes";
                    return null;
                }
                try
                {
                    strings[i] = StrictUtf8.GetString(raw, (int)at, (int)length);
                }
                catch (DecoderFallbackException)
                {
                    problem = "string " + i + " at offset " + at + " is not valid UTF-8";
                    return null;
                }
                at += length;
            }
            if (at != raw.Length)
            {
                problem = "offsets cover " + at + " of " + raw.Length + " value bytes";
                return null;
            }
            return strings;
        }

        // ------------------------------------------------- dictionary -> wire

        /// <summary>Append every attribute under a root as wire sections plus
        /// descriptors. The attribute index is ``index0`` plus the descriptor
        /// count, so several groups can be collected into one frame. ``prefix``
        /// and ``definition`` namespace a definition's attributes (§5.2);
        /// ``part`` marks the part of a multi-part definition, and the caller's
        /// ``prefix`` carries that part's ``idef.&lt;i&gt;.&lt;p&gt;.`` namespace.
        /// A single-part definition passes no part and keeps the original names.
        ///
        /// This is the outbound boundary: a stored record whose type, domain or
        /// value array is malformed is a typed bad_attribute instead of an
        /// InvalidCastException inside Encode. Valid records encode exactly as
        /// before.</summary>
        public static void Collect(ArchivableDictionary root, string group,
                                   List<Section> sections, List<Dictionary<string, object>> descriptors,
                                   string prefix = "", long? definition = null,
                                   long? part = null, int index0 = 0)
        {
            if (root == null) return;
            if (sections == null || descriptors == null)
                throw new FrameException("bad_attribute", "attribute output lists are missing");
            foreach (string name in root.Keys)
            {
                if (string.IsNullOrEmpty(name))
                    throw new FrameException("bad_attribute",
                        "attribute name must be a non-empty string");
                ArchivableDictionary record;
                if (!root.TryGetDictionary(name, out record) || record == null)
                    throw new FrameException("bad_attribute",
                        "attribute " + name + ": record is not a dictionary");
                string type, domain, problem;
                object values;
                if (!TryRecord(record, out type, out domain, out values, out problem))
                    throw new FrameException("bad_attribute", "attribute " + name + ": " + problem);
                problem = DomainProblem(group, domain);
                if (problem != null)
                    throw new FrameException("bad_attribute", "attribute " + name + ": " + problem);
                int count;
                if (!TryValues(type, values, out count, out problem))
                    throw new FrameException("bad_attribute", "attribute " + name + ": " + problem);

                int components = Types[type].Components;
                int index = index0 + descriptors.Count;
                string sectionName = prefix + "attr." + index + ".values";

                var entry = new Dictionary<string, object>();
                entry["group"] = group;
                entry["name"] = name;
                entry["domain"] = domain;
                entry["type"] = type;
                entry["components"] = (long)components;
                entry["count"] = (long)count;
                entry["section"] = sectionName;
                if (definition.HasValue) entry["definition"] = definition.Value;
                if (part.HasValue) entry["part"] = part.Value;

                string offsetsName = prefix + "attr." + index + ".offsets";
                Section valueSection = Types[type].Kind.Encode(sectionName, values);
                sections.Add(valueSection);
                if (type == "STRING")
                {
                    var lengths = new List<uint>();
                    foreach (string item in (string[])values)
                        lengths.Add((uint)Encoding.UTF8.GetByteCount(item));
                    sections.Add(Section.U32(offsetsName, lengths.ToArray()));
                    entry["offsets"] = offsetsName;
                }
                descriptors.Add(entry);
            }
        }

        // ------------------------------------------------------- geometry glue

        private static ArchivableDictionary Root(GeometryBase geometry, bool create)
        {
            ArchivableDictionary root;
            if (geometry.UserDictionary.TryGetDictionary(RootKey, out root) && root != null)
                return root;
            if (!create) return null;
            root = new ArchivableDictionary();
            geometry.UserDictionary.Set(RootKey, root);
            return root;
        }

        public static void Attach(GeometryBase geometry, string group,
                                  List<Dictionary<string, object>> descriptors, List<Section> sections,
                                  long? definition = null, Action<string> note = null,
                                  long? part = null)
        {
            ArchivableDictionary existing = Root(geometry, false);
            if (existing == null && (descriptors == null || descriptors.Count == 0)) return;
            Attach(Root(geometry, true), group, descriptors, sections, definition, note, part);
        }

        /// <summary>Attach a frame's crv-group attributes to each curve.
        ///
        /// A per-curve attribute (domain CURVE) is stored as a one-element
        /// array on that curve's dictionary. A per-point attribute (domain
        /// POINT) is sliced by crv.counts and stored as one array of N values
        /// per curve, so Attribute Get returns a list per curve rather than one
        /// flat list for the whole component. The wire itself stays flat; the
        /// grouping is exactly the same counts section that rebuilds the curves.
        /// </summary>
        public static void AttachCurves(IList<Curve> curves, uint[] counts,
                                        List<Dictionary<string, object>> descriptors,
                                        List<Section> sections,
                                        long? definition = null, Action<string> note = null,
                                        long? part = null)
        {
            if (curves == null || curves.Count == 0) return;
            var roots = new List<ArchivableDictionary>(curves.Count);
            foreach (Curve curve in curves) roots.Add(Root(curve, true));
            AttachCurves(roots, counts, descriptors, sections, definition, note, part);
        }

        /// <summary>The root-level half of <see cref="AttachCurves(IList{Curve}, uint[], List{Dictionary{string, object}}, List{Section}, long?, Action{string}, long?)"/>.
        /// Kept separate so the slicing logic can be tested without constructing
        /// native Rhino geometry.</summary>
        internal static void AttachCurves(IList<ArchivableDictionary> roots, uint[] counts,
                                          List<Dictionary<string, object>> descriptors,
                                          List<Section> sections,
                                          long? definition = null, Action<string> note = null,
                                          long? part = null)
        {
            if (roots == null || roots.Count == 0 || descriptors == null || sections == null)
                return;

            Dictionary<string, Section> byName = SectionMap(sections);

            bool countsMatch = counts != null && counts.Length == roots.Count;
            long totalPoints = 0;
            if (countsMatch)
                foreach (uint count in counts) totalPoints += count;

            foreach (Dictionary<string, object> descriptor in descriptors)
            {
                DescriptorFields fields;
                if (!SelectDescriptor(descriptor, "crv", definition, part, note, out fields))
                    continue;

                object flat;
                if (!DecodeSelected(fields, "crv", byName, note, out flat)) continue;

                int components = Types[fields.Type].Components;
                if (fields.Domain == "CURVE" || fields.Domain == "EDGE")
                {
                    if (fields.Count != roots.Count)
                    {
                        AddNote(note, fields.Name, "count " + fields.Count
                            + " does not match " + roots.Count + " curves");
                        continue;
                    }
                    // Build every slice before storing any, so a malformed
                    // attribute is one skip, never a half-attached curve set.
                    var slices = new object[roots.Count];
                    bool attached = true;
                    for (int i = 0; i < roots.Count; i++)
                    {
                        slices[i] = Types[fields.Type].Kind.Slice(flat, i, 1, components);
                        if (slices[i] == null)
                        {
                            AddNote(note, fields.Name, "cannot slice per-curve values");
                            attached = false;
                            break;
                        }
                    }
                    if (!attached) continue;
                    for (int i = 0; i < roots.Count; i++)
                        Store(roots[i], fields.Name, fields.Type, "CURVE", slices[i]);
                }
                else // POINT or CURVE_POINT
                {
                    if (!countsMatch)
                    {
                        AddNote(note, fields.Name, counts == null
                            ? "curve point counts are missing"
                            : counts.Length + " curve counts for " + roots.Count + " curves");
                        continue;
                    }
                    if (fields.Count != totalPoints)
                    {
                        AddNote(note, fields.Name, "count " + fields.Count
                            + " does not match " + totalPoints + " curve points");
                        continue;
                    }
                    var slices = new object[roots.Count];
                    bool attached = true;
                    int at = 0;
                    for (int i = 0; i < roots.Count; i++)
                    {
                        int n = (int)counts[i];
                        slices[i] = Types[fields.Type].Kind.Slice(flat, at, n, components);
                        if (slices[i] == null)
                        {
                            AddNote(note, fields.Name, "cannot slice curve point values");
                            attached = false;
                            break;
                        }
                        at += n;
                    }
                    if (!attached) continue;
                    for (int i = 0; i < roots.Count; i++)
                        Store(roots[i], fields.Name, fields.Type, "CURVE_POINT", slices[i]);
                }
            }
        }

        private static T[] SliceArray<T>(T[] source, int start, int count, int components)
        {
            if (start < 0 || count < 0 || components <= 0) return null;
            long first = (long)start * components;
            long length = (long)count * components;
            if (first > source.Length || length > source.Length - first) return null;
            var result = new T[(int)length];
            Array.Copy(source, (int)first, result, 0, (int)length);
            return result;
        }

        public static void Collect(GeometryBase geometry, string group,
                                   List<Section> sections, List<Dictionary<string, object>> descriptors,
                                   string prefix = "", long? definition = null,
                                   long? part = null, int index0 = 0)
        {
            Collect(Root(geometry, false), group, sections, descriptors, prefix, definition,
                    part, index0);
        }

        public static bool TryGet(GeometryBase geometry, string name,
                                  out string type, out string domain, out object values)
        {
            return TryGet(Root(geometry, false), name, out type, out domain, out values);
        }

        public static void Set(GeometryBase geometry, string name, string type, string domain, object values)
        {
            Set(Root(geometry, true), name, type, domain, values);
        }

        public static string[] Names(GeometryBase geometry)
        {
            return Names(Root(geometry, false));
        }

        public static bool Has(GeometryBase geometry)
        {
            return Has(Root(geometry, false));
        }
    }
}
