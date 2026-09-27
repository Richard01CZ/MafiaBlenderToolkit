"""Non-visual frame payloads: sectors, portals, dummies, targets, occluders, joints."""

from ...common.binary import FormatError
from ...common.constants import MAX_HULL_VERTICES, MAX_OCCLUDER_FACES
from .types import (Dummy, Joint, Light, Occluder, Portal, Sector,
                    Target)


# ── Sector ────────────────────────────────────────────────────────────────────
# i32 flags1, i32 flags2
# u32 vertex count, u32 face count
# vertices (3f), faces (3 u16)
# 3f bbox min, 3f bbox max
# u8 portal count, then each portal:
#   u8  vertex count
#   u32 flags
#   f32 near range, f32 far range
#   3f  plane normal, f32 plane offset
#   vertex count * 3f
#
# Sector and portal vertices are stored in the same space as the frame's own
# transform, so a sector whose frame transform is not identity would be offset
# twice. Every shipping sector frame is at identity; the exporter enforces the
# same invariant rather than silently baking a double transform.

def read_sector(reader):
    sector = Sector()
    sector.flags1 = reader.i32()
    sector.flags2 = reader.i32()

    vertex_count = reader.u32()
    face_count = reader.u32()
    coords = reader.f32_array(vertex_count * 3)
    sector.vertices = [
        (coords[i], coords[i + 1], coords[i + 2])
        for i in range(0, len(coords), 3)
    ]
    indices = reader.u16_array(face_count * 3)
    sector.faces = [
        (indices[i], indices[i + 1], indices[i + 2])
        for i in range(0, len(indices), 3)
    ]

    sector.bbox_min = reader.vec3()
    sector.bbox_max = reader.vec3()

    for _ in range(reader.u8()):
        portal = Portal()
        portal_vertex_count = reader.u8()
        portal.flags = reader.u32()
        portal.near_range = reader.f32()
        portal.far_range = reader.f32()
        portal.plane_normal = reader.vec3()
        portal.plane_offset = reader.f32()
        portal_coords = reader.f32_array(portal_vertex_count * 3)
        portal.vertices = [
            (portal_coords[i], portal_coords[i + 1], portal_coords[i + 2])
            for i in range(0, len(portal_coords), 3)
        ]
        sector.portals.append(portal)

    return sector


def write_sector(writer, sector, context=""):
    if len(sector.vertices) > MAX_HULL_VERTICES:
        raise FormatError(
            f"{context} sector has {len(sector.vertices)} vertices, over the "
            f"4DS limit of {MAX_HULL_VERTICES}. Split it into several sectors."
        )

    writer.i32(sector.flags1)
    writer.i32(sector.flags2)
    writer.u32(len(sector.vertices))
    writer.u32(len(sector.faces))
    for vertex in sector.vertices:
        writer.vec3(*vertex)
    for a, b, c in sector.faces:
        writer.u16_triple(a, b, c)
    writer.vec3(*sector.bbox_min)
    writer.vec3(*sector.bbox_max)

    writer.u8(len(sector.portals))
    for portal in sector.portals:
        writer.u8(len(portal.vertices))
        writer.u32(portal.flags)
        writer.f32(portal.near_range)
        writer.f32(portal.far_range)
        writer.vec3(*portal.plane_normal)
        writer.f32(portal.plane_offset)
        for vertex in portal.vertices:
            writer.vec3(*vertex)


# ── Dummy ─────────────────────────────────────────────────────────────────────
def read_dummy(reader):
    return Dummy(bbox_min=reader.vec3(), bbox_max=reader.vec3())


def write_dummy(writer, dummy):
    writer.vec3(*dummy.bbox_min)
    writer.vec3(*dummy.bbox_max)


# ── Target ────────────────────────────────────────────────────────────────────
def read_target(reader):
    target = Target()
    target.flags = reader.u16()
    target.links = list(reader.u16_array(reader.u8()))
    return target


def write_target(writer, target):
    writer.u16(target.flags)
    writer.u8(len(target.links))
    for link in target.links:
        writer.u16(link)


# ── Occluder ──────────────────────────────────────────────────────────────────
def read_occluder(reader):
    occluder = Occluder()
    vertex_count = reader.u32()
    face_count = reader.u32()
    coords = reader.f32_array(vertex_count * 3)
    occluder.vertices = [
        (coords[i], coords[i + 1], coords[i + 2])
        for i in range(0, len(coords), 3)
    ]
    indices = reader.u16_array(face_count * 3)
    occluder.faces = [
        (indices[i], indices[i + 1], indices[i + 2])
        for i in range(0, len(indices), 3)
    ]
    return occluder


def write_occluder(writer, occluder, context=""):
    if len(occluder.vertices) > MAX_HULL_VERTICES:
        raise FormatError(
            f"{context} occluder has {len(occluder.vertices)} vertices, over "
            f"the 4DS limit of {MAX_HULL_VERTICES}."
        )
    if len(occluder.faces) > MAX_OCCLUDER_FACES:
        raise FormatError(
            f"{context} occluder has {len(occluder.faces)} faces, over the "
            f"limit of {MAX_OCCLUDER_FACES}."
        )
    writer.u32(len(occluder.vertices))
    writer.u32(len(occluder.faces))
    for vertex in occluder.vertices:
        writer.vec3(*vertex)
    for a, b, c in occluder.faces:
        writer.u16_triple(a, b, c)


# ── Light ─────────────────────────────────────────────────────────────────────
# u32 mode, u32 type, f32 power, 3f color, f32 near range, f32 far range,
# f32 inner cone, f32 outer cone            = 40 bytes, read as one block
#
# The cone angles are full angles in radians. There is no direction here: a spot
# or directional light faces the way its frame faces.

def read_light(reader):
    light = Light()
    light.mode = reader.u32()
    light.light_type = reader.u32()
    light.power = reader.f32()
    light.color = reader.vec3()
    light.range_near = reader.f32()
    light.range_far = reader.f32()
    light.cone_inner = reader.f32()
    light.cone_outer = reader.f32()
    return light


def write_light(writer, light):
    writer.u32(light.mode & 0xFFFFFFFF)
    writer.u32(light.light_type & 0xFFFFFFFF)
    writer.f32(light.power)
    writer.vec3(*light.color)
    writer.f32(light.range_near)
    writer.f32(light.range_far)
    writer.f32(light.cone_inner)
    writer.f32(light.cone_outer)


# ── Joint ─────────────────────────────────────────────────────────────────────
def read_joint(reader):
    return Joint(matrix=reader.mat4(), joint_id=reader.u32())


def write_joint(writer, joint):
    for value in joint.matrix:
        writer.f32(value)
    writer.u32(joint.joint_id)
