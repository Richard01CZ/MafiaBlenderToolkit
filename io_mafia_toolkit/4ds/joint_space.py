"""A model's joints in the file's own terms, worked out from its armature.

Blender holds a skeleton as bones placed in the armature's space. A 4DS holds
it as frames, each with a transform relative to the frame above it, and chains
those transforms the way any frame is chained - scale included. A joint scaled
by 1.05 puts everything below it 5% further out, and a bone cannot hold that
scale at rest. So the bones sit where the game puts the joints, the scale is a
4DS value on the joint, and this module is the one place that turns the pair
back into what the file stores.

Everything is recomputed from the scene, top-down, every time: each joint's
position from its bone's head measured in its parent frame's whole world, its
rotation from the bone's tail and roll, and its scale from the joint's own
value. The arithmetic is the
import's own, in 64 bits (see :mod:`.joint_math`), so a joint's values give
back the same bone, and the same values, however often a model goes round. The
export writes the result and the animation code measures keys against it, so
the two can never disagree about where a joint rests.

The skinned mesh a skeleton belongs to is part of that chain, and the
armature stands for its frame: the armature sits where the mesh frame does,
turned and scaled as it is, and holds that in its Delta Transform - Blender's
own offset beneath an object's location, rotation and scale. Those stay free
for the body's movement, which the game's character animations key on the mesh
frame, so a crouch or a roll turns the body about the frame's own point, as the
game does. The joints at the top of the skeleton hang from the frame, so the
armature's own terms are the frame's. No bone stands for it and the mesh
object's origin does not decide it: the mesh's vertices are brought into the
frame's terms wherever the mesh sits.

The mesh frame hangs from whatever the armature hangs from - the model itself,
or, where the armature is hung on a joint of another, that joint, as the mesh
of I04Delnik01+ is. An armature with no skinned mesh stands for nothing: its
joints hang from the model, and it has to stand at the model's origin.
"""

import math

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from ..common import constants as C
from . import joint_math

_UNIT_SCALE = (1.0, 1.0, 1.0)

#: Pose-bone value: a joint's 4DS scale.
JOINT_SCALE = "ls3d_joint_scale"


def find_armature(obj):
    """The armature deforming *obj*, by modifier or by parenting."""
    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object is not None:
            return modifier.object
    if obj.parent is not None and obj.parent.type == "ARMATURE":
        return obj.parent
    return None


def is_skinned_mesh(obj):
    """True for a mesh set to Single Mesh or Single Morph."""
    if obj is None or obj.type != "MESH":
        return False
    try:
        frame_type = int(obj.ls3d_frame_type)
        visual_type = int(obj.visual_type)
    except (AttributeError, TypeError, ValueError):
        return False
    return (frame_type == C.FRAME_VISUAL
            and visual_type in C.SKINNED_VISUAL_TYPES)


def skinned_meshes_of(armature, objects):
    """Every skinned mesh among *objects* that *armature* deforms."""
    return [obj for obj in objects
            if is_skinned_mesh(obj) and find_armature(obj) is armature]


def numbered_joints(space):
    """``{bone name: skin group number}`` for the joints a skin carries.

    A skin numbers the joints hanging from its mesh in the order of the mesh's
    vertex groups - the game's files do not number them in the order of the
    skeleton - and any such joint with no vertex group after those, in the
    armature's own order.
    """
    if space.skin is None:
        return {}
    below = [bone.name for bone in space.joints() if space.below_mesh(bone)]
    wanted = set(below)
    numbered = [group.name for group in space.skin.vertex_groups
                if group.name in wanted]
    numbered += [name for name in below if name not in numbered]
    return {name: number for number, name in enumerate(numbered, start=1)}


def skinned_mesh_of(armature, objects):
    """The skinned mesh *armature* deforms, or ``None``.

    A 4DS holds one skinned mesh per skeleton. Where the scene binds more, the
    export refuses and names them; everything else takes the first.
    """
    found = skinned_meshes_of(armature, objects)
    return found[0] if found else None


def delta_channels(obj):
    """*obj*'s Delta Transform as ``(location, rotation, scale)``, read the way
    Blender reads it for the object's rotation mode.

    It is where an animated object rests: Blender adds it beneath the
    location, rotation and scale an animation keys, so the rest survives the
    animation. Blender keeps no delta for the axis-and-angle mode, and places
    an object in that mode with none, so neither is one read here.
    """
    mode = obj.rotation_mode
    if mode == "QUATERNION":
        rotation = tuple(obj.delta_rotation_quaternion)
    elif mode == "AXIS_ANGLE":
        rotation = (1.0, 0.0, 0.0, 0.0)
    else:
        rotation = tuple(Euler(obj.delta_rotation_euler, mode).to_quaternion())
    return tuple(obj.delta_location), rotation, tuple(obj.delta_scale)


#: A Delta Transform that moves nothing.
NO_DELTA = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0))


def delta_matrix(obj):
    """*obj*'s Delta Transform, as a matrix."""
    location, rotation, scale = delta_channels(obj)
    return Matrix.LocRotScale(Vector(location), Quaternion(rotation),
                              Vector(scale))


def set_delta(obj, location, rotation, scale):
    """Set *obj*'s Delta Transform, in quaternion mode."""
    obj.rotation_mode = "QUATERNION"
    obj.delta_location = tuple(location)
    obj.delta_rotation_quaternion = tuple(rotation)
    obj.delta_scale = tuple(scale)


def rest_own_channels(obj, location=True, rotation=True, scale=True):
    """Put *obj*'s own location, rotation and scale back to moving nothing."""
    if location:
        obj.location = (0.0, 0.0, 0.0)
    if rotation:
        obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        obj.rotation_euler = (0.0, 0.0, 0.0)
        obj.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
    if scale:
        obj.scale = (1.0, 1.0, 1.0)


def rest_in_delta(obj):
    """Move where *obj* stands into its Delta Transform, leaving its own
    location, rotation and scale moving nothing - free for an animation.

    Blender adds the two the same way before and after, so nothing moves. It
    is how an object keeps its place in the model under an animation that
    keys its own channels: those hold whatever frame is showing, and would
    otherwise be all there is of where it rests.
    """
    location, rotation, scale = object_channels(obj)
    delta_location, delta_rotation, delta_scale = delta_channels(obj)
    if (tuple(location), tuple(Quaternion(rotation)), tuple(scale)) == NO_DELTA:
        return
    placed = (Vector(delta_location) + Vector(location),
              (Quaternion(delta_rotation) @ Quaternion(rotation)
               if delta_rotation != NO_DELTA[1] else Quaternion(rotation)),
              Vector(tuple(a * b for a, b in zip(delta_scale, scale))))
    set_delta(obj, *placed)
    rest_own_channels(obj)


def mesh_frame_channels(armature):
    """The mesh frame's own ``(location, rotation, scale)``: where *armature*
    stands, in its Delta Transform."""
    return delta_channels(armature)


def mesh_frame_matrix(armature):
    """The mesh frame's own transform, as a matrix."""
    return delta_matrix(armature)


def set_mesh_frame(armature, location, rotation, scale):
    """Stand *armature* where its skinned mesh's frame is, in its Delta Transform."""
    set_delta(armature, location, rotation, scale)


def own_channels_at_rest(obj):
    """True where *obj*'s location, rotation and scale move nothing.

    Its Delta Transform is not among them: on an armature with a skinned mesh
    that is where its mesh frame is held.
    """
    location, rotation, scale = object_channels(obj)
    turned = Quaternion(rotation).normalized().angle
    turned = min(turned, 2.0 * 3.141592653589793 - turned)
    return (Vector(location).length <= REST_TOLERANCE
            and turned <= REST_TOLERANCE
            and all(abs(v - 1.0) <= REST_TOLERANCE for v in scale))


#: How far an armature's own channels may be from nothing and still be at
#: rest - they are set, not worked out, so anything past rounding is a move.
REST_TOLERANCE = 1.0e-6


def share_group(obj, armature):
    """``(vertex group, inverted)`` holding *obj*'s own share, or ``None``.

    A vertex at the top of the skeleton can be shared between its joint and
    the mesh frame, and whatever the joint does not move stays with the mesh.
    Blender shows that through the Vertex Group of the Armature modifier that
    binds *obj* to *armature*: it holds a vertex back from the joints by its
    weight there. With Invert on, that weight is the mesh's share - how the
    import sets it up; with Invert off, it is the joints' share and the rest
    is the mesh's. No group named there means no vertex is held back.
    """
    for modifier in obj.modifiers:
        if modifier.type != "ARMATURE" or modifier.object is not armature:
            continue
        group = (obj.vertex_groups.get(modifier.vertex_group)
                 if modifier.vertex_group else None)
        return (group, modifier.invert_vertex_group) if group else None
    return None


def shares_of(obj, mesh, armature):
    """``{vertex index: the mesh frame's share}`` for every vertex with one.

    What the Armature modifier shows: a vertex held back from its joints by
    the modifier's Vertex Group stays that much with the mesh.
    """
    held = share_group(obj, armature)
    if held is None:
        return {}
    group, inverted = held
    shares = {}
    for vertex in mesh.vertices:
        weight = next((entry.weight for entry in vertex.groups
                       if entry.group == group.index), 0.0)
        share = weight if inverted else 1.0 - weight
        if share > 0.0:
            shares[vertex.index] = share
    return shares


def joint_scale(armature, bone_name):
    """A joint's own 4DS scale, as a vector."""
    pose_bone = armature.pose.bones.get(bone_name) if armature.pose else None
    value = pose_bone.get(JOINT_SCALE, _UNIT_SCALE) if pose_bone else _UNIT_SCALE
    return Vector(tuple(value))


def bones_parents_first(armature):
    """Every bone, each after its parent, keeping the armature's own order.

    Blender keeps bones in the order they were made, so an imported skeleton
    is already in file order and a hand-made one in the order it was built.
    """
    position = {bone.name: index for index, bone in enumerate(armature.data.bones)}
    placed, result = set(), []

    def emit(bone):
        if bone.name in placed:
            return
        placed.add(bone.name)
        if bone.parent is not None:
            emit(bone.parent)
        result.append(bone)

    for bone in sorted(armature.data.bones, key=lambda b: position[b.name]):
        emit(bone)
    return result


def object_channels(obj):
    """An object's own ``(location, rotation, scale)`` as plain numbers."""
    if obj.rotation_mode == "QUATERNION":
        rotation = tuple(obj.rotation_quaternion)
    elif obj.rotation_mode == "AXIS_ANGLE":
        angle, *axis = obj.rotation_axis_angle
        rotation = tuple(Quaternion(Vector(axis), angle))
    else:
        rotation = tuple(obj.rotation_euler.to_quaternion())
    return tuple(obj.location), rotation, tuple(obj.scale)


def bone_rest(bone):
    """A bone's ``(head, tail, roll)``, as near as it can be read outside Edit Mode.

    The head and tail come back exactly. The roll does not: Blender keeps it
    only while the armature is in Edit Mode, and working it back out of the
    bone's matrix is a few rounding steps off. See :func:`edit_rest`.
    """
    head = tuple(bone.head_local)
    tail = tuple(bone.tail_local)
    _axis, roll = bpy.types.Bone.AxisRollFromMatrix(
        bone.matrix_local.to_3x3(), axis=Vector(tail) - Vector(head))
    return head, tail, roll


def edit_rest(armature):
    """Every bone's exact ``(head, tail, roll)``, read in Edit Mode.

    Returns ``None`` when the armature cannot be put in Edit Mode - hidden, or
    not in the view layer - and the export then reads the bones as near as it
    can.
    """
    context = bpy.context
    view_layer = context.view_layer
    if armature.name not in view_layer.objects or not armature.visible_get():
        return None
    if armature.mode == "EDIT":
        armature.update_from_editmode()
        return {bone.name: (tuple(bone.head), tuple(bone.tail), bone.roll)
                for bone in armature.data.edit_bones}
    previous_active = view_layer.objects.active
    previous_mode = armature.mode
    try:
        view_layer.objects.active = armature
        with context.temp_override(active_object=armature, object=armature,
                                   selected_objects=[armature]):
            bpy.ops.object.mode_set(mode="EDIT")
            rest = {bone.name: (tuple(bone.head), tuple(bone.tail), bone.roll)
                    for bone in armature.data.edit_bones}
            bpy.ops.object.mode_set(mode=previous_mode)
    except RuntimeError:
        return None
    finally:
        view_layer.objects.active = previous_active
    return rest


#: How far a posed bone may sit from its rest and still count as unposed.
#: A pose that moves nothing reads back bit for bit, so anything at all above
#: zero is somebody having posed the skeleton.
POSE_TOLERANCE = 1.0e-9


def is_posed(armature):
    """True while any bone has been posed away from its rest.

    Read from the transform on each bone - what posing a bone sets - rather
    than from where the bones have ended up. Where they end up is evaluated
    data, which is stale until the scene is worked out again, so a skeleton
    that has just been built would read as posed on the strength of a matrix
    nobody has filled in yet.

    A bone a constraint moves is not posed either. The one the game's own
    characters carry is the Track To on the neck, which is how a target frame
    is held in Blender: it is part of the model, and the head turning to
    follow a target is not a pose anybody typed.
    """
    pose = getattr(armature, "pose", None)
    if pose is None:
        return False
    for pose_bone in pose.bones:
        if max(abs(a - b)
               for row, rest_row in zip(pose_bone.matrix_basis,
                                        Matrix.Identity(4))
               for a, b in zip(row, rest_row)) > POSE_TOLERANCE:
            return True
    return False


def posed_matrices(armature):
    """``{bone name: where the pose puts it}``, in the armature's own terms.

    Worked out from what the pose holds rather than read off the bones: the
    export suspends every animation while it runs, and where a posed bone ends
    up is evaluated data, so reading it there would give back the rest it was
    suspended to. The arithmetic is Blender's own - a bone's place is its
    parent's place, then the step from the parent's rest to its own, then
    whatever the bone is posed by - so parents have to be done first.

    Everything that has to agree about where a posed bone is comes through
    here: the joints written for the file, and whatever hangs off them.
    """
    pose = getattr(armature, "pose", None)
    if pose is None:
        return {}
    placed = {}
    for bone in bones_parents_first(armature):
        pose_bone = pose.bones.get(bone.name)
        basis = (pose_bone.matrix_basis if pose_bone is not None
                 else Matrix.Identity(4))
        if bone.parent is None or bone.parent.name not in placed:
            placed[bone.name] = bone.matrix_local @ basis
        else:
            placed[bone.name] = (placed[bone.parent.name]
                                 @ bone.parent.matrix_local.inverted()
                                 @ bone.matrix_local @ basis)
    return placed


def pose_rest(armature):
    """Every bone's ``(head, tail, roll)`` as the pose leaves it.

    The same three numbers :func:`edit_rest` reads, taken from where the bones
    have been posed to rather than from where they rest, so a posed skeleton
    is written as the model's own - what is seen is what is written.
    """
    if getattr(armature, "pose", None) is None:
        return None
    rest = {}
    placed = posed_matrices(armature)
    for bone in bones_parents_first(armature):
        matrix = placed[bone.name]
        _axis, roll = bpy.types.Bone.AxisRollFromMatrix(matrix.to_3x3())
        rest[bone.name] = (tuple(matrix.translation),
                           tuple(matrix @ Vector((0.0, bone.length, 0.0))),
                           float(roll))
    return rest


class JointSpace:
    """One armature's joints as the file sees them.

    ``parent_of[name]`` says what a joint hangs from - ``("joint", bone name)``,
    ``("mesh", None)`` for the skinned mesh frame, or ``None`` for nothing.
    ``locals[name]`` is ``(location, rotation, scale)`` in that parent's
    terms, ``worlds[name]`` the joint's whole world matrix in armature space
    and ``frames[name]`` the world in 64 bits. The armature's own terms are
    its mesh frame's, so ``mesh_frame`` is nothing at all there;
    ``model_frame`` gives a joint's world in the model's terms.

    With *rest* - every bone's exact head, tail and roll, from
    :func:`edit_rest` - each joint's values are the 32-bit ones that place its
    bone exactly where it is (see :mod:`.joint_math`), so they come back the
    same however often a model goes round. Without it they are the nearest
    values, which is all a panel or an animation needs.
    """

    def __init__(self, armature, skin=None, rest=None, anchor=None):
        self.armature = armature
        self.skin = skin
        self.exact = rest is not None
        self.rest = rest or {}
        self.parent_of = {}
        self.locals = {}
        self.worlds = {}
        self.frames = {}
        self._mesh_frame = None
        self._anchor = anchor
        self._model_mesh_frame = None
        self._values = {}
        self._model_frames = {}
        for bone in bones_parents_first(armature):
            self._place(bone)

    # ── the mesh frame ────────────────────────────────────────────────────────
    @property
    def mesh_frame(self):
        """The skinned mesh's frame in the armature's terms: nothing at all,
        since the armature stands where it does."""
        if self._mesh_frame is None:
            self._mesh_frame = joint_math.Frame()
        return self._mesh_frame

    @property
    def anchor(self):
        """The frame the armature hangs on, in the model's terms: nothing, or
        the joint of another armature it is hung on."""
        if self._anchor is None:
            self._anchor = hung_frame(self.armature)
        return self._anchor

    @property
    def model_mesh_frame(self):
        """The skinned mesh's frame in the model's terms.

        A skin's binds are worked out there, from the joints' own values, the
        way the game's own exporter worked them - the same numbers measured
        from the mesh frame instead round a few of them the other way.
        """
        if self._model_mesh_frame is None:
            self._model_mesh_frame = (
                self.anchor.child(*mesh_frame_channels(self.armature))
                if self.skin is not None else self.anchor)
        return self._model_mesh_frame

    def model_frame(self, bone_name):
        """A joint's whole world in the model's terms, from its own values."""
        found = self._model_frames.get(bone_name)
        if found is None:
            parent = self.parent_of[bone_name]
            if parent is None:
                above = self.anchor
            elif parent[0] == "mesh":
                above = self.model_mesh_frame
            else:
                above = self.model_frame(parent[1])
            found = above.child(*self._values[bone_name])
            self._model_frames[bone_name] = found
        return found

    def mesh_offset(self, obj):
        """What takes *obj*'s own vertices into the mesh frame's terms.

        ``None`` where they are in those terms already: the mesh sitting
        exactly on its frame, as a model opened from a file does, so its
        vertices go out as they are, to the bit. Anywhere else - a mesh made
        with its origin at its feet, or at the world's - the vertices are
        brought over, and the mesh's origin makes no difference to the file.

        A LOD rides on the mesh it is a level of, so it is where that mesh
        is unless it has been moved off it.
        """
        skin = self.skin
        if obj is not skin:
            if (skin is not None and obj.parent is skin
                    and obj.parent_type == "OBJECT"
                    and _is_identity(obj.matrix_parent_inverse)
                    and _is_identity(obj.matrix_basis)):
                return self.mesh_offset(skin)
        elif (obj.parent is self.armature and obj.parent_type == "OBJECT"
              and _is_identity(obj.matrix_parent_inverse)
              and _is_identity(obj.matrix_basis)):
            return None
        return self.armature.matrix_world.inverted() @ obj.matrix_world

    def mesh_placement(self, obj):
        """Where *obj*'s own vertices sit in the armature's terms, as rows.

        The mesh frame's own 64-bit world where the mesh sits exactly on it,
        so measuring a model opened from a file gives exactly what the export
        measures; wherever else it sits, that place.
        """
        offset = self.mesh_offset(obj)
        if offset is None:
            return self.mesh_frame.matrix()
        return [list(row) for row in offset]

    def joints(self):
        """Every joint bone, parents first."""
        return bones_parents_first(self.armature)

    def below_mesh(self, bone):
        """True for a joint the skinned mesh skins with: every joint of an
        armature with one, since they all hang from its frame."""
        return self.skin is not None

    def _parent_frame(self, bone):
        parent = bone.parent
        if parent is not None:
            self._place(parent)
            return ("joint", parent.name), self.frames[parent.name]
        if self.skin is not None:
            return ("mesh", None), self.mesh_frame
        return None, joint_math.Frame()

    def parent_space(self, bone):
        """``(parent, world, rotation)`` a bone is measured against."""
        parent, frame = self._parent_frame(bone)
        return (parent, Matrix(frame.matrix()),
                Quaternion(joint_math.matrix_to_quat(frame.rotation)))

    def _place(self, bone):
        if bone.name in self.frames:
            return
        parent, parent_frame = self._parent_frame(bone)
        self.parent_of[bone.name] = parent
        head, tail, roll = (self.rest[bone.name] if bone.name in self.rest
                            else bone_rest(bone))
        if self.exact:
            location, rotation = joint_math.solve_joint(parent_frame, head,
                                                        tail, roll)
        else:
            location, rotation = joint_math.nearest_joint(parent_frame, head,
                                                          tail, roll)
        scale = tuple(joint_scale(self.armature, bone.name))
        frame = parent_frame.child(location, rotation, scale)
        self._values[bone.name] = (location, rotation, scale)
        self.locals[bone.name] = (Vector(location), Quaternion(rotation),
                                  Vector(scale))
        self.frames[bone.name] = frame
        self.worlds[bone.name] = Matrix(frame.matrix())


def hung_frame(armature):
    """The joint *armature* is hung on, in the model's terms, or nothing."""
    parent = armature.parent
    if (parent is None or parent.type != "ARMATURE"
            or armature.parent_type != "BONE"
            or armature.parent_bone not in parent.data.bones):
        return joint_math.Frame()
    held = JointSpace(parent, skinned_mesh_of(parent,
                                              list(bpy.context.scene.objects)))
    return held.model_frame(armature.parent_bone)


def _is_identity(matrix):
    return all(matrix[row][column] == (1.0 if row == column else 0.0)
               for row in range(4) for column in range(4))


#: How far from the default a character's origin may already stand and be
#: left exactly where it is. The game's own characters sit up to 5.2 cm from
#: the point their thighs give - 6.6 cm on the cow - each placed by hand, so
#: every one of them is left as it came.
ORIGIN_AT_HIPS = 0.1


def origin_at_hips(armature, place):
    """Whether *armature* already stands at its hips: near *place*, unturned.

    Unturned as every game character's mesh frame is; its scale is its own.
    """
    location, rotation, _scale = armature.matrix_basis.decompose()
    angle = rotation.angle
    return ((location - place).length <= ORIGIN_AT_HIPS
            and min(angle, 2.0 * math.pi - angle) <= 1.0e-6)


def posed_place(armature, bone):
    """Where *bone*'s joint sits in the armature's terms, pose and all.

    Worked out from what is set - each bone's rest and what it is posed by -
    rather than read from what Blender has worked out, which is left stale
    until the scene is brought up to date: a skeleton just changed reads at
    its old place. A bone a constraint moves is taken unconstrained, as the
    joints this is asked about carry none.
    """
    posed = getattr(armature, "pose", None)
    handle = posed.bones.get(bone.name) if posed is not None else None
    basis = handle.matrix_basis if handle is not None else Matrix.Identity(4)
    if bone.parent is None:
        return (bone.matrix_local @ basis).translation
    step = bone.parent.matrix_local.inverted() @ bone.matrix_local
    parent = posed_matrix(armature, bone.parent)
    return (parent @ step @ basis).translation


def posed_matrix(armature, bone):
    """*bone*'s whole place and turn in the armature's terms, pose and all."""
    posed = getattr(armature, "pose", None)
    handle = posed.bones.get(bone.name) if posed is not None else None
    basis = handle.matrix_basis if handle is not None else Matrix.Identity(4)
    if bone.parent is None:
        return bone.matrix_local @ basis
    step = bone.parent.matrix_local.inverted() @ bone.matrix_local
    return posed_matrix(armature, bone.parent) @ step @ basis


def default_mesh_origin(armature):
    """Where *armature* stands by default, in the space it stands in, or ``None``.

    Between the character's own thighs, placed from them the way Tommy's mesh
    origin is from his: 6.66 cm above the point midway between them and 3.9 mm
    forward. So it follows the character wherever it stands, whatever its
    size. ``None`` for a skeleton with no thighs to find it from.

    The thighs are taken where they are posed, not where they rest: a pose
    nobody keyed is the model, and the export writes the skeleton bent that
    way, so the origin belongs with it.
    """
    from ..common import convert
    from .character_preset import ORIGIN_FROM_THIGHS, THIGHS
    bones = armature.data.bones
    if any(name not in bones for name in THIGHS):
        return None
    world = armature.matrix_world
    # What the armature stands in: its parent, a joint it hangs on, or the
    # world - everything that places it except its own transform.
    holder = world @ armature.matrix_basis.inverted()
    middle = sum((world @ posed_place(armature, bones[name])
                  for name in THIGHS), Vector()) * 0.5
    return (holder.inverted() @ middle
            + Vector(convert.to_blender_vector(ORIGIN_FROM_THIGHS)))
