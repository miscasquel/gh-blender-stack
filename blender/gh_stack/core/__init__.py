"""Pure core of the GH Stack add-on. Must not import bpy.

Covers the protocol codec, hashing, request IDs, the per-stage decision rules,
and the cache index. Everything here is testable without Blender or Grasshopper.
"""

PROTOCOL_VERSION = 2
