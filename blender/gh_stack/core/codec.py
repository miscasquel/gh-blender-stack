"""Protocol v2 frame codec: encode and decode the binary WebSocket frames.

One frame is::

    [u32 little-endian: header length][header: UTF-8 JSON][sections: raw bytes]

The header lists each section's name, dtype, shape, and byte length, in the order
the bytes appear after the header. This module is pure Python: no numpy, no bpy,
no network. Numpy arrays are whatever the caller passes in, and are converted with
:mod:`array` semantics so the layer A tests run on any Python 3.11+.

Everything here is deterministic: the same section bytes always produce the same
header, which is what the golden fixtures in ``protocol/fixtures`` rely on.
"""

import json
import struct

PROTOCOL_VERSION = 2

# Header size cap (protocol §2). A frame with a longer header is not decodable.
MAX_HEADER_BYTES = 1024 * 1024
# Message size cap (protocol §2).
MAX_MESSAGE_BYTES = 512 * 1024 * 1024

_HEADER_LEN = struct.Struct("<I")


class CodecError(Exception):
    """Raised for any frame that does not follow the protocol."""


class HeaderError(CodecError):
    """Header could not be decoded, or disagrees with the payload."""


class SectionError(CodecError):
    """A section's declared size or dtype is inconsistent."""


def canonical_json(obj):
    """Encode JSON the way the protocol demands: sorted keys, tight separators.

    ``separators=(",", ":")`` and UTF-8. Fixtures compare bytes, so this has to be
    byte-identical on every platform.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def section_bytes(name, dtype, array_like):
    """Return the raw little-endian bytes for one section.

    ``array_like`` may be a numpy array, a list of numbers, or anything supporting
    the buffer protocol. Shapes are taken from the caller (the header), not
    recomputed, so a section can be encoded from flat data.
    """
    if dtype == "f32":
        return struct.pack("<%df" % (len(array_like),), *array_like)
    if dtype == "u32":
        return struct.pack("<%dI" % (len(array_like),), *array_like)
    if dtype == "u8":
        return struct.pack("%dB" % (len(array_like),), *array_like)
    raise SectionError("unknown dtype %r for section %r" % (dtype, name))


def unpack_section(name, dtype, raw):
    """Decode one section's bytes into a tuple of numbers."""
    if dtype == "f32":
        return struct.unpack("<%df" % (len(raw) // 4), raw)
    if dtype == "u32":
        return struct.unpack("<%dI" % (len(raw) // 4), raw)
    if dtype == "u8":
        return struct.unpack("%dB" % len(raw), raw)
    raise SectionError("unknown dtype %r for section %r" % (dtype, name))


def check_version(header, expect=PROTOCOL_VERSION):
    """Validate the protocol version. Returns the version or raises CodecError."""
    v = header.get("v")
    if v != expect:
        raise CodecError("proto_mismatch: header v=%r, expected %r" % (v, expect))
    return v


def encode_frame(header, sections=None):
    """Encode one frame.

    ``sections`` is a list of ``(name, dtype, array_like)`` tuples. The header must
    have a ``type``; it may have ``v`` (defaults to the current protocol version).
    The ``sections`` array is generated here so the caller cannot get it out of
    sync with the actual bytes.

    Returns the frame as bytes.
    """
    header = dict(header)
    header.setdefault("v", PROTOCOL_VERSION)
    type_ = header.get("type")
    if not type_:
        raise HeaderError("header needs a type")

    body = b""
    section_list = []
    for name, dtype, array_like in (sections or []):
        raw = section_bytes(name, dtype, array_like)
        section_list.append(
            {"name": name, "dtype": dtype, "shape": [len(array_like)], "bytes": len(raw)}
        )
        body += raw

    if section_list:
        header["sections"] = section_list
    else:
        header.pop("sections", None)

    head = canonical_json(header).encode("utf-8")
    if len(head) > MAX_HEADER_BYTES:
        raise HeaderError("header too large: %d bytes" % len(head))
    frame = _HEADER_LEN.pack(len(head)) + head + body
    if len(frame) > MAX_MESSAGE_BYTES:
        raise CodecError("too_large: frame is %d bytes" % len(frame))
    return frame


def decode_frame(frame):
    """Decode one frame. Returns ``(header, sections)``.

    ``sections`` maps each section name to its decoded tuple of numbers. Raises
    :class:`HeaderError` or :class:`CodecError` (``proto_mismatch``) on anything
    inconsistent, per protocol §4 and §5.
    """
    if not isinstance(frame, (bytes, bytearray)):
        raise HeaderError("frame must be bytes, not %s" % type(frame).__name__)
    if len(frame) < 4:
        raise HeaderError("frame too short for the header length prefix")
    (head_len,) = _HEADER_LEN.unpack_from(bytes(frame), 0)
    if head_len > MAX_HEADER_BYTES:
        raise HeaderError("bad_frame: header length %d over the cap" % head_len)
    if len(frame) < 4 + head_len:
        raise HeaderError("bad_frame: header is truncated")
    head_bytes = bytes(frame[4 : 4 + head_len])
    body = bytes(frame[4 + head_len :])

    try:
        header = json.loads(head_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise HeaderError("bad_frame: header is not valid JSON: %s" % ex)
    if not isinstance(header, dict):
        raise HeaderError("bad_frame: header is not a JSON object")
    check_version(header)

    sections = {}
    total = 0
    for entry in header.get("sections", []):
        name = entry["name"]
        dtype = entry["dtype"]
        nbytes = entry["bytes"]
        if total + nbytes > len(body):
            raise HeaderError(
                "bad_frame: section %r runs past the end of the payload" % name
            )
        raw = body[total : total + nbytes]
        item = 1 if dtype == "u8" else 4
        if nbytes % item != 0:
            raise SectionError("bad_frame: section %r length %d is not a multiple of %d" % (name, nbytes, item))
        sections[name] = unpack_section(name, dtype, raw)
        total += nbytes

    if total != len(body):
        raise HeaderError(
            "bad_frame: %d trailing bytes after the declared sections" % (len(body) - total)
        )
    return header, sections
