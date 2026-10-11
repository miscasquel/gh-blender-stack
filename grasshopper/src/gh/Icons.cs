// Component icons, loaded from PNG resources embedded in the .gha.
//
// The images are rasterised from the official Blender icon set (see icons/README.md),
// so the Grasshopper components and the Blender panel share one visual language.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;

namespace GhStack
{
    public static class Icons
    {
        private static readonly Dictionary<string, Bitmap> Cache = new Dictionary<string, Bitmap>();

        /// <summary>A 24x24 icon by resource name (e.g. "import"), or null when it
        /// is missing: a missing icon must never break a component.</summary>
        public static Bitmap Get(string name)
        {
            lock (Cache)
            {
                Bitmap cached;
                if (Cache.TryGetValue(name, out cached)) return cached;
                Bitmap loaded = null;
                try
                {
                    Assembly assembly = Assembly.GetExecutingAssembly();
                    string resource = null;
                    foreach (string candidate in assembly.GetManifestResourceNames())
                    {
                        if (candidate.EndsWith("." + name + ".png", StringComparison.OrdinalIgnoreCase))
                        {
                            resource = candidate;
                            break;
                        }
                    }
                    if (resource != null)
                    {
                        using (Stream stream = assembly.GetManifestResourceStream(resource))
                        {
                            if (stream != null) loaded = new Bitmap(stream);
                        }
                    }
                }
                catch (Exception)
                {
                    loaded = null;
                }
                Cache[name] = loaded;
                return loaded;
            }
        }
    }
}
