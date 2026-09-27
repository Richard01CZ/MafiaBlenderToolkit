"""Turning shadow pieces into Blender meshes, and reading them back.

A shadow piece is a plain triangle mesh with nothing else on it - no
materials, no UVs, no normals worth keeping - so the conversion is only the
axis swap and the winding that comes with it.
"""

import re

import bpy

from ..common import convert

#: The switch that says a mesh is part of the shadow rather than the model.
SHADOW_FLAG = "ls3d_is_shadow"


def is_shadow_piece(obj):
    """True when *obj* is a mesh belonging to the shadow, not the model."""
    return bool(obj and obj.type == "MESH"
                and getattr(obj, SHADOW_FLAG, False))


def shadow_pieces(scene, objects=None):
    """Every shadow piece, in the order they would be written."""
    return [obj for obj in (scene.objects if objects is None else objects)
            if is_shadow_piece(obj)]


def build_mesh(group, name):
    """One shadow piece as a Blender mesh, or ``None`` when it has no faces."""
    mesh = bpy.data.meshes.new(name)
    positions = [convert.to_blender_vector(vertex) for vertex in group.vertices]
    faces = [convert.face_to_blender(face) for face in group.faces]
    mesh.from_pydata(positions, [], faces)
    mesh.update()
    return mesh


#: Blender's uniquifying suffix, e.g. "base.001". A shadow piece is named
#: after a frame of the model, so with that model loaded every piece collides
#: with the frame it belongs to and picks one up.
_DUPLICATE_SUFFIX = re.compile(r"\.\d{3}$")


def frame_name(obj):
    """The frame a piece belongs to: its name, without Blender's suffix."""
    return _DUPLICATE_SUFFIX.sub("", obj.name)


def hang_from_frame(piece, scene, name=None):
    """Put *piece* in the space of the frame whose name it carries.

    The engine multiplies a piece by the world matrix of the frame it matched,
    so the piece belongs in that frame's space. A model's frames arrive as
    objects, except its joints, which arrive as bones - both are handled.
    Returns True when a frame was found.
    """
    from mathutils import Matrix

    # Named explicitly where it is known, since Blender may already have
    # renamed the piece to avoid colliding with the very frame it wants.
    name = name or frame_name(piece)
    frame = scene.objects.get(name)
    if frame is not None and frame is not piece:
        piece.parent = frame
        piece.matrix_parent_inverse = Matrix.Identity(4)
        return True

    for candidate in scene.objects:
        if candidate.type != "ARMATURE":
            continue
        bone = candidate.data.bones.get(name)
        if bone is None:
            continue
        piece.parent = candidate
        piece.parent_type = "BONE"
        piece.parent_bone = name
        # Blender hangs a bone-parented child off the bone's tail; the frame's
        # own space starts at its head, so that length is taken back out.
        piece.matrix_parent_inverse = Matrix.Translation(
            (0.0, -bone.length, 0.0))
        return True
    return False


def read_mesh(obj):
    """A mesh's vertices and triangles, in the file's axes and winding.

    The vertices come out in the space of whatever the piece hangs from, since
    that is what the file holds: the engine multiplies each piece by the world
    matrix of the frame it matched, so the numbers stored are local to that
    frame. An unparented piece is therefore written as it stands.

    Returns ``(vertices, faces, untriangulated)`` - the last being how many
    polygons had more than three corners and were therefore skipped, since the
    format holds nothing but triangles.
    """
    mesh = obj.data
    # The piece's own transform, ignoring whatever it hangs from. The file
    # holds coordinates local to the matched frame, and the parenting below is
    # only what puts the piece in the right place on screen - so what is
    # written is the mesh as it sits in its own space.
    matrix = obj.matrix_basis
    vertices = [convert.to_mafia_vector(tuple(matrix @ vertex.co))
                for vertex in mesh.vertices]
    faces = []
    untriangulated = 0
    for polygon in mesh.polygons:
        if len(polygon.vertices) != 3:
            untriangulated += 1
            continue
        faces.append(convert.face_to_mafia(tuple(polygon.vertices)))
    return vertices, faces, untriangulated
