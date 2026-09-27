"""Axis conversion between Mafia (LS3D) space and Blender space.

Mafia is Y-up left-handed; Blender is Z-up right-handed. The conversion is a
straight Y/Z swap, which is its own inverse — but the original code spelled it
out inline in roughly forty places, and a swap written the wrong way round is
invisible until something is mirrored in game. Everything goes through here
instead.

Because the swap is an involution, the same function serves both directions.
The differently-named aliases exist so call sites read correctly.
"""

from mathutils import Matrix, Quaternion, Vector


def swap_yz(vec):
    """Swap the Y and Z components of a 3-component sequence."""
    return (vec[0], vec[2], vec[1])


#: Mafia (x, y_up, z_fwd) -> Blender (x, y_fwd, z_up)
to_blender_vector = swap_yz
#: Blender (x, y_fwd, z_up) -> Mafia (x, y_up, z_fwd)
to_mafia_vector = swap_yz


def to_blender_vec(vec):
    return Vector(swap_yz(vec))


def to_blender_quaternion(quat):
    """Convert a stored ``(w, x, y, z)`` quaternion into Blender's axes."""
    w, x, y, z = quat
    return Quaternion((w, x, z, y))


def to_mafia_quaternion(quat):
    """Convert a Blender quaternion into the stored ``(w, x, y, z)`` order."""
    return (quat.w, quat.x, quat.z, quat.y)


def matrix_from_mafia_rows(values):
    """Rebuild a Blender 4x4 from the file's 16-float row-major layout.

    File rows become Blender columns, with rows 1 and 2 swapped back: the
    file's row 1 carries Mafia's up axis, which is Blender's Z, and its row 2
    the forward axis, which is Blender's Y.
    """
    return Matrix((
        (values[0], values[8], values[4], values[12]),
        (values[2], values[10], values[6], values[14]),
        (values[1], values[9], values[5], values[13]),
        (0.0, 0.0, 0.0, 1.0),
    ))


def uv_to_blender(uv):
    """Flip V: the file's origin is top-left, Blender's is bottom-left."""
    return (uv[0], 1.0 - uv[1])


#: The flip is its own inverse.
uv_to_mafia = uv_to_blender


#: The file winds triangles (0, 2, 1) relative to Blender's (0, 1, 2).
def face_to_blender(face):
    return (face[0], face[2], face[1])


face_to_mafia = face_to_blender


def flip_ring(ring):
    """Rewind a face loop of any length, keeping its first element first.

    The axis swap flips handedness, so a loop carried across it comes out wound
    the other way and its normal points the opposite direction. Triangles are
    corrected by :func:`face_to_blender`; a portal outline can have up to eight
    corners and needs the same correction generalized. For three elements this
    *is* ``face_to_blender``, and like it the operation is its own inverse, so
    one function serves both directions.
    """
    items = list(ring)
    return items[:1] + items[1:][::-1]
