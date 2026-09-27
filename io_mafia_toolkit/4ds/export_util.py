"""Small pieces the 4DS export shares.

The exporter is split across a few modules for size, and these are what
more than one of them needs: the error it raises, the regexes that read
Blender's naming, and the mesh evaluation wrappers.
"""

import os
import re

import bmesh
import bpy
from mathutils import Quaternion, Vector

from ..common import constants as C
from ..common import convert

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


class evaluated_mesh:
    """Context manager yielding an object's evaluated mesh, then releasing it.

    Shape key sliders are forced to zero for the duration. Leaving a slider up
    bakes that pose into the exported base mesh while the morph block still
    stores undeformed basis positions, so the model visibly pops in game the
    moment a morph engages. Armatures are switched to rest for the same reason.
    """

    def __init__(self, obj, armature=None):
        self.obj = obj
        self.armature = armature
        self.evaluated = None
        self.mesh = None
        self._saved_pose = None
        self._saved_values = []

    def __enter__(self):
        obj = self.obj
        if obj.type != "MESH":
            return None

        keys = obj.data.shape_keys
        if keys is not None:
            for block in keys.key_blocks:
                self._saved_values.append((block, block.value))
                block.value = 0.0

        if self.armature is not None:
            self._saved_pose = self.armature.data.pose_position
            self.armature.data.pose_position = "REST"

        depsgraph = bpy.context.evaluated_depsgraph_get()
        self.evaluated = obj.evaluated_get(depsgraph)
        self.mesh = self.evaluated.to_mesh(
            preserve_all_data_layers=True, depsgraph=depsgraph)
        return self.mesh

    def __exit__(self, exc_type, exc, tb):
        if self.evaluated is not None:
            self.evaluated.to_mesh_clear()
        if self._saved_pose is not None:
            self.armature.data.pose_position = self._saved_pose
        for block, value in self._saved_values:
            block.value = value
        return False


class undeformed_mesh:
    """Context manager yielding a skinned mesh as it is, before any bending.

    A skinned mesh is written at rest, and at rest the armature moves nothing
    - but running the mesh through it anyway rounds every vertex by a bit or
    two. So a mesh whose only modifiers are armatures is read straight from its
    own data, and one with other modifiers is evaluated with its armatures
    switched off.
    """

    def __init__(self, obj):
        self.obj = obj
        self._evaluated = None
        self._switched = []

    def __enter__(self):
        obj = self.obj
        if obj.type != "MESH":
            return None
        others = [m for m in obj.modifiers if m.type != "ARMATURE"]
        if not others:
            return obj.data
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and modifier.show_viewport:
                modifier.show_viewport = False
                self._switched.append(modifier)
        self._evaluated = evaluated_mesh(obj)
        return self._evaluated.__enter__()

    def __exit__(self, exc_type, exc, tb):
        if self._evaluated is not None:
            self._evaluated.__exit__(exc_type, exc, tb)
        for modifier in self._switched:
            modifier.show_viewport = True
        return False


class in_frame:
    """Context manager yielding *mesh* brought over by *offset*, or as it is.

    With no offset the mesh itself comes back, untouched, so its numbers go
    out to the bit. With one, a copy is moved - vertices and normals together
    - and thrown away afterwards.
    """

    def __init__(self, mesh, offset):
        self.mesh = mesh
        self.offset = offset
        self.copy = None

    def __enter__(self):
        if self.mesh is None or self.offset is None:
            return self.mesh
        self.copy = self.mesh.copy()
        self.copy.transform(self.offset)
        self.copy.update()
        return self.copy

    def __exit__(self, exc_type, exc, tb):
        if self.copy is not None:
            bpy.data.meshes.remove(self.copy)
        return False


def _triangulated(mesh):
    """A triangulated bmesh copy with valid vertex indices."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.triangulate(bm, faces=list(bm.faces))
    # ensure_lookup_table only fixes subscripting; index_update refreshes the
    # .index attributes the face writer reads.
    bm.verts.index_update()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return bm


def _empty_skin_lod():
    from .codec.types import SkinLOD
    return SkinLOD()


def _prop(obj, name, convert):
    """The value the object carries, or a stop. Never a stand-in.

    The export writes what is set and nothing else. A fallback here would be
    unreachable anyway - every one of these is a registered property, so it
    always has a value - which makes it dead code that only ever fires when
    something is genuinely wrong, and then hides it behind a number nobody
    chose. Better to say so and write no file at all.
    """
    try:
        return convert(getattr(obj, name))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExportError(
            f"'{getattr(obj, 'name', obj)}' has no usable {name} "
            f"({exc}). Nothing was written - the export does not guess at a "
            f"value the object does not carry."
        ) from exc


def _int_prop(obj, name):
    return _prop(obj, name, int)


def _float_prop(obj, name):
    return _prop(obj, name, float)


# ── scene-level orchestration ─────────────────────────────────────────────────
