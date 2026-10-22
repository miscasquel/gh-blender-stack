# SPDX-FileCopyrightText: 2026 Miquel Oller
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Stageloom add-on: drive Grasshopper definitions from Blender as modifiers.

Milestone demo v0: one stack object with GH stages, one mesh flow, parameters,
and a minimal panel. See DEMO.md for what is deliberately not here yet.

Only the pure core is imported at module load, so tests and tools can import
``stageloom.core`` outside Blender. The bpy layer is imported when the add-on is
registered.

`blender_manifest.toml` is the single source for the add-on metadata (the
extension system reads it); the development install (scripts/install-addon)
generates the legacy `bl_info` from it, so there is no second copy to drift.
"""

from . import core  # noqa: F401,E402  (pure Python; safe outside Blender)

# Registration order matters, and `prefs` goes first: `engine.register()` starts
# the server, and the server reads the port preference. With prefs registered last
# the engine could not see it, so a saved port was silently ignored — the socket
# listened on the default while the panel, reading the preference, reported the
# saved one. Unregister walks this in reverse, so prefs still tears down last.
MODULE_ORDER = (
    "prefs",
    "props",
    "stack",
    "helpers",
    "nodes",
    "server",
    "engine",
    "ops",
    "ui",
)


def _modules():
    from importlib import import_module

    return [import_module("." + name, __name__ + ".blender") for name in MODULE_ORDER]


def register():
    for module in _modules():
        hook = getattr(module, "register", None)
        if hook is not None:
            hook()


def unregister():
    for module in reversed(_modules()):
        hook = getattr(module, "unregister", None)
        if hook is not None:
            hook()
