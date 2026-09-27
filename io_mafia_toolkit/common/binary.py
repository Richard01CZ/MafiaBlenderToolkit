"""Bounds-checked binary readers and writers for the 4DS container.

Every primitive read is range-checked and reports the byte offset it failed at.
That matters more than it sounds: the 4DS format has no per-frame length field
and no resynchronization marker, so a single mis-sized read silently reinterprets
the rest of the file as garbage. Failing loudly at the first short read is the
only way to tell "this file uses a feature we don't parse" apart from "this file
is corrupt".
"""

import struct

from .constants import MAX_STRING_BYTES, STRING_ENCODING


class FormatError(Exception):
    """Raised when the byte stream does not match the expected 4DS layout."""


class TruncatedError(FormatError):
    """Raised when a read runs past the end of the buffer."""


class BinaryReader:
    """Sequential little-endian reader over an in-memory buffer."""

    __slots__ = ("buf", "pos", "name")

    def __init__(self, buf, name="<stream>"):
        self.buf = buf
        self.pos = 0
        self.name = name

    # ── plumbing ──────────────────────────────────────────────────────────────
    @property
    def remaining(self):
        return len(self.buf) - self.pos

    def _take(self, count):
        end = self.pos + count
        if end > len(self.buf):
            raise TruncatedError(
                f"{self.name}: needed {count} byte(s) at offset {self.pos} "
                f"but only {self.remaining} remain (file is {len(self.buf)} bytes)"
            )
        chunk = self.buf[self.pos:end]
        self.pos = end
        return chunk

    def _unpack(self, fmt, size):
        if self.pos + size > len(self.buf):
            raise TruncatedError(
                f"{self.name}: needed {size} byte(s) at offset {self.pos} "
                f"but only {self.remaining} remain (file is {len(self.buf)} bytes)"
            )
        value = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += size
        return value

    def skip(self, count):
        self._take(count)

    # ── scalars ───────────────────────────────────────────────────────────────
    def u8(self):
        return self._unpack("<B", 1)[0]

    def u16(self):
        return self._unpack("<H", 2)[0]

    def u32(self):
        return self._unpack("<I", 4)[0]

    def i32(self):
        return self._unpack("<i", 4)[0]

    def f32(self):
        return self._unpack("<f", 4)[0]

    def bool8(self):
        return self._unpack("<B", 1)[0] != 0

    # ── aggregates ────────────────────────────────────────────────────────────
    def vec2(self):
        return self._unpack("<2f", 8)

    def vec3(self):
        return self._unpack("<3f", 12)

    def vec4(self):
        return self._unpack("<4f", 16)

    def mat4(self):
        """16 floats in the file's row-major order."""
        return self._unpack("<16f", 64)

    def u16_array(self, count):
        return self._unpack(f"<{count}H", count * 2) if count else ()

    def f32_array(self, count):
        return self._unpack(f"<{count}f", count * 4) if count else ()

    def string(self):
        """Read a byte-length-prefixed string."""
        length = self.u8()
        if length == 0:
            return ""
        return self._take(length).decode(STRING_ENCODING, errors="replace")


class BinaryWriter:
    """Sequential little-endian writer that buffers into memory.

    Nothing reaches the filesystem until :meth:`save` is called, and ``save``
    writes to a sibling temporary file before atomically replacing the target.
    An export that fails validation half-way therefore leaves any existing
    file on disk completely untouched.
    """

    __slots__ = ("chunks", "_len", "on_truncate")

    def __init__(self, on_truncate=None):
        self.chunks = []
        self._len = 0
        #: Optional ``callable(kind, original, kept)`` invoked when a string is
        #: clipped to the format's 255-byte limit, so callers can warn.
        self.on_truncate = on_truncate

    def __len__(self):
        return self._len

    @property
    def pos(self):
        return self._len

    def _emit(self, data):
        self.chunks.append(data)
        self._len += len(data)

    # ── scalars ───────────────────────────────────────────────────────────────
    def u8(self, v):
        self._emit(struct.pack("<B", v & 0xFF))

    def u16(self, v):
        self._emit(struct.pack("<H", v & 0xFFFF))

    def u32(self, v):
        self._emit(struct.pack("<I", v & 0xFFFFFFFF))

    def i32(self, v):
        self._emit(struct.pack("<i", v))

    def f32(self, v):
        self._emit(struct.pack("<f", v))

    def bool8(self, v):
        self._emit(struct.pack("<B", 1 if v else 0))

    # ── aggregates ────────────────────────────────────────────────────────────
    def vec2(self, x, y):
        self._emit(struct.pack("<2f", x, y))

    def vec3(self, x, y, z):
        self._emit(struct.pack("<3f", x, y, z))

    def vec4(self, x, y, z, w):
        self._emit(struct.pack("<4f", x, y, z, w))

    def u16_triple(self, a, b, c):
        self._emit(struct.pack("<3H", a, b, c))

    def raw(self, data):
        self._emit(bytes(data))

    def string(self, text, context=""):
        """Write a byte-length-prefixed string, reporting any truncation."""
        if not text:
            self.u8(0)
            return 0
        encoded = text.encode(STRING_ENCODING, errors="replace")
        if len(encoded) > MAX_STRING_BYTES:
            if self.on_truncate:
                self.on_truncate(context or "string", text, MAX_STRING_BYTES)
            encoded = encoded[:MAX_STRING_BYTES]
        self.u8(len(encoded))
        self._emit(encoded)
        return len(encoded)

    # ── output ────────────────────────────────────────────────────────────────
    def getvalue(self):
        if len(self.chunks) > 1:
            self.chunks = [b"".join(self.chunks)]
        return self.chunks[0] if self.chunks else b""

    def save(self, filepath):
        """Atomically write the buffer to *filepath*.

        The data lands in ``<filepath>.tmp`` first and is then moved into place,
        so a crash or a failed validation never destroys the previous export.
        """
        import os

        data = self.getvalue()
        tmp = filepath + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, filepath)
        return len(data)
