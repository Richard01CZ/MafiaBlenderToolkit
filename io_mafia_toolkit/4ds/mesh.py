"""Turning a Blender mesh into a 4DS LOD, with optional skin data.

The format stores exactly one normal and one UV per vertex, while Blender
stores them per face corner. Any vertex whose corners disagree therefore has to
be split into several file vertices. :func:`build_lod` does that splitting,
returns the mapping from Blender vertex to file vertices (the morph exporter
needs it), and for skinned meshes reorders the buffer into the contiguous
bone-group runs the skin block requires.

Vertices go out in the mesh's own order, each followed by any split of it, so
a mesh that needs no splitting - every imported one - is written vertex for
vertex as Blender holds it. A copy standing in for a vertex a face names twice
is not written at all (see :func:`repeated_vertices`); a vertex that only looks
like one - alike at rest but weighted elsewhere, or moved elsewhere by a morph
target - is written, because the two part company as soon as the model moves.
"""

import math
import struct

from ..common import convert
from .codec.types import BoneGroup, FaceGroup, LOD, SkinLOD, Vertex

#: How far two corner normals may differ and still count as the same vertex.
#: Roughly 0.06 degrees: far below any authored hard edge, and comfortably above
#: the renormalization drift Blender introduces when custom split normals are
#: applied. That drift measures up to 1.4e-4 per component on shipping models,
#: and splitting on it grows the mesh a little more on every round trip.
NORMAL_TOLERANCE = 1e-3
#: Matching tolerance for UVs, well under a texel on any realistic texture.
UV_TOLERANCE = 1e-6

# One case still splits legitimately. Some models make a surface double-sided by
# storing a second copy of each triangle wound the other way, sharing the same
# vertices and the same single stored normal. Blender gives those two faces
# opposite corner normals, so the shared vertex splits in two on export and the
# mesh grows slightly. The result shades the back face correctly rather than
# lighting it as if it faced front, so this is left alone. Imported meshes carry
# the file's normals on every corner, so this only happens to meshes whose
# normals Blender works out itself.


def _close(a, b, tolerance):
    # A value that is not a number matches another that is not one.
    return all(x == y or (x != x and y != y) or abs(x - y) <= tolerance
               for x, y in zip(a, b))


def _same(a, b):
    return all(x == y or (x != x and y != y) for x, y in zip(a, b))


class VertexBuffer:
    """The file vertices a mesh splits into, and where each one came from."""

    def __init__(self):
        self.vertices = []          # list of format Vertex
        self.source = []            # buffer index -> Blender vertex index
        self.mapping = {}           # Blender vertex index -> [buffer indices]


def _alike_in_motion(mesh, one, other):
    """True when two vertices move alike: same weights, same shapes.

    Two vertices sitting on each other at rest can still part company once a
    joint turns or a morph target opens, and a vertex written in place of
    another could not follow it. The weights that move a vertex and the place
    every shape key puts it therefore have to match as well, or the two are
    not the same vertex twice - they are two vertices that happen to meet.
    """
    def weights(index):
        return {group.group: group.weight for group in mesh.vertices[index].groups
                if group.weight != 0.0}

    if weights(one) != weights(other):
        return False
    keys = mesh.shape_keys
    if keys is not None:
        for block in keys.key_blocks:
            if not _same(block.data[one].co, block.data[other].co):
                return False
    return True


def repeated_vertices(mesh):
    """``{copy: vertex it repeats}`` for a face that names a vertex twice.

    Blender cannot hold such a face, so the import gives the repeat a copy of
    the vertex, after every vertex the file has. That is what a copy is: one of
    the mesh's last vertices, used by a single face, where a vertex earlier in
    the same face sits at the same spot with the same normal and UV, and moves
    with it under every joint and every morph target. Walked back from the end,
    the copies stop at the first vertex that is not one. Across every model the
    game ships, this reads one vertex wrong - the last vertex of '4old071' is a
    real one that happens to look like a copy; nothing is drawn differently for
    it, because a face whose corners meet in every value covers no area in any
    pose.
    """
    count = len(mesh.vertices)
    if count == 0:
        return {}
    uses = [0] * count
    corner_of = {}              # vertex -> (polygon, loop index) of its use
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            vertex = mesh.loops[loop_index].vertex_index
            uses[vertex] += 1
            corner_of.setdefault(vertex, (polygon, loop_index))
    uv_layer = mesh.uv_layers.active

    def corner(loop_index):
        uv = tuple(uv_layer.data[loop_index].uv) if uv_layer else ()
        return tuple(mesh.loops[loop_index].normal), uv

    twins = {}
    vertex = count - 1
    while vertex >= 0 and uses[vertex] == 1:
        polygon, loop_index = corner_of[vertex]
        spot = mesh.vertices[vertex].co
        own = corner(loop_index)
        twin = None
        for other_loop in polygon.loop_indices:
            other = mesh.loops[other_loop].vertex_index
            if (other < vertex and other not in twins
                    and _same(mesh.vertices[other].co, spot)
                    and all(_same(a, b) for a, b in zip(corner(other_loop), own))
                    and _alike_in_motion(mesh, other, vertex)):
                twin = other
                break
        if twin is None:
            break
        twins[vertex] = twin
        vertex -= 1
    return twins


def build_lod(obj, mesh, distance, material_index_of, joint_map=None,
              skin_mesh_matrix=None, bone_world_matrices=None, parents=None,
              weight_override=None):
    """Build one LOD from *mesh*.

    ``material_index_of(slot)`` maps a Blender material slot to a 1-based file
    material id.

    Returns ``(lod, mapping, skin_lod)``. ``skin_lod`` is ``None`` unless
    *joint_map* (bone name -> 1-based group index) is supplied. *parents* names
    the joint each joint's blended vertices blend with (``None`` for the mesh
    frame). *weight_override* gives some vertices their weights outright,
    ``{vertex: {joint: weight}}``, in place of their vertex groups.
    """
    mesh.calc_loop_triangles()
    uv_layer = mesh.uv_layers.active
    twins = repeated_vertices(mesh)

    # Every distinct corner of every vertex, in the order the corners come.
    clusters = {}               # vertex -> [(normal, uv)]
    triangles = []              # (material slot, ((vertex, cluster), ...))
    used_slots = set()
    for tri in mesh.loop_triangles:
        used_slots.add(tri.material_index)
        corners = []
        for loop_index in tri.loops:
            loop = mesh.loops[loop_index]
            vertex = twins.get(loop.vertex_index, loop.vertex_index)
            uv = uv_layer.data[loop_index].uv if uv_layer else (0.0, 0.0)
            normal = tuple(loop.normal)
            uv = (uv[0], uv[1])
            found = clusters.setdefault(vertex, [])
            for index, (known_normal, known_uv) in enumerate(found):
                if (_close(known_normal, normal, NORMAL_TOLERANCE)
                        and _close(known_uv, uv, UV_TOLERANCE)):
                    break
            else:
                found.append((normal, uv))
                index = len(found) - 1
            corners.append((vertex, index))
        triangles.append((tri.material_index, tuple(corners)))

    buffer = VertexBuffer()
    slot_of = {}
    for vertex in sorted(clusters):
        position = convert.to_mafia_vector(mesh.vertices[vertex].co)
        for index, (normal, uv) in enumerate(clusters[vertex]):
            slot_of[(vertex, index)] = len(buffer.vertices)
            buffer.mapping.setdefault(vertex, []).append(len(buffer.vertices))
            buffer.source.append(vertex)
            buffer.vertices.append(Vertex(
                position=position,
                normal=convert.to_mafia_vector(normal),
                uv=convert.uv_to_mafia(uv),
            ))
    for copy, vertex in twins.items():
        if vertex in buffer.mapping:
            buffer.mapping.setdefault(copy, []).extend(buffer.mapping[vertex])
    triangles = [(slot, tuple(slot_of[corner] for corner in corners))
                 for slot, corners in triangles]

    skin_lod = None
    if joint_map is not None:
        order, skin_lod = _sort_for_skinning(
            obj, mesh, buffer, joint_map, skin_mesh_matrix, bone_world_matrices,
            parents or {}, weight_override)
        buffer = _reorder(buffer, order, twins)
        remap = {old: new for new, old in enumerate(order)}
        triangles = [(slot, tuple(remap[i] for i in tri)) for slot, tri in triangles]

    lod = LOD(distance=distance, vertices=buffer.vertices)
    for slot in sorted(used_slots):
        group = FaceGroup(material_id=material_index_of(slot))
        group.faces = [convert.face_to_mafia(tri)
                       for tri_slot, tri in triangles if tri_slot == slot]
        lod.face_groups.append(group)

    return lod, buffer.mapping, skin_lod


def _reorder(buffer, order, twins):
    """Rebuild a buffer in *order*, keeping the source mapping consistent."""
    result = VertexBuffer()
    result.vertices = [buffer.vertices[i] for i in order]
    result.source = [buffer.source[i] for i in order]
    for new_index, old_index in enumerate(order):
        result.mapping.setdefault(buffer.source[old_index], []).append(new_index)
    for copy, vertex in twins.items():
        if vertex in result.mapping:
            result.mapping[copy] = list(result.mapping[vertex])
    return result


def _classify_vertex(obj, mesh, vertex_index, joint_map, parents, override=None):
    """Return ``(group_index, weight)`` for a vertex, or ``None`` if unbound.

    The format binds a vertex to one joint, blended against the joint that
    joint's vertices blend with. So of two joints the vertex carries weight
    for, the one the other is the blend partner of owns it. ``weight`` is the
    owner's weight as it is set; only a weight of exactly 1 means no blend.
    """
    bones = {}
    if override is not None and vertex_index in override:
        bones = {name: weight for name, weight in override[vertex_index].items()
                 if name in joint_map and weight > 0.0}
    for entry in (() if override is not None and vertex_index in override
                  else mesh.vertices[vertex_index].groups):
        if entry.group >= len(obj.vertex_groups):
            continue
        name = obj.vertex_groups[entry.group].name
        if name in joint_map and entry.weight > 0.0:
            bones[name] = entry.weight
    if not bones:
        return None
    if len(bones) == 1:
        name = next(iter(bones))
    else:
        owners = [n for n in bones if parents.get(n) in bones]
        name = (owners[0] if len(owners) == 1
                else max(bones, key=lambda n: joint_map[n]))
    return joint_map[name] - 1, bones[name]


def _sort_for_skinning(obj, mesh, buffer, joint_map, skin_mesh_matrix,
                       bone_world_matrices, parents, override=None):
    """Order the vertex buffer into the runs the skin block expects.

    Layout: for each bone group, its fully-weighted vertices then its blended
    ones; finally every vertex bound to nothing, which the engine treats as
    belonging to the mesh frame. Within a run vertices keep their order.
    """
    group_count = len(joint_map)
    unweighted = [[] for _ in range(group_count)]
    weighted = [[] for _ in range(group_count)]
    root = []

    for index, source_vertex in enumerate(buffer.source):
        classified = _classify_vertex(obj, mesh, source_vertex, joint_map, parents,
                                      override)
        if classified is None:
            root.append(index)
            continue
        group_index, weight = classified
        if not (0 <= group_index < group_count):
            root.append(index)
        elif weight >= 1.0:
            unweighted[group_index].append(index)
        else:
            weighted[group_index].append((index, weight))

    order = []
    for group_index in range(group_count):
        order.extend(unweighted[group_index])
        order.extend(index for index, _ in weighted[group_index])
    order.extend(root)

    skin_lod = SkinLOD(root_unweighted=len(root))
    # The box of the vertices that follow the mesh frame itself - the ones the
    # joints do not move. 522 of the game's 526 skins store exactly that.
    skin_lod.bbox_min, skin_lod.bbox_max = _bounds(
        [buffer.vertices[i].position for i in root])

    ordered_bones = sorted(joint_map, key=lambda n: joint_map[n])
    for group_index, bone_name in enumerate(ordered_bones):
        partner = parents.get(bone_name)
        blended = weighted[group_index]
        inverse_bind = _inverse_bind(bone_name, skin_mesh_matrix,
                                     bone_world_matrices)
        members = unweighted[group_index] + [i for i, _ in blended]
        bbox_min, bbox_max = _bounds([
            _bind_space(buffer.vertices[i].position, inverse_bind)
            for i in members])
        skin_lod.groups.append(BoneGroup(
            inverse_bind=inverse_bind,
            unweighted_count=len(unweighted[group_index]),
            weighted_count=len(blended),
            # A group names the joint its vertices blend with only when it has
            # vertices to blend: 9437 of the game's 9468 groups follow that.
            parent_group=(joint_map.get(partner, 0)
                          if partner and blended else 0),
            bbox_min=bbox_min,
            bbox_max=bbox_max,
            weights=[weight for _, weight in blended],
        ))

    return order, skin_lod


def joint_parents(armature, joint_map):
    """For every joint the skin numbers, the joint its blends go to.

    That is the nearest joint above it that the skin numbers too, or ``None``
    for the mesh frame.
    """
    parents = {}
    for bone_name in joint_map:
        bone = armature.data.bones.get(bone_name)
        partner = None
        while bone is not None and bone.parent is not None:
            if bone.parent.name in joint_map:
                partner = bone.parent.name
                break
            bone = bone.parent
        parents[bone_name] = partner
    return parents


def _inverse_bind(bone_name, mesh_frame, bone_frames):
    """The joint's bind, in the file's rows: its whole world undone, then the mesh's.

    The joint's world includes its scale - see joint_space.JointSpace: the
    engine cancels this against the scaled joint world at skin time, so a bind
    built from an unscaled rest matrix leaves the scale uncanceled and deforms
    the mesh. Worked out in 64 bits from the joints' own values, which gives
    back 3629 of the game's 9468 binds; the rest were rounded another way.
    """
    from .joint_math import Frame, mat_inverse, mat_mul, mat_vec
    mesh = mesh_frame or Frame()
    joint = (bone_frames or {}).get(bone_name) or Frame()
    undo = mat_inverse(joint.linear)
    linear = mat_mul(undo, mesh.linear)
    offset = mat_vec(undo, [mesh.translation[i] - joint.translation[i]
                            for i in range(3)])
    return (linear[0][0], linear[2][0], linear[1][0], 0.0,
            linear[0][2], linear[2][2], linear[1][2], 0.0,
            linear[0][1], linear[2][1], linear[1][1], 0.0,
            offset[0], offset[2], offset[1], 1.0)


#: What the format writes for a bone group that ends up with no vertices: an
#: inside-out box, min at +1e16 and max at -1e16. 1511 of the 1513 empty groups
#: the game ships use it. A zero box would instead claim the group sits at the
#: origin and drag any union of the groups out to meet it.
EMPTY_BOUND = 1.0e16


def _f32(value):
    """*value* rounded the way a 32-bit float holds it."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def bounds_center(low, high):
    """The middle of a box, worked out the way the game's files have it.

    In 32-bit arithmetic, as low plus half the span: that gives back the
    stored center of every mirror and 181 of the 184 morph blocks the game
    ships, where the plain average of the corners gives back 98.
    """
    return tuple(_f32(low[axis] + _f32(_f32(high[axis] - low[axis]) * 0.5))
                 for axis in range(3))


def bounds_radius(low, high):
    """Half the length of a box's diagonal, the way the game's files have it.

    In 32-bit arithmetic, summing the squares from the last axis to the first:
    every mirror and 181 of the 184 morph blocks.
    """
    span = [_f32(high[axis] - low[axis]) for axis in range(3)]
    total = _f32(_f32(_f32(span[2] * span[2]) + _f32(span[1] * span[1]))
                 + _f32(span[0] * span[0]))
    return _f32(_f32(math.sqrt(total)) * 0.5)


def _face_normal_sums(positions, faces):
    """Each vertex's sum of the unit normals of the faces around it."""
    sums = [[0.0, 0.0, 0.0] for _ in positions]
    for a, b, c in faces:
        if a == b or b == c or a == c:
            continue
        pa, pb, pc = positions[a], positions[b], positions[c]
        u = [pb[i] - pa[i] for i in range(3)]
        w = [pc[i] - pa[i] for i in range(3)]
        normal = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2],
                  u[0] * w[1] - u[1] * w[0]]
        length = math.sqrt(sum(x * x for x in normal))
        if length == 0.0:
            continue
        normal = [x / length for x in normal]
        for vertex in (a, b, c):
            total = sums[vertex]
            total[0] += normal[0]
            total[1] += normal[1]
            total[2] += normal[2]
    return sums


def target_shading(lod, positions):
    """The normals a morph target shades with, for every vertex of *lod*.

    The game's files give a target the normals of its own shape: each vertex's
    faces' unit normals added up and scaled back to unit length, worked out on
    the file's own vertices, so a seam is not smoothed across. Where a target
    leaves everything around a vertex as the mesh has it, the vertex keeps the
    mesh's own normal - authored, and not always what its faces add up to.
    That gives back 93905 of the game's 108851 target normals to a hundredth of
    a degree. The rest, nearly all in characters' faces, were made from shapes
    the file does not hold: some lip vertices smoothed together in some targets
    and not in others, and normals turned where the file's shape does not move.
    Nothing in the file gives those back; weighting the faces by area or by
    angle, or turning the mesh's own normal with its faces, gives back fewer.
    """
    faces = [face for group in lod.face_groups for face in group.faces]
    mesh_positions = [vertex.position for vertex in lod.vertices]
    at_rest = _face_normal_sums(mesh_positions, faces)
    moved = _face_normal_sums(positions, faces)
    normals = []
    for index, (rest, total) in enumerate(zip(at_rest, moved)):
        if rest == total:
            normals.append(lod.vertices[index].normal)
            continue
        length = math.sqrt(sum(x * x for x in total))
        normals.append(tuple(x / length for x in total) if length
                       else lod.vertices[index].normal)
    return normals


def _bind_space(position, rows):
    """A file position carried through a bind matrix as the file's rows hold it.

    In 32-bit arithmetic, with the matrix as it is written: that gives back
    3950 of the game's 8065 bone group boxes, where working in 64 bits gives
    back 3395.
    """
    matrix = [_f32(value) for value in rows]
    return tuple(
        _f32(_f32(_f32(_f32(position[0] * matrix[axis])
                       + _f32(position[1] * matrix[4 + axis]))
                  + _f32(position[2] * matrix[8 + axis]))
             + matrix[12 + axis])
        for axis in range(3))


def portal_plane(ring):
    """``(normal, offset)`` of a portal's outline, or ``(None, None)`` if flat.

    Worked out the way the game's files have it, in 32-bit arithmetic: the
    normal is the cross product of the edge from the first corner to the
    second and the edge from the first to the last, scaled by one over its
    length, and the offset is minus its dot product with the last corner.
    """
    first, second, last = ring[0], ring[1], ring[-1]
    u = [_f32(second[axis] - first[axis]) for axis in range(3)]
    w = [_f32(last[axis] - first[axis]) for axis in range(3)]
    normal = [_f32(_f32(u[1] * w[2]) - _f32(u[2] * w[1])),
              _f32(_f32(u[2] * w[0]) - _f32(u[0] * w[2])),
              _f32(_f32(u[0] * w[1]) - _f32(u[1] * w[0]))]
    length = _f32(math.sqrt(_f32(_f32(_f32(normal[0] * normal[0])
                                      + _f32(normal[1] * normal[1]))
                                 + _f32(normal[2] * normal[2]))))
    if length == 0.0 or length != length:
        return None, None
    inverse = _f32(1.0 / length)
    normal = [_f32(component * inverse) for component in normal]
    offset = _f32(-_f32(_f32(_f32(normal[0] * last[0]) + _f32(normal[1] * last[1]))
                        + _f32(normal[2] * last[2])))
    return normal, offset


def _bounds(positions):
    if not positions:
        return ((EMPTY_BOUND,) * 3, (-EMPTY_BOUND,) * 3)
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
