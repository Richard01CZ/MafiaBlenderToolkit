"""The 5DS sidebar: which animation is playing, its cues and its movement.

In the viewport sidebar rather than the properties editor, because it is used
while animating - scrubbing the timeline - not while setting a model up.
"""

import bpy

from ..common.panels import say
from ..packages import module
from ..ui import _show_raw_flags
from ..tck import io as motion_io
from . import io as anim_io
from . import codec as anim

ops_anim = module("5ds.ops")


class LS3D_UL_animations(bpy.types.UIList):
    """Every animation in the file, one row each.

    The name is editable in place, which is the point: an imported animation
    arrives named after its file and a set of them is unreadable otherwise.
    """

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_prop, index):
        anim_io = module("5ds.io")
        if self.layout_type == "GRID":
            layout.label(text="", icon="ACTION")
            return
        row = layout.row(align=True)
        playing = any(obj.animation_data is not None
                      and obj.animation_data.action is item
                      for obj in context.scene.objects)
        row.label(text="", icon="PLAY" if playing else "ACTION")
        row.prop(item, "name", text="", emboss=False)

        note = row.row()
        note.alignment = "RIGHT"
        note.active = False
        if context.scene.ls3d_action_show_all:
            owner = anim_io.object_for_action(item, context.scene)
            note.label(text=owner.name if owner is not None else "unused")
        else:
            # One row stands for the whole animation, so say how much of it
            # is hidden behind this row rather than the one object.
            parts = len(anim_io.actions_in_animation(item))
            note.label(text=f"{parts} parts" if parts > 1 else "")

    def draw_filter(self, context, layout):
        row = layout.row(align=True)
        row.prop(context.scene, "ls3d_action_show_all", toggle=True)
        row.prop(self, "filter_name", text="", icon="VIEWZOOM")

    def filter_items(self, context, data, propname):
        """One row per animation, unless every action is asked for.

        An animation driving several things keeps an action for each. Listing
        them all makes twenty animations look like forty, so only one stands
        for each - worked out from the names as they are now.
        """
        anim_io = module("5ds.io")
        actions = getattr(data, propname)
        flags = [self.bitflag_filter_item] * len(actions)

        if not context.scene.ls3d_action_show_all:
            keep = anim_io.leading_actions(actions)
            for index, action in enumerate(actions):
                if action not in keep:
                    flags[index] &= ~self.bitflag_filter_item

        if self.filter_name:
            wanted = self.filter_name.lower()
            for index, action in enumerate(actions):
                if wanted not in action.name.lower():
                    flags[index] &= ~self.bitflag_filter_item
        return flags, []


class LS3D_UL_named_events(bpy.types.UIList):
    """Named cues: a frame and a word, both typed in place."""

    FRAME_SCALE = 0.3

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_prop, index):
        if self.layout_type == "GRID":
            layout.label(text="", icon="SORTALPHA")
            return
        split = layout.split(factor=self.FRAME_SCALE, align=True)
        split.prop(item, "frame", text="")
        split.prop(item, "text", text="", emboss=False)


class The5DSAnimationPanel(bpy.types.Panel):
    """5DS animation: what the scene writes, the movement track, and cues.

    In the sidebar rather than the properties editor, because it is used while
    animating - scrubbing the timeline in the viewport - not while setting a
    model up.
    """

    bl_label = "5DS Animation"
    bl_idname = "VIEW3D_PT_5ds_animation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "5DS Animation"

    @classmethod
    def poll(cls, context):
        # Most of this panel is about the scene - the animations in the file,
        # the frame rate, the movement track - so it is worth having with
        # nothing selected.
        return True

    #: Tracks listed in the panel before it stops and gives a count instead.
    LISTED_TRACKS = 12
    #: The same, for the target frames each with its own aim switch.
    LISTED_TARGETS = 8

    def _draw_scene(self, layout, context):
        """What the scene holds, and whether it would export."""
        anim_io = module("5ds.io")
        anim = module("5ds.codec")

        row = layout.row()
        row.scale_y = 1.4
        row.operator("ls3d.check_animation", icon="CHECKMARK")

        box = layout.box()
        box.label(text="Animations", icon="ACTION")
        if not bpy.data.actions:
            box.label(text="Nothing loaded yet", icon="INFO")
            box.operator("ls3d.add_action", icon="ADD")
        else:
            box.template_list("LS3D_UL_animations", "", bpy.data, "actions",
                              context.scene, "ls3d_action_index", rows=4)
            row = box.row(align=True)
            row.scale_y = 1.2
            row.operator("ls3d.activate_action", icon="PLAY")
            row.operator("ls3d.add_action", text="", icon="ADD")
            row.operator("ls3d.delete_action", text="", icon="TRASH")
            box.operator("ls3d.unload_animation", icon="X")
            hint = box.column()
            hint.scale_y = 0.8
            hint.label(text="Double-click a name to rename it.")

        # The number the .4DS carries so the game knows to look for a .5DS
        # beside it. It belongs with the animation as much as with the model,
        # and this is where it is being changed.
        box = layout.box()
        box.label(text="Model", icon="SCENE_DATA")
        box.prop(context.scene, "ls3d_animated_object_count")
        counted = anim_io.count_animated_objects(context.scene)
        note = box.column()
        note.scale_y = 0.8
        if not counted:
            note.label(text="Follows the scene upwards as you", icon="BLANK1")
            note.label(text="animate; an import never touches it.",
                       icon="BLANK1")
        elif counted == context.scene.ls3d_animated_object_count:
            say(note, f"Matches the {counted} animated in the scene.",
                icon="BLANK1")
        else:
            say(note, f"{counted} thing(s) in the scene are animated. The "
                      f"export says so if it matters.",
                icon="ERROR" if counted > context.scene
                .ls3d_animated_object_count else "INFO")

        summary = anim_io.scene_animation_summary(context.scene)
        tracks = summary["tracks"]
        box = layout.box()
        box.label(text="Animation", icon="ARMATURE_DATA")

        # Which empty carries the travel, whatever is selected - it is one
        # per scene and easy to lose track of once the outliner fills up.
        carrying = next((o for o in context.scene.objects
                         if motion_io.is_motion_track(o)), None)
        row = box.row()
        row.label(text="Track:", icon="ANIM")
        if carrying is None:
            spare = row.row()
            spare.active = False
            spare.label(text="none")
        else:
            row.label(text=carrying.name)
            if not carrying.ls3d_motion_enabled:
                muted = box.row()
                muted.active = False
                muted.label(text="Its movement is switched off.",
                            icon="BLANK1")

        # A target frame's Track To beats whatever the animation says, so it
        # has to be possible to call the whole lot off while animating.
        aiming = [o for o in context.scene.objects
                  if getattr(o, "ls3d_target_objects", None)]
        if aiming:
            box.prop(context.scene, "ls3d_targets_ignored")
            everything = context.scene.ls3d_targets_ignored
            if everything:
                note = box.row()
                note.active = False
                note.label(text=f"{len(aiming)} target frame(s) are not "
                                f"steering anything.", icon="BLANK1")

            # One at a time as well. Muting the lot is the blunt instrument;
            # usually it is one frame fighting the animation, and reaching its
            # switch meant finding it in the outliner and opening the object
            # properties. The scene switch wins, so with it on these are shown
            # grayed rather than hidden - what each one is set to still matters
            # the moment it goes off again.
            listed = box.column(align=True)
            listed.active = not everything
            for target in aiming[:self.LISTED_TARGETS]:
                row = listed.row(align=True)
                aims = len(target.ls3d_target_objects)
                row.label(text=f"{target.name} ({aims})", icon="EMPTY_ARROWS")
                row.prop(target, "ls3d_target_enabled", text="",
                         icon=("CON_TRACKTO" if target.ls3d_target_enabled
                               else "UNLINKED"))
            over = len(aiming) - self.LISTED_TARGETS
            if over > 0:
                more = listed.row()
                more.active = False
                more.label(text=f"... and {over} more", icon="BLANK1")
        if not tracks:
            box.label(text="Nothing in the scene is animated", icon="INFO")
        else:
            length = context.scene.frame_end - context.scene.frame_start
            rate = anim_io.scene_frame_rate(context.scene)
            # "Track" means two things in this addon - one per animated
            # frame here, and the .tck's movement elsewhere - so this one
            # says which it is rather than leaving it to be guessed.
            box.label(text=f"{len(tracks)} animated frame(s), keyed from "
                           f"{summary['first']} to {summary['last']}")
            row = box.row()
            row.label(text=f"Scene range {context.scene.frame_start} to "
                           f"{context.scene.frame_end} - "
                           f"{length / anim.FRAMES_PER_SECOND:.2f} s in game")
            if not anim_io.on_game_frame_rate(context.scene):
                say(box, f"Scene runs at {rate:g} fps; the game plays at "
                         f"{anim.FRAMES_PER_SECOND}, and an animation cannot "
                         f"be written from any other rate", icon="ERROR")
                fix = box.row()
                fix.scale_y = 1.2
                fix.operator("ls3d.set_frame_rate", icon="TIME")
            if summary["last"] > context.scene.frame_end:
                box.label(text=f"Keys run past frame "
                               f"{context.scene.frame_end} and would not play",
                          icon="ERROR")
                # The same shape as the frame rate fix above: say what is
                # wrong, then offer the one button that puts it right. Picking
                # an animation does not move the range on its own - it is the
                # user's setting, and the export reads it.
                reach = box.row()
                reach.scale_y = 1.2
                reach.operator("ls3d.fit_scene_range", icon="ARROW_LEFTRIGHT")

            column = box.column(align=True)
            for name, kinds in tracks[:self.LISTED_TRACKS]:
                column.label(text=f"{name}   {', '.join(kinds)}")
            if len(tracks) > self.LISTED_TRACKS:
                column.label(text=f"... and {len(tracks) - self.LISTED_TRACKS} "
                                  f"more")

    #: Cues listed before the panel stops and gives a count instead.
    LISTED_CUES = 10

    def _draw_events(self, layout, context, obj):
        """The cues on this frame - what the game acts on as it plays."""
        ops_anim = module("5ds.ops")
        anim = module("5ds.codec")
        event_label = anim.event_label

        owner = ops_anim.event_owner(context)
        cues = ops_anim.event_cues(owner) if owner else []
        box = layout.box()
        row = box.row()
        row.label(text="Events", icon="MARKER_HLT")
        if owner is not None and owner is not obj:
            row.label(text=f"on '{owner.name}'")
        elif cues:
            row.label(text=f"{len(cues)} cue(s)")
        if owner is None:
            box.label(text="Nothing to hang cues on.", icon="INFO")
            return

        # Cues are only ever added to the notify frame, because that is the
        # only place the game reads them from. With anything else selected the
        # control is not offered at all rather than offered and then refused.
        if ops_anim.is_notify_frame(obj):
            row = box.row(align=True)
            row.prop(context.scene, "ls3d_event_kind", text="")
            row.operator("ls3d.add_event_cue", text="", icon="ADD")
            row.operator("ls3d.remove_event_cue", text="", icon="REMOVE")
        else:
            hint = box.column()
            hint.scale_y = 0.8
            named = context.scene.objects.get(anim.NOTIFY_TRACK)
            if named is None:
                hint.label(text=f"No Dummy called '{anim.NOTIFY_TRACK}' in "
                                f"the scene.", icon="INFO")
                say(hint, "Add one from Add > 4DS > Dummy - the game reads "
                          "cues from that one frame.", icon="BLANK1")
            elif not ops_anim.is_notify_frame(named):
                hint.label(text=f"'{anim.NOTIFY_TRACK}' is not a Dummy "
                                f"frame.", icon="ERROR")
                hint.label(text="Set its frame type to Dummy to hang",
                           icon="BLANK1")
                hint.label(text="cues on it.", icon="BLANK1")
            else:
                hint.label(text=f"Select the '{anim.NOTIFY_TRACK}' Dummy",
                           icon="INFO")
                hint.label(text="to add cues.", icon="BLANK1")

        strays = ops_anim.stray_cue_owners(context.scene)
        if strays:
            warn = box.column()
            warn.label(text="Cues sit on a frame the game never", icon="ERROR")
            warn.label(text=f"reads: {', '.join(o.name for o in strays[:3])}",
                       icon="BLANK1")
            warn.label(text="The export refuses these.", icon="BLANK1")

        here = round(context.scene.frame_current)
        for frame, value in cues[:self.LISTED_CUES]:
            line = box.row(align=True)
            line.alert = frame == here
            jump = line.operator("ls3d.jump_to_event_cue",
                                 text=f"{frame}", icon="KEYFRAME_HLT")
            jump.frame = frame
            line.label(text=event_label(value))
            move = line.operator("ls3d.move_event_cue", text="",
                                 icon="DRIVER_TRANSFORM")
            move.frame = frame
            drop = line.operator("ls3d.remove_event_cue", text="", icon="X")
            drop.frame = frame
        if len(cues) > self.LISTED_CUES:
            box.label(text=f"... and {len(cues) - self.LISTED_CUES} more")
        if cues and len(cues) % 2 == 0:
            say(box, "An even number of cues: the game reads those one "
                     "value out of step.", icon="ERROR")

        self._draw_named_events(layout, context, obj, cues)

    def _draw_named_events(self, layout, context, obj, numbered):
        """Cues carrying a word instead of a number.

        A track holds one kind or the other, never both, so the panel says so
        rather than letting the export be the one to find out.
        """
        ops_anim = module("5ds.ops")
        if not ops_anim.is_notify_frame(obj) and not ops_anim.named_events(obj):
            return
        box = layout.box()
        row = box.row()
        row.label(text="Named Events", icon="SORTALPHA")
        named = obj.ls3d_named_events
        if named:
            row.label(text=f"{len(named)} cue(s)")
        if named:
            box.template_list("LS3D_UL_named_events", "", obj,
                              "ls3d_named_events", obj,
                              "ls3d_named_event_index", rows=3)
        controls = box.row(align=True)
        controls.operator("ls3d.add_named_event", text="Add", icon="ADD")
        controls.operator("ls3d.remove_named_event", text="", icon="REMOVE")
        if named and numbered:
            warn = box.column()
            warn.label(text="This frame carries both kinds of cue.",
                       icon="ERROR")
            warn.label(text="A track holds one or the other, so the",
                       icon="BLANK1")
            warn.label(text="export refuses it. Remove one set.",
                       icon="BLANK1")
        elif not named:
            hint = box.column()
            hint.scale_y = 0.8
            say(hint, "Free text - the game hands it straight to whatever is "
                      "listening, unread.")

    def draw(self, context):
        anim = module("5ds.codec")
        layout = self.layout
        obj = context.object

        self._draw_scene(layout, context)

        # Everything past here is about one object; the scene sections above
        # stand on their own with nothing selected.
        if obj is None:
            hint = layout.column()
            hint.scale_y = 0.8
            hint.label(text="Select something for its own key flags,",
                       icon="INFO")
            hint.label(text="cues and movement.", icon="BLANK1")
            return

        anim_io = module("5ds.io")
        if motion_io.is_motion_track(obj):
            box = layout.box()
            box.label(text="Movement Track", icon="ANIM")
            # The switch sits first whichever state it is in, so it does not
            # move around under the pointer as the box grows and shrinks.
            box.prop(obj, "ls3d_is_motion_track", text="Movement Track",
                     toggle=True, icon="ANIM")
            box.prop(obj, "ls3d_motion_enabled")
            box.prop(obj, "ls3d_motion_period")
            reference = box.column(align=True)
            reference.label(text="Blend Reference")
            reference.prop(obj, "ls3d_motion_base_position", text="")
            reference.prop(obj, "ls3d_motion_base_direction", text="")
            hint = box.column()
            hint.scale_y = 0.8
            say(hint, "Where this animation hands the actor over, and where "
                      "it picks them up. The game eases the difference in so "
                      "nothing jumps between animations. Left at zero they "
                      "follow the start of the movement.")
            tail = box.column()
            tail.scale_y = 0.8
            say(tail, "Where the actor travels while the animation plays. "
                      "Written as a .tck beside it.")
        elif obj.type == "EMPTY":
            box = layout.box()
            box.label(text="Movement Track", icon="ANIM")
            box.prop(obj, "ls3d_is_motion_track",
                     text="Use As Movement Track", toggle=True, icon="ANIM")
            column = box.column()
            column.scale_y = 0.8
            column.label(text="One per animation: a .tck holds one",
                         icon="BLANK1")
            column.label(text="actor's travel.", icon="BLANK1")

        action = (obj.animation_data.action if obj.animation_data else None)
        if action is not None:
            row = layout.row()
            row.label(text="Action:", icon="ACTION")
            row.label(text=action.name)

        target = obj
        # Key flags belong to a joint when one is being posed, and to the
        # object otherwise. Bound either way, since the draw below reads it.
        bone = None
        if obj.type == "ARMATURE" and obj.mode == "POSE":
            bone = context.active_pose_bone
            if bone is None:
                layout.label(text="Select a joint to set its key flags.",
                             icon="INFO")
                return
            target = bone
            layout.label(text=f"Joint: {bone.name}", icon="BONE_DATA")

        self._draw_events(layout, context, obj)

        box = layout.box()
        box.label(text="Key Flags", icon="KEYFRAME")
        box.prop(target, "ls3d_anim_auto_flags")
        column = box.column(align=True)
        if target.ls3d_anim_auto_flags:
            # Read off the curves as they stand, so the panel cannot show a
            # flag word the keys stopped agreeing with.
            _anim_io = module("5ds.io")
            live = _anim_io.derived_key_flags(obj, bone)
            column.label(text="Worked out from the keys this frame has")
            grid = column.grid_flow(columns=2, align=True)
            for mask, _attr, label, _description in anim.KEY_FLAG_TABLE:
                grid.label(text=label,
                           icon="CHECKBOX_HLT" if live & mask
                           else "CHECKBOX_DEHLT")
        else:
            grid = column.grid_flow(columns=2, align=True)
            for _mask, attr, _label, _description in anim.KEY_FLAG_TABLE:
                grid.prop(target, f"af_{attr}", toggle=True)

        self._draw_raw_flags(layout, context, target, obj, bone)

    def _draw_raw_flags(self, layout, context, target, obj, bone):
        """The flag word itself, behind the addon preference.

        A 5DS track begins with a word saying which key arrays follow it. The
        game defines five bits and nothing else - the panel shows the word that
        would be written, which on automatic flags is worked out from the keys
        rather than read from anything stored.
        """
        if not _show_raw_flags():
            return
        anim = module("5ds.codec")
        automatic = target.ls3d_anim_auto_flags
        if automatic:
            word = module("5ds.io").derived_key_flags(
                obj, bone if target is not obj else None)
        else:
            word = int(getattr(target, "ls3d_anim_flags", 0)) & 0xFF

        box = layout.box()
        box.label(text="Raw Flags", icon="SCRIPT")
        if automatic:
            # Computed, so there is nothing here to type into.
            row = box.row()
            row.enabled = False
            row.label(text=f"0x{word:08X}")
        else:
            box.prop(target, "ls3d_anim_flags_str", text="Value")

        named = [label for mask, _attr, label, _d in anim.KEY_FLAG_TABLE
                 if word & mask]
        detail = box.column()
        detail.scale_y = 0.8
        detail.label(text=" + ".join(named) if named
                     else "No key arrays, so nothing is written")
        unknown = word & ~anim.KEY_FLAGS
        if unknown:
            say(detail, "A switch is on that the game does not know",
                icon="ERROR")


CLASSES = (
    LS3D_UL_animations,
    LS3D_UL_named_events,
    The5DSAnimationPanel,
)
