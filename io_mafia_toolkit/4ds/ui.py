"""The 4DS sidebar: a model's frames, its materials and its morphs."""

import math
import os
import re

import bpy

from ..common import constants as C
from ..common.panels import say
from ..packages import module
from ..properties import active_morph_group
from ..ui import _show_raw_flags, _show_reserved_flags
from . import ops_lensflare
from .validation import mirror_facing, text_length
from .joint_space import mesh_frame_channels, skinned_mesh_of
from . import influence
from .viewport import (carries_frame, dummy_box_is_drawable,
                       has_mirror_box, instance_plan,
                       is_portal, mesh_sharers)

#: Which setting blocks each visual type exposes, in display order.
VISUAL_BLOCKS = {
    C.VISUAL_OBJECT:      ("instance", "render", "logic", "cull", "lod", "user"),
    C.VISUAL_LITOBJECT:   ("litobject", "instance", "render", "logic", "cull",
                           "lod", "user"),
    C.VISUAL_MIRROR:      ("mirror", "render", "logic", "cull", "user"),
    C.VISUAL_BILLBOARD:   ("instance", "billboard", "render", "logic", "cull",
                           "lod", "user"),
    C.VISUAL_LENSFLARE:   ("lensflare", "render", "logic", "cull", "user"),
    C.VISUAL_PROJECTOR:   ("projector", "render", "logic", "cull", "user"),
    C.VISUAL_SINGLEMESH:  ("render", "logic", "cull", "lod", "user"),
    C.VISUAL_SINGLEMORPH: ("render", "logic", "cull", "lod", "user"),
    C.VISUAL_MORPH:       ("render", "logic", "cull", "lod", "user"),
}

#: Which setting blocks each non-visual frame type exposes.
FRAME_BLOCKS = {
    C.FRAME_SECTOR:   ("sector", "cull", "user"),
    C.FRAME_DUMMY:    ("dummy", "cull", "user"),
    C.FRAME_OCCLUDER: ("occluder", "cull", "user"),
    C.FRAME_EMITOR:   ("emitor", "lod", "cull", "user"),
    C.FRAME_LIGHT:    ("light", "cull", "user"),
    C.FRAME_TARGET:   ("target", "cull", "user"),
}
FRAME_BLOCKS.update({frame_type: ("bare", "cull", "user")
                     for frame_type in C.PAYLOADLESS_FRAME_TYPES})

PORTAL_BLOCKS = ("portal", "cull", "user")

#: Blender's uniquifying suffix, e.g. "WOOD.BMP.001".
_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")


def _int_prop(obj, name, default=0):
    try:
        return int(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


def _float_prop(obj, name, default=0.0):
    try:
        return float(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


def _has_diffuse(material):
    """Whether *material* will write a diffuse texture name."""
    from .codec.material import has_diffuse_texture
    return bool(getattr(material, "ls3d_diffuse_tex", None) is not None
                and has_diffuse_texture(
                    getattr(material, "ls3d_material_flags", 0) & 0xFFFFFFFF))


def _frame_type_of(obj):
    """The object's 4DS frame type as an int, defaulting to Visual."""
    return _int_prop(obj, "ls3d_frame_type", C.FRAME_VISUAL)


def _raw_field(box, target, prop_name, label="Raw Value"):
    """Direct numeric access to a flag byte, for bits the panel does not list.

    Every bit still round-trips whether or not this field is visible - the
    checkboxes are views onto the same value, and editing either one moves the
    other.
    """
    if _show_raw_flags():
        box.prop(target, prop_name, text=label)


def _reserved_grid(box, target, table, raw_prop, title="Engine Internals",
                   columns=2):
    """A switch for every bit in *table*, none of them out of reach.

    With the preference on, all of them are listed. Otherwise only the ones
    this file has switched on are - so a flag a model really uses is never
    hidden - and the rest are left out altogether; the raw field above still
    reaches every bit of the word.
    """
    value = getattr(target, raw_prop, 0) & 0xFFFFFFFF
    if _show_reserved_flags():
        box.separator()
        box.label(text=title, icon="LOCKED")
        _flag_grid(box, target, [row[1] for row in table], columns)
        return
    on = [attr for mask, attr, _label, _desc in table if value & mask]
    if on:
        box.separator()
        box.label(text=f"{title} (on in this file)", icon="LOCKED")
        _flag_grid(box, target, on, columns)


def _flag_grid(box, target, names, columns=2):
    grid = box.grid_flow(columns=columns, align=True)
    for name in names:
        grid.prop(target, name, toggle=True)



#: A line or two under the light type for each kind, saying what it does in game.
_LIGHT_KIND_NOTES = {
    C.LIGHT_NONE: ("Does nothing in game.",),
    C.LIGHT_POINT: ("Shines every way from where it is.",),
    C.LIGHT_SPOT: ("Shines in a cone from where it is.",),
    C.LIGHT_DIRECTIONAL: ("Shines one way from nowhere in particular;",
                          "only its heading is read."),
    C.LIGHT_AMBIENT: ("Adds its color, times its power, evenly to",
                      "everything in the sectors it lights."),
    C.LIGHT_FOG: ("Linear fog for the sectors it lights.",
                  "Nothing is lit by it."),
    C.LIGHT_POINTAMBIENT: ("Exponential fog for the sectors it lights.",
                           "Despite the name, nothing is lit by it."),
    C.LIGHT_POINTFOG: ("Exponential-squared fog for the sectors it",
                       "lights. Nothing is lit by it."),
    C.LIGHT_LAYEREDFOG: ("Draws no fog and lights nothing. The game",
                         "only counts it toward how dark shadows are."),
}

#: The kinds that light meshes, and every kind that does anything at all.
_LIGHTING_KINDS = frozenset({C.LIGHT_POINT, C.LIGHT_SPOT, C.LIGHT_DIRECTIONAL,
                             C.LIGHT_AMBIENT})
_ANY_KIND = frozenset(range(C.LIGHT_POINT, C.LIGHT_TYPE_COUNT))
_LIGHTING_OR_LAYERED = _LIGHTING_KINDS | {C.LIGHT_LAYEREDFOG}

#: The folded "Switching It On" section under a light, one row per requirement:
#: (what, which kinds it is for, the kinds as a set). Grounds, in the game:
#: a model with animated objects fails its load without its .5ds and its
#: frames are never indexed; a light that is off is skipped; lighting skips a
#: light with no power or without the mode switch for that kind of mesh; a
#: layered fog is only read by the shadow pass, which wants Affects Shadows;
#: color times power, a spot's reach and a layered fog's range are only worked
#: out when the datablock sets them; a second copy of a model file in the same
#: mission is duplicated without its mode.
_LIGHT_MODEL_NEEDS = (
    ("Animated Objects 0, or its .5ds beside it", "All kinds", _ANY_KIND),
    ("Enable on", "All kinds", _ANY_KIND),
    ("Power not 0", "Lighting kinds, Layered Fog", _LIGHTING_OR_LAYERED),
    ("Lights Objects on", "Lighting kinds, to light meshes", _LIGHTING_KINDS),
    ("Lights Lit Objects on", "Lighting kinds, to light lit objects",
     _LIGHTING_KINDS),
    ("Affects Shadows on", "Layered Fog", frozenset({C.LIGHT_LAYEREDFOG})),
)
_LIGHT_DATABLOCK_NEEDS = (
    ("Sector", "All kinds", _ANY_KIND),
    ("Color or Power", "Lighting kinds, Layered Fog", _LIGHTING_OR_LAYERED),
    ("Range or Cone", "Spot", frozenset({C.LIGHT_SPOT})),
    ("Range", "Layered Fog", frozenset({C.LIGHT_LAYEREDFOG})),
    ("Mode, if the model file is used twice", "Lighting kinds, Layered Fog",
     _LIGHTING_OR_LAYERED),
)


def _light_requirement(column, text, kinds, applies, icon="DOT"):
    """One requirement and the kinds it is for, dimmed when not this light's."""
    pair = column.column(align=True)
    pair.active = applies
    pair.label(text=text, icon=icon)
    pair.label(text=kinds, icon="BLANK1")


class The4DSObjectPanel(bpy.types.Panel):
    bl_label = "4DS Object Properties"
    bl_idname = "OBJECT_PT_4ds"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    def draw(self, context):
        layout = self.layout
        obj = context.object
        if obj is None:
            return

        if obj.type == "ARMATURE":
            self._draw_armature(layout, obj)
            return

        # An empty whose shape does not stand for a frame is somebody's own
        # helper. It gets no 4DS settings, because it is not going into the
        # model and offering it any would only suggest otherwise.
        if not carries_frame(obj):
            box = layout.box()
            box.label(text="Not a 4DS Frame", icon="BLANK1")
            note = box.column()
            note.scale_y = 0.8
            say(note, "An empty stands for a frame by its shape, and this one "
                      "is not one of them:")
            for display, means in sorted(C.EMPTY_FRAME_DISPLAYS.items()):
                note.label(text=f"   {display.replace('_', ' ').title()} - {means}")
            note.label(text="Change Display As to use it in the model.")
            return

        if module("tck.io").is_motion_track(obj):
            box = layout.box()
            box.label(text="Movement Track", icon="ANIM")
            say(box, "Not a 4DS frame, so it has no settings of its own and "
                     "is never written into the model. Its animation becomes "
                     "the .tck beside the animation.")
            box.separator()
            box.prop(obj, "ls3d_is_motion_track", text="Movement Track",
                     toggle=True, icon="ANIM")
            return

        # A shadow piece is not a frame of the model - it goes into the .6ds
        # beside it - so it is offered none of the settings below.
        shadow_io = module("6ds.io")
        if shadow_io.is_shadow_piece(obj):
            box = layout.box()
            box.label(text="Shadow Piece", icon="MOD_CAST")
            box.prop(obj, "ls3d_is_shadow", text="Shadow Piece",
                     toggle=True, icon="MOD_CAST")
            note = box.column()
            note.scale_y = 0.8
            say(note, "Part of the model's shadow, written into the .6ds "
                      "beside it and never into the model. The game matches "
                      "it to a frame by name.")
            return

        if obj.type == "MESH":
            mark = layout.row()
            mark.operator("ls3d.mark_shadow_piece", icon="MOD_CAST")

        layout.separator()
        layout.prop(obj, "ls3d_frame_type", text="Frame Type")

        frame_type = _int_prop(obj, "ls3d_frame_type", -1)
        if frame_type < 0:
            return

        if frame_type == C.FRAME_VISUAL:
            layout.prop(obj, "visual_type", text="Visual Type")
            blocks = VISUAL_BLOCKS.get(_int_prop(obj, "visual_type"), ())
        elif frame_type == C.FRAME_SECTOR and is_portal(obj):
            blocks = PORTAL_BLOCKS
        else:
            blocks = FRAME_BLOCKS.get(frame_type, ())

        for block in blocks:
            getattr(self, f"_draw_{block}")(layout, obj)

    def _draw_armature(self, layout, obj):
        if obj.mode != "POSE":
            box = layout.box()
            say(box, "This object holds 4DS joints. Enter Pose Mode and "
                     "select a joint to edit its joint properties.",
                icon="BONE_DATA")
            box.separator()
            say(box, "Use the 4DS joint scale property rather than scaling "
                     "the joint.")
            self._draw_mesh_frame(layout, obj)
            present = influence.boxes_of(obj)
            missing = [b.name for b in obj.data.bones if b.name not in present]
            box = layout.box()
            box.label(text="Influence Boxes", icon="CUBE")
            if missing:
                box.label(text=f"{len(missing)} joint(s) have none; the export "
                               f"needs one on each", icon="ERROR")
                box.operator("ls3d.add_influence_boxes", icon="ADD")
            else:
                box.label(text="Every joint has one.", icon="CHECKMARK")
            return

        active = obj.data.bones.active
        bone = obj.pose.bones.get(active.name) if active else None
        if bone is None:
            box = layout.box()
            say(box, "No joint selected. Select a joint in the viewport.",
                icon="INFO")
            return

        layout.label(text=f"Joint: {bone.name}", icon="BONE_DATA")
        self._draw_joint(layout, bone)

    def _draw_mesh_frame(self, layout, armature):
        """Where the armature's skinned mesh has its frame."""
        box = layout.box()
        box.label(text="Mesh Frame", icon="OUTLINER_OB_MESH")
        place, turn, size = mesh_frame_channels(armature)
        say(box, f"Stands {place[2]:.3f} m up, {place[1]:.3f} m forward, "
                 f"{place[0]:.3f} m to the side", icon="BLANK1")
        if tuple(turn) != (1.0, 0.0, 0.0, 0.0) or tuple(size) != (1.0, 1.0, 1.0):
            box.label(text="and turned or scaled, as its file had it",
                      icon="BLANK1")
        note = box.column()
        note.scale_y = 0.8
        say(note, "The armature stands where the skinned mesh's frame is: "
                  "the point the joints hang from and the game's animations "
                  "move and turn the body about - on a character, at hip "
                  "height. Its Location, Rotation and Scale stay at nothing, "
                  "for that animation. The mesh's own origin can be anywhere. "
                  "Set Default Mesh Origin, in the 4DS Model tab, stands it "
                  "where it belongs.")

    def _draw_joint(self, layout, bone):
        if "ls3d_joint_scale" not in bone:
            bone["ls3d_joint_scale"] = (1.0, 1.0, 1.0)
        box = layout.box()
        box.label(text="Joint Scale", icon="FULLSCREEN_ENTER")
        row = box.row(align=True)
        for index, axis in enumerate("XYZ"):
            row.prop(bone, '["ls3d_joint_scale"]', index=index, text=axis)

        box = layout.box()
        box.label(text="Frame Flags", icon="PROPERTIES")
        _raw_field(box, bone, "cull_flags_str", "Raw Flags")
        _flag_grid(box, bone, ("cf_enabled", "cf_pos_locked"))
        _reserved_grid(box, bone, C.RESERVED_CULL_FLAGS, "cull_flags")

        layout.box().prop(bone, "user_props", text="User Props", icon="TEXT")
        self._draw_influence(layout, bone)

    def _draw_influence(self, layout, bone):
        """The joint's influence box: where it sits, how big, how turned."""
        armature = bone.id_data
        skin = skinned_mesh_of(armature, bpy.context.scene.objects)
        painted = False
        if skin is not None:
            group = skin.vertex_groups.get(bone.name)
            painted = group is not None and any(
                entry.group == group.index and entry.weight > 0.0
                for vertex in skin.data.vertices for entry in vertex.groups)

        box = layout.box()
        header = box.row(align=True)
        header.label(text="Influence Box", icon="CUBE")
        if influence.has_box(armature, bone.name):
            header.operator("ls3d.fit_influence_box", text="",
                            icon="FULLSCREEN_ENTER")
            header.operator("ls3d.remove_influence_box", text="", icon="X")
            fields = box.column(align=True)
            for label, part in (("Center", "center"), ("Size", "size"),
                                ("Turn", "turn")):
                row = fields.row(align=True)
                row.label(text=label)
                for index in range(3):
                    row.prop(bone, f"ls3d_box_{part}_{index}", text="")
            say(box, "Drag the handles on the box to move, turn or resize "
                     "it in the viewport.", icon="BLANK1")
        else:
            # Only a joint with nothing painted needs one: it is what its
            # weights would be made from. A painted joint is written the box a
            # new joint is made with, which the game never reads.
            box.label(text=("None - the default one is written" if painted
                            else "None - the export needs one"),
                      icon="INFO" if painted else "ERROR")
            box.operator("ls3d.add_influence_box", icon="ADD")
        if skin is not None:
            box.label(text=("Weights: painted on the mesh" if painted
                            else "Weights: made from the box"),
                      icon="MOD_VERTEX_WEIGHT")
        note = box.column()
        note.scale_y = 0.8
        say(note, "Painted weights export as they are. A joint with none has "
                  "them made from its box: shared with its parent inside, by "
                  "where they sit along the box's Y; its own alone ahead of "
                  "the +Y face.")

    # ── setting blocks ────────────────────────────────────────────────────────
    def _draw_render(self, layout, obj):
        box = layout.box()
        box.label(text="Rendering Flags", icon="RESTRICT_RENDER_OFF")
        _raw_field(box, obj, "render_flags_str", "Raw Flags")
        _flag_grid(box, obj, ("rf1_flat_light", "rf1_no_mirror",
                              "rf1_managed_lod"))
        _reserved_grid(box, obj, C.RESERVED_RENDER_FLAGS, "render_flags")

    def _draw_logic(self, layout, obj):
        box = layout.box()
        box.label(text="Visual Flags", icon="MODIFIER")
        _raw_field(box, obj, "render_flags2_str", "Raw Flags")
        _flag_grid(box, obj, (
            "rf2_zbias", "rf2_is_mesh_object",
            "rf2_shadow_diffuse", "rf2_shadow_alpha",
            "rf2_projection_diffuse", "rf2_projection_alpha",
            "rf2_no_fog", "rf2_no_twosided_collision"))

    def _draw_cull(self, layout, obj):
        box = layout.box()
        box.label(text="Frame Flags", icon="PROPERTIES")
        _raw_field(box, obj, "cull_flags_str", "Raw Flags")
        _flag_grid(box, obj, ("cf_enabled", "cf_pos_locked"))
        _reserved_grid(box, obj, C.RESERVED_CULL_FLAGS, "cull_flags")

    def _draw_mirror(self, layout, obj):
        box = layout.box()
        box.label(text="Mirror", icon="MOD_MIRROR")
        facing = mirror_facing(obj)
        if facing == "BACK":
            say(box, "Surface faces local -Y: reflects from behind",
                icon="ERROR")
        elif facing == "TILTED":
            box.label(text="Surface is not square to local Y",
                      icon="ERROR")
        else:
            box.label(text="Reflects along the object's local +Y",
                      icon="ORIENTATION_NORMAL")
        box.prop(obj, "ls3d_mirror_color", text="Color")
        box.prop(obj, "ls3d_mirror_range", text="Active Range")
        if has_mirror_box(obj):
            column = box.column(align=True)
            column.label(text="Bound (outlined in the viewport):")
            size = tuple(obj.bbox_max[axis] - obj.bbox_min[axis]
                         for axis in range(3))
            column.label(text="Size  X %.3f   Y %.3f   Z %.3f" % size)
            say(column, "The game culls the mirror by this, and trusts it "
                        "rather than measuring the mesh. Recomputed from the "
                        "geometry on export.")
        view = layout.box()
        view.label(text="View Box (outlined in the viewport)", icon="CUBE")
        column = view.column(align=True)
        column.prop(obj, "ls3d_mirror_box_center")
        column = view.column(align=True)
        column.prop(obj, "ls3d_mirror_box_x")
        column.prop(obj, "ls3d_mirror_box_y")
        column.prop(obj, "ls3d_mirror_box_z")
        size = tuple(2.0 * math.sqrt(sum(c * c for c in axis)) for axis in
                     (obj.ls3d_mirror_box_x, obj.ls3d_mirror_box_y,
                      obj.ls3d_mirror_box_z))
        view.label(text="Size  X %.3f   Y %.3f   Z %.3f" % size)
        note = view.column()
        note.scale_y = 0.8
        say(note, "What the mirror is allowed to reflect. Each axis runs from "
                  "the center to a face; drag the faces in the viewport, or "
                  "type the numbers.")
        center = obj.ls3d_mirror_box_center
        axes = (obj.ls3d_mirror_box_x, obj.ls3d_mirror_box_y,
                obj.ls3d_mirror_box_z)
        if not all(any(axis) for axis in axes):
            view.label(text="Not a box yet: an axis has no length",
                       icon="ERROR")
        elif center[1] + sum(abs(axis[1]) for axis in axes) <= 0.0:
            view.label(text="Sits behind the mirror surface", icon="ERROR")
        view.operator("ls3d.fit_view_box", icon="PIVOT_BOUNDBOX")

    def _draw_instance(self, layout, obj):
        """Show whether this frame owns its mesh or reuses another's.

        Only drawn when the mesh is actually shared, so ordinary objects are
        not cluttered with a row that always says "not an instance".
        """
        sharers = mesh_sharers(obj)
        if len(sharers) < 2:
            return

        box = layout.box()
        # The export's own plan, so the panel says what the export will write
        # whether the objects were imported or duplicated by hand.
        plan = instance_plan()
        master = plan.get(obj)

        if master is not None:
            box.label(text="Instancing: COPY", icon="LINK_BLEND")
            box.label(text=f"Master: {master.name}", icon="OBJECT_DATA")
            say(box, "Shares that mesh, so it exports as an instance with no "
                     "geometry.")
            return

        copies = [o for o, owner in plan.items() if owner is obj]
        if copies:
            box.label(text="Instancing: MASTER", icon="OBJECT_DATA")
            plural = "copy" if len(copies) == 1 else "copies"
            box.label(text=f"Owns the mesh; {len(copies)} {plural}:",
                      icon="LINK_BLEND")
            names = sorted(o.name for o in copies)
            shown = ", ".join(names[:3])
            if len(names) > 3:
                shown += f", +{len(names) - 3} more"
            box.label(text=shown)
            return

        # Shares a mesh and is neither: the export writes it out in full. That
        # is a frame whose type is never instanced - a skinned or morphing mesh,
        # or not a visual at all - or one with LOD levels of its own, which an
        # instance cannot carry.
        box.label(text="Instancing: WRITTEN IN FULL", icon="INFO")
        say(box, f"Mesh shared with {len(sharers) - 1} other object(s), but "
                 f"exported with its own copy of it: its type is never "
                 f"instanced, or it has LOD levels of its own.")

    def _draw_billboard(self, layout, obj):
        box = layout.box()
        box.label(text="Billboard", icon="IMAGE_PLANE")
        box.prop(obj, "rot_mode", text="Rotation Mode")
        # The axis is stored whichever mode is set, and most shipping billboards
        # keep a meaningful axis with the lock clear, so it is always editable.
        box.prop(obj, "rot_axis", text="Rotation Axis")

    def _draw_bare(self, layout, obj):
        """The frame types that store nothing but a transform and a name."""
        frame_type = _int_prop(obj, "ls3d_frame_type")
        box = layout.box()
        box.label(text=C.FRAME_TYPE_NAMES.get(frame_type, "Frame"),
                  icon="EMPTY_AXIS")
        note = box.column()
        note.scale_y = 0.8
        say(note, "Stores its transform, its name and its user properties - "
                  "nothing else.")
        if frame_type == C.FRAME_SHADOW:
            note.separator()
            say(note, "Not the .6ds shadow beside the model. That is a "
                      "separate file, marked with Shadow Piece on a mesh.")

    def _draw_light(self, layout, obj):
        box = layout.box()
        box.label(text="Light", icon="LIGHT")
        box.prop(obj, "ls3d_light_type", text="Type")

        kind = _int_prop(obj, "ls3d_light_type_value") & 0xFFFFFFFF
        if kind < C.LIGHT_TYPE_COUNT:
            _raw_field(box, obj, "ls3d_light_type_value", "Type Value")
        note = box.column()
        note.scale_y = 0.8
        if kind >= C.LIGHT_TYPE_COUNT:
            # The word in the file is not one of the nine. It is kept as it is
            # and shown below, because picking from the dropdown would throw
            # it away.
            note.label(text=f"The file says type {kind}, which the",
                       icon="ERROR")
            say(note, "engine has no case for - it does nothing in game. "
                      "Picking from the list above replaces it.")
            box.prop(obj, "ls3d_light_type_value", text="Stored Type")
        else:
            for line in _LIGHT_KIND_NOTES.get(kind, ()):
                note.label(text=line)

        fog = kind in C.LIGHT_ATMOSPHERE_TYPES
        box.prop(obj, "ls3d_light_color", text="Fog Color" if fog else "Color")
        if not fog:
            # A fog is drawn in the light's color alone; its power is not read.
            box.prop(obj, "ls3d_light_power", text="Power")

        # The same two stored numbers, labeled for what this kind makes of them.
        if kind in C.LIGHT_RANGED_TYPES:
            band = box.column(align=True)
            band.prop(obj, "ls3d_light_range_near", text="Near Range")
            band.prop(obj, "ls3d_light_range_far", text="Far Range")
            near = _float_prop(obj, "ls3d_light_range_near")
            if _float_prop(obj, "ls3d_light_range_far") < near:
                box.label(text="Far is nearer than near - the band",
                          icon="ERROR")
                box.label(text="it fades across runs backwards.")
        elif kind == C.LIGHT_FOG:
            band = box.column(align=True)
            band.prop(obj, "ls3d_light_range_near", text="Start %")
            band.prop(obj, "ls3d_light_range_far", text="Thickest %")
            share = C.LIGHT_ATMOSPHERE_RANGE_SCALE
            far = _float_prop(obj, "ls3d_light_range_far") * share
            near = _float_prop(obj, "ls3d_light_range_near") * share
            said = box.column()
            said.scale_y = 0.8
            say(said, f"Thickest at {far:.0%} of the view distance, "
                      f"starting at {near:.0%} of that - "
                      f"{near * far:.0%} of the view.", icon="INFO")
        elif kind in C.LIGHT_DENSITY_FOG_TYPES:
            box.prop(obj, "ls3d_light_range_near", text="Density")
            said = box.column()
            said.scale_y = 0.8
            said.label(text="Used as typed. Far Range is not read.",
                       icon="INFO")

        if kind == C.LIGHT_SPOT:
            cones = box.column(align=True)
            cones.prop(obj, "ls3d_light_cone_inner", text="Inner Cone")
            cones.prop(obj, "ls3d_light_cone_outer", text="Outer Cone")
            outer = _float_prop(obj, "ls3d_light_cone_outer")
            if _float_prop(obj, "ls3d_light_cone_inner") > outer:
                box.label(text="Inner is wider than outer, so the",
                          icon="ERROR")
                box.label(text="beam has no soft edge at all.")
            hint = box.column()
            hint.scale_y = 0.8
            hint.label(text="Both are full angles, not half-angles.")

        # The handles in the viewport, and aiming. The file has no direction
        # field - a light shines the way its frame faces - so aiming edits the
        # object's rotation and nothing else.
        hints = box.column()
        hints.scale_y = 0.8
        if kind == C.LIGHT_SPOT:
            say(hints, "Drag the boxes on the beam for the range, and on the "
                       "rims for the cones.")
        elif kind == C.LIGHT_POINT:
            hints.label(text="Drag the boxes along +Y for the range.")
        if kind in (C.LIGHT_SPOT, C.LIGHT_DIRECTIONAL):
            say(hints, "Shines along local +Y, the way the cone points. Drag "
                       "the ball to aim it.")
            box.operator("ls3d.light_aim", icon="TRACKER")

        flags = box.box()
        flags.label(text="Mode", icon="MODIFIER")
        _raw_field(flags, obj, "ls3d_light_mode_str", "Raw Flags")
        _flag_grid(flags, obj, [attr for _b, attr, _l, _d in C.LIGHT_MODE_FLAGS])
        mode = _int_prop(obj, "ls3d_light_mode") & 0xFFFFFFFF
        lighting = kind in C.LIGHT_SHADING_TYPES or kind == C.LIGHT_AMBIENT
        if lighting and not mode & (C.LM_REALTIME | C.LM_LIT_OBJECTS):
            flags.label(text="With neither Lights Objects nor Lights",
                        icon="ERROR")
            flags.label(text="Lit Objects on, it lights nothing in game.")
        unread = mode & C.LIGHT_MODE_UNREAD_BITS
        if unread:
            kept = flags.column()
            kept.scale_y = 0.8
            count = bin(unread).count("1")
            kept.label(text=f"{count} more switch{'es are' if count > 1 else ' is'}"
                            " on that the game never",
                       icon="INFO")
            kept.label(text="reads; they are kept exactly as they are.",
                       icon="BLANK1")

        # Nothing in Blender could otherwise say this: a light lights nothing
        # at all until a sector lists it, and that list is not in the model.
        where = box.column()
        where.scale_y = 0.8
        where.label(text="In game a light works only in the sectors",
                    icon="INFO")
        where.label(text="that list it. That list is in the mission's",
                    icon="BLANK1")
        where.label(text="scene data, not in the model, so a light in",
                    icon="BLANK1")
        where.label(text="a model on its own lights nothing.", icon="BLANK1")

        # The how-to for that list, folded away until it is wanted.
        header, body = box.panel("ls3d_light_scene2", default_closed=True)
        header.label(text="Switching It On in scene2.bin", icon="INFO")
        if body is not None:
            steps = body.column()
            steps.scale_y = 0.8
            # Rows that are not for this light's kind are dimmed, not hidden,
            # so the list reads the same whichever kind is picked.
            animated = bpy.context.scene.ls3d_animated_object_count
            steps.label(text="In the model's .4ds:", icon="FILE_3D")
            for text, kinds, applies_to in _LIGHT_MODEL_NEEDS:
                warn = text.startswith("Animated") and animated
                _light_requirement(steps, text, kinds, kind in applies_to,
                                   icon="ERROR" if warn else "DOT")
            steps.separator()
            steps.label(text="In the mission's scene2.bin:", icon="FILE_TEXT")
            _light_requirement(steps, "Placed after the model's object",
                               "All kinds", kind in _ANY_KIND)
            _light_requirement(steps, f"Frame <model object>.{obj.name}",
                               "All kinds; no frame type", kind in _ANY_KIND)
            steps.label(text="Its light datablock holds:", icon="BLANK1")
            for text, kinds, applies_to in _LIGHT_DATABLOCK_NEEDS:
                _light_requirement(steps, text, kinds, kind in applies_to)
            steps.separator()
            say(steps, "Lighting kinds: Point, Spot, Directional, Ambient. In "
                       "the mission's own scene.4ds, the frame is named "
                       "without the prefix.")

    def _draw_emitor(self, layout, obj):
        box = layout.box()
        box.label(text="Emitor", icon="PARTICLES")
        note = box.column()
        note.scale_y = 0.8
        say(note, "A particle emitter. This mesh is the particle it spawns, "
                  "copied once per particle - not the shape of the emitter.")
        note.separator()
        say(note, "How fast, how many, how big, which way and what color are "
                  "not in the format. The game sets them in code, so one "
                  "placed here emits nothing on its own.")

    def _draw_litobject(self, layout, obj):
        box = layout.box()
        box.label(text="Lit Object", icon="LIGHTPROBE_VOLUME")
        note = box.column()
        note.scale_y = 0.8
        say(note, "A mesh with baked lighting. The model stores exactly what "
                  "a plain Object does - the bake lives in the mission that "
                  "places it, not in this file.")

    def _draw_projector(self, layout, obj):
        # Deliberately terse. What each setting does lives in its tooltip, and
        # the shape it makes is outlined in the viewport, which says it better
        # than a paragraph. Only what cannot be seen or hovered is written out.
        box = layout.box()
        box.label(text="Projector", icon="OUTLINER_OB_LIGHT")

        material = obj.ls3d_projector_material
        picker = box.row(align=True)
        picker.template_ID(obj, "ls3d_projector_material")
        # Blender's own New makes a material this addon knows nothing about,
        # so the button beside it makes one set up the way the game reads it.
        picker.operator("ls3d.create_material", icon="ADD",
                        text="").target = "projector"
        if material is None:
            box.label(text="No material - paints nothing.", icon="ERROR")
        elif not _has_diffuse(material):
            box.label(text="Material has no diffuse texture.", icon="ERROR")

        # The projector's own settings come before the material, and the
        # material's own go in a box of their own below. Drawn the other way
        # round, opening the material ran its rows and these together and the
        # falloff read as one of the material's.
        box.prop(obj, "ls3d_projector_orthogonal", text="Straight On")
        box.prop(obj, "ls3d_projector_falloff", text="Depth Falloff")
        box.prop(obj, "ls3d_projector_blend", text="Blend Mode")
        if _int_prop(obj, "ls3d_projector_mode") not in C.PROJECTOR_MODES:
            box.label(text=f"Mode {_int_prop(obj, 'ls3d_projector_mode')} is "
                           f"not one the game knows.", icon="ERROR")
        _raw_field(box, obj, "ls3d_projector_mode")

        # A projector has no mesh, so its material has no slot and never shows
        # up in Blender's Material tab. Same problem the lens flare has, same
        # answer: edit it here, folded away until asked for.
        if material is not None:
            settings = box.box()
            row = settings.row(align=True)
            row.prop(obj, "ls3d_show_projector_material", text="",
                     emboss=False,
                     icon="TRIA_DOWN" if obj.ls3d_show_projector_material
                          else "TRIA_RIGHT")
            # Not the name - the picker one row up already carries it.
            row.label(text="Material Settings", icon="MATERIAL")
            if obj.ls3d_show_projector_material:
                # The colors and the switches below belong to the material and
                # reach anything it is put on - but a projector takes only the
                # diffuse texture out of it. Said here because the panel
                # otherwise offers a color that changes nothing.
                say(settings,
                    "A projector paints the diffuse texture and nothing else "
                    "from this material: its colors and switches do not reach "
                    "the paint. The color a projector is tinted with is its "
                    "own, and the file has nowhere to keep it.",
                    icon="INFO")
                draw_material_body(settings, material)

        # The Visual Flags box sits directly below with a Projection on Diffuse
        # checkbox in it, which reads as this projector's and is not.
        note = box.column()
        note.scale_y = 0.8
        say(note, "Projects along local +Y. Projection on Diffuse below is a "
                  "receiver setting, not this one's.")

    def _draw_lensflare(self, layout, obj):
        box = layout.box()
        box.label(text="Lens Flare", icon="LIGHT")

        count = len(obj.ls3d_glows)
        row = box.row()
        row.template_list("LS3D_UL_Glows", "ls3d_glows", obj, "ls3d_glows",
                          obj, "ls3d_glows_index", rows=4)
        column = row.column(align=True)
        column.operator("ls3d.add_glow", icon="ADD", text="")
        column.operator("ls3d.remove_glow", icon="REMOVE", text="")
        column.separator()
        column.operator("ls3d.move_glow", icon="TRIA_UP", text="").direction = "UP"
        column.operator("ls3d.move_glow", icon="TRIA_DOWN",
                        text="").direction = "DOWN"

        index = obj.ls3d_glows_index
        entry = obj.ls3d_glows[index] if 0 <= index < count else None
        if entry is not None:
            column = box.column(align=True)
            picker = column.row(align=True)
            picker.template_ID(entry, "material")
            picker.operator("ls3d.create_material", icon="ADD",
                            text="").target = "glow"
            sub_column = column.column()
            # A single-element flare is drawn at the object's own position, so
            # the offset has nothing to act on.
            sub_column.enabled = count > 1
            sub_column.prop(entry, "position")

        if entry is not None and entry.material is not None:
            row = box.row(align=True)
            row.prop(obj, "ls3d_show_glow_material", text="", emboss=False,
                     icon="TRIA_DOWN" if obj.ls3d_show_glow_material
                          else "TRIA_RIGHT")
            row.label(text=f"Material: {entry.material.name}", icon="MATERIAL")
            if obj.ls3d_show_glow_material:
                draw_material_body(box, entry.material)

        if not count:
            box.label(text="No elements - nothing is drawn.", icon="ERROR")
        elif count == 1:
            box.label(text="Lamp: drawn at the object's position.", icon="INFO")
        else:
            box.label(text="Direction = object location, normalized.",
                      icon="INFO")
            box.label(text="First element is the head.")

    def _draw_lod(self, layout, obj):
        box = layout.box()
        box.label(text="Level Of Detail", icon="MESH_DATA")
        box.prop(obj, "ls3d_lod_distance_m", text="Switch Distance")
        if _show_raw_flags():
            box.prop(obj, "ls3d_lod_dist", text="Raw Value (squared)")
        chain = [c for c in bpy.context.scene.objects
                 if c.name.lower().startswith(f"{obj.name.lower()}_lod")]
        if chain:
            box.label(text=f"{len(chain)} additional LOD mesh(es) found",
                      icon="INFO")

    def _draw_sector(self, layout, obj):
        box = layout.box()
        box.label(text="Sector", icon="SCENE_DATA")
        if _show_raw_flags():
            box.prop(obj, "ls3d_sector_flags1_str", text="Raw Flags")
        _flag_grid(box, obj, ("sf_sound_reverb", "sf_occluder",
                              "sf_small_portal_cull"), columns=1)

        # No switches for the light groups. They would gate which sectors a
        # projector or a projected shadow can reach, but Mafia turns every
        # group on for every sector the moment a mission finishes loading, so
        # nothing you could set here would survive. The value still round-trips
        # and the raw field reaches it if another tool ever wants it.
        if _show_raw_flags():
            box.prop(obj, "ls3d_sector_flags2_str", text="Group Mask")
        _reserved_grid(box, obj, C.RESERVED_SECTOR_FLAGS, "ls3d_sector_flags1")
        # Imported sectors often arrive with these on. They look alarming in
        # the list above and mean nothing, so say so.
        wiped = (obj.ls3d_sector_flags1 & 0xFFFFFFFF) & C.SECTOR_PER_FRAME_BITS
        if wiped:
            names = [label for mask, _attr, label, _desc
                     in C.RESERVED_SECTOR_FLAGS if wiped & mask]
            box.label(text=f"{', '.join(names)}: redone every frame, harmless",
                      icon="INFO")
        box.operator("ls3d.add_portal", icon="ADD")

    def _draw_portal(self, layout, obj):
        box = layout.box()
        box.label(text="Portal", icon="OUTLINER_OB_LIGHT")
        _raw_field(box, obj, "ls3d_portal_flags_str", "Raw Flags")
        box.prop(obj, "ls3d_portal_view_dist_m", text="View Distance")
        # Second Range is stored data, not a view onto a flag word, so the raw
        # flag preference has no business hiding it.
        box.prop(obj, "ls3d_portal_far", text="Second Range")
        if _show_raw_flags():
            box.prop(obj, "ls3d_portal_near", text="Raw Value (squared)")
        _flag_grid(box, obj, ("pf_enabled", "pf_far_cull"))
        _reserved_grid(box, obj, C.RESERVED_PORTAL_FLAGS, "ls3d_portal_flags")
        say(box, "The plane is recomputed from the geometry on export.",
            icon="INFO")

    def _draw_dummy(self, layout, obj):
        box = layout.box()
        box.label(text="Dummy", icon="MESH_CUBE")
        # subtype="XYZ" puts the axis letter on each field, and the box uses
        # the same axes as the object itself - Z is up, as everywhere else.
        column = box.column(align=True)
        column.label(text="Min")
        column.prop(obj, "bbox_min", text="")
        column.separator()
        column.label(text="Max")
        column.prop(obj, "bbox_max", text="")
        column.separator()
        size = tuple(obj.bbox_max[axis] - obj.bbox_min[axis] for axis in range(3))
        box.label(text="Size  X %.3f   Y %.3f   Z %.3f" % size)
        if not dummy_box_is_drawable(obj):
            say(box, "Outlined in the viewport - the empty's own cube "
                     "cannot show a box this shape.", icon="INFO")

    def _draw_occluder(self, layout, obj):
        box = layout.box()
        box.label(text="Occluder", icon="MOD_BOOLEAN")
        box.label(text="No editable properties.")

    def _draw_user(self, layout, obj):
        box = layout.box()
        box.prop(obj, "ls3d_user_props", text="User Props", icon="TEXT")
        encoded = text_length(obj.ls3d_user_props)
        if encoded > C.MAX_STRING_BYTES:
            box.label(text=f"{encoded} bytes - the format holds "
                           f"{C.MAX_STRING_BYTES}", icon="ERROR")
            box.label(text="Export refuses it rather than cutting it")

    def _draw_target(self, layout, obj):
        box = layout.box()
        box.label(text="Target", icon="EMPTY_ARROWS")
        _raw_field(box, obj, "ls3d_target_flags_str", "Raw Flags")
        _flag_grid(box, obj, [row[1] for row in C.TARGET_FLAGS])
        if obj.ls3d_target_flags & C.TF_LOOK_AT and obj.ls3d_target_flags & (
                C.TF_FOLLOW_ROTATION | C.TF_FOLLOW_POSITION | C.TF_FOLLOW_SCALE):
            note = box.column()
            note.scale_y = 0.8
            say(note, "Look At is on, so the follow switches are not read.",
                icon="INFO")
        row = box.row()
        row.prop(obj, "ls3d_target_enabled",
                 icon="CON_TRACKTO" if obj.ls3d_target_enabled else "UNLINKED")
        if not obj.ls3d_target_enabled:
            box.label(text="Aiming is off; the links still export",
                      icon="INFO")
        box.separator()
        box.label(text="Targeted objects:")
        row = box.row()
        row.template_list("UI_UL_list", "ls3d_target_objects",
                          obj, "ls3d_target_objects",
                          obj, "ls3d_target_objects_index", rows=4)
        row.operator("ls3d.remove_target_object", icon="REMOVE", text="")
        row = box.row(align=True)
        row.prop(obj, "ls3d_target_add_name", text="")
        row.operator("ls3d.add_target_object", icon="ADD", text="Add")
        box.label(text="Use 'armature:bone' for a joint link.", icon="INFO")


class The4DSMaterialPanel(bpy.types.Panel):
    bl_label = "4DS Material Properties"
    bl_idname = "MATERIAL_PT_4ds"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    #: Frame types written as bare geometry. They have no face groups, so a
    #: material on one of them cannot reach the file.
    HULL_FRAME_TYPES = (C.FRAME_SECTOR, C.FRAME_OCCLUDER)

    def draw(self, context):
        layout = self.layout
        obj = context.object
        mat = context.material

        if obj is not None and _frame_type_of(obj) in self.HULL_FRAME_TYPES:
            kind = "Portal" if is_portal(obj) else (
                "Occluder" if _frame_type_of(obj) == C.FRAME_OCCLUDER
                else "Sector")
            box = layout.box()
            box.label(text=f"{kind}: materials are not used", icon="INFO")
            say(box, "It is exported as a bare hull, so nothing here would "
                     "reach the file.")
            if any(slot.material for slot in obj.material_slots):
                box.operator("ls3d.clear_frame_materials", icon="TRASH")
            return

        if obj is not None:
            row = layout.row()
            row.template_list("MATERIAL_UL_matslots", "", obj, "material_slots",
                              obj, "active_material_index", rows=3)
            column = row.column(align=True)
            column.operator("object.material_slot_add", icon="ADD", text="")
            column.operator("object.material_slot_remove", icon="REMOVE", text="")
            picker = layout.row(align=True)
            picker.template_ID(obj, "active_material")
            picker.operator("ls3d.create_material", icon="ADD", text="")

        layout.separator()
        layout.operator("ls3d.create_material", icon="MATERIAL")

        if mat is None:
            return

        # The same preview sphere Blender's own material tab shows, in its own
        # collapsible section so it can be folded away - it is the slowest
        # thing on the panel to draw.
        header, body = layout.panel("ls3d_material_preview", default_closed=True)
        header.label(text="Preview", icon="MATERIAL")
        if body is not None:
            body.template_preview(mat)

        layout.separator()
        draw_material_body(layout, mat)



# ── material drawing ──────────────────────────────────────────────────────────
# Module level rather than panel methods: a lens flare element owns a material
# that is on no mesh, so it never appears in Blender's Material tab. The flare
# panel draws these itself.
def _material_toggle_header(box, mat, prop, label, icon):
    row = box.row()
    row.label(text=label, icon=icon)
    row.prop(mat, prop, toggle=True,
             icon="CHECKBOX_HLT" if getattr(mat, prop) else "CHECKBOX_DEHLT")


def _draw_material_colors(layout, mat):
    box = layout.box()
    box.label(text="Material Colors", icon="COLOR")
    column = box.column(align=True)
    column.prop(mat, "ls3d_diffuse_color")
    column.prop(mat, "ls3d_ambient_color")
    column.prop(mat, "ls3d_emission_color")
    column.separator()
    column.prop(mat, "ls3d_opacity", slider=True)

    # Opacity is the one setting a material does not get to keep: the mesh
    # hands the same one to everything drawn on it. Saying so beside the slider
    # is the only way a value that does nothing is visible.
    materials = module("4ds.materials")
    wears = materials.effective_opacity(mat)
    if abs(wears - mat.ls3d_opacity) > 1e-4:
        note = box.column(align=True)
        note.scale_y = 0.8
        note.label(text=f"Drawn at {wears:.2f}, not {mat.ls3d_opacity:.2f}.",
                   icon="INFO")
        say(note, "A mesh gives every material on it the same opacity, "
                  "taken from the last one on it that asks for no "
                  "transparency of its own.", icon="BLANK1")

    # The raw word is all this box holds, so with the preference off there is
    # nothing to put under the heading and the heading does not appear.
    if _show_raw_flags():
        box = layout.box()
        box.label(text="Global Material Flags", icon="PREFERENCES")
        box.prop(mat, "ls3d_material_flags_str", text="Raw Value")

    if mat.ls3d_flag_diffuse_animated or mat.ls3d_flag_alpha_animated:
        box = layout.box()
        box.label(text="Texture Animation", icon="ANIM")

        # Which of the three channels will actually move, drawn as ticks. They
        # are labels rather than switches because there is nothing to switch:
        # each one is the answer the game works out from the numbers below, so
        # a clickable box would offer an edit that goes nowhere.
        state = box.column(align=True)
        for label, detail, running in animation_status(mat):
            state.label(text=f"{label}: {detail}",
                        icon="CHECKBOX_HLT" if running else "CHECKBOX_DEHLT")

        # Playing it is a preview, not animation data - no keys are inserted
        # and the material's own texture never moves off its first frame. What
        # steps is the image on the preview node underneath.
        texanim = module("4ds.ops_texanim")
        transport = box.row(align=True)
        transport.scale_y = 1.2
        playing = texanim.is_playing(mat)
        play = transport.row(align=True)
        play.enabled = texanim.can_play(mat) and not playing
        play.operator("ls3d.texture_anim_play", icon="PLAY")
        pause = transport.row(align=True)
        pause.enabled = playing
        pause.operator("ls3d.texture_anim_pause", icon="SNAP_FACE")

        # Frame files in the range the Frames fields set that the game would
        # misread. Offered only when there are some, like the texture slots'
        # own button.
        broken = module("4ds.ops_texfix").animation_plans(mat)
        if broken:
            fix = box.column(align=True)
            say(fix, f"{len(broken)} frame file(s) the game misreads",
                icon="ERROR")
            op = fix.operator("ls3d.texture_anim_fix", icon="TOOL_SETTINGS",
                              text=f"Fix Frame Files ({len(broken)})")
            op.material = mat.name

        column = box.column(align=True)
        column.separator()
        column.prop(mat, "ls3d_anim_frames")
        column.prop(mat, "ls3d_anim_period")
        column.prop(mat, "ls3d_anim_loop_start")

        named = [("Diffuse", mat.ls3d_diffuse_tex)]
        if mat.ls3d_flag_alpha_animated:
            named.append(("Alpha", mat.ls3d_alpha_tex))
        _draw_animation_names(box, mat, named, mat.ls3d_anim_frames)

        # The environment texture has the same three settings and its own copy
        # of the stepping code, so it gets the same three controls - always
        # there, since the tick above says it is not animating and these are
        # the only things that would change that. It has no flag of its own:
        # the count and the frame time are the whole switch.
        column = box.column(align=True)
        column.separator()
        column.label(text="Environment Texture", icon="WORLD_DATA")
        column.prop(mat, "ls3d_env_anim_frames")
        column.prop(mat, "ls3d_env_anim_period")
        column.prop(mat, "ls3d_env_anim_loop_start")
        # Named only when those two will actually run it, the way the alpha
        # names appear only when the alpha is animated.
        if mat.ls3d_env_anim_frames >= 2 and mat.ls3d_env_anim_period > 0:
            _draw_animation_names(box, mat, [("Env", mat.ls3d_env_tex)],
                                  mat.ls3d_env_anim_frames)

        # How the frames have to be named. Worth reading once and in the way
        # afterwards, so it folds away and starts folded - the names it works
        # out for this material are listed above regardless.
        box.separator()
        opened = mat.ls3d_show_anim_info
        head = box.row()
        head.alignment = "LEFT"
        head.prop(mat, "ls3d_show_anim_info", text="Info", emboss=False,
                  icon="TRIA_DOWN" if opened else "TRIA_RIGHT")
        if opened:
            # No icon column and left-aligned, so the text starts under the
            # arrow rather than a notch in from it.
            rule = box.column(align=True)
            rule.alignment = "LEFT"
            rule.scale_y = 0.8
            last = C.MAX_ANIM_FRAMES - 1
            example = ", ".join(f"FIRE{number:02d}.BMP" for number in range(3))
            say(rule, f"Frames are numbered {0:02d} to {last:02d} at the "
                      f"end of the name: {example} ... Only the last two "
                      f"digits ever change.")


def animation_status(mat):
    """What each of the three texture channels will actually do.

    ``[(label, state, running)]``. A channel needs two frames and a frame time
    before the game steps it; short of either it sits on its first frame, which
    is easy to arrive at by accident and impossible to see from the numbers.
    The alpha has no timing of its own - it is renumbered alongside the diffuse
    frames, so it moves only when they do, and it wraps where they wrap.
    """
    material_codec = module("4ds.codec.material")
    flags = mat.ls3d_material_flags & 0xFFFFFFFF

    def stepping(frames, period):
        if frames < 2:
            return "needs 2 frames or more"
        if period <= 0:
            return "needs a frame time"
        return ""

    def running(frames, period, loop):
        """The line for a channel that will step, wrap included."""
        if loop == material_codec.ANIM_RANDOM:
            wrap = "in random order"
        elif loop >= frames:
            # The game works the wrap out as loop + index % (frames - loop),
            # so a loop at or past the last frame divides by zero or steps off
            # the end of the frame list.
            wrap = f"loops to {loop:02d}, past its last frame"
        elif loop:
            wrap = f"loops to {loop:02d}"
        else:
            wrap = "loops from the start"
        return f"{frames} frames, {period} ms, {wrap}"

    rows = []

    held = stepping(mat.ls3d_anim_frames, mat.ls3d_anim_period)
    if not mat.ls3d_flag_diffuse_animated:
        rows.append(("Diffuse", "not animated", False))
    elif held:
        rows.append(("Diffuse", f"not animated - {held}", False))
    else:
        rows.append(("Diffuse", running(mat.ls3d_anim_frames,
                                        mat.ls3d_anim_period,
                                        mat.ls3d_anim_loop_start), True))
    diffuse_running = rows[0][2]

    if not mat.ls3d_flag_alpha_animated:
        rows.append(("Alpha", "not animated", False))
    elif not material_codec.has_alpha_texture(flags):
        rows.append(("Alpha", "not animated - carries no alpha texture", False))
    elif not diffuse_running:
        rows.append(("Alpha", "not animated - nor is the diffuse", False))
    else:
        rows.append(("Alpha", "follows the diffuse frames", True))

    held = stepping(mat.ls3d_env_anim_frames, mat.ls3d_env_anim_period)
    if held:
        rows.append(("Environment", f"not animated - {held}", False))
    else:
        rows.append(("Environment", running(mat.ls3d_env_anim_frames,
                                            mat.ls3d_env_anim_period,
                                            mat.ls3d_env_anim_loop_start),
                     True))
    return rows


def _exported_texture_name(image):
    """The file name an image will be written under, or ``""``."""
    if image is None:
        return ""
    if image.filepath:
        base = os.path.basename(image.filepath_from_user() or image.filepath)
        if base:
            return base
    return _DUPLICATE_SUFFIX_RE.sub("", image.name)


def _draw_animation_names(box, mat, shown, frames):
    """Spell out the first and last file name an animation asks for.

    Every channel is numbered the same way, off its own texture's name, so the
    environment one goes through here too on the rare file that sets it up.
    """
    material_codec = module("4ds.codec.material")
    note = box.column(align=True)
    note.scale_y = 0.8

    for label, image in shown:
        # The name that will be written, not the datablock's - Blender appends
        # ".001" when two images collide and the game would go looking for it.
        name = _exported_texture_name(image)
        if not name:
            continue
        if len(name) < material_codec.ANIM_NAME_TAIL:
            say(note, f"{label}: '{name}' is too short to be numbered.",
                icon="ERROR")
            continue
        stem = name[:-material_codec.ANIM_NAME_TAIL]
        suffix = name[-material_codec.ANIM_NAME_TAIL
                      + material_codec.ANIM_COUNTER_DIGITS:]
        last = min(max(frames - 1, 0), C.MAX_ANIM_FRAMES - 1)
        say(note, f"{label}: {stem}00{suffix} to {stem}{last:02d}{suffix}",
            icon="BLANK1")
    if frames > C.MAX_ANIM_FRAMES:
        note.label(text=f"Only {C.MAX_ANIM_FRAMES} frames can be numbered.",
                   icon="ERROR")


def _draw_texture_fix(column, mat, slot):
    """What is wrong with the file behind a texture slot, and a button to fix it.

    Drawn only for a BMP or TGA the repair can rewrite; the export's checks
    name everything else.
    """
    fixer = module("4ds.ops_texfix")
    plan = fixer.plan_for_image(getattr(mat, fixer.SLOT_PROPERTIES[slot]))
    if plan is None:
        return
    column.label(text=plan.summary, icon="ERROR")
    op = column.operator("ls3d.texture_fix", icon="TOOL_SETTINGS")
    op.material = mat.name
    op.slot = slot


def _draw_material_diffuse(layout, mat):
    box = layout.box()
    _material_toggle_header(box, mat, "ls3d_flag_diffuse_enable",
                            "Diffuse Texture", "TEXTURE")
    column = box.column(align=True)
    for pair in (("ls3d_flag_diffuse_doublesided", "ls3d_flag_diffuse_colored"),
                 ("ls3d_flag_disable_u_tiling", "ls3d_flag_disable_v_tiling"),
                 ("ls3d_flag_diffuse_animated", "ls3d_flag_alpha_in_tex")):
        row = column.row(align=True)
        for name in pair:
            row.prop(mat, name, toggle=True)
    column.separator()
    column.label(text="Texture Loading:")
    for pair in (("ls3d_flag_force_truecolor", "ls3d_flag_no_compression"),
                 ("ls3d_flag_no_cached_texture", "ls3d_flag_texture_manager"),
                 ("ls3d_flag_diffuse_mipmap",)):
        row = column.row(align=True)
        for name in pair:
            row.prop(mat, name, toggle=True)
    column.separator()
    column.label(text="Diffuse Texture:")
    column.template_ID(mat, "ls3d_diffuse_tex", open="image.open")
    _draw_texture_fix(column, mat, "DIFFUSE")
    if mat.ls3d_diffuse_tex and not mat.ls3d_flag_diffuse_enable:
        say(column, "Texture set but 'Use Diffuse Texture' is off - it will "
                    "not be exported.", icon="ERROR")


#: The switches that stop a separate alpha texture from being written, since
#: those materials carry their transparency in the diffuse texture instead.
_ALPHA_TEXTURE_VETOES = (
    ("ls3d_flag_alpha_additive", "Alpha Additive"),
    ("ls3d_flag_alpha_colorkey", "Color Key"),
    ("ls3d_flag_alpha_in_tex", "Truecolor Texture"),
)


def _draw_material_alpha(layout, mat):
    materials = module("4ds.materials")

    box = layout.box()
    _material_toggle_header(box, mat, "ls3d_flag_alphatex",
                            "Alpha Texture", "GHOST_ENABLED")

    # These four switches override one another, and an emission color joins in
    # without being on this list at all, so the panel says where they landed
    # rather than leaving it to be worked out.
    label, detail, icon = materials.BLEND_PATH_LABELS[materials.blend_path(mat)]
    result = box.column(align=True)
    result.label(text=f"Draws as: {label}", icon=icon)
    note = result.column()
    note.scale_y = 0.8
    note.label(text=detail, icon="BLANK1")
    reason = materials.blend_path_reason(mat)
    if reason:
        note.label(text=reason, icon="BLANK1")

    column = box.column(align=True)
    column.separator()
    for pair in (("ls3d_flag_alpha_enable", "ls3d_flag_alpha_colorkey"),
                 ("ls3d_flag_alpha_additive", "ls3d_flag_alpha_animated")):
        row = column.row(align=True)
        for name in pair:
            row.prop(mat, name, toggle=True)
    column.separator()
    column.label(text="Image:")
    column.template_ID(mat, "ls3d_alpha_tex", open="image.open")
    _draw_texture_fix(column, mat, "ALPHA")
    if mat.ls3d_alpha_tex:
        vetoed = [label for prop, label in _ALPHA_TEXTURE_VETOES
                  if getattr(mat, prop)]
        if not mat.ls3d_flag_alphatex:
            # Named as the switch is labeled, so it can be found.
            say(column, "Needs 'Use Alpha Texture' to be exported.",
                icon="ERROR")
        elif vetoed:
            column.label(text=f"{', '.join(vetoed)} takes transparency from "
                              f"the diffuse texture - this image is not "
                              f"exported.", icon="ERROR")


def _draw_material_environment(layout, mat):
    # Blend, Mapping and Repeat are small numbers packed into the flag
    # word, not individual bits - a menu and a slider read far better than
    # the six checkboxes that used to stand in for them.
    box = layout.box()
    _material_toggle_header(box, mat, "ls3d_flag_env_enable",
                            "Environment Mapping", "WORLD_DATA")
    column = box.column(align=True)
    column.prop(mat, "ls3d_env_blend")
    column.prop(mat, "ls3d_env_uv_mode")
    column.separator()
    column.prop(mat, "ls3d_env_amount", slider=True)
    column.prop(mat, "ls3d_env_tiling")
    column.separator()
    column.label(text="Environment Texture:")
    column.template_ID(mat, "ls3d_env_tex", open="image.open")
    _draw_texture_fix(column, mat, "ENV")
    if mat.ls3d_env_tex and not mat.ls3d_flag_env_enable:
        say(column, "Texture set but 'Use Environment Texture' is off - it "
                    "will not be exported.", icon="ERROR")


def draw_material_body(layout, mat):
    """Every 4DS setting of *mat*, with no dependency on the Material tab."""
    _draw_material_colors(layout, mat)
    _draw_material_diffuse(layout, mat)
    _draw_material_alpha(layout, mat)
    _draw_material_environment(layout, mat)


# ── morph group UI ────────────────────────────────────────────────────────────
class LS3D_UL_Glows(bpy.types.UIList):
    #: Share of the row given to the index. A plain row would split evenly and
    #: leave a gap the width of half the list between the number and the name.
    INDEX_SCALE = 0.16
    #: Share of what is left given to the material, so the offset sits right.
    MATERIAL_SCALE = 0.72

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        split = layout.split(factor=self.INDEX_SCALE, align=True)
        split.label(text="head" if index == 0 else f"{index + 1}")
        rest = split.split(factor=self.MATERIAL_SCALE, align=True)
        if item.material is None:
            rest.label(text="(none)", icon="ERROR")
        else:
            rest.label(text=item.material.name, icon="MATERIAL")
        rest.label(text=f"{item.position:.2f}")


class LS3D_UL_MorphGroups(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        layout.prop(item, "name", text="", emboss=False, icon="GROUP_VERTEX")
        layout.label(text=f"{len(item.targets)}")


class LS3D_UL_MorphTargets(bpy.types.UIList):
    #: Width of the inline value slider relative to a full column.
    VALUE_SLIDER_SCALE = 0.55

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        obj = context.object
        keys = obj.data.shape_keys if obj and obj.data else None
        key_block = keys.key_blocks.get(item.shape_key_name) if keys else None

        if key_block is None:
            layout.label(text=item.shape_key_name or "(none)", icon="ERROR")
            return

        layout.label(text=key_block.name, icon="SHAPEKEY_DATA")

        row = layout.row(align=True)
        row.alignment = "RIGHT"
        if index == 0:
            # Slot 0 is this group's basis, which is not necessarily Blender's
            # own reference key, so it has no value to blend.
            row.label(text="Basis")
            return

        slider = row.row()
        slider.scale_x = self.VALUE_SLIDER_SCALE
        slider.prop(key_block, "value", text="", emboss=False)
        toggle = row.operator("ls3d.morph_select_toggle", text="",
                              icon="CHECKBOX_HLT" if item.select
                              else "CHECKBOX_DEHLT", emboss=False)
        toggle.target_index = index


class The4DSModelPanel(bpy.types.Panel):
    """The model as a whole: its check, its animated object count, its joints.

    In the sidebar, where it is at hand while the model is being built, rather
    than in the properties editor behind whatever object happens to be picked -
    none of it belongs to one object.
    """

    bl_label = "4DS Model"
    bl_idname = "VIEW3D_PT_4ds_model"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "4DS Model"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.operator("ls3d.check_scene", icon="CHECKMARK")

        box = layout.box()
        box.label(text="Animated Objects", icon="SCENE_DATA")
        count_row = box.row(align=True)
        count_row.prop(scene, "ls3d_animated_object_count")
        # Say which it is, because a number that stops following the scene with
        # no explanation reads as a bug rather than as the setting it is.
        if scene.ls3d_animated_count_pinned:
            count_row.operator("ls3d.follow_animated_count", text="",
                               icon="PINNED")
        else:
            following = box.row()
            following.active = False
            following.label(text="Following what the scene has keyed",
                            icon="BLANK1")

        counted = sum(len(influence.boxes_of(obj)) for obj in scene.objects
                      if obj.type == "ARMATURE")
        box = layout.box()
        box.label(text="Joints", icon="BONE_DATA")
        box.prop(scene, C.JOINT_DISPLAY_SCALE_PROP, slider=True)
        # A row each: side by side, the sidebar cuts "Influence Boxes" short
        # at the width Blender opens it.
        box.prop(scene, C.SHOW_INFLUENCE_BOXES_PROP, toggle=True, icon="CUBE")
        in_front = box.row(align=True)
        in_front.active = getattr(scene, C.SHOW_INFLUENCE_BOXES_PROP, True)
        in_front.prop(scene, C.INFLUENCE_BOXES_IN_FRONT_PROP, toggle=True,
                      icon="XRAY")
        handles = box.row(align=True)
        handles.active = getattr(scene, C.SHOW_INFLUENCE_BOXES_PROP, True)
        handles.prop(scene, C.INFLUENCE_HANDLES_PROP, expand=True)
        box.operator("ls3d.default_mesh_origin", icon="ARMATURE_DATA")
        box.operator("ls3d.weights_from_boxes", icon="MOD_VERTEX_WEIGHT")
        box.operator("ls3d.clear_weights", icon="X")

        is_projector = module("4ds.viewport").is_projector
        projectors = sum(1 for obj in scene.objects if is_projector(obj))
        if projectors:
            painted = layout.box()
            painted.label(text="Projectors", icon="OUTLINER_OB_LIGHT")
            painted.prop(scene, C.PROJECT_TEXTURES_PROP, toggle=True,
                         icon="TEXTURE")
            say(painted,
                f"{projectors} projector(s). Painted onto what they cover, "
                f"with the falloff and blend each one's mode asks for. "
                f"Drawing only.")
        note = box.column()
        note.scale_y = 0.8
        if counted:
            note.label(text=f"{counted} joint(s) carry an influence box.",
                       icon="BLANK1")
        else:
            note.label(text="No joint here carries an influence box.",
                       icon="BLANK1")
        # Broken where the sidebar can show it at its own width: a line longer
        # than that is cut short with no way to read the rest.
        say(note, "A joint's box is what its weights are made from, when none "
                  "are painted. Hiding the boxes changes nothing that is "
                  "written.", icon="BLANK1")


class The4DSMorphPanel(bpy.types.Panel):
    """4DS morph groups: the shapes a mesh blends between.

    In the sidebar rather than the properties editor, because morph targets are
    shaped in the viewport - pulling vertices about and watching the blend -
    rather than set up once and left alone.
    """

    bl_label = "4DS Morph Groups"
    bl_idname = "VIEW3D_PT_4ds_morph"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "4DS Morph"

    @classmethod
    def poll(cls, context):
        # Kept open whatever is selected, so the tab does not appear and
        # vanish as the selection changes - the draw says what is missing.
        return True

    @staticmethod
    def _ready(obj, layout):
        """Whether *obj* can carry morphs, saying what is missing when it cannot."""
        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh to morph.", icon="INFO")
            return False
        if _int_prop(obj, "ls3d_frame_type", -1) != C.FRAME_VISUAL:
            layout.label(text=f"'{obj.name}' is not a Visual frame.",
                         icon="INFO")
            return False
        if _int_prop(obj, "visual_type") not in C.MORPH_VISUAL_TYPES:
            column = layout.column(align=True)
            column.label(text=f"'{obj.name}' is not a morph.", icon="INFO")
            note = column.column()
            note.scale_y = 0.8
            say(note, "Set Visual Type to Morph or Single Morph in the 4DS "
                      "Object panel.", icon="BLANK1")
            return False
        return True

    def draw(self, context):
        layout = self.layout
        obj = context.object
        if not self._ready(obj, layout):
            return

        # Which mesh these belong to. The properties editor made that obvious;
        # the sidebar does not, and morph groups are per-mesh.
        layout.label(text=f"'{obj.name}'", icon="MESH_DATA")

        row = layout.row()
        row.template_list("LS3D_UL_MorphGroups", "",
                          obj, "ls3d_morph_groups",
                          obj, "ls3d_active_morph_group", rows=3)
        column = row.column(align=True)
        column.operator("ls3d.morph_group", icon="ADD", text="").action = "ADD"
        column.operator("ls3d.morph_group", icon="REMOVE", text="").action = "REMOVE"

        group, _ = active_morph_group(obj)
        if group is None:
            layout.label(text="Add a morph group to begin.", icon="INFO")
            return

        # Which vertices this group's morph covers is not a choice: a group
        # that came out of a file has the region the file listed for it, and
        # a group made here takes the vertices its targets move. Said in
        # words, not offered as a control - and picking the group puts its
        # region up as the mesh's active vertex group, for Blender's own
        # tools to work on.
        if not group.vertex_group:
            note = layout.row()
            note.active = False
            note.label(text="Region: every vertex its targets move",
                       icon="GROUP_VERTEX")
        elif obj.vertex_groups.get(group.vertex_group) is not None:
            note = layout.row()
            note.active = False
            note.label(text="Region: the vertices the file listed for it",
                       icon="GROUP_VERTEX")
        else:
            say(layout, f"The vertex group '{group.vertex_group}' held this "
                        f"group's region and is gone. The vertices its "
                        f"targets move are the region now.", icon="ERROR")

        counts = {len(g.targets) for g in obj.ls3d_morph_groups if len(g.targets)}
        if len(counts) > 1:
            layout.label(text=f"Groups have different target counts "
                              f"({', '.join(map(str, sorted(counts)))}); shorter "
                              f"groups export padded with their basis.",
                         icon="INFO")

        layout.separator()
        row = layout.row()
        row.template_list("LS3D_UL_MorphTargets", "",
                          group, "targets",
                          group, "active_target_index", rows=4)
        column = row.column(align=True)
        column.operator("ls3d.morph_target", icon="ADD", text="").action = "ADD"
        column.operator("ls3d.morph_target", icon="REMOVE", text="").action = "REMOVE"
        column.separator()
        # Taking a shape key the mesh already carries is its own intention,
        # and its own button: Add makes a new one, this one picks.
        column.operator("ls3d.morph_add_existing", icon="SHAPEKEY_DATA",
                        text="")
        column.separator()
        # The basis is pinned to the first slot, so neither arrow can act on it
        # and nothing may move up into it.
        index = group.active_target_index
        last = len(group.targets) - 1
        up = column.row(align=True)
        up.enabled = index > 1
        up.operator("ls3d.morph_target", icon="TRIA_UP", text="").action = "UP"
        down = column.row(align=True)
        down.enabled = 0 < index < last
        down.operator("ls3d.morph_target", icon="TRIA_DOWN", text="").action = "DOWN"
        column.separator()
        column.operator("ls3d.morph_transfer", icon="COPY_ID", text="")
        column.operator("ls3d.morph_make_basis", icon="CHECKMARK", text="")


class LS3D_OT_ClearFrameMaterials(bpy.types.Operator):
    """Remove every material slot from this sector, portal or occluder"""

    bl_idname = "ls3d.clear_frame_materials"
    bl_label = "Clear Material Slots"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (obj is not None and obj.type == "MESH"
                and _frame_type_of(obj) in The4DSMaterialPanel.HULL_FRAME_TYPES
                and bool(obj.material_slots))

    def execute(self, context):
        obj = context.object
        removed = len(obj.material_slots)
        obj.data.materials.clear()
        self.report({"INFO"}, f"Removed {removed} material slot(s).")
        return {"FINISHED"}



CLASSES = (
    LS3D_OT_ClearFrameMaterials,
    LS3D_UL_Glows,
    LS3D_UL_MorphGroups,
    LS3D_UL_MorphTargets,
    The4DSObjectPanel,
    The4DSMaterialPanel,
    The4DSModelPanel,
    The4DSMorphPanel,
)
