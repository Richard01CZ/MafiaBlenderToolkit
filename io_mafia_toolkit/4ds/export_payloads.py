"""Building what hangs off a frame: geometry, morphs,
and the frame kinds that are not meshes at all.

Split out of :mod:`.exporter` for size; these are methods of the exporter
and only make sense mixed into it.
"""

import os
import re

import bmesh
from mathutils import Quaternion, Vector

from ..common import constants as C
from ..common import convert
from .codec.types import (Billboard, Dummy, Frame, Geometry, Glow, Joint, LOD,
                          LensFlare, Light, Mirror, Morph, MorphLOD,
                          MorphRegion, Occluder, Portal, Projector, Sector,
                          Target)
from . import influence
from .mesh import (_f32, build_lod, target_shading, bounds_center, bounds_radius,
                   joint_parents, portal_plane)
from .ops_lensflare import glow_entries
from .export_util import (_DUPLICATE_SUFFIX_RE, _IDENTITY_TRANSFORM, _LOD_RE,
                          _MATERIALLESS_FRAME_TYPES, _PORTAL_RE, _RESERVED_RE,
                          _empty_skin_lod, _float_prop, _int_prop,
                          _triangulated, evaluated_mesh, in_frame,
                          undeformed_mesh)
from .joint_space import shares_of
from .validation import (validate_light, validate_mirror_facing,
                         validate_mirror_view_box, validate_occluder,
                         validate_portal_flags, validate_portal_on_hull,
                         validate_portal_outline, validate_sector,
                         validate_sector_flags)

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


#: The rotation axis a billboard stores, by the identifier the panel uses.
#: A plain lookup rather than one with a fallback: a value outside this is not
#: something to guess a substitute for.
BILLBOARD_AXES = {"1": 0, "2": 1, "3": 2}

#: How close to identity a sector's world matrix counts as identity.
SECTOR_IDENTITY_TOLERANCE = 1e-6


class PayloadsMixin:
    """Part of :class:`~.exporter.Exporter`."""

    # ── geometry ──────────────────────────────────────────────────────────────
    def _build_geometry(self, obj, chain, armature):
        geometry = Geometry()
        mappings = []
        skin_lods = []

        joint_map = self.joint_maps.get(armature) if armature else None
        skin_matrix = None
        bone_worlds = None
        parents = None
        if armature is not None and armature in self.ordered:
            space = self._joint_space(armature)
            skin_matrix = space.model_mesh_frame
            bone_worlds = {bone.name: space.model_frame(bone.name)
                           for bone in space.joints()}
            if joint_map is not None:
                parents = joint_parents(armature, joint_map)

        # A skinned mesh is written at rest, and at rest the armature moves
        # nothing - so it is read straight from its own data rather than run
        # through a rest pose that only rounds it. A posed skeleton is another
        # matter: the model is what is on screen, so the mesh goes out bent
        # the way the pose bends it.
        posed = armature is not None and armature in self.posed
        for level, lod_obj in enumerate(chain):
            # A skinned mesh's vertices are written in its frame's terms,
            # wherever the mesh object's origin happens to be.
            offset = (self._joint_space(armature).mesh_offset(lod_obj)
                      if armature is not None and armature in self.ordered
                      else None)
            with (undeformed_mesh(lod_obj) if armature is not None and not posed
                  else evaluated_mesh(lod_obj,
                                      None if posed else armature)) as read, \
                    in_frame(read, offset) as mesh:
                if mesh is None or not mesh.vertices:
                    # The format has two kinds of empty: a frame with no LOD
                    # blocks at all and a frame with a LOD that holds nothing.
                    # They look the same in Blender. A mesh with nothing in it
                    # and no further levels is written as the first, the way
                    # 35 of the game's 40 empty meshes are; an empty level in
                    # a chain of them as the second.
                    if len(chain) == 1:
                        self.report.warn(
                            f"'{lod_obj.name}' has no geometry and was written "
                            f"as a frame with no mesh.")
                    else:
                        self.report.warn(
                            f"'{lod_obj.name}' has no geometry and was written "
                            f"as an empty LOD.")
                        geometry.lods.append(
                            LOD(distance=_float_prop(lod_obj, "ls3d_lod_dist")))
                    mappings.append({})
                    continue

                box_weights = (self._box_weights(lod_obj, mesh, armature,
                                                 joint_map, parents)
                               if armature is not None and joint_map else None)
                lod, mapping, skin_lod = build_lod(
                    lod_obj, mesh,
                    distance=_float_prop(lod_obj, "ls3d_lod_dist"),
                    material_index_of=lambda slot: self._slot_material_id(lod_obj, slot),
                    joint_map=joint_map,
                    skin_mesh_matrix=skin_matrix,
                    bone_world_matrices=bone_worlds,
                    parents=parents,
                    weight_override=box_weights,
                )

            if len(lod.vertices) > C.MAX_VERTICES_PER_LOD:
                self.fail(
                    f"'{lod_obj.name}' has {len(lod.vertices)} vertices after "
                    f"UV and normal splitting, over the 4DS limit of "
                    f"{C.MAX_VERTICES_PER_LOD}.",
                    fix="Reduce the polygon count or merge UV islands.")

            geometry.lods.append(lod)
            mappings.append(mapping)
            skin_lods.append(skin_lod or _empty_skin_lod())

        return geometry, mappings, skin_lods

    def _slot_material_id(self, obj, slot):
        if 0 <= slot < len(obj.material_slots):
            return self._material_id(obj.material_slots[slot].material)
        return 0

    # ── morphs ────────────────────────────────────────────────────────────────
    def _refuse_posed_morph(self, obj, armature):
        """A posed morph mesh would pop the moment a target blends.

        A morph target is written as whole positions, shaped against the mesh
        at rest. Bend the mesh with a pose and the two are in different places:
        the model would draw bent and snap back to its rest shape as soon as a
        target came in. Rather than write that, the export stops.
        """
        if armature is None or armature not in self.posed:
            return False
        if not len(obj.ls3d_morph_groups):
            return False
        self.fail(f"'{obj.name}' has morph groups and its armature is posed. "
                  f"Morph targets are shaped against the mesh at rest, so the "
                  f"posed mesh and its targets would not be in the same "
                  f"place.",
                  fix="Clear the pose before exporting, or apply it as the "
                      "rest pose so the targets are shaped against it.")
        return True

    def _morph_offset(self, obj, lod_obj):
        """What takes a skinned LOD's shapes into its mesh frame's terms."""
        armature = self._skin_armature(obj)
        if armature is None:
            return None
        return self._joint_space(armature).mesh_offset(lod_obj)

    def _build_morph(self, obj, chain, mappings, lods):
        """Build the morph block from the object's morph groups.

        Every region is written with the same target count, so shorter groups
        are padded with their own basis. A group names the vertex group holding
        its region; one that names none takes the vertices its targets move,
        and a vertex moved by more than one such group goes to whichever moved
        it most: the engine applies regions in order and the last write wins,
        so overlapping regions would let a later region's basis overwrite an
        earlier region's pose.

        A Single Morph with no morph groups at all writes a morph block with no
        targets, as thirteen of the game's models do.
        """
        groups = obj.ls3d_morph_groups
        target_count = max((len(g.targets) for g in groups), default=0)
        if target_count == 0:
            return Morph()

        morph = Morph(target_count=target_count)
        all_positions = []

        for level, lod_obj in enumerate(chain):
            keys = lod_obj.data.shape_keys
            if keys is None:
                continue
            if not any(keys.key_blocks.get(t.shape_key_name)
                       for g in groups for t in g.targets):
                continue

            mapping = mappings[level] if level < len(mappings) else {}
            morph_lod, positions = self._build_morph_lod(
                lod_obj, groups, target_count, mapping, lods[level],
                self._morph_offset(obj, lod_obj))
            morph.lods.append(morph_lod)
            all_positions.extend(positions)

        if not morph.lods:
            return Morph()

        if all_positions:
            xs = [p[0] for p in all_positions]
            ys = [p[1] for p in all_positions]
            zs = [p[2] for p in all_positions]
            morph.bbox_min = (min(xs), min(ys), min(zs))
            morph.bbox_max = (max(xs), max(ys), max(zs))
            morph.center = bounds_center(morph.bbox_min, morph.bbox_max)
            morph.radius = bounds_radius(morph.bbox_min, morph.bbox_max)
        return morph

    def _build_morph_lod(self, lod_obj, groups, target_count, mapping, file_lod,
                         offset=None):
        mesh = lod_obj.data

        def placed(co):
            return offset @ co if offset is not None else co
        keys = mesh.shape_keys
        vertex_count = len(mesh.vertices)

        resolved = []       # per group: list of key blocks, padded to target_count
        movement = []       # per group: {vertex index: largest component delta}
        listed = []         # per group: vertices of its vertex group, or None

        for group in groups:
            basis = (keys.key_blocks.get(group.targets[0].shape_key_name)
                     if group.targets else None)
            blocks = [keys.key_blocks.get(t.shape_key_name) or basis
                      for t in group.targets]
            blocks += [basis] * (target_count - len(blocks))
            resolved.append(blocks)

            vertex_group = (lod_obj.vertex_groups.get(group.vertex_group)
                            if group.vertex_group else None)
            if vertex_group is not None:
                index = vertex_group.index
                listed.append({v.index for v in mesh.vertices
                               if any(g.group == index and g.weight > 0.0
                                      for g in v.groups)})
                movement.append({})
                continue
            listed.append(None)

            deltas = {}
            if basis is not None:
                for block in blocks[1:]:
                    if block is None or block is basis:
                        continue
                    for index in range(vertex_count):
                        reference = basis.data[index].co
                        moved = block.data[index].co
                        delta = max(abs(reference.x - moved.x),
                                    abs(reference.y - moved.y),
                                    abs(reference.z - moved.z))
                        if delta > 0.0 and delta > deltas.get(index, 0.0):
                            deltas[index] = delta
            movement.append(deltas)

        owner = {}
        for group_index, deltas in enumerate(movement):
            for vertex, delta in deltas.items():
                current = owner.get(vertex)
                if current is None or delta > movement[current].get(vertex, 0.0):
                    owner[vertex] = group_index

        members = [set() for _ in groups]
        for vertex, group_index in owner.items():
            members[group_index].add(vertex)
        for group_index, vertices in enumerate(listed):
            if vertices is not None:
                members[group_index] = vertices

        source_of = {}
        for vertex, file_indices in mapping.items():
            for file_index in file_indices:
                source_of.setdefault(file_index, vertex)

        morph_lod = MorphLOD()
        positions_seen = []

        # Every target's shape, all regions moved to it at once, in the file's
        # own vertices and faces; its normals are worked out from that.
        file_indices = [
            sorted({file_index for vertex in members[group_index]
                    for file_index in mapping.get(vertex, [])})
            for group_index in range(len(groups))]
        target_normals = []
        for target in range(target_count):
            positions = [vertex.position for vertex in file_lod.vertices]
            for group_index, blocks in enumerate(resolved):
                block = blocks[target]
                if block is None:
                    continue
                for file_index in file_indices[group_index]:
                    positions[file_index] = convert.to_mafia_vector(
                        placed(block.data[source_of[file_index]].co))
            target_normals.append(target_shading(file_lod, positions))

        for group_index, blocks in enumerate(resolved):
            region = MorphRegion()
            buffer_indices = sorted(
                {file_index
                 for vertex in members[group_index]
                 for file_index in mapping.get(vertex, [])})
            if not buffer_indices:
                morph_lod.regions.append(region)
                continue

            region.indices = buffer_indices
            region.positions = [[] for _ in range(target_count)]
            region.normals = [[] for _ in range(target_count)]

            for file_index in buffer_indices:
                vertex = source_of[file_index]
                for target, block in enumerate(blocks):
                    if block is not None:
                        position = convert.to_mafia_vector(
                            placed(block.data[vertex].co))
                    else:
                        position = convert.to_mafia_vector(
                            placed(mesh.vertices[vertex].co))
                    region.positions[target].append(position)
                    positions_seen.append(position)

                    # Each target shades as its own shape does, not as the
                    # mesh does: 154402 of the game's 156578 target normals
                    # differ between targets, by up to a full flip.
                    region.normals[target].append(target_normals[target][file_index])

            morph_lod.regions.append(region)

        return morph_lod, positions_seen

    # ── other payloads ────────────────────────────────────────────────────────
    def _build_billboard(self, obj):
        """Write both billboard fields.

        The axis is stored independently of the lock flag, so it must be written
        whichever rotation mode is selected: 127 billboard frames across 28
        shipping files carry axis 2 with the lock *clear*, which is the single
        most common combination in the game. Forcing the axis to 0 whenever the
        lock was clear silently rewrote all of them.
        """
        axis = BILLBOARD_AXES[obj.rot_axis]
        locked = obj.rot_mode == "2"
        return Billboard(axis=axis, axis_locked=locked)

    def _build_lens_flare(self, obj):
        """Write every element of the flare, in list order.

        Order is not cosmetic: the game draws the first element larger and at
        full brightness and dims the rest, so the list is written as it stands.
        """
        flare = LensFlare()
        for position, material in glow_entries(obj):
            flare.glows.append(Glow(
                position=position,
                material_id=self._material_id(material)))
        return flare

    def _build_projector(self, obj):
        """The projector's four bytes: its shape, its mode and its material.

        Nothing about the volume is written - the game rebuilds it from the
        frame's transform - so this is the whole payload.

        The material id is checked here as well as in the panel, because the
        two can disagree: a material can be picked and still miss the table,
        and a projector that names material 0 is not one that draws nothing.
        The game subtracts one and follows the result without a bounds check,
        so a 0 reads a pointer out of the word in front of the table and
        writes a reference count through it.
        """
        material = obj.ls3d_projector_material
        material_id = self._material_id(material)
        # A projector with no material at all is reported by the validation
        # pass, which has better words for it; this is the other way to end up
        # with a zero - a material that is picked but never reached the table.
        if material is not None and material_id < 1:
            self.fail(
                f"Projector '{obj.name}' uses material '{material.name}', "
                f"which did not reach the model's material table, so it would "
                f"be written as material {material_id}.",
                fix="Pick a material that this scene actually uses, or assign "
                    "it to a mesh as well.")
        return Projector(
            orthogonal=bool(obj.ls3d_projector_orthogonal),
            mode=_int_prop(obj, "ls3d_projector_mode") & 0xFF,
            material_id=material_id,
        )

    def _build_dummy(self, obj):
        """The dummy's box, kept as imported.

        An empty carries a single half-size, so anything derived from it is
        centered and cubic. 146 of the 19323 dummies the game ships are neither
        - one runs (-6.888, -13.131, -6.888) to (19.668, 13.131, 20.664) - and
        deriving the box puts that one 6.65 m out. Those are the ones the
        viewport outlines, since the empty cannot draw them either.
        """
        low = Vector(obj.bbox_min)
        high = Vector(obj.bbox_max)
        if low.length_squared == 0.0 and high.length_squared == 0.0:
            # Never imported and never edited: fall back to the empty's size,
            # which is all a box authored from scratch in Blender can offer.
            size = obj.empty_display_size
            low = Vector((-size, -size, -size))
            high = Vector((size, size, size))
        return Dummy(bbox_min=convert.to_mafia_vector(low),
                     bbox_max=convert.to_mafia_vector(high))

    def _build_target(self, obj):
        target = Target(flags=_int_prop(obj, "ls3d_target_flags") & 0xFFFF)
        for entry in obj.ls3d_target_objects:
            if entry.target_armature and entry.bone_name:
                frame_id = self.frame_ids.get((entry.target_armature, entry.bone_name))
            else:
                linked = entry.target_object
                frame_id = self.frame_ids.get(linked) if linked else None
            if frame_id is None:
                self.report.warn(
                    f"Target '{obj.name}' links to '{entry.name}', which is not "
                    f"being exported; that link was dropped.")
                continue
            target.links.append(frame_id)
        return target

    def _build_light(self, obj):
        """The light's forty bytes, exactly as the object carries them.

        Nothing here is derived. Where it shines from and which way it faces are
        the frame's transform, which is written like any other frame's.
        """
        light = Light(
            light_type=_int_prop(obj, "ls3d_light_type_value") & 0xFFFFFFFF,
            mode=_int_prop(obj, "ls3d_light_mode") & 0xFFFFFFFF,
            power=_float_prop(obj, "ls3d_light_power"),
            color=tuple(obj.ls3d_light_color),
            range_near=_float_prop(obj, "ls3d_light_range_near"),
            range_far=_float_prop(obj, "ls3d_light_range_far"),
            cone_inner=_float_prop(obj, "ls3d_light_cone_inner"),
            cone_outer=_float_prop(obj, "ls3d_light_cone_outer"),
        )
        validate_light(obj, light, self.report.warn)
        return light

    def _build_emitor(self, obj):
        """The particle mesh, in the same block a visual's geometry uses.

        No skin and no morph: an emitor is not one of the engine's object
        visuals and reads neither. It is never written as an instance either,
        for the reason on :meth:`_is_instanceable`.
        """
        geometry, _mappings, _skin = self._build_geometry(
            obj, self.lods.get(obj, [obj]), None)
        return geometry

    def _build_occluder(self, obj):
        occluder = Occluder()
        validate_occluder(obj, self.fail, self.report.warn)
        with evaluated_mesh(obj) as mesh:
            if mesh is None:
                return occluder
            triangulated = _triangulated(mesh)
            try:
                occluder.vertices = [convert.to_mafia_vector(v.co)
                                     for v in triangulated.verts]
                occluder.faces = [
                    convert.face_to_mafia(tuple(v.index for v in face.verts))
                    for face in triangulated.faces]
            finally:
                triangulated.free()
        self._check_vertex_limit(obj, len(occluder.vertices), "occluder")
        # The face count has its own ceiling, and it is not one the format
        # states: the silhouette walk indexes faces through a uint16 adjacency
        # table, and past that it follows the wrong neighbors without saying so.
        if len(occluder.faces) > C.MAX_OCCLUDER_FACES:
            self.fail(
                f"Occluder '{obj.name}' has {len(occluder.faces)} faces, over "
                f"the limit of {C.MAX_OCCLUDER_FACES}.",
                fix=f"Simplify '{obj.name}' - an occluder is a coarse blocking "
                    f"volume, not a copy of the geometry it hides.")
        return occluder

    def _build_mirror(self, obj):
        mirror = Mirror()
        # Kept in Blender space as well: which way a mirror points is checked
        # against its own local Y, and saying so in file axes would name an
        # axis that is not on screen.
        local_vertices = []
        local_faces = []
        with evaluated_mesh(obj) as mesh:
            if mesh is not None:
                triangulated = _triangulated(mesh)
                try:
                    local_vertices = [v.co.copy() for v in triangulated.verts]
                    local_faces = [tuple(v.index for v in face.verts)
                                   for face in triangulated.faces]
                finally:
                    triangulated.free()
        mirror.vertices = [convert.to_mafia_vector(v) for v in local_vertices]
        mirror.faces = [convert.face_to_mafia(f) for f in local_faces]

        self._check_vertex_limit(obj, len(mirror.vertices), "mirror")
        if not mirror.faces:
            self.fail(f"Mirror '{obj.name}' has no faces, so there is no "
                      f"surface to reflect in.",
                      fix="Give the mirror a flat mesh - a plane is enough.")

        if mirror.vertices:
            xs = [v[0] for v in mirror.vertices]
            ys = [v[1] for v in mirror.vertices]
            zs = [v[2] for v in mirror.vertices]
            mirror.bbox_min = (min(xs), min(ys), min(zs))
            mirror.bbox_max = (max(xs), max(ys), max(zs))
            mirror.center = bounds_center(mirror.bbox_min, mirror.bbox_max)
            mirror.radius = bounds_radius(mirror.bbox_min, mirror.bbox_max)

        validate_mirror_facing(obj, local_vertices, local_faces,
                               self.report.warn)

        # The view box, column by column, as the mirror holds it.
        center = tuple(obj.ls3d_mirror_box_center)
        x_axis = tuple(obj.ls3d_mirror_box_x)
        y_axis = tuple(obj.ls3d_mirror_box_y)
        z_axis = tuple(obj.ls3d_mirror_box_z)
        validate_mirror_view_box(obj, center, (x_axis, y_axis, z_axis),
                                 self.fail, self.report.warn)
        mirror.view_matrix = (
            x_axis[0], x_axis[2], x_axis[1], 0.0,
            z_axis[0], z_axis[2], z_axis[1], 0.0,
            y_axis[0], y_axis[2], y_axis[1], 0.0,
            center[0], center[2], center[1], 1.0)

        mirror.tint = tuple(obj.ls3d_mirror_color)
        mirror.draw_distance = _float_prop(obj, "ls3d_mirror_range")
        if mirror.draw_distance <= 0.0:
            self.report.warn(
                f"Mirror '{obj.name}' has a reflection range of "
                f"{mirror.draw_distance:g}, so it will never reflect - the "
                f"game measures the camera against it and gives up at any "
                f"distance past it.",
                fix=f"Set Active Range above 0. Every mirror the game ships "
                    f"uses {C.DEFAULT_MIRROR_RANGE:g}.")
        return mirror

    # ── sectors and portals ───────────────────────────────────────────────────
    def _build_sector(self, obj):
        if self._is_portal(obj):
            # Portals are written inside their parent sector, never standalone.
            return Sector()

        sector = Sector(flags1=_int_prop(obj, "ls3d_sector_flags1"),
                        flags2=_int_prop(obj, "ls3d_sector_flags2"))
        validate_sector(obj, self.fail, self.report.warn)
        validate_sector_flags(obj, _int_prop(obj, "ls3d_sector_flags1"),
                              self.fail)

        # Sector geometry is absolute, not relative to the frame. Every one of
        # the 1125 sector frames in the shipping missions has an identity
        # transform, and the single sector that does have a parent
        # ('sector Box01kr' in MISE04-MESTO, parented to a frame translated by
        # ~(202, 28, 1363)) stores vertices that ignore that translation
        # entirely. So world-space coordinates are written and the frame's own
        # transform is forced to identity by _build_frame.
        world = obj.matrix_world
        # A sector hanging from a frame cancels that frame's transform, and
        # the cancellation leaves a matrix a hair off identity. Its hull is in
        # world space already, so it is written as it is.
        if all(abs(world[row][column] - (1.0 if row == column else 0.0))
               <= SECTOR_IDENTITY_TOLERANCE
               for row in range(4) for column in range(4)):
            world = None
        with evaluated_mesh(obj) as mesh:
            if mesh is not None:
                triangulated = _triangulated(mesh)
                try:
                    sector.vertices = [convert.to_mafia_vector(
                                           v.co if world is None else world @ v.co)
                                       for v in triangulated.verts]
                    sector.faces = [
                        convert.face_to_mafia(tuple(v.index for v in face.verts))
                        for face in triangulated.faces]
                finally:
                    triangulated.free()

        self._check_vertex_limit(obj, len(sector.vertices), "sector")

        if sector.vertices:
            xs = [v[0] for v in sector.vertices]
            ys = [v[1] for v in sector.vertices]
            zs = [v[2] for v in sector.vertices]
            sector.bbox_min = (min(xs), min(ys), min(zs))
            sector.bbox_max = (max(xs), max(ys), max(zs))

        interior = None
        hull = []
        if sector.vertices:
            interior = tuple(sum(v[axis] for v in sector.vertices)
                             / len(sector.vertices) for axis in range(3))
            hull = [(sector.vertices[a], sector.vertices[b], sector.vertices[c])
                    for a, b, c in sector.faces
                    if max(a, b, c) < len(sector.vertices)]

        def portal_order(portal_obj):
            """Sort by the number in ``..._portalN``, not by the text.

            Sorting by name alone puts _portal10 between _portal1 and _portal2,
            which silently renumbers every portal in a sector that has ten or
            more - and mise17-vezeni has sectors with fifteen.
            """
            match = _PORTAL_RE.search(portal_obj.name)
            digits = re.search(r"(\d+)$", match.group(0)) if match else None
            return (int(digits.group(1)) if digits else 0, portal_obj.name)

        portals = sorted(
            [c for c in obj.children if self._is_portal(c)], key=portal_order)
        for portal_obj in portals:
            sector.portals.append(
                self._build_portal(portal_obj, interior, hull))
        return sector

    #: How far outside its sector a portal has to sit before the turnaround is
    #: worth mentioning. A portal whose sector's center lies all but on its own
    #: plane falls either side of zero on rounding alone - the game ships one
    #: at a hundred-thousandth of a meter - and there is nothing to report.
    PORTAL_FACING_EPSILON = 1e-3

    def _build_portal(self, obj, interior=None, hull=None):
        portal = Portal(
            flags=_int_prop(obj, "ls3d_portal_flags") & 0xFFFFFFFF,
            near_range=_float_prop(obj, "ls3d_portal_near"),
            far_range=_float_prop(obj, "ls3d_portal_far"),
        )
        vertices, normal = self._portal_outline(obj)
        if len(vertices) < 3:
            self.fail(
                f"Portal '{obj.name}' has fewer than 3 usable vertices.",
                fix="Give the portal a single flat face with at least 3 corners.")
            return portal

        if len(vertices) > C.MAX_PORTAL_VERTICES:
            self.fail(
                f"Portal '{obj.name}' has {len(vertices)} corners, over the "
                f"limit of {C.MAX_PORTAL_VERTICES}.",
                fix=f"Reduce the portal outline to at most "
                    f"{C.MAX_PORTAL_VERTICES} corners.")
            return portal

        validate_portal_outline(obj.name, vertices, normal, self.report.warn)
        validate_portal_on_hull(obj.name,
                                [convert.to_mafia_vector(v) for v in vertices],
                                hull, self.report.warn)
        validate_portal_flags(obj.name, portal.flags, self.report.warn)

        # Rewound on the way out for the same reason the importer rewinds on
        # the way in. The winding is the part that counts - the game derives
        # the portal's plane from it - so the ring and the plane written
        # beside it have to say the same thing.
        portal.vertices = [convert.to_mafia_vector(v)
                           for v in convert.flip_ring(vertices)]

        # Always recomputed from the current geometry, the way the game's files
        # have it: the normal is the cross product of the ring's first edge
        # and the edge back to its last corner, and the offset is measured at
        # that last corner, all in 32-bit arithmetic. That gives back 1469 of
        # the game's 2720 portal normals to the bit and 2362 of their offsets;
        # Blender's own face normal gives back 567.
        plane_normal, offset = portal_plane(portal.vertices)
        flat = plane_normal is None
        if flat:
            # An outline with no area has no plane of its own. The game meets
            # the same dead end and settles on a unit axis rather than a zero
            # plane, so the file gets one too - and it is rebuilt from the
            # corners on load either way, so which axis it is changes nothing.
            plane_normal = [0.0, 1.0, 0.0]
            offset = _f32(-sum(a * b for a, b in zip(plane_normal,
                                                     portal.vertices[-1])))

        # The game rebuilds this plane from the outline as the level loads and
        # sorts the sector's portals into front and back by the sign of
        # camera . n + d, so a portal wound to face out of its sector ends up
        # in the wrong list. 2767 of the 2770 usable portals the game ships
        # face into their own; the other three are written as they are.
        if interior is not None and not flat:
            reach = sum(a * b for a, b in zip(plane_normal, interior)) + offset
            if reach < -self.PORTAL_FACING_EPSILON:
                self.report.warn(
                    f"Portal '{obj.name}' faces out of its sector.",
                    fix="Flip the portal's face (Mesh > Normals > Flip) so "
                        "it points into the sector, the way its sector's "
                        "own faces do.")

        portal.plane_normal = tuple(plane_normal)
        portal.plane_offset = offset
        return portal

    def _portal_outline(self, obj):
        """Return the portal's outline vertices, wound around its normal."""
        with evaluated_mesh(obj) as mesh:
            if mesh is None or not mesh.vertices:
                return [], Vector((0.0, 0.0, 1.0))
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                bm.transform(obj.matrix_world)
                supplied = len(bm.verts)
                # Faces that touch merge into one n-gon here, bent or not, so
                # what survives to the next test is genuinely separate pieces.
                if len(bm.faces) > 1:
                    bmesh.ops.dissolve_faces(bm, faces=list(bm.faces))
                bm.faces.ensure_lookup_table()
                if not bm.faces:
                    return [], Vector((0.0, 0.0, 1.0))
                if len(bm.faces) > 1:
                    # Coplanar faces were merged above, so more than one left
                    # means the portal genuinely bends. The format has room for
                    # a single ring of corners, so there is no honest way to
                    # write this - picking one face would drop the rest without
                    # the shape ever looking wrong in Blender.
                    self.fail(
                        f"Portal '{obj.name}' is {len(bm.faces)} separate "
                        f"faces, and the format holds one outline.",
                        fix="Make the portal a single flat n-gon.")
                    return [], Vector((0.0, 0.0, 1.0))
                face = bm.faces[0]
                normal = face.normal.copy()
                # Face loops are already wound consistently with the normal, so
                # take them in order rather than re-sorting by angle.
                vertices = [v.co.copy() for v in face.verts]
                # The format stores a portal as one ring of corners, so a
                # vertex inside the outline - or floating loose - has nowhere
                # to go. Dropping it changes nothing about the opening, but it
                # is still the user's geometry going missing.
                if supplied > len(vertices):
                    self.report.warn(
                        f"Portal '{obj.name}' has {supplied - len(vertices)} "
                        f"vertex/vertices off its outline, which the format "
                        f"cannot hold.",
                        fix="Delete them, or move them onto the outline.")
                return vertices, normal
            finally:
                bm.free()

    def _check_vertex_limit(self, obj, count, kind):
        if count > C.MAX_VERTICES_PER_LOD:
            self.fail(
                f"{kind.capitalize()} '{obj.name}' has {count} vertices, over "
                f"the 4DS limit of {C.MAX_VERTICES_PER_LOD}.",
                fix=f"Split '{obj.name}' into several {kind}s.")

    # ── joints ────────────────────────────────────────────────────────────────
    def _boxes_of(self, armature):
        """``{bone name: box}`` for the joints carrying one, gathered once."""
        if armature not in self._influence_lists:
            self._influence_lists[armature] = influence.boxes_of(armature)
        return self._influence_lists[armature]

    def _influence_box(self, armature, bone_name):
        """A joint's influence box in its own space, as a Blender matrix.

        A joint with no box of its own - which only the joints painted by hand
        may be, and which the export says so about - is written the box a new
        joint is made with.
        """
        box = self._boxes_of(armature).get(bone_name)
        return box if box is not None else influence.box_from_file(
            C.DEFAULT_JOINT_BOX)

    def _influence_values(self, armature, bone_name):
        """A joint's sixteen numbers, the file's own where it has them."""
        values = influence.file_box(armature, bone_name)
        return values if values is not None else C.DEFAULT_JOINT_BOX

    def _box_weights(self, obj, mesh, armature, joint_map, parents):
        """Weights for the joints with none painted, made from their boxes.

        ``None`` when every joint of the skin is painted. A joint's painted
        weights are exported as they are; the boxes only weight what no
        painted weight decides.
        """
        space = self._joint_space(armature)
        painted, taken = influence.painted_joints(
            obj, mesh, set(joint_map), shares_of(obj, mesh, armature))
        wanting = [bone.name for bone in space.joints()
                   if bone.name in joint_map and bone.name not in painted]
        if not wanting:
            return None
        boxes = []
        for name in wanting:
            if self._boxes_of(armature).get(name) is None:
                continue        # neither box nor paint: the check already said so
            box = self._influence_box(armature, name)
            if influence.is_flat(box):
                self.fail(f"Joint '{name}' has no weights painted on "
                          f"'{obj.name}', and its influence box has an axis of "
                          f"no length, so no weights can be made from it.",
                          fix="Scale the joint's influence box out, or paint "
                              "the joint's weights.")
                return None
            boxes.append((name, influence.box_world(space, name, box)))
        if not boxes:
            return None
        # The mesh has already been brought into its frame's terms.
        weights = influence.box_weights(mesh, space.mesh_frame.matrix(), boxes,
                                        parents,
                                        influence.ancestors_of(armature), taken)
        self.report.info(f"'{obj.name}': weights made from the influence boxes "
                         f"of {len(boxes)} joint(s) with none painted")
        return weights

    def _build_joint_frame(self, armature, bone_name):
        """The joint frame one bone writes.

        Its transform is worked out from its bone, measured in the whole world
        of the frame it hangs from (see :mod:`.joint_space`); the frame standing
        for the mesh is the mesh itself and is not written here.

        A joint the skin does not number is numbered 0, as the game's own
        files number theirs: the game reads a joint's number only when it
        gathers the joints below a skinned mesh.
        """
        space = self._joint_space(armature)
        joint_map = self.joint_maps.get(armature, {})
        parent = space.parent_of[bone_name]
        if parent is None:
            # Whatever the armature itself hangs from, or nothing.
            parent_frame = self._parent_id(armature)
        elif parent[0] == "mesh":
            parent_frame = self.frame_ids.get(space.skin, 0)
        else:
            parent_frame = self.frame_ids[(armature, parent[1])]
        location, rotation, scale = space.locals[bone_name]

        pose_bone = armature.pose.bones.get(bone_name)
        number = joint_map.get(bone_name)
        box = self._influence_values(armature, bone_name)
        # A rotation part that works out to nothing is written as a negative
        # zero, the way 15244 of the 15460 such parts in the game's joints are.
        rotation = tuple(-0.0 if _f32(part) == 0.0 else part
                         for part in convert.to_mafia_quaternion(rotation))
        return Frame(
            frame_type=C.FRAME_JOINT,
            parent_id=parent_frame,
            position=convert.to_mafia_vector(location),
            scale=convert.to_mafia_vector(scale),
            rotation=rotation,
            cull_flags=(pose_bone.cull_flags & 0xFF) if pose_bone else 0,
            name=bone_name,
            user_props=(pose_bone.user_props if pose_bone else ""),
            joint=Joint(
                matrix=box,
                joint_id=number - 1 if number else 0,
            ),
        )
