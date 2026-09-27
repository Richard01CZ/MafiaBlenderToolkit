"""Small checks the 4DS import shares.

The import is split across a few modules for size; these are the guards
more than one of them needs against what a file may actually contain.
"""


from . import joint_display

#: Length given to imported bones. 4DS joints are points with no length, so
#: this is display only and deliberately uniform.
BONE_DISPLAY_LENGTH = 0.1

#: Name of the shared empty used as a bone custom shape.
JOINT_SHAPE_NAME = joint_display.JOINT_SHAPE_NAME

#: Pose-bone property holding the opaque 16-float FRAME_JOINT payload
#: verbatim, so exporting a model the addon imported reproduces it.


# ── helpers ───────────────────────────────────────────────────────────────────
def _to_signed32(value):
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value
