"""Pure core of the Stageloom add-on. Must not import bpy.

Covers the protocol codec, hashing and request IDs. Everything here is testable
without Blender or Grasshopper.
"""

PROTOCOL_VERSION = 2

# Optional `caps` vocabulary (protocol §4.1/§4.3, §8). `blocks/2` means the peer
# reads multi-part definition entries (`definitions[i].parts > 1`) and their
# `idef.<i>.<part>.*` namespace. It is a negotiation flag, never hashed.
CAP_BLOCKS_2 = "blocks/2"
