"""Where an actor travels while an animation of it plays, on the Blender side.

The travel rides on an empty above the model rather than on the model itself,
so it can be muted while animating and written to its own file. The generic
keyframe plumbing is shared with the animation package next door; only what is
particular to a motion track lives here.
"""

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..packages import module

#: The rate the game plays animations at, shared with them.
FRAMES_PER_SECOND = module("5ds.codec").FRAMES_PER_SECOND

# ── The movement empty ──────────────────────────────────────────────────────
#: What a new movement empty is called. Only a name - what makes an object
#: the movement track is the switch on it, so renaming one changes nothing.
MOTION_NAME = "_TRACK_"
#: The switch itself. One per animation: a .tck holds a single actor's travel
#: and the game keeps one current track, so a second would mean nothing.
MOTION_FLAG = "ls3d_is_motion_track"
#: The axis a frame faces along, in Blender's axes. The file stores a facing
#: vector, and a frame's forward is its own local Y once the axes are swapped.
MOTION_FORWARD = "Y"



def is_motion_track(obj):
    """True when *obj* is the empty carrying an animation's travel.

    Decided by the switch on the object, never by what it is called: a name is
    something a person changes without meaning anything by it, and an export
    that quietly stopped finding the movement would be hard to explain.
    """
    return bool(obj and obj.type == "EMPTY"
                and getattr(obj, MOTION_FLAG, False))


def motion_holder(obj):
    """The empty carrying *obj*'s world movement, or ``None``."""
    parent = obj.parent
    return parent if is_motion_track(parent) else None


def ensure_motion_holder(obj, collection):
    """The empty above *obj* that carries its movement, made if missing.

    The game adds an animation's travel on top of whatever the model is
    already doing, which is what a parent transform is, and it keeps the two
    apart - the 5DS moves the model's own frames, so the movement goes on
    something above them rather than in among them.
    """
    existing = motion_holder(obj)
    if existing is not None:
        return existing

    holder = bpy.data.objects.new(MOTION_NAME, None)
    setattr(holder, MOTION_FLAG, True)
    holder.empty_display_type = "ARROWS"
    holder.empty_display_size = 0.5
    holder.rotation_mode = "QUATERNION"
    collection.objects.link(holder)
    # The model keeps where it stands; only what happens above it is new.
    holder.matrix_world = Matrix.Identity(4)
    obj.parent = holder
    obj.matrix_parent_inverse = Matrix.Identity(4)
    return holder


def direction_to_rotation(direction):
    """A facing vector as a rotation whose forward axis points along it."""
    vector = Vector(direction)
    if vector.length <= 1e-9:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    # The shortest turn from the forward axis onto the direction. Aiming an
    # axis at a target instead would need a second axis to hold the roll
    # against, and near the forward axis that rounds the facing off - which a
    # facing barely off vertical cannot afford.
    return _forward_axis().rotation_difference(vector)


def _forward_axis():
    """The forward axis as a vector."""
    return {"X": Vector((1.0, 0.0, 0.0)), "Y": Vector((0.0, 1.0, 0.0)),
            "Z": Vector((0.0, 0.0, 1.0))}[MOTION_FORWARD]


def rotation_to_direction(rotation):
    """Which way a rotation faces, as a vector."""
    # Deliberately not normalized, on either side: rescaling a facing would
    # be this addon changing what the animator keyed. A facing that has drifted
    # off unit length is reported on the way out instead.
    return rotation @ _forward_axis()


def motion_frames(track, frames_per_second=FRAMES_PER_SECOND):
    """When each sample of *track* falls, as a Blender frame number.

    Samples are spaced in milliseconds and Blender counts frames, so a track
    sampled more often than the animation lands between them. Blender is happy
    with a fractional frame, and keeping them where the file put them is what
    lets the same periods come back out.
    """
    step = track.key_period * frames_per_second / 1000.0
    return [index * step for index in range(len(track.positions))]


def motion_end_frame(track, frames_per_second=FRAMES_PER_SECOND):
    """The frame a motion track reaches, for the scene's range to cover.

    Taken from the length the file records rather than the sample count, since
    that is what the export works the sample count back out of.
    """
    return int(round(track.duration * frames_per_second / 1000.0))


