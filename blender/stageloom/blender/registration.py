"""Class registration helpers shared by the add-on modules.

Disabling an add-on can race a partially registered state (for example when an
extension replaces a development install); an unregister that raises leaves the
rest of the module registered, so teardown uses the tolerant helpers below.
"""

import bpy


def safe_unregister(cls):
    """Unregister a class that may already be gone."""
    try:
        bpy.utils.unregister_class(cls)
    except (RuntimeError, ValueError):
        pass


def register_classes(classes):
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister_classes(classes):
    for cls in classes:
        safe_unregister(cls)
