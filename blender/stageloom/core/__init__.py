"""Pure core of the Stageloom add-on. Must not import bpy.

Covers the protocol codec, hashing and request IDs. Everything here is testable
without Blender or Grasshopper.
"""

PROTOCOL_VERSION = 2
