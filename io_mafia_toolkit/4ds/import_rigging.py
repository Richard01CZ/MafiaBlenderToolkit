"""The parts of a model that move: its skeleton, the mesh
bound to it, and its morph targets.

Split out of :mod:`.importer` for size; these are methods of the importer
and only make sense mixed into it.
"""


import bpy
from mathutils import Matrix, Quaternion, Vector

from ..common import constants as C
from ..common import convert
from .import_util import joint_display
from . import joint_math
from . import influence
from .joint_space import (JointSpace, edit_rest, set_mesh_frame,
                          settle_bones, skinned_mesh_of)
from .ops_morph import MORPH_VERTEX_GROUP_PREFIX


class RiggingMixin:
    """Part of :class:`~.importer.Importer`."""

    # ── stage 4: armatures ────────────────────────────────────────────────────
    def _world_matrix(self, frame_id, cache):
        """Accumulate a frame's world matrix from its parent chain.

        Every transform on the way down counts, joint scale included, because
        that is what the game chains: a joint scaled by 1.05 puts everything
        below it 5% further out.
        """
        if frame_id in cache:
            return cache[frame_id]
        local = self.local_matrices.get(frame_id)
        if local is None:
            return None
        cache[frame_id] = local            # guards against a malformed parent cycle
        parent = self.parent_of.get(frame_id)
        if parent:
            parent_world = self._world_matrix(parent, cache)
            if parent_world is not None:
                local = parent_world @ local
        cache[frame_id] = local
        return local

    def _world_rotation(self, frame_id, cache):
        """Chain quaternions only, so ancestor scale cannot skew the result."""
        if frame_id in cache:
            return cache[frame_id]
        transform = self.local_transforms.get(frame_id)
        if transform is None:
            cache[frame_id] = Quaternion()
            return cache[frame_id]
        cache[frame_id] = transform[1]
        parent = self.parent_of.get(frame_id)
        result = transform[1]
        if parent:
            result = self._world_rotation(parent, cache) @ transform[1]
        cache[frame_id] = result
        return result

    def _children_of(self):
        """``{frame id: [child frame ids]}``, each list in file order."""
        children = {}
        for frame_id in sorted(self.parent_of):
            children.setdefault(self.parent_of[frame_id], []).append(frame_id)
        return children

    def _joint_table(self, mesh_frame):
        """``{1-based joint number: frame id}`` for the joints a mesh skins with.

        The game gathers a skinned mesh's joints by walking down from the mesh
        itself, children in file order, and takes the first joint it meets for
        each number. A joint anywhere else takes no part, even one carrying a
        number a joint below the mesh also uses - I04Delnik01+ has one above
        its mesh, numbered the same as its left thigh.
        """
        children = self._children_of()
        numbers = {joint["frame_id"]: joint["joint_id"] for joint in self.joints}
        table = {}

        def walk(frame_id):
            for child in children.get(frame_id, ()):
                if child in numbers:
                    table.setdefault(numbers[child], child)
                walk(child)

        walk(mesh_frame)
        return table

    def _build_armatures(self):
        """The file's joints as armatures.

        The joints hanging from the skinned mesh are the character's armature,
        and the armature stands where the mesh's frame does: it holds that in
        its Delta Transform, and its bones are measured from it, as the joints
        are measured from the frame in the file. No bone stands for the frame.

        A 4DS chains joints through whatever it likes: a lone joint stands
        beside the skeleton in BobAut01, and the skinned mesh of I04Delnik01+
        hangs from a joint of its own. Joints not below the mesh are an
        armature of their own, at the model's origin; where the mesh hangs
        from one of them, the character's armature hangs on it, so the chain is
        the file's: that joint, then the mesh frame, then the skeleton.

        Bones sit where the game puts the joints, with every scale on the way
        down applied, so the skeleton Blender draws is the one the game plays.
        """
        joint_frames = [joint["frame_id"] for joint in self.joints]
        mesh_frames = []
        for _objects, _skin, frame_id in self.skin_jobs:
            if self._joint_table(frame_id) and frame_id not in mesh_frames:
                mesh_frames.append(frame_id)
        self.skin_frames = set(mesh_frames)
        if len(mesh_frames) > 1:
            names = ", ".join(f"'{self.objects_by_frame[f].name}'"
                              for f in mesh_frames[1:])
            self.report.warn(
                f"The file has {len(mesh_frames)} skinned meshes with joints "
                f"below them; the armature holds the frame of the first. The "
                f"joints below {names} hang from it instead.")
        children = self._children_of()
        below_mesh = set()

        def gather(frame_id):
            for child in children.get(frame_id, ()):
                below_mesh.add(child)
                gather(child)

        for frame_id in mesh_frames:
            gather(frame_id)
        holder = self.parent_of.get(mesh_frames[0]) if mesh_frames else None
        if holder not in set(joint_frames):
            holder = None

        # Worked out in 64 bits, the way the export reads them back (see
        # joint_math), so a joint's values place its bone the same way every
        # time a model is opened. *top* is the frame an armature hangs on,
        # which its bones are measured from.
        chain = set(mesh_frames) | set(joint_frames)
        frames = {}

        def frame_of(frame_id, top=None):
            key = (frame_id, top)
            if key not in frames:
                parent = self.parent_of.get(frame_id)
                above = (frame_of(parent, top)
                         if parent in chain and parent != top
                         else joint_math.Frame())
                location, rotation, scale = self.local_transforms[frame_id]
                frames[key] = above.child(tuple(location), tuple(rotation),
                                          tuple(scale))
            return frames[key]

        wanted = {joint["frame_id"]: joint["name"] for joint in self.joints}
        first_mesh = (self.objects_by_frame.get(mesh_frames[0])
                      if mesh_frames else None)
        skeleton = [f for f in joint_frames if f in below_mesh]
        loose = [f for f in joint_frames if f not in below_mesh]
        if first_mesh is not None and skeleton:
            plan = ([(f"{wanted[loose[0]]}_Armature", loose, None)]
                    if loose else [])
            plan.append((f"{first_mesh.name}_Armature", skeleton,
                         mesh_frames[0]))
        else:
            plan = [(f"{self.joints[0]['name']}_Armature", joint_frames, None)]

        self.armatures = [
            self._armature_of_joints(name, members, top, frame_of, mesh_frames,
                                     wanted)
            for name, members, top in plan]
        # The armature the skinned mesh binds to.
        armature = self.armature = self.armatures[-1]
        if first_mesh is not None and skeleton:
            # It stands where the mesh frame does: the file's own values for
            # the frame, in the terms of what the frame hangs from - the model,
            # or the joint the armature is hung on.
            location, rotation, scale = self.local_transforms[mesh_frames[0]]
            set_mesh_frame(armature, location, rotation, scale)
            if holder is not None:
                self._parent_to_bone(armature, holder)

        shape = self._joint_shape()
        for joint in self.joints:
            owner = self.armature_by_frame.get(joint["frame_id"])
            bone_name = self.bone_name_by_frame.get(joint["frame_id"])
            if owner is None or not bone_name or bone_name not in owner.pose.bones:
                continue
            pose_bone = owner.pose.bones[bone_name]
            pose_bone.cull_flags = joint["cull_flags"]
            pose_bone.user_props = joint["user_props"]
            transform = self.local_transforms.get(joint["frame_id"])
            # Seed unconditionally: the display driver reads this by path and
            # errors if it is absent.
            pose_bone["ls3d_joint_scale"] = (
                tuple(transform[2]) if transform else (1.0, 1.0, 1.0))
            # Joint scale is a 4DS property with no Blender equivalent: a bone
            # cannot hold scale at rest. Its effect on where the joints below
            # sit is already in the bone positions; the value itself only
            # drives how large the joint is drawn, and the export divides it
            # back out of the positions it writes.
            pose_bone.lock_scale = (True, True, True)
            if shape is not None:
                pose_bone.custom_shape = shape
                pose_bone.use_custom_shape_bone_size = False
            self._drive_joint_display(owner, bone_name)

        for frame_id in mesh_frames:
            mesh = self.objects_by_frame.get(frame_id)
            if mesh is not None:
                self.armature_by_mesh[mesh] = armature

    def _armature_of_joints(self, name, members, top, frame_of, mesh_frames,
                            wanted):
        """One armature holding the joints *members*, measured from *top*."""
        data = bpy.data.armatures.new(name)
        armature = bpy.data.objects.new(name, data)
        self.collection.objects.link(armature)

        previous_active = bpy.context.view_layer.objects.active
        bpy.context.view_layer.objects.active = armature
        try:
            bpy.ops.object.mode_set(mode="EDIT")

            edit_bones = {}
            for frame_id in members:
                bone = data.edit_bones.new(wanted[frame_id])
                bone.use_connect = False
                head, tail, roll = joint_math.bone_rest(frame_of(frame_id, top))
                bone.head = head
                bone.tail = tail
                bone.roll = roll
                edit_bones[frame_id] = bone

                # Blender may rename on collision or length; record what it
                # actually used so vertex groups and links match.
                self.bone_name_by_frame[frame_id] = bone.name
                # Objects hanging off a bone are measured against the joint's
                # whole world, scale included, which the bone cannot hold.
                self.bone_world_by_frame[frame_id] = Matrix(
                    frame_of(frame_id, top).matrix())
                self.armature_by_frame[frame_id] = armature
                if bone.name != wanted[frame_id]:
                    self.report.warn(
                        f"Joint '{wanted[frame_id]}' was renamed to "
                        f"'{bone.name}' by Blender; skinning follows the new "
                        f"name.")

            for frame_id, bone in edit_bones.items():
                parent = self.parent_of.get(frame_id)
                if parent in edit_bones:
                    bone.parent = edit_bones[parent]
                elif (parent is not None and parent not in mesh_frames
                      and parent != top):
                    self.report.warn(
                        f"Joint '{bone.name}' hangs from a "
                        f"{C.FRAME_TYPE_NAMES.get(self.frame_types.get(parent), 'frame')}"
                        f", which a Blender armature cannot hold between its "
                        f"bones. It stays where it is, but as a joint of its "
                        f"own; an export writes it with no parent.")
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
            bpy.context.view_layer.objects.active = previous_active
        return armature

    def _settle_bones(self):
        """Move each bone to where the joint values it exports put it.

        A bone is 32-bit, and some of a file's joints place one a rounding step
        from any bone their exported values can place exactly - posed joints,
        and some turned ones. The export writes the values nearest the bone, so
        the next import would build the bone a step away, and everything hung
        on it would move by that step once. Settled here instead, the bone is
        where every later import puts it, and the joint values written are the
        same either way.
        """
        for armature in self.armatures:
            self._settle(armature)

    def _settle(self, armature):
        settle_bones(armature)

    def _build_influence_boxes(self):
        """Give every joint its influence box: its own sixteen numbers.

        Kept on the joint as the file holds them, so a box nobody touches is
        written back to the bit.
        """
        for joint in self.joints:
            armature = self.armature_by_frame.get(joint["frame_id"])
            bone_name = self.bone_name_by_frame.get(joint["frame_id"])
            if (armature is None or not bone_name
                    or bone_name not in armature.data.bones):
                continue
            influence.set_file_box(armature, bone_name, joint["matrix"])

    def _joint_shape(self):
        """The shared empty used to draw joints as spheres."""
        return joint_display.shared_shape()

    def _drive_joint_display(self, armature, bone_name):
        """Drive the joint's custom-shape size from its ls3d_joint_scale."""
        joint_display.drive_display(armature, bone_name)

    # ── stage 5: skinning ─────────────────────────────────────────────────────
    def _apply_skinning(self):
        for objects, skin, frame_id in self.skin_jobs:
            armature = getattr(self, "armature", None)
            table = self._joint_table(frame_id)
            if armature is None or not table:
                self.report.warn(
                    f"Skinned mesh at frame {frame_id} has no joints below it; "
                    f"its joint weights were not applied.")
                continue
            # The mesh's own share of a vertex goes in a vertex group named
            # after the mesh, which its Armature modifier holds back from
            # moving - so a vertex half on a joint and half on the mesh moves
            # half as far, as it does in game.
            share = objects[0].name
            for level, lod in enumerate(skin.lods):
                if level >= len(objects):
                    break
                self._apply_skin_lod(objects[level], lod, armature, share,
                                     table, frame_id, is_primary=(level == 0))

    def _apply_skin_lod(self, obj, lod, armature, share, table, frame_id,
                        is_primary):
        """Rebuild vertex groups from the skin block.

        The block has no vertex indices: each bone group owns a contiguous run
        of the vertex buffer, unweighted vertices first, then vertices blended
        against the bone's parent, and finally a run bound to the mesh root.
        What a vertex keeps of the mesh frame goes in the *share* group.
        """
        weights = {}    # (vertex index, bone name) -> weight
        cursor = 0

        def add(vertex, bone_name, amount):
            if not bone_name or amount <= 0.0:
                return
            key = (vertex, bone_name)
            weights[key] = weights.get(key, 0.0) + amount

        def joint_name(number):
            joint_frame = table.get(number)
            return (self.bone_name_by_frame.get(joint_frame)
                    if joint_frame is not None else None)

        # One vertex group per bone group, in the file's order and empty ones
        # included: the order of a skinned mesh's vertex groups is the order
        # its joints are numbered in, which the game's files do not tie to the
        # order of the skeleton.
        for number in range(1, len(lod.groups) + 1):
            name = joint_name(number)
            if name and obj.vertex_groups.get(name) is None:
                obj.vertex_groups.new(name=name)

        for group_index, group in enumerate(lod.groups, start=1):
            bone_name = joint_name(group_index)
            parent_name = (joint_name(group.parent_group)
                           if group.parent_group else share)

            for _ in range(group.unweighted_count):
                add(cursor, bone_name, 1.0)
                cursor += 1
            for weight in group.weights:
                add(cursor, bone_name, weight)
                add(cursor, parent_name, 1.0 - weight)
                cursor += 1

        for _ in range(lod.root_unweighted):
            add(cursor, share, 1.0)
            cursor += 1

        copies = self.vertex_copies.get(obj.data, {})
        vertex_count = len(obj.data.vertices)
        if cursor != vertex_count - len(copies):
            self.report.warn(
                f"'{obj.name}': skin data covers {cursor} vertices but the mesh "
                f"has {vertex_count - len(copies)}; weights may be misaligned.")

        # A copy made for a face naming its vertex twice is that vertex, so it
        # is weighted the same.
        if copies:
            by_vertex = {}
            for (vertex, bone_name), amount in weights.items():
                by_vertex.setdefault(vertex, []).append((bone_name, amount))
            for copy, original in copies.items():
                for bone_name, amount in by_vertex.get(original, ()):
                    weights[(copy, bone_name)] = amount

        groups = {}
        for (vertex, bone_name), amount in weights.items():
            if vertex >= vertex_count:
                continue
            group = groups.get(bone_name)
            if group is None:
                group = obj.vertex_groups.get(bone_name) or obj.vertex_groups.new(
                    name=bone_name)
                groups[bone_name] = group
            group.add([vertex], min(amount, 1.0), "REPLACE")

        if is_primary:
            # Hung on the armature object, never on one of its bones: the
            # armature already moves every vertex, so a mesh that also rode a
            # bone would move twice. The armature stands where the mesh frame
            # does, so the mesh sits on it with nothing of its own, and its
            # vertices are the file's own numbers.
            obj.parent = armature
            obj.parent_type = "OBJECT"
            obj.parent_bone = ""
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.rotation_mode = "QUATERNION"
            obj.location = (0.0, 0.0, 0.0)
            obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            obj.scale = (1.0, 1.0, 1.0)
        modifier = next((m for m in obj.modifiers
                         if m.type == "ARMATURE" and m.object == armature), None)
        if modifier is None:
            modifier = obj.modifiers.new("Armature", "ARMATURE")
            modifier.object = armature
        if share in groups:
            modifier.vertex_group = share
            modifier.invert_vertex_group = True

    # ── stage 6: morphs ───────────────────────────────────────────────────────
    def _apply_morphs(self):
        for objects, morph, frame_id in self.morph_jobs:
            for level, lod in enumerate(morph.lods):
                if level >= len(objects):
                    break
                self._apply_morph_lod(objects[level], morph, lod,
                                      define_groups=(level == 0))

    def _apply_morph_lod(self, obj, morph, lod, define_groups):
        mesh = obj.data
        vertex_count = len(mesh.vertices)
        if define_groups:
            obj.ls3d_morph_groups.clear()

        copies = self.vertex_copies.get(mesh, {})
        copies_of = {}
        for copy, original in copies.items():
            copies_of.setdefault(original, []).append(copy)

        for region_index, region in enumerate(lod.regions, start=1):
            group = obj.ls3d_morph_groups.add() if define_groups else None
            if group is not None:
                group.name = f"Group{region_index}"
                group.vertex_group = f"{MORPH_VERTEX_GROUP_PREFIX}{region_index}"

            # The region's vertices, as a vertex group: the file lists them
            # whether they move or not, so which vertices move says nothing
            # about which belong.
            members = obj.vertex_groups.new(
                name=f"{MORPH_VERTEX_GROUP_PREFIX}{region_index}")
            listed = [v for v in region.indices if v < vertex_count]
            listed += [c for v in listed for c in copies_of.get(v, ())]
            if listed:
                members.add(listed, 1.0, "REPLACE")

            for target_index in range(morph.target_count):
                name = (f"G{region_index}_Basis" if target_index == 0
                        else f"G{region_index}_Target{target_index}")
                key = obj.shape_key_add(name=name, from_mix=False)
                key.value = 0.0

                # A new key starts at the mesh's own positions; the region's
                # vertices move. Each target's normals are not kept: the export
                # works them out from the target's shape (see the export's
                # morph block).
                positions = region.positions[target_index] if region.indices else ()
                for slot, vertex in enumerate(region.indices):
                    if vertex >= vertex_count:
                        continue
                    position = Vector(convert.to_blender_vector(positions[slot]))
                    key.data[vertex].co = position
                    for copy in copies_of.get(vertex, ()):
                        key.data[copy].co = position

                if group is not None:
                    entry = group.targets.add()
                    entry.shape_key_name = key.name
