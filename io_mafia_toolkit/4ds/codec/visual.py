"""Visual frame payloads: geometry, skin, morph, mirror, billboard, lens
flare, projector."""

from ...common.binary import FormatError
from ...common.constants import MAX_HULL_VERTICES, MAX_VERTICES_PER_LOD
from .types import (
    Billboard, BoneGroup, FaceGroup, Geometry, Glow, LensFlare, LOD, Mirror,
    Morph, MorphLOD, MorphRegion, Projector, Skin, SkinLOD, Vertex,
)


# ── Standard geometry ─────────────────────────────────────────────────────────
# u16  instance id (non-zero => reuse frame N's mesh, no further bytes)
# u8   LOD count
# per LOD:
#   f32  switch distance
#   u16  vertex count, or SHARED_UV_VERTICES
#   if the count is SHARED_UV_VERTICES:
#       2f uv per vertex of LOD 0    (8 bytes)
#   else:
#       per vertex: 3f position, 3f normal, 2f uv          (32 bytes)
#   u8   face-group count
#   per face group: u16 face count, face count * 3 u16 indices, u16 material id

#: The vertex count that is not a count. A LOD marked with it keeps LOD 0's
#: positions and normals and carries only its own UVs, two floats a vertex, for
#: as many vertices as LOD 0 has. It is why a real LOD cannot have 65535
#: vertices: the count would be indistinguishable from this.
SHARED_UV_VERTICES = 0xFFFF


def read_geometry(reader):
    geom = Geometry()
    geom.instance_id = reader.u16()
    if geom.instance_id:
        return geom

    for _ in range(reader.u8()):
        lod = LOD()
        lod.distance = reader.f32()

        count = reader.u16()
        if count == SHARED_UV_VERTICES:
            # As many UVs as LOD 0 has vertices. A first LOD marked this way
            # has nothing to borrow from, which is a corrupt file rather than a
            # shape the format allows.
            if not geom.lods:
                raise FormatError(
                    "the first LOD says it reuses an earlier LOD's vertices, "
                    "and there is no earlier LOD."
                )
            shared = len(geom.lods[0].vertices)
            raw = reader.f32_array(shared * 2)
            lod.shared_uv = [(raw[i], raw[i + 1])
                             for i in range(0, len(raw), 2)]
        else:
            raw = reader.f32_array(count * 8)
            for i in range(count):
                base = i * 8
                lod.vertices.append(Vertex(
                    position=(raw[base], raw[base + 1], raw[base + 2]),
                    normal=(raw[base + 3], raw[base + 4], raw[base + 5]),
                    uv=(raw[base + 6], raw[base + 7]),
                ))

        for _ in range(reader.u8()):
            group = FaceGroup()
            face_count = reader.u16()
            indices = reader.u16_array(face_count * 3)
            group.faces = [
                (indices[i], indices[i + 1], indices[i + 2])
                for i in range(0, len(indices), 3)
            ]
            group.material_id = reader.u16()
            lod.face_groups.append(group)

        geom.lods.append(lod)
    return geom


def write_geometry(writer, geom, context=""):
    writer.u16(geom.instance_id)
    if geom.instance_id:
        return

    writer.u8(len(geom.lods))
    for level, lod in enumerate(geom.lods):
        if lod.shared_uv is not None:
            expected = len(geom.lods[0].vertices) if geom.lods else 0
            if level == 0 or len(lod.shared_uv) != expected:
                raise FormatError(
                    f"{context} LOD {level} reuses LOD 0's vertices but "
                    f"carries {len(lod.shared_uv)} UV(s) for its "
                    f"{expected} vertex(es)."
                )
            writer.f32(lod.distance)
            writer.u16(SHARED_UV_VERTICES)
            for u, v in lod.shared_uv:
                writer.vec2(u, v)
        else:
            if len(lod.vertices) > MAX_VERTICES_PER_LOD:
                raise FormatError(
                    f"{context} LOD {level} has {len(lod.vertices)} vertices "
                    f"after UV/normal splitting, over the 4DS limit of "
                    f"{MAX_VERTICES_PER_LOD}. Reduce the polygon count or "
                    f"merge UV islands."
                )
            writer.f32(lod.distance)
            writer.u16(len(lod.vertices))
            for vert in lod.vertices:
                writer.vec3(*vert.position)
                writer.vec3(*vert.normal)
                writer.vec2(*vert.uv)

        writer.u8(len(lod.face_groups))
        for group in lod.face_groups:
            writer.u16(len(group.faces))
            for a, b, c in group.faces:
                writer.u16_triple(a, b, c)
            writer.u16(group.material_id)


# ── Skin ──────────────────────────────────────────────────────────────────────
# Written once per geometry LOD, immediately after the geometry block.
#   u8   bone group count
#   u32  root unweighted vertex count
#   3f   root bbox min, 3f root bbox max
#   per bone group:
#     16f inverse bind matrix
#     u32 unweighted count, u32 weighted count, u32 parent group (1-based, 0=root)
#     3f  bbox min, 3f bbox max
#     weighted count * f32 blend weights
#
# The vertex buffer is pre-sorted so each group's vertices are contiguous:
# group 0 unweighted, group 0 weighted, group 1 unweighted, ... then the root
# unweighted run at the end. There are no explicit vertex indices.

def read_skin(reader, lod_count):
    skin = Skin()
    for _ in range(lod_count):
        lod = SkinLOD()
        group_count = reader.u8()
        lod.root_unweighted = reader.u32()
        lod.bbox_min = reader.vec3()
        lod.bbox_max = reader.vec3()
        for _ in range(group_count):
            group = BoneGroup()
            group.inverse_bind = reader.mat4()
            group.unweighted_count = reader.u32()
            group.weighted_count = reader.u32()
            group.parent_group = reader.u32()
            group.bbox_min = reader.vec3()
            group.bbox_max = reader.vec3()
            group.weights = list(reader.f32_array(group.weighted_count))
            lod.groups.append(group)
        skin.lods.append(lod)
    return skin


def write_skin(writer, skin):
    for lod in skin.lods:
        writer.u8(len(lod.groups))
        writer.u32(lod.root_unweighted)
        writer.vec3(*lod.bbox_min)
        writer.vec3(*lod.bbox_max)
        for group in lod.groups:
            for value in group.inverse_bind:
                writer.f32(value)
            writer.u32(group.unweighted_count)
            writer.u32(group.weighted_count)
            writer.u32(group.parent_group)
            writer.vec3(*group.bbox_min)
            writer.vec3(*group.bbox_max)
            for weight in group.weights:
                writer.f32(weight)


# ── Morph ─────────────────────────────────────────────────────────────────────
# u8  target count  (0 => no morph data, block ends here)
# u8  region count
# u8  LOD count
# per LOD, per region:
#   u16 vertex count
#   if vertex count == 0: nothing further for this region  <- occurs in 28
#                                                             shipping regions
#   per vertex, per target: 3f position, 3f normal
#   u8  flag (always 1 in shipping data)
#   vertex count * u16 vertex index
# 3f bbox min, 3f bbox max, 3f center, f32 radius
#
# Positions are absolute, not deltas, and each target carries its own normal
# for every vertex — the engine swaps in that target's shading as the morph
# blends. Collapsing them to one normal per vertex is a silent quality loss.

def read_morph(reader):
    morph = Morph()
    morph.target_count = reader.u8()
    if morph.target_count == 0:
        return morph

    region_count = reader.u8()
    lod_count = reader.u8()

    for _ in range(lod_count):
        lod = MorphLOD()
        for _ in range(region_count):
            region = MorphRegion()
            vertex_count = reader.u16()
            if vertex_count == 0:
                lod.regions.append(region)
                continue

            region.positions = [[] for _ in range(morph.target_count)]
            region.normals = [[] for _ in range(morph.target_count)]
            raw = reader.f32_array(vertex_count * morph.target_count * 6)
            cursor = 0
            for _v in range(vertex_count):
                for target in range(morph.target_count):
                    region.positions[target].append(
                        (raw[cursor], raw[cursor + 1], raw[cursor + 2]))
                    region.normals[target].append(
                        (raw[cursor + 3], raw[cursor + 4], raw[cursor + 5]))
                    cursor += 6

            reader.u8()  # flag
            region.indices = list(reader.u16_array(vertex_count))
            lod.regions.append(region)
        morph.lods.append(lod)

    morph.bbox_min = reader.vec3()
    morph.bbox_max = reader.vec3()
    morph.center = reader.vec3()
    morph.radius = reader.f32()
    return morph


def write_morph(writer, morph):
    if morph.target_count == 0 or not morph.lods:
        writer.u8(0)
        return

    writer.u8(morph.target_count)
    writer.u8(len(morph.lods[0].regions))
    writer.u8(len(morph.lods))

    for lod in morph.lods:
        for region in lod.regions:
            count = len(region.indices)
            writer.u16(count)
            if count == 0:
                continue
            for i in range(count):
                for target in range(morph.target_count):
                    writer.vec3(*region.positions[target][i])
                    writer.vec3(*region.normals[target][i])
            writer.u8(1)
            for index in region.indices:
                writer.u16(index)

    writer.vec3(*morph.bbox_min)
    writer.vec3(*morph.bbox_max)
    writer.vec3(*morph.center)
    writer.f32(morph.radius)


# ── Mirror ────────────────────────────────────────────────────────────────────
# 3f bbox min, 3f bbox max, 3f center, f32 radius,
# 16f view matrix (row-major), 3f tint, f32 draw distance,
# u32 vertex count, u32 face count, vertices (3f), faces (3 u16)

def read_mirror(reader):
    mirror = Mirror()
    mirror.bbox_min = reader.vec3()
    mirror.bbox_max = reader.vec3()
    mirror.center = reader.vec3()
    mirror.radius = reader.f32()
    mirror.view_matrix = reader.mat4()
    mirror.tint = reader.vec3()
    mirror.draw_distance = reader.f32()

    vertex_count = reader.u32()
    face_count = reader.u32()
    coords = reader.f32_array(vertex_count * 3)
    mirror.vertices = [
        (coords[i], coords[i + 1], coords[i + 2])
        for i in range(0, len(coords), 3)
    ]
    indices = reader.u16_array(face_count * 3)
    mirror.faces = [
        (indices[i], indices[i + 1], indices[i + 2])
        for i in range(0, len(indices), 3)
    ]
    return mirror


def write_mirror(writer, mirror, context=""):
    if len(mirror.vertices) > MAX_HULL_VERTICES:
        raise FormatError(
            f"{context} mirror has {len(mirror.vertices)} vertices, over the "
            f"4DS limit of {MAX_HULL_VERTICES}."
        )
    writer.vec3(*mirror.bbox_min)
    writer.vec3(*mirror.bbox_max)
    writer.vec3(*mirror.center)
    writer.f32(mirror.radius)
    for value in mirror.view_matrix:
        writer.f32(value)
    writer.vec3(*mirror.tint)
    writer.f32(mirror.draw_distance)
    writer.u32(len(mirror.vertices))
    writer.u32(len(mirror.faces))
    for vertex in mirror.vertices:
        writer.vec3(*vertex)
    for a, b, c in mirror.faces:
        writer.u16_triple(a, b, c)


# ── Billboard ─────────────────────────────────────────────────────────────────
def read_billboard(reader):
    board = Billboard()
    board.axis = reader.u32()
    board.axis_locked = reader.bool8()
    return board


def write_billboard(writer, board):
    writer.u32(board.axis)
    writer.bool8(board.axis_locked)


# ── Projector ─────────────────────────────────────────────────────────────────
# u8  orthogonal
# u8  mode
# u16 material id, 1-based into the file's material table
#
# Four bytes and no geometry. Everything else about a projector - how far it
# reaches, how wide it spreads, which surfaces it paints - comes from the
# frame's own transform and from whatever it happens to overlap in the scene.

def read_projector(reader):
    projector = Projector()
    projector.orthogonal = reader.bool8()
    projector.mode = reader.u8()
    projector.material_id = reader.u16()
    return projector


def write_projector(writer, projector, context=""):
    # The id is 1-based and the game does not check it against zero: it
    # subtracts one and follows the result, so a 0 sends it to the word in
    # front of the material table and a negative further back still. Refuse it
    # here rather than write a file that reads a pointer out of the loader's
    # own state.
    if projector.material_id < 1:
        raise FormatError(
            f"{context} projector names material {projector.material_id}. A "
            f"projector's material id is 1-based and must be at least 1."
        )
    writer.bool8(projector.orthogonal)
    writer.u8(projector.mode & 0xFF)
    writer.u16(projector.material_id)


# ── Lens flare ────────────────────────────────────────────────────────────────
# u8 glow count, then per glow: f32 position along the flare, u16 material id.
# A lens flare frame has no geometry block at all.

def read_lens_flare(reader):
    flare = LensFlare()
    for _ in range(reader.u8()):
        flare.glows.append(Glow(position=reader.f32(), material_id=reader.u16()))
    return flare


def write_lens_flare(writer, flare):
    writer.u8(len(flare.glows))
    for glow in flare.glows:
        writer.f32(glow.position)
        writer.u16(glow.material_id)
