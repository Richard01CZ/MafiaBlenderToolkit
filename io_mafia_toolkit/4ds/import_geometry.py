"""Building a model's materials and its meshes.

Split out of :mod:`.importer` for size; these are methods of the importer
and only make sense mixed into it.
"""

import os

import bpy
from mathutils import Matrix

from ..common import constants as C
from ..common import convert
from ..common import describe
from .codec.types import LOD, Vertex
from .materials import (
    effective_opacity, holding_updates, read_bmp_color_key,
    rebuild_material_nodes, sync_material_flags,
)
from .validation import texture_problems
from .ops_texfix import refusal, texture_name_supported
from .import_util import _to_signed32


class GeometryMixin:
    """Part of :class:`~.importer.Importer`."""

    # ── stage 1: materials ────────────────────────────────────────────────────
    def _build_materials(self, doc):
        total = len(doc.materials)
        for index, source in enumerate(doc.materials, start=1):
            self.report.item("Mat", index, total, describe.material_line(
                "", source.flags,
                (source.diffuse_texture, source.alpha_texture,
                 source.env_texture)))
            mat = bpy.data.materials.new(f"4ds_material_{index}")
            try:
                self._apply_material(mat, source)
            except Exception as exc:
                self.report.warn(f"Material {index} ('{mat.name}'): {exc}")
            self.materials.append(mat)

    def _settle_opacity(self):
        """Re-read the opacity of every faded material now the meshes exist.

        Opacity is not a material's to decide - the mesh it sits on hands one
        to everything drawn on it - and the materials are built before any of
        those meshes are, so the first pass has nothing to look at.
        """
        settled = 0
        # Index 0 is the placeholder that keeps the file's ids 1-based.
        for mat in self.materials[1:]:
            if mat is None or mat.ls3d_opacity >= 1.0:
                continue
            sync_material_flags(mat)
            if effective_opacity(mat) != mat.ls3d_opacity:
                settled += 1
        if settled:
            self.report.info(
                f"{settled} material(s) have an opacity their mesh overrides; "
                f"the preview shows what the game draws.")

    def _apply_material(self, mat, source):
        # Setting any of these rebuilds the preview on its own, which is what
        # keeps it live under the user's hands but means building the graph a
        # dozen times over here and throwing eleven away. Held until the end,
        # then built once.
        with holding_updates():
            mat.ls3d_material_flags = _to_signed32(source.flags)
            mat.ls3d_ambient_color = source.ambient
            mat.ls3d_diffuse_color = source.diffuse
            mat.ls3d_emission_color = source.emission
            mat.ls3d_opacity = source.opacity
            mat.ls3d_env_amount = source.env_amount
            mat.ls3d_anim_frames = source.anim_frames
            mat.ls3d_anim_period = source.anim_period
            mat.ls3d_anim_loop_start = source.anim_loop_start
            mat.ls3d_env_anim_frames = source.env_anim_frames
            mat.ls3d_env_anim_period = source.env_anim_period
            mat.ls3d_env_anim_loop_start = source.env_anim_loop_start

            diffuse = self._load_texture(source.diffuse_texture)
            if source.flags & C.MTL_ALPHA_COLORKEY and diffuse is not None:
                key = read_bmp_color_key(diffuse)
                if key is not None:
                    mat.ls3d_color_key = key
                elif self.texture_dir:
                    # Only worth saying when a texture folder is set and the
                    # file still would not open. Without one the import already
                    # said so once, and repeating it per color-keyed material
                    # buries the warnings that matter.
                    self.report.warn(
                        f"Could not open '{source.diffuse_texture}' to read its "
                        f"color key - transparency may look wrong.",
                        fix="Check that the texture exists in the texture "
                            "folder.")

            mat.ls3d_diffuse_tex = diffuse
            mat.ls3d_alpha_tex = self._load_texture(source.alpha_texture)
            mat.ls3d_env_tex = self._load_texture(source.env_texture)

        rebuild_material_nodes(mat)

    def _load_texture(self, filename):
        """Load a texture by name, matching case-insensitively.

        A texture that cannot be loaded - no texture folder configured, or the
        file simply is not there - still gets a datablock carrying its name, so
        the material shows what it references and the name survives a re-export.
        Returning None here used to blank the field, and because the exporter
        writes the image's file name, exporting that material afterwards wrote
        an empty texture name and quietly lost it.
        """
        if not filename:
            return None
        key = os.path.basename(filename).lower()
        if key in self.texture_cache:
            return self.texture_cache[key]

        # The game reads BMP and TGA and nothing else, so a texture of another
        # kind is refused rather than assigned - once, however many materials
        # name it, since the empty answer is kept like any other.
        if not texture_name_supported(filename):
            self.report.warn(refusal(os.path.basename(filename)),
                             fix="Convert the texture to a BMP and assign it "
                                 "to the material.")
            self.texture_cache[key] = None
            return None

        path = None
        if self.texture_dir:
            candidate = os.path.join(self.texture_dir, key)
            if os.path.exists(candidate):
                path = candidate
            else:
                try:
                    for entry in os.listdir(self.texture_dir):
                        if entry.lower() == key:
                            path = os.path.join(self.texture_dir, entry)
                            break
                except OSError:
                    path = None

        image = None
        if path:
            # The same checks the export and Check Scene make, run on the file
            # the game would load - once per texture, however many materials
            # share it, since the cache below stops a second look.
            for problem, fix in texture_problems(path):
                self.report.warn(f"Texture '{filename}' {problem}.", fix=fix)
            try:
                image = bpy.data.images.load(path, check_existing=True)
            except RuntimeError as exc:
                self.report.warn(f"Could not load texture '{filename}': {exc}")
        if image is None:
            image = self._named_placeholder(filename)
        self.texture_cache[key] = image
        return image

    def _named_placeholder(self, filename):
        """A stand-in datablock that remembers a texture's name.

        Blender shows it as a missing image, which is the honest thing: the
        name and path are right, the pixels are simply not available.
        """
        name = os.path.basename(filename)
        existing = bpy.data.images.get(name)
        if existing is not None:
            return existing
        try:
            image = bpy.data.images.new(name, 1, 1)
        except RuntimeError:
            return None
        image.source = "FILE"
        image.filepath = (os.path.join(self.texture_dir, name)
                          if self.texture_dir else f"//{name}")
        return image

    # ── geometry ──────────────────────────────────────────────────────────────
    def _apply_geometry(self, obj, frame, frame_id):
        geometry = frame.geometry
        if geometry.instance_id:
            self.instance_jobs.append((obj, geometry.instance_id))
            self.lod_objects[frame_id] = [obj]
            return

        objects = []
        for level, lod in enumerate(geometry.lods):
            if level == 0:
                target = obj
                mesh = obj.data
            else:
                mesh = bpy.data.meshes.new(f"{obj.name}_lod{level}")
                target = bpy.data.objects.new(mesh.name, mesh)
                target.parent = obj
                target.matrix_parent_inverse = Matrix.Identity(4)
                self.collection.objects.link(target)
                target.hide_set(True)
                target.hide_render = True
            target.ls3d_lod_dist = lod.distance
            self._fill_mesh(mesh, self._expanded_lod(geometry, level, lod,
                                                     obj.name),
                            obj.name, level)
            objects.append(target)
        self.lod_objects[frame_id] = objects

    def _expanded_lod(self, geometry, level, lod, label):
        """*lod* with real vertices, whether or not it stored any.

        A LOD may keep nothing but its own UVs and borrow LOD 0's positions and
        normals. Blender has no such thing - a mesh owns its vertices - so the
        two are put back together here and the LOD becomes an ordinary one.
        """
        if lod.shared_uv is None:
            return lod

        base = geometry.lods[0].vertices
        self.report.warn(
            f"'{label}' LOD {level} carries only UVs and borrows LOD 0's "
            f"{len(base)} vertices. It is imported as a mesh of its own, so "
            f"exporting writes it out in full rather than as the shorthand.")
        return LOD(
            distance=lod.distance,
            vertices=[Vertex(position=vertex.position, normal=vertex.normal,
                             uv=uv)
                      for vertex, uv in zip(base, lod.shared_uv)],
            face_groups=lod.face_groups,
        )

    def _fill_mesh(self, mesh, lod, label, level):
        # Every value goes in as the file holds it, the odd ones included: a
        # position or UV that is not a number, and a normal of no length, are
        # written back out exactly as they came. The game ships all three.
        positions = [convert.to_blender_vector(v.position) for v in lod.vertices]
        normals = [convert.to_blender_vector(v.normal) for v in lod.vertices]
        uvs = [convert.uv_to_blender(v.uv) for v in lod.vertices]

        # Duplicate and reversed-winding faces are kept: Blender stores them as
        # given, and they are real geometry — 25,705 faces across 614 shipping
        # models are a second copy of a triangle wound the other way to make a
        # surface double-sided.
        #
        # A face naming the same vertex twice is kept too, but not as it is:
        # Blender counts such a face as broken, and entering Edit Mode on a
        # mesh holding one freezes it. So the repeat gets a copy of the vertex
        # at the same spot, after every vertex the file has, and the face
        # becomes an ordinary triangle with no area. The export writes such a
        # copy as the vertex it repeats (see mesh.repeated_vertices). The game's
        # files hold 1203 such faces.
        faces = []
        face_materials = []
        out_of_range = 0
        vertex_count = len(positions)
        copies = {}                     # copy vertex -> the vertex it repeats
        for group in lod.face_groups:
            slot = self._material_slot(mesh, group.material_id)
            for face in group.faces:
                corners = list(convert.face_to_blender(face))
                if max(corners) >= vertex_count:
                    out_of_range += 1
                    continue
                for index in range(1, 3):
                    if corners[index] in corners[:index]:
                        original = corners[index]
                        copy = len(positions)
                        positions.append(positions[original])
                        normals.append(normals[original])
                        uvs.append(uvs[original])
                        copies[copy] = original
                        corners[index] = copy
                faces.append(tuple(corners))
                face_materials.append(slot)

        if out_of_range:
            self.report.warn(
                f"'{label}' LOD {level}: dropped {out_of_range} face(s) "
                f"referring to vertices outside the mesh; the file may be "
                f"damaged.")

        mesh.from_pydata(positions, [], faces)
        mesh.update()
        if copies:
            self.vertex_copies[mesh] = copies

        if len(mesh.polygons) != len(face_materials):
            self.report.warn(
                f"'{label}' LOD {level}: Blender built {len(mesh.polygons)} "
                f"face(s) from {len(face_materials)} - material assignment "
                f"skipped for this LOD.")
        elif face_materials:
            mesh.polygons.foreach_set("material_index", face_materials)

        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))

        if mesh.loops:
            uv_layer = mesh.uv_layers.new(name="UVMap")
            loop_normals = []
            for index, loop in enumerate(mesh.loops):
                uv_layer.data[index].uv = uvs[loop.vertex_index]
                loop_normals.append(normals[loop.vertex_index])
            self._apply_custom_normals(mesh, loop_normals, label, level)
        mesh.update()

    def _apply_custom_normals(self, mesh, loop_normals, label, level):
        """Give the mesh the file's normals without Blender rounding them.

        ``normals_split_custom_set`` stores each normal as a pair of 16-bit
        numbers. That costs about half a degree on the way in, and worse, a
        minority of them then shift one step further on every later save,
        always the same way, without ever settling - a model saved a hundred
        times has normals a quarter of a degree out through no fault of its
        own. Writing the corner attribute as float vectors holds the file's
        values exactly. Blender shades from the same attribute either way; the
        only difference is the precision it is kept at.
        """
        try:
            attribute = mesh.attributes.get("custom_normal")
            if attribute is not None and (
                    attribute.data_type != "FLOAT_VECTOR"
                    or attribute.domain != "CORNER"):
                mesh.attributes.remove(attribute)
                attribute = None
            if attribute is None:
                attribute = mesh.attributes.new("custom_normal",
                                                "FLOAT_VECTOR", "CORNER")
            attribute.data.foreach_set(
                "vector", [axis for normal in loop_normals for axis in normal])
            return
        except (AttributeError, RuntimeError, TypeError):
            pass

        # Older Blender keeps custom normals to itself; the rounding comes back
        # with it, but the normals are still the file's to within that.
        try:
            mesh.normals_split_custom_set(loop_normals)
        except RuntimeError as exc:
            self.report.warn(
                f"'{label}' LOD {level}: could not apply custom normals "
                f"({exc}); Blender will recompute them.")

    def _material_slot(self, mesh, material_id):
        """The slot holding *material_id*, made if the mesh lacks one.

        A face group with no material gets an empty slot of its own. Sharing
        slot 0 instead would hand those faces whichever material lands there
        first, and the export would write that material for them.
        """
        mat = (self.materials[material_id]
               if 0 < material_id < len(self.materials) else None)
        for index, existing in enumerate(mesh.materials):
            if existing is mat or (mat is not None and existing is not None
                                   and existing.name == mat.name):
                return index
        mesh.materials.append(mat)
        return len(mesh.materials) - 1
