"""Preferences: port, timeouts, cache location, log level (plan §11)."""

import os

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty

DEFAULT_DATA_ROOT = "~/.local/share/gh-stack"
DEFAULT_CACHE_MB = 256

# A plain snapshot of `log_level`, refreshed on the main thread (register and
# every change), so any thread can call `log()` without touching bpy — the
# socket thread prints disconnects and decode failures (plan §3, principle 7).
_log_info = True


def _log_level_changed(self, context):
    refresh_log_level()


def refresh_log_level():
    global _log_info
    _log_info = _pref("log_level", "info") != "quiet"


def log(*args, **kwargs):
    """A gated `print` for the `[gh-stack]` diagnostics: `quiet` silences them,
    `info` (the default) prints. Errors still surface in the panel state either
    way; this only controls the system console."""
    if _log_info:
        print(*args, **kwargs)


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
    data_root: StringProperty(
        name="Data root",
        description="Where the frame cache and logs live",
        subtype="DIR_PATH",
        default=DEFAULT_DATA_ROOT,
    )
    cache_size_mb: IntProperty(
        name="Cache size limit (MB)",
        description="Disk budget for cached Grasshopper frames, per stack object and stage",
        default=DEFAULT_CACHE_MB,
        min=1,
    )
    log_level: EnumProperty(
        name="Log level",
        description="How much the add-on prints to the system console",
        items=[
            ("quiet", "Quiet", "Print nothing; stage states stay visible in the panel"),
            ("info", "Info", "Print the add-on's [gh-stack] diagnostics"),
        ],
        default="info",
        update=_log_level_changed,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "port")
        layout.prop(self, "timeout")
        layout.prop(self, "data_root")
        layout.prop(self, "cache_size_mb")
        layout.prop(self, "log_level")


def _pref(name, fallback):
    """One preference value, or the fallback when the add-on is not registered
    (headless tools, tests that import the engine directly)."""
    try:
        prefs = bpy.context.preferences.addons[__package__.rsplit(".", 1)[0]].preferences
        return getattr(prefs, name)
    except (KeyError, AttributeError):
        return fallback


def data_root():
    """The data root as an absolute path; ``~`` is resolved."""
    root = _pref("data_root", DEFAULT_DATA_ROOT) or DEFAULT_DATA_ROOT
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        root = os.path.abspath(bpy.path.abspath(root))
    return root


def cache_size_bytes():
    return int(_pref("cache_size_mb", DEFAULT_CACHE_MB)) * 1024 * 1024


def register():
    bpy.utils.register_class(GhStackPreferences)
    refresh_log_level()


def unregister():
    try:
        bpy.utils.unregister_class(GhStackPreferences)
    except (RuntimeError, ValueError):
        pass
