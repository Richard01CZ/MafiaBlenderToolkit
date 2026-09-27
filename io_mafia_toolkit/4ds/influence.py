"""A joint's influence box, and the skin weights it makes.

Every joint in a 4DS carries sixteen numbers describing a box in its own space:
its area of influence. The file's first three rows are the box's axes, each
from the center to the middle of a face, and the fourth is its center. The skin
weights of the game's characters were made from these boxes when they were
exported, and they give them back: every blended vertex the game ships sits
inside its joint's box, weighted by where it sits along the box's third axis.

In Blender the box lives on the joint itself, as sixteen numbers on its pose
bone, drawn in the viewport over the bone it belongs to. Nothing stands in the
scene for it: a skeleton of twenty-five joints would otherwise be twenty-five
more objects to scroll past, select by accident and hide, and a box is not a
thing in the model - it is a number on a joint, like its culling byte. The
numbers are the file's own, so a box nobody has touched goes back out as it
came in.

A box is moved, turned and resized by the handles drawn on it, or by the fields
in the joint panel, which read it as a center, a size and a turn (see
:func:`box_parts`). Its local +Y is the third axis, the one the weight runs
along (the file's Y and Z trade places on the way in, as everywhere).

A joint with painted weights exports them. A joint with none has them made from
its box, the way the game's own exporter made them:

1. A vertex strictly inside a box goes to that joint, shared with the joint's
   parent by where it sits along the box's +Y - none of it the joint's at the
   back face, all of it at the front. Where boxes overlap, a joint beats its
   ancestors whatever the weights; between other joints the larger weight
   wins.
2. The vertices left over are claimed by growing those regions along the
   mesh's edges, one ring at a time: a vertex joins, wholly, the region it is
   reached from, as long as it is not behind that joint's box. A vertex two
   regions reach at once goes to the one it lies further ahead in.
3. Whatever is still unclaimed follows the mesh itself.

Vertices at the same spot count as one. With every weight cleared, this gives
back 105,578 of the 109,578 vertex positions of the game's 289 skinned models -
joint, and weight to 0.0001 - and 68 of the models entirely.
"""

import heapq

from mathutils import Euler, Matrix, Vector
from mathutils.kdtree import KDTree

from ..common import constants as C
from . import joint_math

#: Vertices closer than this (summed over the three axes) count as one.
WELD_DISTANCE = 0.001


# ── plain 64-bit 4x4 arithmetic, rows of a column-vector matrix ──────────────
def _mul(a, b):
    return [[a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c] + a[r][3] * b[3][c]
             for c in range(4)] for r in range(4)]


def _inverse(m):
    """Inverse of an affine 4x4."""
    linear = joint_math.mat_inverse([row[:3] for row in m[:3]])
    t = [m[0][3], m[1][3], m[2][3]]
    moved = [-(linear[r][0] * t[0] + linear[r][1] * t[1] + linear[r][2] * t[2]) for r in range(3)]
    return [linear[0] + [moved[0]], linear[1] + [moved[1]], linear[2] + [moved[2]],
            [0.0, 0.0, 0.0, 1.0]]


def _apply(m, p):
    return [m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3] for r in range(3)]


def _is_identity(m):
    return all(m[r][c] == (1.0 if r == c else 0.0) for r in range(4) for c in range(4))


# ── the file's sixteen numbers and a Blender matrix ─────────────────────────
def box_from_file(values):
    """The file's sixteen numbers as a Blender matrix (unit cube -> joint space).

    The file's rows are the box's axes as the file holds them; Blender's matrix
    has them as columns, with Y and Z traded, so the file's third axis - the one
    the weight runs along - is the box's local Y.
    """
    swap = lambda v: (v[0], v[2], v[1])
    x_axis, y_axis = swap(values[0:3]), swap(values[8:11])
    z_axis, center = swap(values[4:7]), swap(values[12:15])
    return [[x_axis[i], y_axis[i], z_axis[i], center[i]] for i in range(3)] \
        + [[0.0, 0.0, 0.0, 1.0]]


def box_to_file(box):
    """A Blender box matrix as the file's sixteen numbers."""
    column = lambda j: (box[0][j], box[1][j], box[2][j])
    swap = lambda v: (v[0], v[2], v[1])
    first, third = swap(column(0)), swap(column(1))
    second, center = swap(column(2)), swap(column(3))
    return (*first, 0.0, *second, 0.0, *third, 0.0, *center, 1.0)


# ── the box on its joint ────────────────────────────────────────────────────
def joint_of(armature, bone_name):
    """The pose bone a box is kept on, or ``None``."""
    pose = getattr(armature, "pose", None)
    return pose.bones.get(bone_name) if pose is not None else None


def has_box(armature, bone_name):
    """True while *bone_name* carries an influence box."""
    joint = joint_of(armature, bone_name)
    return bool(joint is not None and getattr(joint, C.HAS_JOINT_BOX_PROP, False))


def file_box(armature, bone_name):
    """A joint's box as the file's own sixteen numbers, or ``None``.

    What came in is what goes out: the numbers are kept as the file holds
    them, so a box nobody has touched is written back to the bit.
    """
    joint = joint_of(armature, bone_name)
    if joint is None or not getattr(joint, C.HAS_JOINT_BOX_PROP, False):
        return None
    return tuple(getattr(joint, C.JOINT_BOX_PROP))


def box_of(armature, bone_name):
    """A joint's box in its own space, as a Blender matrix, or ``None``."""
    values = file_box(armature, bone_name)
    return box_from_file(values) if values is not None else None


def set_file_box(armature, bone_name, values):
    """Give a joint the box *values* describe, in the file's own terms."""
    joint = joint_of(armature, bone_name)
    if joint is None:
        return None
    setattr(joint, C.JOINT_BOX_PROP, tuple(float(v) for v in values))
    setattr(joint, C.HAS_JOINT_BOX_PROP, True)
    return joint


def set_box(armature, bone_name, box):
    """Give a joint the box a Blender matrix describes."""
    return set_file_box(armature, bone_name, box_to_file(box))


def clear_box(armature, bone_name):
    """Take a joint's box away, leaving its weights to what is painted."""
    joint = joint_of(armature, bone_name)
    if joint is not None:
        setattr(joint, C.HAS_JOINT_BOX_PROP, False)


def boxes_of(armature):
    """``{bone name: box in the joint's own space}`` for the joints with one."""
    found = {}
    for bone in armature.data.bones:
        box = box_of(armature, bone.name)
        if box is not None:
            found[bone.name] = box
    return found


# ── a box as something to move, turn and resize ─────────────────────────────
def box_parts(box):
    """A box as ``(center, size, turn)``: where it sits, how big, how turned.

    The matrix holds each axis as a column, running from the center to the
    middle of a face, so a size is twice an axis and the turn is what is left
    once the three are measured off. A box whose axes are mirrored rather than
    only turned keeps the mirroring in a negative size, so the parts always
    build the same box back.
    """
    matrix = Matrix(box)
    axes = [matrix.col[index].to_3d() for index in range(3)]
    lengths = [axis.length for axis in axes]
    basis = Matrix.Identity(3)
    for index, axis in enumerate(axes):
        if lengths[index]:
            basis.col[index] = axis / lengths[index]
    size = [length * 2.0 for length in lengths]
    if basis.determinant() < 0.0:
        basis.col[2] = -basis.col[2]
        size[2] = -size[2]
    return matrix.translation.copy(), Vector(size), basis.to_euler("XYZ")


def box_from_parts(center, size, turn):
    """The box ``(center, size, turn)`` describes, as a Blender matrix."""
    basis = Euler(tuple(turn), "XYZ").to_matrix()
    half = [value * 0.5 for value in size]
    return [[basis[r][c] * half[c] for c in range(3)] + [center[r]]
            for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def bone_box(joint):
    """The box kept on pose bone *joint*, as a Blender matrix, or ``None``."""
    if joint is None or not getattr(joint, C.HAS_JOINT_BOX_PROP, False):
        return None
    return box_from_file(tuple(getattr(joint, C.JOINT_BOX_PROP)))


def bone_parts(joint):
    """``(center, size, turn)`` of the box on *joint*, the default if it has none."""
    box = bone_box(joint)
    if box is None:
        box = box_from_file(C.DEFAULT_JOINT_BOX)
    return box_parts(box)


def set_bone_parts(joint, center=None, size=None, turn=None):
    """Rebuild the box on *joint* with one of its three parts replaced.

    The two parts not being edited are read back off the box and put in again,
    so typing a size leaves the box where it is and turning it leaves its size
    alone. A joint edited this way has a box from then on, whether or not it
    had one before: moving a box is asking for one.
    """
    if joint is None:
        return
    was_center, was_size, was_turn = bone_parts(joint)
    box = box_from_parts(center if center is not None else was_center,
                         size if size is not None else was_size,
                         turn if turn is not None else was_turn)
    setattr(joint, C.JOINT_BOX_PROP, tuple(float(v) for v in box_to_file(box)))
    setattr(joint, C.HAS_JOINT_BOX_PROP, True)


def _joint_world(space, bone_name):
    return [row[:] for row in space.frames[bone_name].matrix()]


# ── a box fitted to the joint it bends ──────────────────────────────────────
#: How far along the way to the child joint a fitted box reaches, each way from
#: the joint. The game's own boxes sit on the joint and reach about a fifth of
#: the way to the child: the bend is at the joint, and the flesh beyond it
#: belongs to the joint outright.
FIT_REACH = 0.2
#: How wide a fitted box is when there is no mesh to measure, as a share of the
#: way to the child. The game's own run from a quarter to two fifths.
FIT_WIDTH = 0.3
#: A fitted box never grows past this much of the way to the child, however far
#: the mesh spreads - a box as wide as the model is a box that holds everything.
FIT_WIDEST = 0.8
#: The last joint of a chain - a head, a hand, a foot - owns everything beyond
#: it, so its box has to be as wide as that is, or nothing can be claimed
#: through it. It is measured over this much of the way onward, in lengths of
#: the step from its parent, and allowed this much wider than an inner joint.
LEAF_REACH = 4.0
LEAF_WIDEST = 3.0


def _perpendicular(axis):
    """Two unit axes square to *axis* and to each other."""
    guess = Vector((0.0, 0.0, 1.0))
    if abs(axis.dot(guess)) > 0.9:
        guess = Vector((1.0, 0.0, 0.0))
    first = axis.cross(guess).normalized()
    return first, axis.cross(first).normalized()


def child_in_joint_space(space, bone_name, child_name):
    """Where a joint's child sits, in that joint's own space."""
    joint = _inverse(_joint_world(space, bone_name))
    child = _joint_world(space, child_name)
    return Vector(_apply(joint, (child[0][3], child[1][3], child[2][3])))


def fitted_box(space, armature, bone_name, child_name, mesh=None,
               mesh_frame=None):
    """A box on the joint, aimed down the chain, as wide as what it moves.

    The axis the weight runs along points at the child, so the weight flows
    down the chain the way the game's own do: nothing of the joint's at the
    back face, all of it at the front. The box reaches a fifth of the way to
    the child either side, which is where the bending is; everything further
    down the limb is claimed from it, not held by it.

    The last joint of a chain has no child to aim at, and carries on the way
    its parent led to it instead - the head past the neck, the fingers past
    the hand. It owns everything beyond it, so its cross-section is measured
    over all of that rather than only beside the joint: a box the width of a
    neck cannot let a region through into a head.

    With a mesh to measure, the cross-section is taken from what is there -
    the width of the limb - rather than guessed at. Only what is nearer this
    joint than any joint outside its own chain counts: a skirt round both legs
    is as wide as both, and a box measured on all of it reached right across
    to the other leg.
    """
    last = child_name is None
    if last:
        parent = armature.data.bones[bone_name].parent
        if parent is None or parent.name not in space.frames:
            return None
        toward = -child_in_joint_space(space, bone_name, parent.name)
    else:
        toward = child_in_joint_space(space, bone_name, child_name)
    gap = toward.length
    if gap < 1.0e-6:
        return None
    axis = toward / gap
    across, up = _perpendicular(axis)
    half_length = gap * FIT_REACH
    half_across = half_up = gap * FIT_WIDTH

    if mesh is not None and mesh_frame is not None:
        # The vertices that lie beside the joint, measured out from the axis.
        into_joint = _inverse(_joint_world(space, bone_name))
        reach = gap * LEAF_REACH if last else half_length
        kin = ancestors_of(armature)
        others = []
        for other in armature.data.bones:
            if (other.name == bone_name or other.name not in space.frames
                    or other.name in kin.get(bone_name, ())
                    or bone_name in kin.get(other.name, ())):
                continue
            place = _joint_world(space, other.name)
            others.append(Vector(_apply(into_joint, (place[0][3], place[1][3],
                                                     place[2][3]))))
        widest_across = widest_up = 0.0
        for vertex in mesh.vertices:
            point = Vector(_apply(into_joint,
                                  _apply(mesh_frame, tuple(vertex.co))))
            along = point.dot(axis)
            if not -half_length <= along <= reach:
                continue
            own = point.length
            if any((point - other).length < own for other in others):
                continue
            widest_across = max(widest_across, abs(point.dot(across)))
            widest_up = max(widest_up, abs(point.dot(up)))
        limit = gap * (LEAF_WIDEST if last else FIT_WIDEST)
        if widest_across > 0.0:
            half_across = min(widest_across, limit)
        if widest_up > 0.0:
            half_up = min(widest_up, limit)

    columns = (across * half_across, axis * half_length, up * half_up)
    return [[columns[c][r] for c in range(3)] + [0.0] for r in range(3)] \
        + [[0.0, 0.0, 0.0, 1.0]]


def fitting_child(armature, bone_name, numbered=None):
    """The child a joint's box should point at, or ``None``.

    The one child where there is one. Where a joint branches - a spine into a
    neck and two shoulders - the nearest, which is the one the joint bends
    with; the others bend at their own joints.
    """
    bone = armature.data.bones.get(bone_name)
    if bone is None:
        return None
    children = [child for child in bone.children
                if numbered is None or child.name in numbered]
    if not children:
        return None
    if len(children) == 1:
        return children[0].name
    head = bone.head_local
    return min(children, key=lambda child:
               (child.head_local - head).length).name


# ── weights from the boxes ───────────────────────────────────────────────────
def painted_joints(obj, mesh, joints, shares=None):
    """Which of *joints* carry any weight on *mesh*, and which vertices carry any.

    A vertex counts as painted when it has weight on a joint, or a share of
    its own kept with the mesh frame (*shares*, from
    :func:`.joint_space.shares_of`) - that says "follow the mesh".
    """
    names = {group.index: group.name for group in obj.vertex_groups}
    painted, taken = set(), set(shares or ())
    for vertex in mesh.vertices:
        for entry in vertex.groups:
            name = names.get(entry.group)
            if entry.weight <= 0.0:
                continue
            if name in joints:
                painted.add(name)
                taken.add(vertex.index)
    return painted, taken


def box_world(space, bone_name, box):
    """A joint's box in the armature's space."""
    return _mul(_joint_world(space, bone_name), box)


def is_flat(box):
    """True for a box with an axis of no length, which holds no vertex."""
    return any(box[0][c] == 0.0 and box[1][c] == 0.0 and box[2][c] == 0.0
               for c in range(3))


def ancestors_of(armature):
    """``{bone name: set of the bones above it}``."""
    found = {}
    for bone in armature.data.bones:
        above, parent = set(), bone.parent
        while parent is not None:
            above.add(parent.name)
            parent = parent.parent
        found[bone.name] = above
    return found


def _weld(positions):
    """Each vertex's stand-in: the first vertex found at the same spot."""
    order = sorted(range(len(positions)), key=lambda i: positions[i][0])
    stand_in = list(range(len(positions)))
    for n, i in enumerate(order):
        if stand_in[i] != i:
            continue
        p = positions[i]
        for j in order[n + 1:]:
            q = positions[j]
            if q[0] - p[0] > WELD_DISTANCE:
                break
            if (stand_in[j] == j and abs(p[0] - q[0]) + abs(p[1] - q[1])
                    + abs(p[2] - q[2]) < WELD_DISTANCE):
                stand_in[j] = i
    return stand_in


def box_weights(mesh, placement, boxes, parents, ancestors, taken):
    """``{vertex index: {joint: weight}}`` made from *boxes*.

    *placement* is where *mesh*'s vertices sit in the armature's terms, as
    rows. *boxes* is ``[(joint name, box world matrix)]`` in the armature's order,
    parents first; *parents* names each joint's blend partner (``None`` for the
    mesh frame); *ancestors* ``{joint: set of the joints above it}``; *taken*
    the vertices painted weights already decide, which the boxes leave alone
    and which stop a region growing through them.

    Joints of one chain - a joint and those above or below it - meet by the
    game's own rules. Joints that are no kin of each other - a left leg and a
    right one, a leg and the spine - meet where the mesh is nearer the one
    than the other: inside overlapping boxes, the box the vertex is further
    into, and outside them, the region nearer along the surface. Counting
    edges alone, as the game's rule does, lets a region run ahead wherever
    the triangles lean its way, and on a skirt one leg took both sides.

    A region grows along the mesh's edges, so a piece of the mesh joined to
    nothing else - a shoe, a collar, an ornament modeled on its own - is cut
    off from the regions growing through the body. What a region stops at
    behind its box there goes up the joint's chain, to the nearest joint it
    does not lie behind as well - as on a body in one piece, where the
    parent's region is there to take it; a piece no region gets into at all
    follows, whole, the joint it rests on most.
    """
    if not boxes:
        return {}
    frame = placement
    count = len(mesh.vertices)
    positions = [tuple(v.co) for v in mesh.vertices]
    stand_in = _weld(positions)
    world = [_apply(frame, p) for p in positions]
    inverses = [(name, _inverse(matrix)) for name, matrix in boxes]

    def place(index, inverse):
        return _apply(inverse, world[index])

    owner = [None] * count
    weight = [0.0] * count
    # How far from the middle of its box the owner holds the vertex, in the
    # box's own proportions: 0 at the middle, 1 at the middle of a face.
    depth = [3.0] * count
    claimed = [False] * count
    blocked = [False] * count
    for index in taken:
        blocked[stand_in[index]] = True

    def related(a, b):
        return a == b or a in ancestors.get(b, ()) or b in ancestors.get(a, ())

    # 1. inside the boxes
    for index in range(count):
        if stand_in[index] != index or blocked[index]:
            continue
        for name, inverse in reversed(inverses):
            x, y, z = place(index, inverse)
            if not (-1.0 < x < 1.0 and -1.0 < y < 1.0 and -1.0 < z < 1.0):
                continue
            share = (y + 1.0) * 0.5
            deep = x * x + y * y + z * z
            if claimed[index] and not related(owner[index], name):
                # Boxes of joints no kin of each other meet or overlap: the
                # one whose middle the vertex is nearer takes it.
                if (deep, -share) < (depth[index], -weight[index]):
                    owner[index], weight[index] = name, share
                    depth[index] = deep
                continue
            if claimed[index]:
                current = owner[index]
                if share <= weight[index]:
                    if current in ancestors.get(name, ()):
                        claimed[index] = False
                elif name in ancestors.get(current, ()):
                    share = 0.0
            if not claimed[index] or share > weight[index]:
                owner[index], weight[index], claimed[index] = name, share, True
                depth[index] = deep

    # 2. grow the regions along the edges, a ring at a time
    neighbours = [set() for _ in range(count)]
    for polygon in mesh.polygons:
        corners = [stand_in[v] for v in polygon.vertices]
        for a in corners:
            for b in corners:
                if a != b:
                    neighbours[a].add(b)
    inverse_of = dict(inverses)

    def by_rings(links):
        # The game's own rule: a ring of edges at a time, a vertex two
        # regions reach at once going to the one it is further ahead in.
        front = [i for i in range(count) if stand_in[i] == i and claimed[i]]
        while front:
            reached = {}
            for source in front:
                name = owner[source]
                for target in sorted(links[source]):
                    if claimed[target] or blocked[target]:
                        continue
                    ahead = place(target, inverse_of[name])[1]
                    if ahead <= -1.0:
                        continue            # behind the box
                    if target not in reached or ahead > reached[target][1]:
                        reached[target] = (name, ahead)
            for target, (name, _ahead) in reached.items():
                owner[target], weight[target], claimed[target] = name, 1.0, True
            front = sorted(reached)

    def by_distance(links):
        # The same regions, the same stops, measured along the surface: the
        # nearest region takes a vertex, the one it is further ahead in
        # breaking a tie.
        queue = [(0.0, 0.0, source, owner[source]) for source in range(count)
                 if stand_in[source] == source and claimed[source]]
        heapq.heapify(queue)
        settled = [False] * count
        while queue:
            distance, _ahead, vertex, name = heapq.heappop(queue)
            if settled[vertex]:
                continue
            if not claimed[vertex]:
                owner[vertex], weight[vertex], claimed[vertex] = name, 1.0, True
            elif owner[vertex] != name:
                continue
            settled[vertex] = True
            here = world[vertex]
            for target in links[vertex]:
                if claimed[target] or blocked[target] or settled[target]:
                    continue
                ahead = place(target, inverse_of[name])[1]
                if ahead <= -1.0:
                    continue            # behind the box
                there = world[target]
                step = ((here[0] - there[0]) ** 2 + (here[1] - there[1]) ** 2
                        + (here[2] - there[2]) ** 2) ** 0.5
                heapq.heappush(queue, (distance + step, -ahead, target, name))

    def grow(links):
        # Grown both ways from the same start. Where the two agree on a
        # joint's chain, the rings stand, as the game has them; where the
        # nearer region along the surface is no kin of the one the rings
        # gave, the nearer takes it.
        start = (owner[:], weight[:], claimed[:])
        by_rings(links)
        rings = (owner[:], weight[:], claimed[:])
        owner[:], weight[:], claimed[:] = start[0][:], start[1][:], start[2][:]
        by_distance(links)
        for vertex in range(count):
            if start[2][vertex] or not rings[2][vertex]:
                continue            # claimed before, or reached by distance alone
            if not claimed[vertex] or related(rings[0][vertex], owner[vertex]):
                owner[vertex], weight[vertex] = rings[0][vertex], rings[1][vertex]
                claimed[vertex] = True

    grow(neighbours)

    # 3. what a region stopped at, behind its box, is its parent's side. On a
    # body in one piece the parent's region is there to take it, or to leave
    # it to the mesh frame, as the game's own do. A piece modeled apart - a
    # shoe past the ankle, a collar round a neck - is cut off from the parent
    # altogether, so the nearest joint up the chain it does not lie behind
    # takes it there, and grows on from it by its own box; behind all of
    # them, it is left to the mesh frame.
    piece_of = _pieces(neighbours, stand_in)
    present = {}
    for vertex in range(count):
        if stand_in[vertex] == vertex and claimed[vertex]:
            present.setdefault(piece_of[vertex], set()).add(owner[vertex])
    while _hand_back(neighbours, stand_in, owner, weight, claimed, blocked,
                     parents, inverse_of, place, piece_of, present):
        grow(neighbours)

    # 4. a piece no region got into follows what it rests on
    _settle_pieces(piece_of, stand_in, world, owner, weight, claimed, blocked)

    # 5. the rest follows the mesh
    result = {}
    for index in range(count):
        base = stand_in[index]
        if not claimed[base] or blocked[base]:
            continue
        name, share = owner[base], weight[base]
        if share >= 1.0:
            result[index] = {name: 1.0}
        else:
            partner = parents.get(name)
            result[index] = ({name: share, partner: 1.0 - share} if partner
                             else {name: share})
    return result


#: How much further than its closest point a piece may lie from the body and
#: still count as resting on it there, in meters.
CONTACT_REACH = 0.01


def _settle_pieces(piece_of, stand_in, world, owner, weight, claimed, blocked):
    """Weight the pieces of the mesh no region got into, from what they rest on.

    A piece is what its own edges hold together. One with nothing claimed and
    nothing painted in it took no part in the growth - hair on a scalp, an
    ornament hanging clear of the body - and it follows, whole, the joint it
    rests on most: of its vertices within a centimeter of its closest approach
    to the weighted mesh, the joint the most of them are nearest. It takes
    that joint's middle share among them. Copied vertex by vertex, hair hanging
    down to the nape went half with the chest and tore when the head turned.
    Painted vertices are the painter's and lend nothing.
    """
    pieces = {}
    for vertex, piece in enumerate(piece_of):
        if piece >= 0 and stand_in[vertex] == vertex:
            pieces.setdefault(piece, []).append(vertex)
    unreached = [members for members in pieces.values()
                 if not any(claimed[v] or blocked[v] for v in members)]
    if not unreached:
        return
    sources = [v for v in range(len(stand_in))
               if stand_in[v] == v and claimed[v] and not blocked[v]]
    if not sources:
        return
    tree = KDTree(len(sources))
    for vertex in sources:
        tree.insert(world[vertex], vertex)
    tree.balance()
    for members in unreached:
        found = [tree.find(world[vertex]) for vertex in members]
        closest = min(distance for _place, _nearest, distance in found)
        resting = {}
        for _place, nearest, distance in found:
            if distance <= closest + CONTACT_REACH:
                resting.setdefault(owner[nearest], []).append(
                    (distance, weight[nearest]))
        joint = max(resting, key=lambda name: (len(resting[name]),
                                               -min(resting[name])[0]))
        shares = sorted(share for _distance, share in resting[joint])
        share = shares[len(shares) // 2]
        for vertex in members:
            owner[vertex], weight[vertex], claimed[vertex] = joint, share, True


def _pieces(neighbours, stand_in):
    """Which piece of the mesh each vertex belongs to: what its edges hold
    together, vertices at the same spot counting as one."""
    count = len(stand_in)
    piece_of = [-1] * count
    pieces = 0
    for seed in range(count):
        if stand_in[seed] != seed or piece_of[seed] != -1:
            continue
        piece_of[seed] = pieces
        stack = [seed]
        while stack:
            vertex = stack.pop()
            for other in neighbours[vertex]:
                if piece_of[other] == -1:
                    piece_of[other] = pieces
                    stack.append(other)
        pieces += 1
    return piece_of


def _hand_back(neighbours, stand_in, owner, weight, claimed, blocked,
               parents, inverse_of, place, piece_of, present):
    """Give what each region stopped at, behind its box, up its joint's chain,
    in pieces of the mesh the parent's own region never reached.

    It goes to the nearest joint above that it does not lie behind as well:
    past the ankle, the shin; round the neck, the chest. Behind every one of
    them - at the hips - it is left to the mesh frame, as the game's own
    characters leave it. *present* is which joints' regions grew in each piece
    along its edges; a piece the parent's region is in keeps the game's rule.
    Only a joint weighted from its own box takes anything, since it grows on
    from there by that box; a painted one keeps what is painted. Returns
    whether anything was given.
    """
    given = {}
    for source in range(len(stand_in)):
        if stand_in[source] != source or not claimed[source]:
            continue
        name = owner[source]
        parent = parents.get(name)
        if parent is None or parent in present.get(piece_of[source], ()):
            continue
        for target in sorted(neighbours[source]):
            if claimed[target] or blocked[target] or target in given:
                continue
            if place(target, inverse_of[name])[1] > -1.0:
                continue
            heir = parent
            while heir is not None and heir in inverse_of:
                if place(target, inverse_of[heir])[1] > -1.0:
                    given[target] = heir
                    break
                heir = parents.get(heir)
    for target, heir in given.items():
        owner[target], weight[target], claimed[target] = heir, 1.0, True
    return bool(given)
