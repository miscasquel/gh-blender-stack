// The Grasshopper ribbon icon for the "GH Stack" category.
//
// A category icon is registered through GH_AssemblyPriority, not through
// GH_AssemblyInfo: the latter only covers the About dialog. The icon is the
// Blender file glyph (FILE_BLEND), the same one the Blender side uses for its
// document, so the ribbon tab matches the add-on's visual language.

using System.Drawing;
using Grasshopper.Kernel;

namespace GhStack
{
    public class GhStackRibbonIcon : GH_AssemblyPriority
    {
        // Held for the component server's lifetime: AddCategoryIcon stores the
        // bitmap, so disposing it here would leave the ribbon tab with a dead
        // GDI+ object.
        private static Bitmap _tabIcon;

        public override GH_LoadingInstruction PriorityLoad()
        {
            Bitmap icon = Icons.Get("file_blend");
            if (icon != null)
            {
                // AddCategoryIcon wants a 16x16 tab icon; the component icons
                // are stored at 24x24 for the canvas.
                _tabIcon = new Bitmap(icon, 16, 16);
                Grasshopper.Instances.ComponentServer.AddCategoryIcon("GH Stack", _tabIcon);
            }
            return GH_LoadingInstruction.Proceed;
        }
    }
}
