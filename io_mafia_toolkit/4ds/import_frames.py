"""Turning the file's frames into objects, and hanging
them off one another the way the file says.

Split out of :mod:`.importer` for size; these are methods of the importer
and only make sense mixed into it.
"""


import bpy
from mathutils import Matrix

from ..common import constants as C
from ..common import convert
from ..common import describe
from .codec.material import has_diffuse_texture
from .ops_target import sync_track_to_constraints
from .import_util import _to_signed32
from .viewport import (LENS_FLARE_MARKER_SIZE, LIGHT_MARKER_SIZE,
                       PROJECTOR_MARKER_SIZE, update_viewport_display)


class FramesMixin:
    """Part of :class:`~.importer.Importer`."""

    # ── stage 2: frames ───────────────────────────────────────────────────────
    def _build_frames(self, doc):
        total = len(doc.frames)
        for index, frame in enumerate(doc.frames, start=1):
            kind = describe.frame_kind(frame.frame_type, frame.visual_type)
            if frame.frame_type in C.SKIPPED_FRAME_TYPES:
                self.report.item("Frame", index, total, f"{kind} - skipped")
            else:
                self.report.item("Frame", index, total, f"'{frame.name}' {kind}")
            self._build_frame(frame, index)
        self._report_skipped_frames()

    def _report_skipped_frames(self):
        if not self.skipped_frames:
            return
        spelled = ", ".join(
            f"{frame_id} ({C.FRAME_TYPE_NAMES[self.frame_types[frame_id]]})"
            for frame_id in sorted(self.skipped_frames)[:6])
        if len(self.skipped_frames) > 6:
            spelled += f" and {len(self.skipped_frames) - 6} more"
        message = (
            f"Skipped {len(self.skipped_frames)} Sound/Area frame(s): frame "
            f"{spelled}. The game builds these itself and reads nothing of one "
            f"from a file beyond its type and parent - no name, no position - so "
            f"there is nothing to import, and an export will not contain them.")
        if self.rehung_frames:
            message += (
                f" {len(self.rehung_frames)} frame(s) that hung from one now hang "
                f"from the frame above it instead, which leaves them where they "
                f"were: the game places a Sound or Area frame exactly on its "
                f"parent.")
        self.report.warn(message)

    def _build_frame(self, frame, frame_id):
        self.frame_types[frame_id] = frame.frame_type

        # A skipped frame sits exactly on its own parent, so whatever hangs from
        # it hangs from that parent instead, with nothing moved. Parents always
        # come before their children, so the chain is already resolved.
        parent_id = frame.parent_id
        while parent_id in self.skipped_frames:
            parent_id = self.skipped_frames[parent_id]

        if frame.frame_type in C.SKIPPED_FRAME_TYPES:
            self.skipped_frames[frame_id] = parent_id
            self.objects_by_frame[frame_id] = None
            return
        if parent_id != frame.parent_id:
            self.rehung_frames.append(frame_id)

        if parent_id:
            self.parent_of[frame_id] = parent_id

        location = convert.to_blender_vec(frame.position)
        scale = convert.to_blender_vec(frame.scale)
        rotation = convert.to_blender_quaternion(frame.rotation)
        self.local_transforms[frame_id] = (location, rotation, scale)
        self.local_matrices[frame_id] = Matrix.LocRotScale(location, rotation, scale)

        if frame.frame_type == C.FRAME_JOINT:
            self.joints.append({
                "frame_id": frame_id,
                "parent_frame_id": parent_id,
                "name": frame.name,
                "joint_id": frame.joint.joint_id + 1,   # stored 0-based
                "matrix": frame.joint.matrix,
                "cull_flags": frame.cull_flags,
                "user_props": frame.user_props,
            })
            self.objects_by_frame[frame_id] = None
            return

        obj = self._create_object(frame, frame_id)
        if obj is None:
            self.objects_by_frame[frame_id] = None
            return

        self.objects_by_frame[frame_id] = obj
        obj.ls3d_frame_type = str(frame.frame_type)
        obj.ls3d_frame_type_override = frame.frame_type
        obj.cull_flags = frame.cull_flags
        obj.ls3d_user_props = frame.user_props
        if frame.frame_type == C.FRAME_VISUAL:
            obj.visual_type = str(frame.visual_type)
            obj.render_flags = frame.render_flags
            obj.render_flags2 = frame.render_flags2
        update_viewport_display(obj)

    def _create_object(self, frame, frame_id):
        ftype = frame.frame_type

        if ftype == C.FRAME_VISUAL:
            if frame.visual_type == C.VISUAL_LENSFLARE:
                return self._create_lens_flare(frame)
            if frame.visual_type == C.VISUAL_PROJECTOR:
                return self._create_projector(frame)
            mesh = bpy.data.meshes.new(frame.name or "visual")
            obj = bpy.data.objects.new(frame.name or "visual", mesh)
            self.collection.objects.link(obj)
            if frame.visual_type == C.VISUAL_MIRROR:
                self._apply_mirror(obj, frame.mirror)
            elif frame.geometry is not None:
                self._apply_geometry(obj, frame, frame_id)
            if frame.billboard is not None:
                self._apply_billboard(obj, frame.billboard)
            if frame.skin is not None:
                self.skin_jobs.append((self.lod_objects.get(frame_id, [obj]),
                                       frame.skin, frame_id))
            if frame.morph is not None and frame.morph.target_count:
                self.morph_jobs.append((self.lod_objects.get(frame_id, [obj]),
                                        frame.morph, frame_id))
            return obj

        if ftype in (C.FRAME_SECTOR, C.FRAME_OCCLUDER):
            mesh = bpy.data.meshes.new(frame.name or "mesh")
            obj = bpy.data.objects.new(frame.name or "mesh", mesh)
            self.collection.objects.link(obj)
            if ftype == C.FRAME_SECTOR:
                self._apply_sector(obj, frame.sector)
            else:
                self._apply_occluder(obj, frame.occluder)
            return obj

        if ftype == C.FRAME_EMITOR:
            # The mesh here is the particle, not the emitter, but it arrives in
            # the same block a visual's geometry does and becomes an ordinary
            # mesh object - LODs, materials and all.
            mesh = bpy.data.meshes.new(frame.name or "emitor")
            obj = bpy.data.objects.new(frame.name or "emitor", mesh)
            self.collection.objects.link(obj)
            if frame.geometry is not None:
                self._apply_geometry(obj, frame, frame_id)
            return obj

        if ftype in (C.FRAME_DUMMY, C.FRAME_TARGET):
            obj = bpy.data.objects.new(frame.name or "empty", None)
            self.collection.objects.link(obj)
            if ftype == C.FRAME_DUMMY:
                obj.empty_display_type = "CUBE"
                obj.bbox_min = convert.to_blender_vector(frame.dummy.bbox_min)
                obj.bbox_max = convert.to_blender_vector(frame.dummy.bbox_max)
                extent = max(
                    abs(v) for v in list(obj.bbox_min) + list(obj.bbox_max)) or 0.1
                obj.empty_display_size = extent
            else:
                obj.empty_display_type = "PLAIN_AXES"
                obj.ls3d_target_flags = frame.target.flags
                self.target_jobs.append((obj, frame.target))
            return obj

        if ftype == C.FRAME_LIGHT:
            # A cone pointing the way it shines, because what a light mostly
            # needs showing is which way it faces - the engine reads its
            # direction straight off the frame, there being no direction field
            # in the payload.
            obj = bpy.data.objects.new(frame.name or "light", None)
            self.collection.objects.link(obj)
            obj.empty_display_type = C.LIGHT_EMPTY_DISPLAY
            obj.empty_display_size = LIGHT_MARKER_SIZE
            self._apply_light(obj, frame.light)
            return obj

        if ftype in C.PAYLOADLESS_FRAME_TYPES:
            # Nothing but a transform and a name, so nothing but an empty.
            obj = bpy.data.objects.new(frame.name or "frame", None)
            self.collection.objects.link(obj)
            obj.empty_display_type = "PLAIN_AXES"
            return obj

        self.report.warn(
            f"Frame {frame_id} ('{frame.name}') has unsupported type "
            f"{C.FRAME_TYPE_NAMES.get(ftype, ftype)} and was skipped.")
        return None

    # ── stage 3: instances ────────────────────────────────────────────────────
    def _resolve_instances(self):
        """Point instanced frames at the mesh they reuse.

        An instance frame stores only the frame id of its source. Leaving that
        unresolved produced an empty mesh, and re-exporting wrote the emptiness
        back - 173 objects in the shipping game data are instances.
        """
        for obj, source_frame in self.instance_jobs:
            source = self.objects_by_frame.get(source_frame)
            if source_frame in self.skipped_frames:
                self.report.warn(
                    f"'{obj.name}' instances frame {source_frame}, a Sound or "
                    f"Area frame the import skipped. It has no mesh to share, so "
                    f"this imports empty - the game cannot take a mesh from "
                    f"one either.")
                continue
            if source is None or source.type != "MESH" or not source.data.vertices:
                self.report.warn(
                    f"'{obj.name}' instances frame {source_frame}, which has no "
                    f"mesh - it will import empty.")
                continue
            # The placeholder mesh created for this frame is left for Blender to
            # collect rather than removed here. Explicitly freeing a datablock
            # the instant it loses its last user has proven able to crash
            # Blender later on; an unused mesh disappears on the next save and
            # reload anyway.
            # Sharing the mesh is the whole of it: the export and the object
            # panel both work out which frame owns a shared mesh for themselves,
            # so nothing about the file's instancing is kept.
            obj.data = source.data
        if self.instance_jobs:
            self.report.substage(f"Linked {len(self.instance_jobs)} instanced mesh(es)")

    # ── other visual payloads ─────────────────────────────────────────────────
    def _apply_billboard(self, obj, billboard):
        # Axis and lock are independent fields; both are kept so both can be
        # written back out unchanged.
        obj.rot_mode = "2" if billboard.axis_locked else "1"
        obj.rot_axis = {0: "1", 1: "2", 2: "3"}.get(billboard.axis, "3")

    def _create_lens_flare(self, frame):
        obj = bpy.data.objects.new(frame.name or "lensflare", None)
        self.collection.objects.link(obj)
        obj.empty_display_type = "SPHERE"
        obj.empty_display_size = LENS_FLARE_MARKER_SIZE
        if frame.lens_flare:
            for number, glow in enumerate(frame.lens_flare.glows, 1):
                entry = obj.ls3d_glows.add()
                entry.name = f"Glow {number}"
                entry.position = glow.position
                if 0 < glow.material_id < len(self.materials):
                    entry.material = self.materials[glow.material_id]
                else:
                    self.report.warn(
                        f"Lens flare '{obj.name}' element {number} names "
                        f"material {glow.material_id}, which the file does not "
                        f"have.")
        return obj

    def _apply_light(self, obj, light):
        """Every field the light block carries, straight onto the object."""
        if light is None:
            return
        obj.ls3d_light_type_value = _to_signed32(light.light_type)
        obj.ls3d_light_mode = _to_signed32(light.mode)
        obj.ls3d_light_power = light.power
        obj.ls3d_light_color = light.color
        obj.ls3d_light_range_near = light.range_near
        obj.ls3d_light_range_far = light.range_far
        obj.ls3d_light_cone_inner = light.cone_inner
        obj.ls3d_light_cone_outer = light.cone_outer

        if (light.light_type & 0xFFFFFFFF) >= C.LIGHT_TYPE_COUNT:
            self.report.warn(
                f"Light '{obj.name}' is type {light.light_type}, which is not "
                f"one of the nine the engine knows. It is carried through "
                f"unchanged, and in game a light like that does nothing.")

        if light.range_far < light.range_near:
            self.report.warn(
                f"Light '{obj.name}' fades out at {light.range_far:g} before it "
                f"starts at {light.range_near:g}, so the band it fades across "
                f"runs backwards.")

    def _create_projector(self, frame):
        """A projector is a volume and a material, so it imports as an empty.

        The volume itself is not stored anywhere: it is a fixed unit shape that
        the frame's own transform carries, so the object needs nothing beyond
        its transform for the viewport to outline it correctly.
        """
        obj = bpy.data.objects.new(frame.name or "projector", None)
        self.collection.objects.link(obj)
        obj.empty_display_type = "ARROWS"
        # Set once, here. The viewport sync deliberately leaves it alone, so a
        # projector you resize stays the size you left it.
        obj.empty_display_size = PROJECTOR_MARKER_SIZE
        projector = frame.projector
        if projector is None:
            return obj

        obj.ls3d_projector_orthogonal = projector.orthogonal
        obj.ls3d_projector_mode = projector.mode & 0xFF
        if (projector.mode & 0xFF) not in C.PROJECTOR_MODES:
            self.report.warn(
                f"Projector '{obj.name}' has mode {projector.mode}, which is "
                f"none of the six the game knows. It behaves as mode 1 there "
                f"- constant falloff, alpha blended - and that is what the "
                f"panel shows. The stored value is kept unless you change the "
                f"falloff or the blend mode.")

        if 0 < projector.material_id < len(self.materials):
            material = self.materials[projector.material_id]
            obj.ls3d_projector_material = material
            if material.ls3d_diffuse_tex is None or not has_diffuse_texture(
                    material.ls3d_material_flags & 0xFFFFFFFF):
                self.report.warn(
                    f"Projector '{obj.name}' uses material '{material.name}', "
                    f"which writes no diffuse texture. A projector paints with "
                    f"that texture and nothing else, so this one draws "
                    f"nothing.")
        else:
            self.report.warn(
                f"Projector '{obj.name}' names material "
                f"{projector.material_id}, which the file does not have. It "
                f"will paint nothing until a material is picked.")
        return obj

    def _apply_mirror(self, obj, mirror):
        obj.bbox_min = convert.to_blender_vector(mirror.bbox_min)
        obj.bbox_max = convert.to_blender_vector(mirror.bbox_max)
        obj.ls3d_mirror_color = mirror.tint
        obj.ls3d_mirror_range = mirror.draw_distance

        vertices = [convert.to_blender_vector(v) for v in mirror.vertices]
        faces = [convert.face_to_blender(f) for f in mirror.faces
                 if max(f) < len(vertices)]
        obj.data.from_pydata(vertices, [], faces)
        obj.data.update()

        # The view box, column by column: its center and its three axes. Only
        # moved between the file's axes and Blender's, so it comes back to
        # the bit - an object's transform would hold it as a location, a
        # rotation and a scale, and give it back a little different each time.
        m = mirror.view_matrix
        obj.ls3d_mirror_box_x = (m[0], m[2], m[1])
        obj.ls3d_mirror_box_y = (m[8], m[10], m[9])
        obj.ls3d_mirror_box_z = (m[4], m[6], m[5])
        obj.ls3d_mirror_box_center = (m[12], m[14], m[13])

    def _apply_sector(self, obj, sector):
        obj.ls3d_frame_type = str(C.FRAME_SECTOR)
        obj.ls3d_sector_flags1 = sector.flags1
        obj.ls3d_sector_flags2 = sector.flags2

        vertices = [convert.to_blender_vector(v) for v in sector.vertices]
        faces = []
        dropped = 0
        for face in sector.faces:
            a, b, c = convert.face_to_blender(face)
            if max(a, b, c) >= len(vertices):
                dropped += 1
                continue
            faces.append((a, b, c))
        if dropped:
            self.report.warn(
                f"Sector '{obj.name}': dropped {dropped} face(s) referring to "
                f"vertices outside the mesh.")
        obj.data.from_pydata(vertices, [], faces)
        obj.data.update()

        for index, portal in enumerate(sector.portals, start=1):
            self._create_portal(obj, portal, index)

    def _create_portal(self, sector_obj, portal, index):
        mesh = bpy.data.meshes.new(f"{sector_obj.name}_portal{index}")
        obj = bpy.data.objects.new(mesh.name, mesh)
        self.collection.objects.link(obj)
        obj.parent = sector_obj
        obj.matrix_parent_inverse = Matrix.Identity(4)

        vertices = [convert.to_blender_vector(v) for v in portal.vertices]
        # Rewound like every other face crossing the axis swap, so the portal
        # arrives facing into its sector - the way the file's plane points -
        # rather than backwards.
        face = ([tuple(convert.flip_ring(range(len(vertices))))]
                if len(vertices) >= 3 else [])
        mesh.from_pydata(vertices, [], face)
        mesh.update()

        obj.ls3d_frame_type = str(C.FRAME_SECTOR)
        obj.ls3d_portal_flags = portal.flags
        obj.ls3d_portal_near = portal.near_range
        obj.ls3d_portal_far = portal.far_range
        obj.ls3d_portal_normal = convert.to_blender_vector(portal.plane_normal)
        obj.ls3d_portal_dot = portal.plane_offset
        update_viewport_display(obj)

    def _apply_occluder(self, obj, occluder):
        vertices = [convert.to_blender_vector(v) for v in occluder.vertices]
        faces = [convert.face_to_blender(f) for f in occluder.faces
                 if max(f) < len(vertices)]
        obj.data.from_pydata(vertices, [], faces)
        obj.data.update()

    # ── stage 7: hierarchy ────────────────────────────────────────────────────
    def _apply_hierarchy(self):
        bone_parented = set()

        for frame_id, parent_id in self.parent_of.items():
            if frame_id == parent_id:
                continue
            child = self.objects_by_frame.get(frame_id)
            if child is None:
                continue
            if frame_id in self.skin_frames:
                # A skinned mesh is hung on the armature by the skinning, and
                # its place in the chain is the armature's setting.
                continue

            if self.frame_types.get(parent_id) == C.FRAME_JOINT:
                if self._parent_to_bone(child, parent_id):
                    location, rotation, scale = self.local_transforms[frame_id]
                    child.rotation_mode = "QUATERNION"
                    child.location = location
                    child.rotation_quaternion = rotation
                    child.scale = scale
                    bone_parented.add(frame_id)
                continue

            parent = self.objects_by_frame.get(parent_id)
            if parent is None:
                # The parent frame produced no object (an unsupported type, or a
                # joint with no armature). Dropping the link silently would move
                # the child to the scene root without explanation.
                self.report.warn(
                    f"'{child.name}' is parented to frame {parent_id}, which "
                    f"produced no object; it stays unparented.")
                continue
            child.parent = parent
            if self.frame_types.get(frame_id) == C.FRAME_SECTOR:
                # Sector geometry is absolute, so a sector must sit at its
                # stored coordinates no matter what its parent's transform is.
                # Canceling the parent here keeps the hierarchy link (which has
                # to be written back out) without displacing the mesh.
                parent_world = self._world_matrix(parent_id, {})
                child.matrix_parent_inverse = (
                    parent_world.inverted() if parent_world else Matrix.Identity(4))
            else:
                child.matrix_parent_inverse = Matrix.Identity(4)

        for frame_id, transform in self.local_transforms.items():
            if frame_id in bone_parented:
                continue
            obj = self.objects_by_frame.get(frame_id)
            if obj is None:
                continue
            location, rotation, scale = transform
            obj.rotation_mode = "QUATERNION"
            obj.location = location
            obj.rotation_quaternion = rotation
            obj.scale = scale

    def _parent_to_bone(self, obj, bone_frame):
        armature = self.armature_by_frame.get(bone_frame)
        bone_name = self.bone_name_by_frame.get(bone_frame)
        if armature is None or not bone_name:
            return False
        if bone_name not in armature.data.bones:
            return False
        bone = armature.data.bones[bone_name]
        obj.parent = armature
        obj.parent_type = "BONE"
        obj.parent_bone = bone_name

        # Blender computes a bone-parented child as
        #     world = bone.matrix_local @ Translation(0, length, 0) @ MPI @ basis
        # while the format's own convention is
        #     world = bone_world_with_scale @ file_local_transform
        #
        # The format composes a frame against its parent using row vectors,
        # which transposes to parent_world @ local here. A frame's local matrix
        # is the rotation with each basis row scaled by the matching scale
        # component and the translation left alone - exactly
        # Matrix.LocRotScale.
        # Solving for MPI gives the expression below. The last term matters:
        # bones sit at their scale-free rest position, so without it every
        # object hanging off a bone whose chain carries a non-unit joint scale
        # lands in the wrong place - hands, eyes and weapon dummies on the
        # high-detail character models are exactly that case.
        scaled_world = self.bone_world_by_frame.get(bone_frame)
        tail_offset = Matrix.Translation((0.0, -bone.length, 0.0))
        if scaled_world is not None:
            obj.matrix_parent_inverse = (
                tail_offset @ bone.matrix_local.inverted() @ scaled_world)
        else:
            obj.matrix_parent_inverse = tail_offset
        return True

    # ── stage 8: targets ──────────────────────────────────────────────────────
    def _resolve_targets(self):
        for obj, target in self.target_jobs:
            for frame_id in target.links:
                linked = self.objects_by_frame.get(frame_id)
                bone_name = self.bone_name_by_frame.get(frame_id)
                if linked is not None:
                    entry = obj.ls3d_target_objects.add()
                    entry.name = linked.name
                    entry.target_object = linked
                elif bone_name:
                    armature = self.armature_by_frame.get(frame_id)
                    if armature is None:
                        continue
                    entry = obj.ls3d_target_objects.add()
                    entry.name = bone_name
                    entry.target_armature = armature
                    entry.bone_name = bone_name
                elif frame_id in self.skipped_frames:
                    self.report.warn(
                        f"Target '{obj.name}' links to frame {frame_id}, a "
                        f"Sound or Area frame the import skipped, so that link "
                        f"is dropped.")
                else:
                    self.report.warn(
                        f"Target '{obj.name}' links to frame {frame_id}, "
                        f"which was not imported.")
            sync_track_to_constraints(obj)
