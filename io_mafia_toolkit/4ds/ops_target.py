"""Operators for 4DS target frames.

A target frame is a look-at anchor: other frames listed on it turn to face it.
The links are mirrored into Blender as Track To constraints on the *linked*
objects so the relationship is visible in the viewport, which is why adding or
removing a link edits constraints on other objects rather than on the target.
"""

import bpy

#: Prefix identifying constraints this addon owns, so user-made Track To
#: constraints are never touched.
CONSTRAINT_PREFIX = "LS3D_Track_"


def resolve_target_entry(entry):
    """Return ``(object, pose_bone)`` for a link entry; either may be ``None``."""
    if entry.target_armature and entry.bone_name:
        pose = getattr(entry.target_armature, "pose", None)
        return None, (pose.bones.get(entry.bone_name) if pose else None)
    if entry.target_object:
        return entry.target_object, None
    return None, None


def _owner_constraints(entry):
    """Return the constraint collection for a link entry, or ``None``."""
    linked, pose_bone = resolve_target_entry(entry)
    if pose_bone is not None:
        return pose_bone.constraints
    if linked is not None:
        return linked.constraints
    return None


def _remove_our_constraints(constraints, name=None):
    for constraint in list(constraints):
        if constraint.type != "TRACK_TO":
            continue
        if name is not None:
            if constraint.name == name:
                constraints.remove(constraint)
        elif constraint.name.startswith(CONSTRAINT_PREFIX):
            constraints.remove(constraint)


def set_constraint_muted(constraint, muted):
    """Switch one constraint off, across Blender's two names for it."""
    if hasattr(constraint, "mute"):
        constraint.mute = muted
    if hasattr(constraint, "enabled"):
        constraint.enabled = not muted


def targeting_ignored(scene=None):
    """Whether the scene has target frames switched off altogether."""
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    return bool(getattr(scene, "ls3d_targets_ignored", False))


def apply_target_aim(target_obj, scene=None):
    """Mute or unmute the constraints this target owns, live.

    A Track To wins over whatever an animation says, so a linked object stays
    pinned on the target instead of turning the way the animation turns it.
    Switching the aim off leaves the links in place - they still export - and
    simply stops them steering anything while the animation is worked on.

    The scene-wide switch wins: with it on, nothing aims at anything however
    the individual targets are set.
    """
    muted = (targeting_ignored(scene)
             or not getattr(target_obj, "ls3d_target_enabled", True))
    name = f"{CONSTRAINT_PREFIX}{target_obj.name}"
    for entry in target_obj.ls3d_target_objects:
        constraints = _owner_constraints(entry)
        if constraints is None:
            continue
        for constraint in constraints:
            if constraint.type == "TRACK_TO" and constraint.name == name:
                set_constraint_muted(constraint, muted)


def _on_target_enabled(self, context):
    apply_target_aim(self, getattr(context, "scene", None))


def apply_all_target_aims(scene):
    """Re-apply every target frame's aim, after the scene-wide switch moves."""
    touched = 0
    for obj in scene.objects:
        if getattr(obj, "ls3d_target_objects", None):
            apply_target_aim(obj, scene)
            touched += 1
    return touched


def sync_track_to_constraints(target_obj):
    """Rebuild the Track To constraints for every object linked to *target_obj*."""
    for entry in target_obj.ls3d_target_objects:
        constraints = _owner_constraints(entry)
        if constraints is None:
            continue
        _remove_our_constraints(constraints)

    for entry in target_obj.ls3d_target_objects:
        constraints = _owner_constraints(entry)
        if constraints is None:
            continue
        constraint = constraints.new("TRACK_TO")
        constraint.name = f"{CONSTRAINT_PREFIX}{target_obj.name}"
        constraint.target = target_obj
        constraint.track_axis = "TRACK_Y"
        constraint.up_axis = "UP_Z"
        set_constraint_muted(
            constraint, not getattr(target_obj, "ls3d_target_enabled", True))


class LS3D_OT_AddTargetObject(bpy.types.Operator):
    """Link an object or joint to this target frame"""

    bl_idname = "ls3d.add_target_object"
    bl_label = "Add Target Link"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object
        raw = obj.ls3d_target_add_name.strip()
        if not raw:
            self.report({"WARNING"}, "Enter an object name, or 'armature:bone'.")
            return {"CANCELLED"}

        if ":" in raw:
            result = self._add_bone_link(obj, raw)
        else:
            result = self._add_object_link(obj, raw)
        if result != {"FINISHED"}:
            return result

        obj.ls3d_target_objects_index = len(obj.ls3d_target_objects) - 1
        obj.ls3d_target_add_name = ""
        sync_track_to_constraints(obj)
        return {"FINISHED"}

    def _add_bone_link(self, obj, raw):
        armature_name, bone_name = raw.split(":", 1)
        armature = bpy.data.objects.get(armature_name)
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, f"Armature '{armature_name}' not found.")
            return {"CANCELLED"}
        if bone_name not in armature.data.bones:
            self.report({"ERROR"},
                        f"Joint '{bone_name}' not found on '{armature_name}'.")
            return {"CANCELLED"}
        for entry in obj.ls3d_target_objects:
            if entry.target_armature == armature and entry.bone_name == bone_name:
                self.report({"WARNING"}, f"'{bone_name}' is already linked.")
                return {"CANCELLED"}

        entry = obj.ls3d_target_objects.add()
        entry.name = bone_name
        entry.target_armature = armature
        entry.bone_name = bone_name
        return {"FINISHED"}

    def _add_object_link(self, obj, name):
        target = bpy.data.objects.get(name)
        if not target:
            self.report({"ERROR"}, f"Object '{name}' not found.")
            return {"CANCELLED"}
        if target == obj:
            self.report({"WARNING"}, "A target cannot link to itself.")
            return {"CANCELLED"}
        for entry in obj.ls3d_target_objects:
            if entry.target_object == target:
                self.report({"WARNING"}, f"'{name}' is already linked.")
                return {"CANCELLED"}

        entry = obj.ls3d_target_objects.add()
        entry.name = name
        entry.target_object = target
        return {"FINISHED"}


class LS3D_OT_RemoveTargetObject(bpy.types.Operator):
    """Remove the selected link from this target frame"""

    bl_idname = "ls3d.remove_target_object"
    bl_label = "Remove Target Link"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object
        index = obj.ls3d_target_objects_index
        if not (0 <= index < len(obj.ls3d_target_objects)):
            return {"CANCELLED"}

        entry = obj.ls3d_target_objects[index]
        constraints = _owner_constraints(entry)
        if constraints is not None:
            _remove_our_constraints(constraints, f"{CONSTRAINT_PREFIX}{obj.name}")

        obj.ls3d_target_objects.remove(index)
        obj.ls3d_target_objects_index = max(0, index - 1)
        return {"FINISHED"}


CLASSES = (LS3D_OT_AddTargetObject, LS3D_OT_RemoveTargetObject)
