"""Putting a model's frames in the order the file wants,
numbering them, and building each one.

Split out of :mod:`.exporter` for size; these are methods of the exporter
and only make sense mixed into it.
"""

import os
import re

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..common import constants as C
from ..common import convert
from .codec.types import Frame, Geometry, Morph, Skin
from .ops_lensflare import glow_entries
from .viewport import carries_frame
from .joint_space import (NO_DELTA, JointSpace, delta_channels,
                          mesh_frame_channels, numbered_joints,
                          posed_matrices, skinned_mesh_of)
from .export_util import (_DUPLICATE_SUFFIX_RE, _IDENTITY_TRANSFORM, _LOD_RE,
                          _MATERIALLESS_FRAME_TYPES, _PORTAL_RE, _RESERVED_RE,
                          _int_prop)
from .validation import (find_armature, validate_character_name,
                         validate_lens_flare, validate_morph_groups,
                         validate_projector, validate_skin_weights,
                         validate_skinned_mesh, validate_vertex_groups)

_LOD_RE = re.compile(C.LOD_SUFFIX_PATTERN, re.IGNORECASE)
_PORTAL_RE = re.compile(C.PORTAL_SUFFIX_PATTERN, re.IGNORECASE)
_RESERVED_RE = re.compile(C.RESERVED_MATERIAL_PATTERN)
#: Frame types written as bare geometry - a hull or an occlusion volume, with
#: no face groups to carry a material id. A material left on one of these used
#: to enter the file's material table anyway, so the game loaded its texture
#: for something that can never draw it.
_MATERIALLESS_FRAME_TYPES = (C.FRAME_SECTOR, C.FRAME_OCCLUDER)

#: Blender's uniquifying suffix, e.g. "WOOD.BMP.001".
_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")

#: (location, rotation, scale) for a frame that carries no transform.
_IDENTITY_TRANSFORM = (Vector((0.0, 0.0, 0.0)),
                       Quaternion((1.0, 0.0, 0.0, 0.0)),
                       Vector((1.0, 1.0, 1.0)))


class ExportError(Exception):
    """Raised when the scene cannot be exported. Nothing has been written."""


def texture_filename(image):
    """The name to write for *image*.

    Prefers the real file name over the datablock name: Blender appends ``.001``
    when a name collides, and writing ``WOOD.BMP.001`` gives the engine a
    texture it cannot find.
    """
    if image is None:
        return ""
    if image.filepath:
        base = os.path.basename(image.filepath_from_user() or image.filepath)
        if base:
            return base.upper()
    return _DUPLICATE_SUFFIX_RE.sub("", image.name).upper()


#: How far a transform worked out through matrices may be from the object's own
#: location, rotation and scale and still be those, rounded.
CHANNEL_TOLERANCE = 1e-5


def _channels(obj):
    """An object's own location, rotation and scale, exactly as set.

    Its Delta Transform counts too, added the way Blender adds it: an object
    with an animation keeps where it rests there, beneath the channels the
    animation keys. With no delta, the channels are written as they are.
    """
    if obj.rotation_mode == "QUATERNION":
        rotation = obj.rotation_quaternion.copy()
    elif obj.rotation_mode == "AXIS_ANGLE":
        angle, *axis = obj.rotation_axis_angle
        rotation = Quaternion(Vector(axis), angle)
    else:
        rotation = obj.rotation_euler.to_quaternion()
    location, scale = obj.location.copy(), obj.scale.copy()
    delta_location, delta_rotation, delta_scale = delta_channels(obj)
    if (delta_location, delta_rotation, delta_scale) == NO_DELTA:
        return location, rotation, scale
    return (Vector(delta_location) + location,
            Quaternion(delta_rotation) @ rotation,
            Vector(tuple(a * b for a, b in zip(delta_scale, scale))))


def _is_identity(matrix):
    return all(matrix[row][column] == (1.0 if row == column else 0.0)
               for row in range(4) for column in range(4))


def _own_channels_if_same(obj, worked_out):
    """*worked_out*, or the object's own values when they are the same.

    The parent compensation a transform is worked out through leaves the
    result a bit or two away from the values it came from. Where it lands on
    the object's own location, rotation and scale, those are written instead.
    """
    return _channels_if_same(_channels(obj), worked_out)


def _channels_if_same(own, worked_out):
    """*worked_out*, or *own* - values as set - when the two are the same."""
    location, rotation, scale = worked_out
    own_location, own_rotation, own_scale = own
    if own_rotation.magnitude == 0.0:
        return worked_out
    turned = own_rotation.normalized().rotation_difference(rotation).angle
    turned = min(turned, 2.0 * 3.141592653589793 - turned)
    if ((location - own_location).length <= CHANNEL_TOLERANCE
            and (scale - own_scale).length <= CHANNEL_TOLERANCE
            and turned <= CHANNEL_TOLERANCE
            and abs(own_rotation.magnitude - 1.0) <= CHANNEL_TOLERANCE):
        return own_location, own_rotation, own_scale
    return worked_out


class FramesMixin:
    """Part of :class:`~.exporter.Exporter`."""

    # ── frame ordering ────────────────────────────────────────────────────────
    def _is_skin_mesh(self, obj):
        return (obj.type == "MESH"
                and _int_prop(obj, "ls3d_frame_type") == C.FRAME_VISUAL
                and _int_prop(obj, "visual_type") in C.SKINNED_VISUAL_TYPES)

    def _armature_of(self, obj):
        return find_armature(obj)

    def _order_frames(self):
        """Put every frame in write order: see :meth:`_walk_frames`."""
        scene_names = {o.name for o in bpy.context.scene.objects}
        candidates = [
            obj for obj in self.source_objects
            if obj.name in scene_names
            and obj.type in ("MESH", "EMPTY", "ARMATURE")
            and obj not in self.lod_members
            and not self._is_portal(obj)
            and not self._is_motion_track(obj)
            and not self._is_shadow(obj)
            and carries_frame(obj)
        ]
        candidate_set = set(candidates)

        # An empty whose shape names no frame is a helper somebody put in the
        # scene. Left out rather than guessed at - it used to be written as a
        # dummy, which put a box in the model for something that was never
        # meant to be in it.
        passed_by = sorted(
            obj.name for obj in self.source_objects
            if obj.name in scene_names and obj.type == "EMPTY"
            and obj not in candidate_set and not carries_frame(obj)
            and not self._is_motion_track(obj))
        if passed_by:
            listed = ", ".join(f"'{name}'" for name in passed_by[:4])
            if len(passed_by) > 4:
                listed += f" and {len(passed_by) - 4} more"
            self.report.warn(
                f"{len(passed_by)} empty(s) were left out: {listed}. An empty "
                f"stands for a frame by its shape, and theirs names none.",
                fix="Set Display As to Cube for a dummy, Sphere for a lens "
                    "flare, Arrows for a projector, or Plain Axes for a target.")

        # Frames go out in the order the scene holds them. Blender keeps a
        # collection's objects in the order they were added, so anything built
        # afterwards follows what was there. The order decides which of several
        # objects sharing a mesh is written first: an instance can only point
        # backwards, and sorting 'SCTR_0psch' ahead of 'SCTR_hlhala' by name cost
        # MISE08-HOTEL a whole duplicated LOD chain.
        LAST = 1 << 30
        scene_order = {obj: i for i, obj
                       in enumerate(bpy.context.scene.objects)}
        ordered = sorted(candidates,
                         key=lambda obj: (scene_order.get(obj, LAST), obj.name))

        skin_meshes = [o for o in ordered if self._is_skin_mesh(o)]

        if len(skin_meshes) > 1:
            names = ", ".join(f"'{o.name}'" for o in skin_meshes)
            self.fail(
                f"A 4DS file supports one Single Mesh / Single Morph, but "
                f"{len(skin_meshes)} were found: {names}.",
                fix="Keep one skinned mesh per file, or merge them.")
        for mesh in skin_meshes:
            if self._armature_of(mesh) is None:
                self.fail(
                    f"'{mesh.name}' is set to "
                    f"{C.VISUAL_TYPE_NAMES[_int_prop(mesh, 'visual_type')]} but "
                    f"is not bound to an armature.",
                    fix="Parent it to an armature or give it an Armature modifier, "
                        "or change its visual type to 'Object'.")

        self.ordered = ordered
        self._candidate_set = set(ordered)
        self.frame_order = self._walk_frames(ordered)
        self._promote_instance_sources(self.frame_order)

    def _parent_entry(self, obj):
        """The frame *obj* hangs from, as the walk names it, or ``None``.

        A joint is ``(armature, bone name)``. A skinned mesh hangs from what
        its armature hangs from - nothing, or a joint of another armature. Only
        frames that are being written count.
        """
        armature = self._skin_armature(obj)
        if armature is not None:
            return self._parent_entry(armature)
        parent = obj.parent
        if parent is None or parent not in self._candidate_set:
            return None
        if obj.parent_type == "BONE" and obj.parent_bone:
            return self._bone_entry(parent, obj.parent_bone)
        if parent.type == "ARMATURE":
            return None
        return parent

    def _bone_entry(self, armature, bone_name):
        if armature.data.bones.get(bone_name) is None:
            return None
        return (armature, bone_name)

    def _walk_frames(self, ordered):
        """Every frame in write order: objects, and a joint for every bone.

        Down the scene's own order, a frame goes out as soon as the frame it
        hangs from has. Whatever hangs from a frame follows it straight away -
        its joints first, in the armature's order, then the objects the walk
        has already passed, in the scene's order - so a frame is never written
        before its parent and never waits longer than it has to. Joints hanging
        from nothing go out where their armature sits in the scene.

        The game's files keep to exactly this: laid out in the scene the way
        the import lays them out, all 290 of its models with joints come out
        in the order they shipped in.
        """
        written = set()
        reached = set()
        order = []
        joint_children = {}
        object_children = {}
        roots = {}

        for armature in (o for o in ordered if o.type == "ARMATURE"):
            space = self._joint_space(armature)
            anchor = self._parent_entry(armature)
            for bone in space.joints():
                entry = (armature, bone.name)
                parent = space.parent_of[bone.name]
                if parent is None and anchor is not None:
                    # An armature hung on a frame hangs its free joints there.
                    joint_children.setdefault(anchor, []).append(entry)
                elif parent is None:
                    roots.setdefault(armature, []).append(entry)
                elif parent[0] == "mesh":
                    if space.skin in self._candidate_set:
                        joint_children.setdefault(space.skin, []).append(entry)
                    else:
                        roots.setdefault(armature, []).append(entry)
                else:
                    joint_children.setdefault((armature, parent[1]),
                                              []).append(entry)
        for obj in ordered:
            if obj.type == "ARMATURE":
                continue
            parent = self._parent_entry(obj)
            if parent is not None:
                object_children.setdefault(parent, []).append(obj)

        def write(entry):
            stack = [entry]
            while stack:
                current = stack.pop()
                if current in written:
                    continue
                written.add(current)
                order.append(current)
                follow = [child for child in joint_children.get(current, ())
                          if child not in written]
                follow += [child for child in object_children.get(current, ())
                           if child in reached and child not in written]
                stack.extend(reversed(follow))

        for obj in ordered:
            if obj.type == "ARMATURE":
                for root in roots.get(obj, ()):
                    write(root)
                continue
            reached.add(obj)
            parent = self._parent_entry(obj)
            if parent is None or parent in written:
                write(obj)

        # Anything still waiting hangs from something that never went out,
        # which a scene can only do through a parenting loop. Written last
        # rather than dropped.
        for obj in ordered:
            if obj.type != "ARMATURE" and obj not in written:
                write(obj)
        return order

    def _promote_instance_sources(self, ordered):
        """Put the LOD-richest user of a shared mesh ahead of the others.

        An instance reference borrows the source frame's whole LOD chain and can
        only point backwards, so whichever object is written first owns the
        geometry. If a plain copy happens to be ordered before the object that
        carries the LODs, that copy becomes the source, the LOD-carrying object
        has to be written out in full, and one instance is lost. Moving the
        richest user to the first slot its group occupies fixes that — unless
        the move would place it before its own parent, in which case the group
        is left alone rather than producing a forward parent reference.
        """
        position = {obj: i for i, obj in enumerate(ordered)}
        groups = {}
        for obj in ordered:
            if isinstance(obj, tuple) or obj.type != "MESH" or not self._is_instanceable(obj):
                continue
            chain = self.lods.get(obj, [obj])
            groups.setdefault(chain[0].data, []).append(obj)

        for members in groups.values():
            if len(members) < 2:
                continue
            depth = lambda o: len(self.lods.get(o, [o]))
            richest = max(members, key=lambda o: (depth(o), -position[o]))
            earliest = min(members, key=lambda o: position[o])
            if richest is earliest or depth(richest) <= depth(earliest):
                continue

            target = position[earliest]
            parent = self._parent_entry(richest)
            if parent is not None and position.get(parent, -1) >= target:
                parent_name = parent[1] if isinstance(parent, tuple) else parent.name
                self.report.warn(
                    f"'{richest.name}' carries {depth(richest)} LODs and shares "
                    f"its mesh with {len(members) - 1} other object(s), but its "
                    f"parent '{parent_name}' forces it later in the file than "
                    f"'{earliest.name}'. '{earliest.name}' carries a copy of "
                    f"the mesh, with the same LOD levels, and the others "
                    f"reference that.",
                    fix=f"Parent '{richest.name}' to something earlier if you "
                        f"want a single copy of the mesh in the file.")
                # That frame is going to own the geometry whether we like it or
                # not, so hand it the full LOD chain. Without this it writes
                # only its own single level and every frame referencing it
                # inherits one LOD - four of the five 'sloupk' pillars in
                # MISE08-HOTEL used to drop from six levels to one.
                self.lods[earliest] = list(self.lods[richest])
                continue

            ordered.remove(richest)
            ordered.insert(target, richest)
            position = {obj: i for i, obj in enumerate(ordered)}

    def _is_instanceable(self, obj):
        # Visual frames only, and that is load-bearing rather than tidy: the
        # game resolves an instance by casting the source frame to a mesh
        # object and reading the pointer at its own mesh offset. An emitor
        # keeps its mesh somewhere else entirely, so naming one as a source
        # would have the game read a particle setting as a mesh pointer.
        if _int_prop(obj, "ls3d_frame_type") != C.FRAME_VISUAL:
            return False
        visual_type = _int_prop(obj, "visual_type")
        return (visual_type in C.GEOMETRY_VISUAL_TYPES
                and visual_type not in C.SKINNED_VISUAL_TYPES
                and visual_type not in C.MORPH_VISUAL_TYPES)

    def _is_portal(self, obj):
        return bool(
            obj.type == "MESH"
            and _int_prop(obj, "ls3d_frame_type") == C.FRAME_SECTOR
            and obj.parent
            and _int_prop(obj.parent, "ls3d_frame_type") == C.FRAME_SECTOR
            and _PORTAL_RE.search(obj.name))

    def _is_motion_track(self, obj):
        """The movement empty is an authoring aid, never a frame of its own."""
        from ..packages import module
        is_motion_track = module("tck.io").is_motion_track
        return is_motion_track(obj)

    def _is_shadow(self, obj):
        """Shadow pieces belong to the .6ds, and the empty holding them.

        The shadow stands in for the model when the engine casts from it, so
        writing those meshes into the model as well would draw them twice.
        """
        from ..packages import module
        return module("6ds.io").is_shadow_piece(obj)

    # ── frame ids ─────────────────────────────────────────────────────────────
    def _joint_space(self, armature):
        """The file's view of *armature*'s joints, worked out once per export."""
        space = self._joint_spaces.get(armature)
        if space is None:
            skin = skinned_mesh_of(armature, self.ordered or self.source_objects)
            # Hung on a joint of another armature: that joint's world, worked
            # out as exactly as its own armature's joints are.
            holder = armature.parent
            anchor = (self._joint_space(holder).model_frame(armature.parent_bone)
                      if holder is not None and holder.type == "ARMATURE"
                      and armature.parent_type == "BONE"
                      and armature.parent_bone in holder.data.bones else None)
            space = JointSpace(armature, skin, self.rest.get(armature), anchor)
            self._joint_spaces[armature] = space
        return space

    def _skin_armature(self, obj):
        """The exported armature whose joints *obj* skins with, or ``None``."""
        if not self._is_skin_mesh(obj):
            return None
        armature = self._armature_of(obj)
        return armature if armature in self.ordered else None

    def _assign_frame_ids(self):
        """Number every frame in write order, and every skin's joints.

        A skin numbers the joints hanging from its mesh in the order of the
        mesh's vertex groups - the game's files do not number them in the order
        of the skeleton - and any such joint with no vertex group after those,
        in the armature's order.
        """
        for frame_id, entry in enumerate(self.frame_order, start=1):
            self.frame_ids[entry] = frame_id
        for armature in (o for o in self.ordered if o.type == "ARMATURE"):
            space = self._joint_space(armature)
            if space.skin is None or space.skin not in self._candidate_set:
                self.joint_maps[armature] = {}
                continue
            self.joint_maps[armature] = numbered_joints(space)
        self.report.info(f"Frames: {len(self.frame_order)}")

    def _bone_frame_id(self, armature, bone_name):
        """The frame a bone stands for."""
        return self.frame_ids.get((armature, bone_name), 0)

    def _parent_id(self, obj):
        armature = self._skin_armature(obj)
        if armature is not None:
            # The mesh hangs from what its armature hangs from.
            return self._parent_id(armature)
        parent = obj.parent
        if parent is None:
            return 0
        if obj.parent_type == "BONE" and obj.parent_bone:
            return self._bone_frame_id(parent, obj.parent_bone)
        if parent.type == "ARMATURE":
            return 0        # the armature itself is not a frame
        return self.frame_ids.get(parent, 0)

    def _scaled_bone_world(self, armature, bone_name):
        """A bone's world matrix the way the format chains it: T@R@S per joint.

        Blender keeps bones at their scale-free rest position so the skeleton
        reads cleanly, so its own bone matrix is not what a child is measured
        against. Cached per armature - the walk is the same one the bind
        matrices use.
        """
        world = self._joint_space(armature).worlds.get(bone_name)
        if world is None:
            return None
        return armature.matrix_world @ world

    def _bone_anchor(self, armature, bone_name, bone):
        """Where a bone's children hang from: its rest, or its pose.

        A skeleton nobody has posed keeps its rest, which is read exactly and
        writes back exactly. A posed one hands over the pose, so that what
        hangs off a bone goes into the file where the viewport shows it.
        """
        if armature not in self.posed:
            return bone.matrix_local
        posed = self._posed_matrices.get(armature)
        if posed is None:
            posed = posed_matrices(armature)
            self._posed_matrices[armature] = posed
        return posed.get(bone_name, bone.matrix_local)

    def _local_transform(self, obj):
        """The frame's transform relative to its parent frame.

        Where the object's own location, rotation and scale are that transform
        they are written as they are - to the bit, with the rotation's sign and
        length as set. Worked out through matrices instead, the same transform
        comes back a bit or two off and with its rotation turned positive.
        """
        armature = self._skin_armature(obj)
        if armature is not None:
            # The skinned mesh's frame is where its armature stands; where the
            # mesh object itself sits makes no difference.
            return self._mesh_frame_local(armature)
        parent = obj.parent
        if parent is None:
            return _channels(obj)
        held = self._skin_armature(parent)
        if held is not None and obj.parent_type == "OBJECT":
            # Hung on a skinned mesh, so hanging from its frame - which is
            # where the mesh object sits only if it sits on it exactly.
            offset = self._joint_space(held).mesh_offset(parent)
            if offset is not None:
                return _own_channels_if_same(
                    obj, (offset @ obj.matrix_parent_inverse
                          @ obj.matrix_basis).decompose())
        if obj.parent_type == "BONE" and obj.parent_bone:
            # Measured against the joint's *scaled* world, which is what the
            # engine chains the child onto. Against Blender's unscaled bone the
            # child lands short by the accumulated joint scale - on TommyHIGH
            # every hand and weapon dummy exported at scale 1.0 instead of
            # 0.95184, and up to 40 mm out of position.
            bone = parent.data.bones.get(obj.parent_bone)
            world = self._scaled_bone_world(parent, obj.parent_bone)
            if world is not None and bone is not None:
                # Built from the object's own transform rather than read off
                # matrix_world: the add-on hangs Track To constraints on the
                # objects a target frame links to, and matrix_world has those
                # baked in. Tommy's eyes track a target and would otherwise
                # export rotated 2.8 degrees off their authored rest.
                #
                # Off the same bone the joint itself is written from - posed
                # where the skeleton is posed. Measured against the bone's
                # rest while the joint went out posed, a weapon dummy would
                # land where the hand no longer is.
                anchor = self._bone_anchor(parent, obj.parent_bone, bone)
                placed = (parent.matrix_world @ anchor
                          @ Matrix.Translation((0.0, bone.length, 0.0))
                          @ obj.matrix_parent_inverse @ obj.matrix_basis)
                return _own_channels_if_same(
                    obj, (world.inverted() @ placed).decompose())
            if bone is not None:
                return (Matrix.Translation((0.0, bone.length, 0.0))
                        @ obj.matrix_parent_inverse
                        @ obj.matrix_basis).decompose()
            return (obj.matrix_parent_inverse @ obj.matrix_basis).decompose()
        if parent.type == "ARMATURE" or parent in self.frame_ids:
            # The object's own transform, not a world round trip: inverting the
            # parent's world matrix and decomposing the product loses about
            # 80 um on a mission scene, and bakes in any constraint the add-on
            # put on the object.
            if _is_identity(obj.matrix_parent_inverse):
                return _channels(obj)
            return _own_channels_if_same(
                obj, (obj.matrix_parent_inverse @ obj.matrix_basis).decompose())
        return obj.matrix_world.decompose()

    def _mesh_frame_local(self, armature):
        """A skinned mesh's frame, as the file holds it.

        It is where the armature stands, which the armature keeps in its Delta
        Transform - its own location, rotation and scale are at rest, the
        check has seen to that. Those values are written as they are set; an
        armature hung on a joint is worked out against that joint's world, and
        still gives them back where it sits on it as the import hangs it.
        """
        location, rotation, scale = mesh_frame_channels(armature)
        own = (Vector(location), Quaternion(rotation), Vector(scale))
        if self._parent_entry(armature) is None:
            return own
        return _channels_if_same(own, self._local_transform(armature))

    # ── frame construction ────────────────────────────────────────────────────
    def _build_frame(self, obj):
        if isinstance(obj, tuple):
            return self._build_joint_frame(*obj)
        frame_type = _int_prop(obj, "ls3d_frame_type")
        if frame_type == C.FRAME_SECTOR:
            # Sector and portal geometry is written in world space, so the frame
            # transform must stay identity or the engine would offset it twice.
            # This matches every sector frame in the shipping missions.
            location, rotation, scale = _IDENTITY_TRANSFORM
        else:
            location, rotation, scale = self._local_transform(obj)

        frame = Frame(
            frame_type=frame_type,
            parent_id=self._parent_id(obj),
            position=convert.to_mafia_vector(location),
            scale=convert.to_mafia_vector(scale),
            rotation=convert.to_mafia_quaternion(rotation),
            cull_flags=_int_prop(obj, "cull_flags") & 0xFF,
            name=obj.name,
            user_props=obj.ls3d_user_props,
        )

        if frame_type == C.FRAME_VISUAL:
            frame.visual_type = _int_prop(obj, "visual_type")
            frame.render_flags = _int_prop(obj, "render_flags") & 0xFF
            frame.render_flags2 = _int_prop(obj, "render_flags2") & 0xFF
            # The bit says the visual is one of the engine's object family, so
            # it follows from the type rather than from a choice: every visual
            # the game ships carries it except its nine mirrors. Getting it
            # wrong is worse than cosmetic - the game would read the frame's
            # own fields as if they were an object's mesh pointer, and follow
            # whatever that lands on. Mirrors and projectors are the two visual
            # kinds outside that family.
            if frame.visual_type in (C.VISUAL_MIRROR, C.VISUAL_PROJECTOR):
                frame.render_flags2 &= ~C.LF_IS_MESH_OBJECT
            else:
                frame.render_flags2 |= C.LF_IS_MESH_OBJECT
            self._build_visual_payload(frame, obj)
        elif frame_type == C.FRAME_SECTOR:
            frame.sector = self._build_sector(obj)
        elif frame_type == C.FRAME_DUMMY:
            frame.dummy = self._build_dummy(obj)
        elif frame_type == C.FRAME_TARGET:
            frame.target = self._build_target(obj)
        elif frame_type == C.FRAME_OCCLUDER:
            frame.occluder = self._build_occluder(obj)
        elif frame_type == C.FRAME_EMITOR:
            frame.geometry = self._build_emitor(obj)
        elif frame_type == C.FRAME_LIGHT:
            frame.light = self._build_light(obj)
        elif frame_type in C.PAYLOADLESS_FRAME_TYPES:
            pass                # the header the frame already carries is all
        else:
            self.fail(f"'{obj.name}' has frame type "
                      f"{C.FRAME_TYPE_NAMES.get(frame_type, frame_type)}, "
                      f"which cannot be written.")
        return frame

    def _build_visual_payload(self, frame, obj):
        visual_type = frame.visual_type

        if visual_type == C.VISUAL_LENSFLARE:
            validate_lens_flare(obj, glow_entries(obj), self.fail,
                                self.report.warn)
            frame.lens_flare = self._build_lens_flare(obj)
            return
        if visual_type == C.VISUAL_MIRROR:
            frame.mirror = self._build_mirror(obj)
            return
        if visual_type == C.VISUAL_PROJECTOR:
            validate_projector(obj, self.fail, self.report.warn)
            frame.projector = self._build_projector(obj)
            return
        if visual_type not in C.GEOMETRY_VISUAL_TYPES:
            self.fail(f"'{obj.name}' has visual type "
                      f"{C.VISUAL_TYPE_NAMES.get(visual_type, visual_type)}, "
                      f"which cannot be written.")
            return

        chain = self.lods.get(obj, [obj])

        if visual_type in C.SKINNED_VISUAL_TYPES:
            validate_skinned_mesh(
                obj, visual_type == C.VISUAL_SINGLEMORPH,
                C.VISUAL_TYPE_NAMES.get(visual_type, "Skinned mesh"), self.fail)
            validate_character_name(
                obj, C.VISUAL_TYPE_NAMES.get(visual_type, "Skinned mesh"),
                self.report.warn)
            validate_vertex_groups(obj, self._armature_of(obj), self.report.warn)
            # Weight rules are checked before the mesh is evaluated, because the
            # auto-fixes rewrite vertex groups in place.
            for lod_obj in chain:
                validate_skin_weights(
                    lod_obj, self.report, self.fail,
                    fix_multi_influences=self.fix_multi_influences,
                    fix_non_parent_child=self.fix_non_parent_child)

        # Objects sharing a mesh datablock are the same geometry, and the format
        # can say so with an instance reference instead of a second copy. The
        # first frame to use a chain owns it; later frames point at that id and
        # carry no payload of their own. Skinned and morph frames are excluded
        # because their skin and morph blocks are per-frame and an instance
        # reference leaves no room for them.
        # An instance reference reuses the source frame's geometry whole, LOD
        # chain included, so it is only safe for an object that has no LODs of
        # its own. An object with its own LOD children is written out in full
        # even if its base mesh is shared.
        if self._is_instanceable(obj):
            key = chain[0].data
            source = self.instance_sources.get(key)
            if source is not None and len(chain) == 1:
                frame.geometry = Geometry(instance_id=source)
                if visual_type == C.VISUAL_BILLBOARD:
                    # The billboard block belongs to the frame, not the mesh, so
                    # an instanced billboard still carries its own axis.
                    frame.billboard = self._build_billboard(obj)
                return
            if source is None:
                self.instance_sources[key] = self.frame_ids[obj]

        armature = self._armature_of(obj) if visual_type in C.SKINNED_VISUAL_TYPES else None
        geometry, mappings, skin_lods = self._build_geometry(obj, chain, armature)
        frame.geometry = geometry

        if visual_type in C.SKINNED_VISUAL_TYPES:
            frame.skin = Skin(lods=skin_lods)
        if visual_type in C.MORPH_VISUAL_TYPES:
            validate_morph_groups(
                obj, chain, C.VISUAL_TYPE_NAMES.get(visual_type, "Morph"),
                self.fail, self.report.warn)
            self._refuse_posed_morph(obj, armature)
            frame.morph = self._build_morph(obj, chain, mappings, geometry.lods)
        if visual_type == C.VISUAL_BILLBOARD:
            frame.billboard = self._build_billboard(obj)
