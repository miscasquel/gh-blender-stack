"""The GH Stack add-on: drive Grasshopper definitions from Blender as modifiers.

Milestone demo v0: one stack object with GH stages, one mesh flow, parameters,
and a minimal panel. See DEMO.md for what is deliberately not here yet.

Only the pure core is imported at module load, so tests and tools can import
``gh_stack.core`` outside Blender. The bpy layer is imported when the add-on is
registered.
"""

# Extension installs read blender_manifest.toml; this bl_info version is what
# the development symlink install (scripts/install-addon) reports, and it must
# stay in sync with the manifest (P8-4).
bl_info = {
    "name": "GH Stack",
    "author": "GH Stack",
    "version": (0, 1, 2),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > GH Stack",
    "description": "Drive Grasshopper definitions from Blender as stackable modifiers",
    "category": "Object",
}

from . import core  # noqa: F401,E402  (pure Python; safe outside Blender)

# Registration order matters: the server and engine hooks first, then records,
# then the UI.
MODULE_ORDER = (
    "props",
    "stack",
    "helpers",
    "nodes",
    "server",
    "engine",
    "ops",
    "ui",
    "prefs",
)


def _modules():
    from importlib import import_module

    return [import_module("." + name, __name__ + ".blender") for name in MODULE_ORDER]


def register():
    for module in _modules():
        module.register()


def unregister():
    for module in reversed(_modules()):
        module.unregister()
