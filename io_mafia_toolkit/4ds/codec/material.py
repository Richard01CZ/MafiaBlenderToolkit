"""Material block.

Layout (v29), in order::

    u32   flags
    3f    ambient
    3f    diffuse
    3f    emission
    f32   opacity
    if flags & ENV_ENABLE:
        f32   env amount
        str   env texture
    if flags & DIFFUSE_ENABLE:
        str   diffuse texture
    if flags & ALPHATEX and not flags & NO_ALPHA_MASK:
        str   alpha texture
    if neither texture string was written:
        u8    padding (always 0)
    if flags & DIFFUSE_ANIMATED:
        u8    diffuse frame count
        u8    environment frame count
        i32   diffuse wrap-to frame  (-1 = pick one at random each interval)
        i32   diffuse frame period (ms)
        i32   environment wrap-to frame
        i32   environment frame period (ms)

The two texture strings are *conditional*. Reading them unconditionally happens
to consume the same number of bytes on every shipping Mafia model — an empty
string and the padding byte are both a single zero — which is why a simpler
parser appears to work. It stops being equivalent the moment a material sets
ALPHATEX without DIFFUSE_ENABLE and has a non-empty alpha name, at which point
the alpha texture gets read into the diffuse slot.

Note that ALPHA_ANIMATED has **no payload block** of its own, despite the name.
Nine shipping materials set that bit and none of them carry extra bytes; adding
a block for it desynchronizes the rest of the file.
"""

from ...common.constants import (
    MTL_ALPHATEX, MTL_DIFFUSE_ANIMATED, MTL_NO_ALPHA_MASK,
    MTL_DIFFUSE_ENABLE, MTL_ENV_ENABLE,
)
from .types import Material

#: Frame names are numbered in the two characters immediately before the
#: extension - the game overwrites them with a counter from 00 upward and puts
#: the dot back. The name stored in the file is therefore frame 00 already, and
#: a name with fewer than six characters has nowhere to put the counter.
ANIM_COUNTER_DIGITS = 2
ANIM_NAME_TAIL = 6

#: A wrap-to frame of -1 asks for a frame picked at random each interval.
ANIM_RANDOM = -1


def has_diffuse_texture(flags):
    return bool(flags & MTL_DIFFUSE_ENABLE)


def has_alpha_texture(flags):
    """Whether a separate alpha-texture name follows the diffuse one.

    Matches what the game expects: the name is present when the alpha-texture
    bit is set and none of the vetoing bits are, because a color-keyed,
    truecolor or alpha-tested material carries its transparency in the diffuse
    texture. The addon used to test the "Alpha Enable" bit here instead and
    ignore the veto - the two rules agree on all 44655 materials shipped with
    the game, but they part company on a hand-authored one, and getting a
    field's presence wrong desyncs every byte after it.
    """
    return bool(flags & MTL_ALPHATEX) and not (flags & MTL_NO_ALPHA_MASK)


def read_material(reader):
    mat = Material()
    mat.flags = reader.u32()
    mat.ambient = reader.vec3()
    mat.diffuse = reader.vec3()
    mat.emission = reader.vec3()
    mat.opacity = reader.f32()

    if mat.flags & MTL_ENV_ENABLE:
        mat.env_amount = reader.f32()
        mat.env_texture = reader.string()

    wrote_any_name = False
    if has_diffuse_texture(mat.flags):
        mat.diffuse_texture = reader.string()
        wrote_any_name = True
    if has_alpha_texture(mat.flags):
        mat.alpha_texture = reader.string()
        wrote_any_name = True
    if not wrote_any_name:
        reader.skip(1)

    if mat.flags & MTL_DIFFUSE_ANIMATED:
        mat.anim_frames = reader.u8()
        mat.env_anim_frames = reader.u8()
        mat.anim_loop_start = reader.i32()
        mat.anim_period = reader.i32()
        mat.env_anim_loop_start = reader.i32()
        mat.env_anim_period = reader.i32()

    return mat


def write_material(writer, mat, context=""):
    writer.u32(mat.flags)
    writer.vec3(*mat.ambient[:3])
    writer.vec3(*mat.diffuse[:3])
    writer.vec3(*mat.emission[:3])
    writer.f32(mat.opacity)

    if mat.flags & MTL_ENV_ENABLE:
        writer.f32(mat.env_amount)
        writer.string(mat.env_texture, f"{context} env texture")

    wrote_any_name = False
    if has_diffuse_texture(mat.flags):
        writer.string(mat.diffuse_texture, f"{context} diffuse texture")
        wrote_any_name = True
    if has_alpha_texture(mat.flags):
        writer.string(mat.alpha_texture, f"{context} alpha texture")
        wrote_any_name = True
    if not wrote_any_name:
        writer.u8(0)

    if mat.flags & MTL_DIFFUSE_ANIMATED:
        writer.u8(mat.anim_frames)
        writer.u8(mat.env_anim_frames)
        writer.i32(mat.anim_loop_start)
        writer.i32(mat.anim_period)
        writer.i32(mat.env_anim_loop_start)
        writer.i32(mat.env_anim_period)
