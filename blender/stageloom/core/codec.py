"""Protocol v2 frame codec: encode and decode the binary WebSocket frames.

One frame is::

    [u32 little-endian: header length][header: UTF-8 JSON][sections: raw bytes]

The header lists each section's name, dtype, shape, and byte length, in the order
the bytes appear after the header. This module is pure Python: no numpy, no bpy,
no network. Numpy arrays are whatever the caller passes in, and are converted with
:mod:`array` semantics so the layer A tests run on any Python 3.11+.

Everything here is deterministic: the same section bytes always produce the same
header, which is what the golden fixtures in ``protocol/fixtures`` rely on.

This module is also the one trust boundary for peer bytes: every header and
section is validated before it is indexed, and every rejection is a
:class:`CodecError` carrying the protocol §4.6 ``.code``. Callers answer with
that code directly; the message is for the log, not for parsing.
"""

import json
import math
import struct

PROTOCOL_VERSION = 2

# Header size cap (protocol §2). A frame with a longer header is not decodable.
MAX_HEADER_BYTES = 1024 * 1024
# Message size cap (protocol §2).
MAX_MESSAGE_BYTES = 512 * 1024 * 1024

_HEADER_LEN = struct.Struct("<I")

# The protocol §4.6 codes this codec can raise. The code travels on the
# exception so no caller has to infer it from message text.
CODEC_CODES = ("proto_mismatch", "bad_frame", "too_large")

# JSON integers on the wire are signed 64-bit. Python's json decoder is
# arbitrary-precision and the C# decoder holds a long, so an out-of-range
# integer would decode on one side and silently round on the other. Every
# valid frame integer (version, counts, shapes, byte lengths) fits.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class CodecError(Exception):
    """Raised for any frame that does not follow the protocol.

    ``code`` is one of :data:`CODEC_CODES`, the protocol §4.6 error code a
    caller can send back verbatim.
    """

    def __init__(self, code, message):
        if code not in CODEC_CODES:
            raise ValueError("unknown codec error code %r" % (code,))
        self.code = code
        super().__init__(message)


class HeaderError(CodecError):
    """Header could not be decoded, or disagrees with the payload."""


class SectionError(CodecError):
    """A section's declared size or dtype is inconsistent."""


def _reject_constant(text):
    """Reject ``NaN``/``Infinity``: they are Python json extensions, not JSON,
    and the C# decoder rejects them too — the two codecs must agree on what is
    valid input."""
    raise ValueError("non-finite JSON literal %r" % (text,))


def _reject_duplicate_keys(pairs):
    """Build one JSON object, rejecting a repeated key.

    ``json.loads`` otherwise keeps the last value, which makes two different
    frames decode to the same header and lets a sender smuggle data past a
    reader that validates a different occurrence.
    """
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key %r" % (key,))
        obj[key] = value
    return obj


def _has_surrogate(text):
    """True for a string containing a lone UTF-16 surrogate code point."""
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def _validate_json_tree(value, where):
    """Reject values a strict UTF-8 JSON peer cannot carry.

    Python's decoder accepts ``"\\ud800"`` escapes and overflows ``1e400`` to
    infinity; strict UTF-8 output and the C# decoder cannot reproduce either.
    Walking the parsed tree keeps both codecs on the same set of valid frames.
    """
    if isinstance(value, str):
        if _has_surrogate(value):
            raise ValueError("lone surrogate in %s" % (where,))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number in %s" % (where,))
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("%s has a non-string key %r" % (where, key))
            if _has_surrogate(key):
                raise ValueError("lone surrogate in %s key" % (where,))
            _validate_json_tree(item, "%s[%r]" % (where, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_tree(item, "%s[%d]" % (where, index))
    elif value is None or isinstance(value, bool):
        return
    elif isinstance(value, int):
        if value < INT64_MIN or value > INT64_MAX:
            raise ValueError("%s has an integer outside the signed 64-bit range" % (where,))
    else:
        raise ValueError("%s contains %s, which JSON cannot carry" % (where, type(value).__name__))


def canonical_json(obj):
    """Encode JSON the way the protocol demands: sorted keys, tight separators.

    ``separators=(",", ":")`` and UTF-8. Fixtures compare bytes, so this has to be
    byte-identical on every platform. Non-finite numbers are rejected: ``NaN``
    and ``Infinity`` are Python json extensions, not JSON, and the C# decoder
    rejects them too.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


_DTYPE_FMT = {
    "f32": ("f", 4),
    "i32": ("i", 4),
    "u32": ("I", 4),
    "u8": ("B", 1),
}

# numpy dtype strings, little-endian, used when the input carries its own buffer.
_NP_DTYPE = {"f32": "<f4", "i32": "<i4", "u32": "<u4", "u8": "u1"}


def element_count(array_like):
    """Element count of a flat or shaped sequence (numpy arrays report ``size``)."""
    size = getattr(array_like, "size", None)
    if size is not None:
        return int(size)
    total = 1
    for dim in _shape_of(array_like):
        total *= dim
    return total


def _shape_of(array_like):
    shape = getattr(array_like, "shape", None)
    if shape is not None:
        return tuple(int(d) for d in shape)
    if isinstance(array_like, (list, tuple)) and array_like and isinstance(array_like[0], (list, tuple)):
        return (len(array_like), len(array_like[0]))
    return (len(array_like),)


def section_bytes(name, dtype, array_like):
    """Return the raw little-endian bytes for one section.

    The input may be a numpy array (any shape), a flat list, or anything with a
    buffer. Shaped numpy input is flattened without importing numpy: the array's
    own ``astype``/``tobytes`` do the work and keep the dtype explicit, which is
    also the fast path. Plain sequences go through :mod:`struct`.

    Any packing failure is a :class:`SectionError`: the encoder's caller supplies
    this value, so an out-of-range integer or a non-numeric sequence is a
    classified refusal, not an exception from inside :mod:`struct`.
    """
    if not isinstance(dtype, str) or dtype not in _DTYPE_FMT:
        raise SectionError("bad_frame", "unknown dtype %r for section %r" % (dtype, name))

    try:
        if hasattr(array_like, "astype") and hasattr(array_like, "tobytes"):
            return array_like.astype(_NP_DTYPE[dtype], copy=False).reshape(-1).tobytes()

        flat = array_like
        if hasattr(flat, "reshape"):
            flat = flat.reshape(-1)
        values = flat.tolist() if hasattr(flat, "tolist") else list(flat)
        char, _ = _DTYPE_FMT[dtype]
        return struct.pack("<%d%s" % (len(values), char), *values)
    except (AttributeError, TypeError, ValueError, OverflowError, struct.error) as exc:
        raise SectionError(
            "bad_frame", "section %r cannot be packed as %s: %s" % (name, dtype, exc)
        )


def unpack_section(name, dtype, raw):
    """Decode one section's bytes into a tuple of numbers."""
    if not isinstance(dtype, str) or dtype not in _DTYPE_FMT:
        raise SectionError("bad_frame", "unknown dtype %r for section %r" % (dtype, name))
    try:
        view = memoryview(raw).cast("B")
    except (TypeError, ValueError) as exc:
        raise SectionError(
            "bad_frame", "section %r is not a byte buffer: %s" % (name, exc)
        )
    char, size = _DTYPE_FMT[dtype]
    if len(view) % size != 0:
        raise SectionError(
            "bad_frame", "section %r length %d is not a multiple of %d" % (name, len(view), size)
        )
    try:
        return struct.unpack_from("<%d%s" % (len(view) // size, char), view, 0)
    except (struct.error, TypeError) as exc:
        raise SectionError(
            "bad_frame", "section %r cannot be decoded as %s: %s" % (name, dtype, exc)
        )


def check_version(header, expect=PROTOCOL_VERSION):
    """Validate the protocol version. Returns the version or raises CodecError.

    The version is an exact JSON integer: ``2.0`` and ``true`` are different
    JSON values and a peer that sends them does not speak v2, even though
    Python's equality would accept ``2.0``.
    """
    v = header.get("v") if isinstance(header, dict) else None
    if isinstance(v, bool) or not isinstance(v, int) or v != expect:
        raise CodecError("proto_mismatch", "header v=%r, expected %r" % (v, expect))
    return v


def _section_tuple(item, index):
    """Unpack and validate one caller-supplied section tuple."""
    if not isinstance(item, (tuple, list)) or len(item) != 3:
        raise SectionError(
            "bad_frame", "section %d must be a (name, dtype, array) triple" % (index,)
        )
    name, dtype, array_like = item
    if not isinstance(name, str) or not name:
        raise SectionError("bad_frame", "section %d name must be a non-empty string" % (index,))
    if _has_surrogate(name):
        raise SectionError("bad_frame", "section %d name contains a lone surrogate" % (index,))
    if not isinstance(dtype, str) or dtype not in _DTYPE_FMT:
        raise SectionError("bad_frame", "section %r has unknown dtype %r" % (name, dtype))
    return name, dtype, array_like


def encode_frame(header, sections=None):
    """Encode one frame.

    ``sections`` is a list of ``(name, dtype, array_like)`` tuples. The header must
    have a non-empty string ``type``; it may have ``v`` (defaults to the current
    protocol version, and must be exactly 2 when supplied). The ``sections``
    array is generated here so the caller cannot get it out of sync with the
    actual bytes.

    A header the decoder would refuse is refused here too: the encoder never
    emits a frame its own decoder would reject. Returns the frame as bytes.
    """
    try:
        header = dict(header)
    except (TypeError, ValueError) as exc:
        raise HeaderError("bad_frame", "header must be a mapping: %s" % (exc,))
    header.setdefault("v", PROTOCOL_VERSION)

    type_ = header.get("type")
    if not isinstance(type_, str) or not type_:
        raise HeaderError("bad_frame", "header type must be a non-empty string")

    try:
        _validate_json_tree(header, "header")
    except (ValueError, RecursionError) as exc:
        raise HeaderError("bad_frame", "header is not JSON: %s" % (exc,))
    check_version(header)

    body_sections = []
    section_list = []
    seen = set()
    section_items = () if sections is None else sections
    try:
        iterator = iter(section_items)
    except TypeError:
        raise SectionError("bad_frame", "sections must be an iterable of triples")
    for index, item in enumerate(iterator):
        name, dtype, array_like = _section_tuple(item, index)
        if name in seen:
            raise SectionError("bad_frame", "duplicate section name %r" % (name,))
        seen.add(name)
        try:
            count = element_count(array_like)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise SectionError(
                "bad_frame", "section %r has no element count: %s" % (name, exc)
            )
        raw = section_bytes(name, dtype, array_like)
        if len(raw) != count * _DTYPE_FMT[dtype][1]:
            raise SectionError(
                "bad_frame",
                "section %r declares %d elements but packed %d bytes"
                % (name, count, len(raw)),
            )
        section_list.append(
            {"name": name, "dtype": dtype, "shape": [count], "bytes": len(raw)}
        )
        body_sections.append(raw)

    if section_list:
        header["sections"] = section_list
    else:
        header.pop("sections", None)

    try:
        head = canonical_json(header).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise HeaderError("bad_frame", "header is not canonical JSON: %s" % (exc,))
    if len(head) > MAX_HEADER_BYTES:
        raise HeaderError(
            "too_large", "header is %d bytes, over the %d cap" % (len(head), MAX_HEADER_BYTES)
        )

    total = _HEADER_LEN.size + len(head) + sum(len(raw) for raw in body_sections)
    if total > MAX_MESSAGE_BYTES:
        raise CodecError(
            "too_large", "frame is %d bytes, over the %d cap" % (total, MAX_MESSAGE_BYTES)
        )
    return b"".join([_HEADER_LEN.pack(len(head)), head] + body_sections)


def _decode_header(view):
    """Split one frame and return ``(header, body_view)``.

    The returned body is a view: section decoding slices it without copying and
    reads values with :func:`struct.unpack_from`.
    """
    size = len(view)
    if size > MAX_MESSAGE_BYTES:
        raise CodecError(
            "too_large", "frame is %d bytes, over the %d cap" % (size, MAX_MESSAGE_BYTES)
        )
    if size < _HEADER_LEN.size:
        raise HeaderError("bad_frame", "frame too short for the header length prefix")
    (head_len,) = _HEADER_LEN.unpack_from(view, 0)
    if head_len > MAX_HEADER_BYTES:
        raise CodecError(
            "too_large", "header length %d over the %d cap" % (head_len, MAX_HEADER_BYTES)
        )
    end = _HEADER_LEN.size + head_len
    if size < end:
        raise HeaderError("bad_frame", "header is truncated")

    try:
        text = view[_HEADER_LEN.size : end].tobytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeaderError("bad_frame", "header is not valid UTF-8: %s" % (exc,))

    try:
        header = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise HeaderError("bad_frame", "header is not valid JSON: %s" % (exc,))
    if not isinstance(header, dict):
        raise HeaderError("bad_frame", "header is not a JSON object")
    try:
        _validate_json_tree(header, "header")
    except (ValueError, RecursionError) as exc:
        raise HeaderError("bad_frame", "header is not valid JSON: %s" % (exc,))

    check_version(header)
    type_ = header.get("type")
    if not isinstance(type_, str) or not type_:
        raise HeaderError("bad_frame", "header type must be a non-empty string")
    return header, view[end:]


def _decode_sections(header, body):
    """Validate the section table and decode it against ``body``."""
    if "sections" in header:
        table = header["sections"]
        if not isinstance(table, list):
            raise HeaderError("bad_frame", "header sections must be a list")
    else:
        table = ()

    sections = {}
    seen = set()
    offset = 0
    body_size = len(body)
    for index, entry in enumerate(table):
        if not isinstance(entry, dict):
            raise SectionError("bad_frame", "section %d is not a JSON object" % (index,))
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise SectionError("bad_frame", "section %d name must be a non-empty string" % (index,))
        dtype = entry.get("dtype")
        if not isinstance(dtype, str) or dtype not in _DTYPE_FMT:
            raise SectionError("bad_frame", "section %r has unknown dtype %r" % (name, dtype))
        nbytes = entry.get("bytes")
        if isinstance(nbytes, bool) or not isinstance(nbytes, int) or nbytes < 0:
            raise SectionError("bad_frame", "section %r bytes must be a non-negative integer" % (name,))
        if name in seen:
            raise SectionError("bad_frame", "duplicate section name %r" % (name,))
        seen.add(name)

        item_size = _DTYPE_FMT[dtype][1]
        if "shape" in entry:
            shape = entry["shape"]
            if not isinstance(shape, list):
                raise SectionError("bad_frame", "section %r shape must be a list" % (name,))
            count = 1
            for dim in shape:
                if isinstance(dim, bool) or not isinstance(dim, int) or dim < 0:
                    raise SectionError(
                        "bad_frame", "section %r shape must hold non-negative integers" % (name,)
                    )
                count *= dim
            if nbytes % item_size != 0 or count != nbytes // item_size:
                raise SectionError(
                    "bad_frame",
                    "section %r shape/product does not match %d bytes" % (name, nbytes),
                )
        if nbytes % item_size != 0:
            raise SectionError(
                "bad_frame", "section %r length %d is not a multiple of %d" % (name, nbytes, item_size)
            )
        # Compare with the remainder, not offset + nbytes, so a huge declared
        # length cannot overflow in a language with bounded ints.
        if nbytes > body_size - offset:
            raise HeaderError("bad_frame", "section %r runs past the end of the payload" % (name,))
        sections[name] = unpack_section(name, dtype, body[offset : offset + nbytes])
        offset += nbytes

    if offset != body_size:
        raise HeaderError(
            "bad_frame", "%d trailing bytes after the declared sections" % (body_size - offset)
        )
    return sections


def decode_frame(frame):
    """Decode one frame. Returns ``(header, sections)``.

    ``sections`` maps each section name to its decoded tuple of numbers. Raises
    :class:`HeaderError` or :class:`SectionError` (both carry ``.code``, either
    ``bad_frame`` or ``too_large``) or :class:`CodecError` (``proto_mismatch``)
    on anything inconsistent, per protocol §4 and §5.
    """
    try:
        view = memoryview(frame).cast("B")
    except (TypeError, ValueError) as exc:
        raise HeaderError(
            "bad_frame", "frame must be bytes-like, not %s: %s"
            % (type(frame).__name__, exc)
        )

    header, body = _decode_header(view)
    return header, _decode_sections(header, body)
