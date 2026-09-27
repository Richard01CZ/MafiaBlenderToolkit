"""Operators for the elements of a lens flare.

A lens flare frame carries an ordered list of elements, each a position on the
flare axis plus the material drawn there. Order matters: the first element is
the flare's head - the game draws it larger than the rest and at full
brightness - so the list can be reordered as well as added to.
"""

import bpy


def glow_entries(obj):
    """``(position, material)`` for every element of *obj*, in list order."""
    return [(entry.position, entry.material) for entry in obj.ls3d_glows]


class LS3D_OT_AddGlow(bpy.types.Operator):
    """Add an element to this lens flare"""

    bl_idname = "ls3d.add_glow"
    bl_label = "Add Flare Element"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object
        entry = obj.ls3d_glows.add()
        entry.name = f"Glow {len(obj.ls3d_glows)}"
        obj.ls3d_glows_index = len(obj.ls3d_glows) - 1
        return {"FINISHED"}


class LS3D_OT_RemoveGlow(bpy.types.Operator):
    """Remove the selected element from this lens flare"""

    bl_idname = "ls3d.remove_glow"
    bl_label = "Remove Flare Element"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and len(obj.ls3d_glows)

    def execute(self, context):
        obj = context.object
        index = obj.ls3d_glows_index
        if not (0 <= index < len(obj.ls3d_glows)):
            return {"CANCELLED"}
        obj.ls3d_glows.remove(index)
        obj.ls3d_glows_index = max(0, index - 1)
        return {"FINISHED"}


class LS3D_OT_MoveGlow(bpy.types.Operator):
    """Move the selected element up or down the flare axis order"""

    bl_idname = "ls3d.move_glow"
    bl_label = "Move Flare Element"
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(
        items=(("UP", "Up", ""), ("DOWN", "Down", "")), default="UP")

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and len(obj.ls3d_glows) > 1

    def execute(self, context):
        obj = context.object
        index = obj.ls3d_glows_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not (0 <= index < len(obj.ls3d_glows)) or not (
                0 <= target < len(obj.ls3d_glows)):
            return {"CANCELLED"}
        obj.ls3d_glows.move(index, target)
        obj.ls3d_glows_index = target
        return {"FINISHED"}


CLASSES = (LS3D_OT_AddGlow, LS3D_OT_RemoveGlow, LS3D_OT_MoveGlow)
