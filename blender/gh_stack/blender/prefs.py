"""Preferences: port, timeouts, data root, log level (plan §11)."""

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty


class GhStackPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__.rsplit(".", 1)[0] if "." in __package__ else __package__

    port: IntProperty(
        name="Server port",
        description="Loopback port the add-on listens on; Grasshopper connects to it",
        default=9130,
        min=1024,
        max=65535,
    )
    timeout: FloatProperty(
        name="Request timeout (s)",
        description="A request with no response in this many seconds goes to error",
        default=30.0,
        min=1.0,
    )
    log_level: StringProperty(
        name="Log level",
        default="info",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "port")
        layout.prop(self, "timeout")
        layout.prop(self, "log_level")


def register():
    bpy.utils.register_class(GhStackPreferences)


def unregister():
    try:
        bpy.utils.unregister_class(GhStackPreferences)
    except (RuntimeError, ValueError):
        pass
