"""Operators for building and editing 4DS morph groups.

A morph group is an ordered list of shape keys: the first is the group's basis,
the rest are poses blended against it. Groups map to the format's "regions", and
one object can carry several — a face rig typically has separate groups for the
jaw, brows and eyelids.

Group membership is stored as names in ``obj.ls3d_morph_groups``, entirely
separate from Blender's own ``key_blocks`` ordering. Operators here therefore
reorder the *group*, never ``key_blocks`` — moving a shape key to slot 0 in
Blender would silently change which key is the mesh's reference basis.
"""

import math

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty
from mathutils import Vector

from ..properties import active_morph_group

#: The group's basis always occupies this slot; see LS3D_OT_MorphTarget.
BASIS_INDEX = 0

#: Shown when something tries to move the basis out of slot 0.
BASIS_PINNED_MESSAGE = ("The group's basis stays in the first slot. "
                        "Use 'Apply as Basis' to make another target the basis.")

#: Name an imported morph region's vertex group starts with, followed by the
#: region's number. The file lists a region's vertices whether they move or
#: not, so the group - not which vertices happen to move - says which belong.
MORPH_VERTEX_GROUP_PREFIX = "Morph Region "


class LS3D_OT_MorphSelectToggle(bpy.types.Operator):
    """Toggle selection of this morph target"""

    bl_idname = "ls3d.morph_select_toggle"
    bl_label = "Toggle Morph Selection"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    target_index: IntProperty(default=-1, options={"HIDDEN"})

    def execute(self, context):
        group, _ = active_morph_group(context.object)
        if group is None or not (0 <= self.target_index < len(group.targets)):
            return {"CANCELLED"}
        target = group.targets[self.target_index]
        target.select = not target.select
        return {"FINISHED"}


class LS3D_OT_MorphGroup(bpy.types.Operator):
    """Add or remove a morph group"""

    bl_idname = "ls3d.morph_group"
    bl_label = "Morph Group"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(
        items=[("ADD", "Add", ""), ("REMOVE", "Remove", "")],
        options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "MESH"

    def execute(self, context):
        obj = context.object
        groups = obj.ls3d_morph_groups

        if self.action == "ADD":
            group = groups.add()
            group.name = f"Group {len(groups)}"
            obj.ls3d_active_morph_group = len(groups) - 1
        else:
            index = obj.ls3d_active_morph_group
            if not (0 <= index < len(groups)):
                return {"CANCELLED"}
            groups.remove(index)
            obj.ls3d_active_morph_group = max(0, index - 1)
        return {"FINISHED"}


class LS3D_OT_MorphTarget(bpy.types.Operator):
    """Add, remove, or reorder shape keys within the active morph group"""

    bl_idname = "ls3d.morph_target"
    bl_label = "Morph Target"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(
        items=[("ADD", "Add", ""), ("REMOVE", "Remove", ""),
               ("UP", "Up", ""), ("DOWN", "Down", "")],
        options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == "MESH" and obj.mode != "EDIT"

    def execute(self, context):
        obj = context.object
        group, index = active_morph_group(obj)
        if group is None:
            return {"CANCELLED"}

        if self.action == "ADD":
            # Always a new one. Taking a shape key that is already on the mesh
            # is its own button beside this, because it is its own intention -
            # and a mesh whose keys are all loose is the usual case right
            # after emptying a group, where a picker is the last thing wanted.
            return _add_new_target(self, context)

        if self.action == "REMOVE":
            return _remove_targets(self, context, group, index)

        # Reordering affects this group's export order only. Blender's own
        # key_blocks order is deliberately left alone.
        #
        # Slot 0 is the group's basis and is pinned there: the whole group is
        # written relative to it, and the list labels that row "Basis" by
        # position. Letting it move - or letting slot 1 move up into it - would
        # silently rebase the group without touching any geometry. Use
        # "Apply as Basis", which rebases the shape keys as well.
        if self.action == "UP":
            if index <= BASIS_INDEX:
                self.report({"WARNING"}, BASIS_PINNED_MESSAGE)
                return {"CANCELLED"}
            if index == BASIS_INDEX + 1:
                self.report({"WARNING"},
                            "That would push the basis out of the first slot. "
                            + BASIS_PINNED_MESSAGE)
                return {"CANCELLED"}
            group.targets.move(index, index - 1)
            group.active_target_index = index - 1
        elif self.action == "DOWN":
            if index == BASIS_INDEX:
                self.report({"WARNING"}, BASIS_PINNED_MESSAGE)
                return {"CANCELLED"}
            if index >= len(group.targets) - 1:
                return {"CANCELLED"}
            group.targets.move(index, index + 1)
            group.active_target_index = index + 1
        return {"FINISHED"}


class LS3D_OT_MorphAddExisting(bpy.types.Operator):
    """Add an existing shape key to the active morph group"""

    bl_idname = "ls3d.morph_add_existing"
    bl_label = "Add Existing Shape Key"
    bl_options = {"REGISTER", "UNDO"}

    shape_key_name: StringProperty(name="Shape Key", default="")

    @classmethod
    def poll(cls, context):
        obj = context.object
        if not obj or obj.type != "MESH" or obj.mode == "EDIT":
            return False
        group, _ = active_morph_group(obj)
        return group is not None

    def _available(self, context):
        obj = context.object
        keys = obj.data.shape_keys if obj and obj.data else None
        if not keys:
            return []
        group, _ = active_morph_group(obj)
        assigned = {t.shape_key_name for t in group.targets} if group else set()
        return [kb for kb in keys.key_blocks if kb.name not in assigned]

    def invoke(self, context, event):
        if not self._available(context):
            self.report({"WARNING"},
                        "No shape key on this mesh that the group does not "
                        "already hold. Add makes a new one.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Add a shape key already on this mesh:")
        column = layout.column(align=True)
        keys = context.object.data.shape_keys
        reference = keys.reference_key.name if keys.reference_key else ""
        for key_block in self._available(context):
            op = column.operator(
                "ls3d.morph_add_existing_pick", text=key_block.name,
                icon="KEY_HLT" if key_block.name == reference else "SHAPEKEY_DATA")
            op.shape_key_name = key_block.name

    def execute(self, context):
        return _add_target(self, context, self.shape_key_name)


class LS3D_OT_MorphNewTarget(bpy.types.Operator):
    """Make a new shape key and add it to the active morph group"""

    bl_idname = "ls3d.morph_new_target"
    bl_label = "New Shape Key"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        if not obj or obj.type != "MESH" or obj.mode == "EDIT":
            return False
        group, _ = active_morph_group(obj)
        return group is not None

    def execute(self, context):
        return _add_new_target(self, context)


class LS3D_OT_MorphAddExistingPick(bpy.types.Operator):
    """Add this shape key to the morph group"""

    bl_idname = "ls3d.morph_add_existing_pick"
    bl_label = "Pick Shape Key"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    shape_key_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        return _add_target(self, context, self.shape_key_name)


def _group_number(obj, group):
    """Which group this is, counting from one, as the import numbers them."""
    for index, other in enumerate(obj.ls3d_morph_groups, start=1):
        if other == group:
            return index
    return len(obj.ls3d_morph_groups)


def new_shape_key(obj, group):
    """A shape key of this group's own, named the way an import names them.

    The first one a group gets is its basis - the shape the whole group is
    written against - and the rest are its targets, numbered from one. Built
    from the mesh as it stands rather than from the mix, so a new target
    starts as the shape it is about to be pulled out of.
    """
    number = _group_number(obj, group)
    stem = (f"G{number}_Basis" if not group.targets
            else f"G{number}_Target{len(group.targets)}")
    keys = obj.data.shape_keys
    name, count = stem, 1
    while keys is not None and name in keys.key_blocks:
        count += 1
        name = f"{stem}.{count:03d}"
    return obj.shape_key_add(name=name, from_mix=False)


def _add_new_target(operator, context):
    """Make a shape key and put it in the group, for when there is none to pick."""
    obj = context.object
    group, _ = active_morph_group(obj)
    if group is None:
        return {"CANCELLED"}
    key = new_shape_key(obj, group)
    if key is None:
        operator.report({"WARNING"}, "Blender would not make a shape key here.")
        return {"CANCELLED"}
    # A mesh with no shape keys at all gets its first one now, so the group's
    # basis is a real shape and not a name pointing at nothing.
    return _add_target(operator, context, key.name)


def _delete_shape_key(obj, name):
    """Take one shape key off the mesh, whatever is active at the time."""
    keys = obj.data.shape_keys
    if keys is None or name not in keys.key_blocks:
        return False
    was = obj.active_shape_key_index
    obj.active_shape_key_index = keys.key_blocks.find(name)
    try:
        bpy.ops.object.shape_key_remove(all=False)
    except RuntimeError:
        obj.active_shape_key_index = was
        return False
    keys = obj.data.shape_keys
    count = len(keys.key_blocks) if keys else 0
    obj.active_shape_key_index = max(0, min(was, count - 1))
    return True


def _remove_targets(operator, context, group, index):
    """Take the picked targets out of the group, and off the mesh with them.

    Removing a target is removing the morph: a shape key left behind after
    its target is gone is a shape nothing plays and nobody asked to keep. One
    that another group still holds is kept all the same - deleting it would
    empty a group that is in use - and the message says which.
    """
    obj = context.object
    going = [t.shape_key_name for t in group.targets if t.select]
    if not going and 0 <= index < len(group.targets):
        going = [group.targets[index].shape_key_name]
    if not going:
        return {"CANCELLED"}

    was_basis = bool(group.targets) and group.targets[0].shape_key_name in going
    kept = []
    for name in going:
        position = next((i for i, t in enumerate(group.targets)
                         if t.shape_key_name == name), -1)
        if position >= 0:
            group.targets.remove(position)
        held_elsewhere = any(
            target.shape_key_name == name
            for other in obj.ls3d_morph_groups if other != group
            for target in other.targets)
        if held_elsewhere:
            kept.append(name)
            continue
        _delete_shape_key(obj, name)

    group.active_target_index = max(
        0, min(group.active_target_index, len(group.targets) - 1))
    if kept:
        operator.report(
            {"INFO"},
            f"{', '.join(kept)} stayed on the mesh: another morph group "
            f"holds it.")
    elif was_basis and group.targets:
        operator.report(
            {"WARNING"},
            f"That was this group's basis, so '{group.targets[0].shape_key_name}' "
            f"is its basis now and the rest are written against it.")
    return {"FINISHED"}


def _unassigned(obj, group):
    """The mesh's shape keys this group does not already hold."""
    keys = obj.data.shape_keys if obj and obj.data else None
    if not keys:
        return []
    held = {target.shape_key_name for target in group.targets}
    return [block for block in keys.key_blocks if block.name not in held]


def _add_target(operator, context, name):
    obj = context.object
    keys = obj.data.shape_keys if obj and obj.data else None
    group, _ = active_morph_group(obj)
    if group is None or keys is None:
        return {"CANCELLED"}
    if not name or name not in keys.key_blocks:
        operator.report({"WARNING"}, f"Shape key '{name}' not found.")
        return {"CANCELLED"}
    if any(t.shape_key_name == name for t in group.targets):
        operator.report({"WARNING"}, f"'{name}' is already in this group.")
        return {"CANCELLED"}
    target = group.targets.add()
    target.shape_key_name = name
    group.active_target_index = len(group.targets) - 1
    return {"FINISHED"}


class LS3D_OT_MorphTransfer(bpy.types.Operator):
    """Copy the active morph target from every other selected object onto this one.

    Select the source morph on the other object, shift-select this object to
    make it active, then press Transfer.
    """

    bl_idname = "ls3d.morph_transfer"
    bl_label = "Transfer Morph"
    bl_options = {"REGISTER", "UNDO"}

    #: How far outside the target's bounding box a transferred vertex may land
    #: before the meshes are judged to have different topology, as a multiple of
    #: the box diagonal.
    BOUNDS_TOLERANCE = 2.0

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (obj is not None and obj.type == "MESH"
                and obj.mode != "EDIT" and len(context.selected_objects) > 1)

    def execute(self, context):
        obj = context.object
        group, _ = active_morph_group(obj)
        if group is None:
            self.report({"ERROR"}, "No active morph group on the target object.")
            return {"CANCELLED"}
        if not obj.data.vertices:
            self.report({"ERROR"}, "Target object has no geometry.")
            return {"CANCELLED"}

        if obj.data.shape_keys is None:
            obj.shape_key_add(name="Basis", from_mix=False)
            basis = group.targets.add()
            basis.shape_key_name = obj.data.shape_keys.key_blocks[0].name

        transferred, failures = 0, []
        for source in context.selected_objects:
            if source is obj or source.type != "MESH":
                continue
            reason = self._transfer_one(obj, group, source)
            if reason:
                failures.append((source.name, reason))
            else:
                transferred += 1

        if transferred:
            group.active_target_index = len(group.targets) - 1

        if transferred and not failures:
            self.report({"INFO"}, f"Transferred {transferred} morph(s).")
        elif transferred:
            detail = "; ".join(f"{n}: {r}" for n, r in failures)
            self.report({"WARNING"},
                        f"Transferred {transferred}, skipped {len(failures)} - {detail}")
        else:
            detail = "; ".join(f"{n}: {r}" for n, r in failures) or "nothing to do"
            self.report({"ERROR"}, f"Nothing transferred - {detail}")
        return {"FINISHED"}

    def _transfer_one(self, obj, group, source):
        """Copy one morph. Returns a failure reason, or ``None`` on success."""
        if not source.data.vertices:
            return "has no geometry"

        source_keys = source.data.shape_keys
        if not source_keys:
            return "has no morph targets"

        source_key = source.active_shape_key
        if not source_key:
            return "no active morph target selected"
        if source_key == source_keys.reference_key:
            return "cannot transfer the basis morph"

        vertex_count = len(obj.data.vertices)
        if len(source_key.data) != vertex_count:
            return (f"vertex count mismatch ({len(source_key.data)} on source vs "
                    f"{vertex_count} on target) - transfer needs identical topology")

        if any(not all(map(math.isfinite, point.co)) for point in source_key.data):
            return f"morph '{source_key.name}' contains NaN or infinite coordinates"

        new_key = obj.shape_key_add(name=source_key.name, from_mix=False)
        for i in range(vertex_count):
            new_key.data[i].co = source_key.data[i].co.copy()
        new_key.value = source_key.value
        new_key.interpolation = source_key.interpolation
        new_key.slider_min = source_key.slider_min
        new_key.slider_max = source_key.slider_max

        if not self._within_bounds(obj, new_key):
            obj.shape_key_remove(new_key)
            return ("morph positions land far outside the target's bounds - the "
                    "meshes likely differ in topology despite matching vertex counts")

        entry = group.targets.add()
        entry.shape_key_name = new_key.name
        return None

    def _within_bounds(self, obj, key_block):
        low = Vector(obj.bound_box[0])
        high = Vector(obj.bound_box[6])
        center = (low + high) * 0.5
        limit = (high - low).length * self.BOUNDS_TOLERANCE + 1.0
        return all((point.co - center).length <= limit for point in key_block.data)


class LS3D_OT_MorphMakeBasis(bpy.types.Operator):
    """Make the active target this group's basis, rebasing the group onto it.

    Only shape keys belonging to the active group move. The original version
    shifted every key on the object, which silently displaced every other
    group's targets by this group's delta.
    """

    bl_idname = "ls3d.morph_make_basis"
    bl_label = "Apply as Basis"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        if not obj or obj.type != "MESH" or not obj.data:
            return False
        if not obj.data.shape_keys:
            return False
        group, index = active_morph_group(obj)
        return group is not None and index > 0

    def execute(self, context):
        obj = context.object
        keys = obj.data.shape_keys
        group, index = active_morph_group(obj)

        new_basis = keys.key_blocks.get(group.targets[index].shape_key_name)
        old_basis = keys.key_blocks.get(group.targets[0].shape_key_name)
        if new_basis is None or old_basis is None:
            self.report({"ERROR"}, "This group's basis or active target is missing.")
            return {"CANCELLED"}
        if new_basis is old_basis:
            return {"CANCELLED"}

        count = len(old_basis.data)
        delta = [new_basis.data[i].co - old_basis.data[i].co for i in range(count)]

        # Shift only the keys this group owns, and only once each even if a name
        # is listed twice.
        members, seen = [], set()
        for target in group.targets:
            block = keys.key_blocks.get(target.shape_key_name)
            if block is not None and block.name not in seen:
                seen.add(block.name)
                members.append(block)

        for block in members:
            if block is new_basis:
                continue
            for i in range(count):
                block.data[i].co = block.data[i].co + delta[i]

        # The new basis now sits where the old one did: zero displacement.
        for i in range(count):
            new_basis.data[i].co = old_basis.data[i].co.copy()

        group.targets.move(index, 0)
        group.active_target_index = 0
        obj.data.update()
        return {"FINISHED"}


CLASSES = (
    LS3D_OT_MorphSelectToggle,
    LS3D_OT_MorphGroup,
    LS3D_OT_MorphTarget,
    LS3D_OT_MorphAddExisting,
    LS3D_OT_MorphNewTarget,
    LS3D_OT_MorphAddExistingPick,
    LS3D_OT_MorphTransfer,
    LS3D_OT_MorphMakeBasis,
)
