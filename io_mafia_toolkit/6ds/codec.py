"""Pure-Python reader and writer for the LS3D ``.6ds`` shadow format.

Like the rest of this package, nothing here imports ``bpy``: it converts
between raw bytes and the plain dataclasses below, so it can be exercised
against the game's own shadows without launching Blender.

A shadow is the coarse mesh the engine casts from, one piece per frame of the
model it belongs to. The file is a short header, then every piece's vertices
end to end, then every piece's triangles end to end, then a table saying how
many of each belong to which piece.

Triangle indices count from the start of the piece that owns them rather than
from the start of the file, so a piece can be drawn on its own.
"""

import struct
from dataclasses import dataclass, field
from typing import List, Tuple

from ..common.constants import STRING_ENCODING

Vec3 = Tuple[float, float, float]
Face = Tuple[int, int, int]

#: What the first four bytes of a shadow say, the null included.
FILE_MAGIC = b"6DS\x00"
#: The only version the game will load.
SHADOW_VERSION = 2
#: Magic (4) + version (2) + timestamp (8) + three counts (6).
HEADER_SIZE = 20
#: The counts are 16-bit, so this is as large as a shadow can be.
MAX_U16 = 0xFFFF
#: A piece's name is measured by a single byte.
MAX_NAME_BYTES = 0xFF


class ShadowError(Exception):
    """The bytes are not a shadow this addon can read."""


@dataclass
class ShadowGroup:
    """One piece of the shadow, cast by one frame of the model.

    Its triangles index its own vertices, counting from zero, which is how the
    file stores them and how the engine draws them.
    """

    name: str = ""
    vertices: List[Vec3] = field(default_factory=list)
    faces: List[Face] = field(default_factory=list)


@dataclass
class Shadow:
    """A whole ``.6ds``: the pieces, and the stamp the game checks."""

    #: The model's write time. The game compares it against the .4ds so a
    #: stale shadow is refused rather than cast against new geometry.
    timestamp: bytes = b"\x00" * 8
    groups: List[ShadowGroup] = field(default_factory=list)

    @property
    def vertex_count(self):
        return sum(len(group.vertices) for group in self.groups)

    @property
    def index_count(self):
        return sum(len(group.faces) for group in self.groups) * 3


def read_shadow(raw, name="<shadow>"):
    """Parse *raw* into a :class:`Shadow`."""
    if len(raw) < HEADER_SIZE:
        raise ShadowError(f"{name}: too short to be a shadow")
    if raw[:4] != FILE_MAGIC:
        raise ShadowError(f"{name}: not a shadow file")
    version = struct.unpack_from("<H", raw, 4)[0]
    if version != SHADOW_VERSION:
        raise ShadowError(
            f"{name}: shadow version {version} is not supported (the game "
            f"reads version {SHADOW_VERSION} and refuses the rest)")

    shadow = Shadow(timestamp=bytes(raw[6:14]))
    vertex_count, index_count, group_count = struct.unpack_from("<3H", raw, 14)

    offset = HEADER_SIZE
    if offset + 12 * vertex_count + 2 * index_count > len(raw):
        raise ShadowError(f"{name}: {vertex_count} vertices and "
                          f"{index_count} indices run off the end")
    vertices = [struct.unpack_from("<3f", raw, offset + 12 * index)
                for index in range(vertex_count)]
    offset += 12 * vertex_count
    indices = struct.unpack_from(f"<{index_count}H", raw, offset)
    offset += 2 * index_count

    # The table at the end says how the two arrays above divide up.
    at_vertex = at_index = 0
    for _ in range(group_count):
        if offset + 9 > len(raw):
            raise ShadowError(f"{name}: the piece table runs off the end")
        group_vertices, group_faces = struct.unpack_from("<2i", raw, offset)
        offset += 8
        length = raw[offset]
        offset += 1
        if group_vertices < 0 or group_faces < 0:
            raise ShadowError(f"{name}: a piece counts {group_vertices} "
                              f"vertices and {group_faces} faces")
        if offset + length > len(raw):
            raise ShadowError(f"{name}: a piece name runs off the end")
        group = ShadowGroup(
            name=raw[offset:offset + length].decode(STRING_ENCODING,
                                                    errors="replace"))
        offset += length
        group.vertices = vertices[at_vertex:at_vertex + group_vertices]
        at_vertex += group_vertices
        for face in range(group_faces):
            start = at_index + 3 * face
            group.faces.append(tuple(indices[start:start + 3]))
        at_index += 3 * group_faces
        shadow.groups.append(group)

    return shadow


def read_shadow_file(path):
    with open(path, "rb") as handle:
        return read_shadow(handle.read(), name=str(path))


def write_shadow(shadow):
    """Serialize *shadow* back to bytes.

    Every count is worked out from the pieces themselves rather than kept, so
    a shadow that has been edited writes the shape it actually has.
    """
    out = bytearray()
    out += FILE_MAGIC
    out += struct.pack("<H", SHADOW_VERSION)
    out += shadow.timestamp[:8].ljust(8, b"\x00")
    out += struct.pack("<3H", shadow.vertex_count, shadow.index_count,
                       len(shadow.groups))

    for group in shadow.groups:
        for vertex in group.vertices:
            out += struct.pack("<3f", *vertex)
    for group in shadow.groups:
        for face in group.faces:
            out += struct.pack("<3H", *face)
    for group in shadow.groups:
        out += struct.pack("<2i", len(group.vertices), len(group.faces))
        encoded = group.name.encode(STRING_ENCODING, errors="replace")
        out += struct.pack("<B", len(encoded))
        out += encoded
    return bytes(out)


def write_shadow_file(shadow, path):
    """Write *shadow* to *path*, leaving any existing file alone on failure."""
    data = write_shadow(shadow)
    temporary = f"{path}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    import os
    os.replace(temporary, path)
    return len(data)


# -- Rules --------------------------------------------------------------------
def validate_shadow(shadow, fail, warn):
    """Check a shadow against what the game's own reader will accept."""
    if not shadow.groups:
        fail("A shadow with no pieces casts nothing.",
             fix="Give the shadow at least one mesh to cast from.")

    if shadow.vertex_count > MAX_U16:
        fail(f"A shadow holds up to {MAX_U16} vertices; this one has "
             f"{shadow.vertex_count}.",
             fix="Simplify the shadow meshes - they are meant to be far "
                 "coarser than the model they stand in for.")
    if shadow.index_count > MAX_U16:
        fail(f"A shadow holds up to {MAX_U16} triangle indices; this one has "
             f"{shadow.index_count}, which is {shadow.index_count // 3} "
             f"triangle(s).",
             fix="Simplify the shadow meshes.")
    if len(shadow.groups) > MAX_U16:
        fail(f"A shadow holds up to {MAX_U16} pieces; this one has "
             f"{len(shadow.groups)}.")

    seen = set()
    for group in shadow.groups:
        encoded = group.name.encode(STRING_ENCODING, errors="replace")
        if len(encoded) > MAX_NAME_BYTES:
            fail(f"'{group.name}' is {len(encoded)} bytes long; a shadow "
                 f"piece's name is measured by a single byte, so it holds "
                 f"{MAX_NAME_BYTES}.")
        if not group.name:
            warn("A shadow piece has no name, so the game cannot match it to "
                 "a frame of the model.",
                 fix="Name it after the frame it casts for.")
        if group.name in seen:
            warn(f"Two shadow pieces are both called '{group.name}'.",
                 fix="Name each piece after the frame it casts for.")
        seen.add(group.name)

        if not group.faces:
            warn(f"'{group.name}' has no triangles, so it casts nothing.")
        limit = len(group.vertices)
        for face in group.faces:
            if any(corner >= limit for corner in face):
                fail(f"'{group.name}' has a triangle pointing outside its own "
                     f"{limit} vertices. A piece's triangles count from its "
                     f"own first vertex.")
                break
