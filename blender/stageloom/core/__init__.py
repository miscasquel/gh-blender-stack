"""Pure core of the Stageloom add-on. Must not import bpy.

Covers the protocol codec, hashing, request IDs, and the cache index.
Everything here is testable without Blender or Grasshopper.
"""

PROTOCOL_VERSION = 2
