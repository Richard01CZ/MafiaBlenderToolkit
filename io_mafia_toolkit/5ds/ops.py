"""The File menu operators for ``.5ds`` animations.

An animation carries no skeleton of its own, so importing one is a matter of
matching its track names against whatever model is already in the scene. Load
the model first, then the animation onto it.
"""

import math
import os

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Quaternion, Vector

from . import io as anim_io
from ..packages import module
from ..tck import io as motion_io
from ..tck import ops as motion_ops
from ..common import constants as C
from ..common import describe
from ..common import report as report_module
from .codec import (Animation, AnimationError, MAX_U16, NOTIFY_TRACK, Track,
                    read_animation_file, validate_animation,
                    write_animation_file)

#: Interpolation for the movement channels. The game walks from one key to the
#: next in a straight line, so anything smoother would preview a motion the
#: game will not play.
MOVEMENT_INTERPOLATION = "LINEAR"
#: Note events hold their value until the next one rather than sliding.
NOTE_INTERPOLATION = "CONSTANT"


def _clear_channels(holder, prefix):
    """Drop the movement curves under *prefix*, leaving everything else.

    All three rotation channels go, not just the quaternion one the import
    writes: the target is put into quaternion mode here, and curves left behind
    on the Euler channel would come back the moment anyone switched the mode
    back.
    """
    if holder is None:
        return
    paths = {prefix + name for _, name, _ in anim_io.CHANNELS}
    paths |= {prefix + path for path, _count, _rest
              in list(anim_io.ROTATION_CHANNELS.values())
              + [anim_io.EULER_CHANNEL]}
    for fcurve in [c for c in holder.fcurves if c.data_path in paths]:
        holder.fcurves.remove(fcurve)


def _rest_under_animation(obj):
    """Put where *obj* rests into its Delta Transform before it is animated.

    An animation keys the object's own location, rotation and scale, and from
    then on those hold whatever frame is showing - so where it stands in the
    model would be gone. Moved into the Delta Transform first, it stays, and
    the keys are measured from it. An object already animated has it there,
    and is left as it is.
    """
    own, _bones = anim_io.keyed_channels(obj)
    if not own:
        module("4ds.joint_space").rest_in_delta(obj)


def _import_track(track, target, report):
    """Put one track's keys onto its target, converting as it goes."""
    holder = anim_io.channelbag(target.holder, create=True)
    group = track.name

    # A movement curve holds one key per frame, and so does the game in
    # practice, so the doubled ones are collapsed here rather than left to
    # Blender to resolve however it happens to. Cues are left alone: a few of
    # the game's animations do put two on one frame, and they get a slot each.
    dropped = 0
    for channel in ("rotations", "positions", "scales"):
        kept, lost = anim_io.without_duplicate_frames(getattr(track, channel))
        setattr(track, channel, kept)
        dropped += lost
    if dropped:
        report.warn(
            f"Track '{track.name}' had {dropped} key(s) sharing a frame with "
            f"another; the later of each pair was kept, which is the one the "
            f"game plays.")

    rotations = {frame: anim_io.to_blender_quaternion(value)
                 for frame, value in track.rotations}

    if track.rotations:
        if target.is_bone:
            keys = [(frame, anim_io.pose_rotation(target.rest, turn))
                    for frame, turn in rotations.items()]
        elif target.rest is not None:
            keys = [(frame, anim_io.object_rotation(target.rest, turn))
                    for frame, turn in rotations.items()]
        else:
            keys = list(rotations.items())
        anim_io.write_curve(holder, target.prefix + "rotation_quaternion", 4,
                            keys, group, MOVEMENT_INTERPOLATION)

    if track.positions:
        keys = []
        for frame, value in track.positions:
            position = anim_io.to_blender_vector(value)
            if target.is_bone:
                keys.append((frame, anim_io.pose_location(target.rest, position)))
            elif target.rest is not None:
                keys.append((frame, anim_io.object_location(target.rest,
                                                            position)))
            else:
                keys.append((frame, position))
        anim_io.write_curve(holder, target.prefix + "location", 3, keys, group,
                            MOVEMENT_INTERPOLATION)

    if track.scales:
        keys = []
        for frame, value in track.scales:
            scale = anim_io.to_blender_vector(value)
            if target.is_bone:
                keys.append((frame, anim_io.pose_scale(target.rest, scale)))
            elif target.rest is not None:
                keys.append((frame, anim_io.object_scale(target.rest, scale)))
            else:
                keys.append((frame, scale))
        anim_io.write_curve(holder, target.prefix + "scale", 3, keys, group,
                            MOVEMENT_INTERPOLATION)

    if track.named_notes:
        # Text cannot live on a curve, so these land in a list on the frame.
        owner = target.holder
        owner.ls3d_named_events.clear()
        for frame, text in track.named_notes:
            entry = owner.ls3d_named_events.add()
            entry.frame = frame
            entry.text = text
        report.info(f"'{track.name}' carries {len(track.named_notes)} named "
                    f"event(s).")

    if track.notes:
        anim_io.ensure_note_slots(target.holder)
        spilled = anim_io.write_note_cues(holder, track.notes, group,
                                          NOTE_INTERPOLATION)
        report.info(f"'{track.name}' carries {len(track.notes)} event cue(s), "
                    f"keyed onto the {anim_io.NOTE_PROPERTY} property.")
        if spilled:
            report.warn(f"'{track.name}' stacks more than "
                        f"{anim_io.NOTE_SLOTS} cues on one frame; {spilled} "
                        f"of them could not be loaded.")


class Import5DS(bpy.types.Operator, ImportHelper):
    """Import an LS3D .5ds animation onto the model already in the scene"""

    bl_idname = "import_scene.5ds"
    bl_label = "Import 5DS"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".5ds"
    filter_glob: StringProperty(default="*.5ds", options={"HIDDEN"})

    new_action: BoolProperty(
        name="New Action",
        description=("Put this animation in an action of its own, named after "
                     "the file, so loading a second one does not overwrite the "
                     "first. Off writes into whatever action is already "
                     "assigned, replacing the channels it touches"),
        default=True)

    load_motion: BoolProperty(
        name="Load Its Movement",
        description=("Also load the .tck beside the animation, which holds "
                     "where the actor travels while it plays. It arrives as a "
                     "parent empty above the model, so it can be switched off "
                     "while animating"),
        default=True)

    set_frame_rate: BoolProperty(
        name="Set Scene Frame Rate",
        description=("Put the scene on 25 fps, which is the rate the game "
                     "plays animations at. Off keeps the scene's own rate and "
                     "the animation plays at the wrong speed"),
        default=True)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Animation", icon="ACTION")
        box.prop(self, "new_action")
        box.prop(self, "load_motion")

        box = layout.box()
        box.label(text="Scene", icon="SCENE_DATA")
        box.prop(self, "set_frame_rate")
        column = box.column()
        column.scale_y = 0.8
        column.label(text="The game plays animations at 25 fps.",
                     icon="BLANK1")

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Importing {filename}", "5DS")
        matched = load_animation(self.filepath, context, result,
                                 new_action=self.new_action,
                                 set_frame_rate=self.set_frame_rate,
                                 load_motion=self.load_motion)
        if matched is None:
            result.finish(f"Import FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Import {status}: {filename}")
        result.show()
        return {"FINISHED"}


@anim_io.holding_count
def load_animation(filepath, context, result, new_action=True,
                   set_frame_rate=True, load_motion=True):
    """Put one animation onto whatever model the scene already holds.

    Shared by the File menu operator and by the model import, which loads a
    model's own animation for it. Returns how many tracks found something to
    drive, or ``None`` when the file could not be used at all - the caller owns
    the report and decides what that means.
    """
    filename = os.path.basename(filepath)
    try:
        animation = read_animation_file(filepath)
    except AnimationError as problem:
        result.error(str(problem),
                     fix="Only version 20 animations are supported - the "
                         "ones in the game's anims folder.")
        return None
    except OSError as problem:
        result.error(f"Could not read the file: {problem}")
        return None

    result.info(f"{len(animation.tracks)} track(s), "
                f"{animation.end_frame + 1} frame(s), "
                f"{animation.duration_seconds:.2f} s")

    result.stage("Tracks")
    result.span(0, 90)
    matched = 0
    unmatched = []
    stem = os.path.splitext(filename)[0]
    started = set()
    settled = set()
    # Resolved up front so the action names can say whether this animation
    # drives one thing or several before the first one is made.
    holders = []
    for track in animation.tracks:
        if track.is_empty:
            continue
        found = anim_io.find_target(track.name, context.scene)
        if found is not None and found.holder not in holders:
            holders.append(found.holder)
    for index, track in enumerate(animation.tracks, start=1):
        result.item("Track", index, len(animation.tracks),
                    describe.track_line(track))
        if track.is_empty:
            continue
        target = anim_io.find_target(track.name, context.scene)
        if target is None:
            unmatched.append(track.name)
            continue
        matched += 1
        holder_object = target.holder
        if not target.is_bone and holder_object.type != "ARMATURE":
            if holder_object not in settled:
                settled.add(holder_object)
                _rest_under_animation(holder_object)
            # Its keys are measured from where it rests, so the rest stays.
            target.rest = module("4ds.joint_space").delta_matrix(holder_object)
        if new_action and holder_object not in started:
            started.add(holder_object)
            anim_io.channelbag(
                holder_object, create=True, fresh=True,
                action_name=anim_io.action_name_for(holder_object, stem,
                                                    holders))
        elif not new_action:
            _clear_channels(anim_io.channelbag(holder_object),
                            target.prefix)
        owner = target.holder
        if not target.is_bone:
            owner.rotation_mode = "QUATERNION"
        else:
            bone_name = target.bone.name if target.bone is not None else track.name
            owner = target.holder.pose.bones.get(bone_name) or owner
            owner.rotation_mode = "QUATERNION"
        # Remember the word the file actually carried. It only differs
        # from what the keys imply on a hand-made file, and then only
        # because someone meant it to.
        owner.ls3d_anim_flags = track.flags
        owner.ls3d_anim_auto_flags = track.forced_flags is None
        _import_track(track, target, result)

    result.stage("Scene")
    result.span(90, 100)
    if unmatched:
        result.warn(
            f"{len(unmatched)} track(s) match nothing in the scene: "
            f"{', '.join(sorted(unmatched)[:6])}"
            + (" ..." if len(unmatched) > 6 else ""),
            fix="Import the model this animation was made for first - the "
                "tracks are matched to it by name.")
    if not matched:
        result.error("Nothing in the scene matches any track in this file.",
                     fix="Import the model first, then the animation.")
        return None

    if set_frame_rate:
        anim_io.apply_frame_rate(context.scene, result)
    anim_io.use_game_interpolation(result)
    context.scene.frame_start = 0
    context.scene.frame_end = animation.end_frame

    if load_motion:
        beside = motion_ops.sibling_track(filepath)
        if beside is not None:
            motion_ops.load_motion_track(beside, context, result, holders, stem)

    result.info(f"{matched} of {len(animation.tracks)} track(s) matched")
    if started:
        names = ", ".join(sorted(
            obj.animation_data.action.name for obj in started
            if obj.animation_data and obj.animation_data.action))
        result.info(f"Action(s): {names}")
    return matched


def _warn_about_easing(scene, report, objects=None):
    """Say when curves ease, because the file cannot carry the easing.

    The game walks from one key to the next in a straight line - a slerp for
    rotation - with no slowing in or out. A Bezier curve previews a motion in
    Blender that the game will not play: same keys, different speeds between
    them.
    """
    kinds = {}
    # Only what is being exported: warning about an object left out by Selected
    # Objects Only reads as though the whole scene went into the file.
    for obj in (scene.objects if objects is None else objects):
        owners = [("", obj)]
        if obj.type == "ARMATURE":
            owners += [(f'pose.bones["{bone.name}"].', bone)
                       for bone in obj.pose.bones]
        for prefix, owner in owners:
            for kind, count in anim_io.eased_curves(obj, prefix, owner).items():
                kinds[kind] = kinds.get(kind, 0) + count
    if not kinds:
        return
    spelled = ", ".join(f"{count} {kind.lower()}"
                        for kind, count in sorted(kinds.items()))
    report.warn(
        f"{sum(kinds.values())} key(s) do not use linear interpolation "
        f"({spelled}). The game walks straight from one key to the next, so "
        f"this will play differently there than it does here.",
        fix="Select the keys and press T in the Graph Editor, then choose "
            "Linear.")


def _warn_about_long_turns(scene, report, fix_long_turns=False,
                           objects=None):
    """Say when two rotation keys are more than half a turn apart.

    The file gives the game the two ends of a rotation, never the path between
    them, so it interpolates the shorter way round. Keys a full turn apart are
    the same rotation twice and do not turn at all - which is what a spin keyed
    as "0 at frame 1, 360 at frame 100" becomes, however convincingly Blender
    plays it in the viewport.
    """
    split = []
    offenders = []
    left_long = []
    for obj in (scene.objects if objects is None else objects):
        holder = anim_io.channelbag(obj)
        if holder is None:
            continue
        owners = [("", obj, obj.name)]
        if obj.type == "ARMATURE":
            owners += [(f'pose.bones["{bone.name}"].', bone,
                        f"{obj.name} / {bone.name}")
                       for bone in obj.pose.bones]
        for prefix, owner, label in owners:
            if not fix_long_turns:
                for first, second, angle in anim_io.turns_to_split(
                        holder, owner, prefix):
                    left_long.append((label, first, second,
                                      math.degrees(angle)))
                continue
            _turns, added, too_fast = anim_io.export_rotation_keys(
                holder, owner, prefix)
            if added:
                split.append((label, added))
            for first, second, angle in too_fast:
                offenders.append((label, first, second, math.degrees(angle)))

    # The fix is off: the keys go out exactly as keyed, so say which turns will
    # play the other way. A warning rather than a refusal - the file is sound,
    # it just moves differently in game - and the same rule the export applies
    # everywhere else.
    if left_long:
        spelled = "; ".join(
            f"'{label}' turns {angle:.0f} degrees between frames {first} and "
            f"{second}" for label, first, second, angle in left_long[:3])
        if len(left_long) > 3:
            spelled += f"; and {len(left_long) - 3} more"
        report.warn(
            f"{len(left_long)} rotation(s) turn {TURN_LIMIT_DEGREES} degrees or "
            f"more between two keys ({spelled}). The game only plays a turn "
            f"under {TURN_LIMIT_DEGREES} degrees between two keys the way it "
            f"was keyed - it always blends the shorter way round - so these "
            f"will play the other way.",
            fix="Tick 'Fix Long Rotation Turns' in the export options, or add "
                "keys along those turns.")

    if split:
        spelled = ", ".join(f"'{label}' {added}" for label, added in split[:4])
        if len(split) > 4:
            spelled += f" and {len(split) - 4} more"
        report.info(
            f"Added {sum(added for _l, added in split)} rotation key(s) where a "
            f"turn was {TURN_LIMIT_DEGREES} degrees or more between two keys "
            f"({spelled}), so no step is more than {TURN_STEP_DEGREES}. The "
            f"game only plays a turn under {TURN_LIMIT_DEGREES} degrees the way "
            f"it was keyed, so without them those turns would play backwards. "
            f"Blender's own keys are left alone.")

    if not offenders:
        return
    spelled = "; ".join(
        f"'{label}' turns {angle:.0f} degrees between frames {first} and "
        f"{second}" for label, first, second, angle in offenders[:3])
    if len(offenders) > 3:
        spelled += f"; and {len(offenders) - 3} more"
    report.warn(
        f"{len(offenders)} rotation(s) turn too far in too few frames to be "
        f"split ({spelled}). Keys sit on whole frames, and there are not "
        f"enough of them between those keys to keep every step under "
        f"{TURN_LIMIT_DEGREES} degrees, so the game will take the shorter way "
        f"round.",
        fix="Give those turns more frames, or add keys along them.")


def _tracks_from_scene(scene, report, objects=None, fix_long_turns=False):
    """Every animated thing, as tracks in the file's own space.

    *objects* narrows it to a chosen set - the export's Selected Objects Only.
    *fix_long_turns* is the export's Fix Long Rotation Turns.
    """
    tracks = []
    for obj in (scene.objects if objects is None else objects):
        holder = anim_io.channelbag(obj)
        if holder is None:
            continue

        # The movement track is the model's path through the world and lives
        # in the .tck beside the animation. Writing it here as well would
        # animate a frame no model has.
        if motion_io.is_motion_track(obj):
            continue

        if obj.type == "ARMATURE":
            joint_space = module("4ds.joint_space")
            skinned = joint_space.skinned_mesh_of(obj, scene.objects)
            if skinned is not None:
                # The armature carries the mesh frame - the whole body - so
                # its own animation is written under the mesh's name, measured
                # from where the armature's setting rests the frame.
                track = _object_track(obj, holder, skinned.name,
                                      joint_space.mesh_frame_matrix(obj),
                                      fix_long_turns)
                if track is not None:
                    tracks.append(track)
            elif _object_track(obj, holder, obj.name, None,
                               fix_long_turns) is not None:
                report.warn(
                    f"'{obj.name}' is animated itself, but an armature with no "
                    f"skinned mesh is not a frame of the model, so that "
                    f"movement is not written.",
                    fix="Animate its joints instead.")
            for pose_bone in obj.pose.bones:
                track = _bone_track(obj, holder, pose_bone, report,
                                    fix_long_turns)
                if track is not None:
                    tracks.append(track)
        else:
            # Measured from where the object rests, which it keeps in its
            # Delta Transform beneath the channels the animation keys.
            track = _object_track(obj, holder, obj.name,
                                  module("4ds.joint_space").delta_matrix(obj),
                                  fix_long_turns)
            if track is not None:
                tracks.append(track)
    return tracks


def _notes_of(holder):
    return anim_io.note_cues(holder)


def scene_has_animation(scene):
    """Whether the scene holds anything a .5ds would carry.

    Counts curves rather than sampling them, so it is cheap enough to run from
    a menu poll - and then looks for named cues, which are a list on a frame
    rather than keys on a curve and would otherwise be missed.
    """
    if anim_io.scene_animation_summary(scene, frames=False)["tracks"]:
        return True
    return any(named_events(obj) for obj in scene.objects)


def named_events(obj):
    """Every named cue on *obj*, as ``(frame, text)`` in frame order."""
    if obj is None:
        return []
    return sorted((int(entry.frame), entry.text)
                  for entry in getattr(obj, "ls3d_named_events", ()))


def _apply_flag_choice(owner, track):
    """Let a track carry the flag word its owner asks for, if it asks."""
    if getattr(owner, "ls3d_anim_auto_flags", True):
        return track
    track.forced_flags = (int(getattr(owner, "ls3d_anim_flags", 0))
                          & 0xFFFFFFFF)
    return track


def _object_track(obj, holder, name, rest, fix_long_turns=False):
    """One object's own animation as a track, or ``None`` if it has none."""
    track = Track(name=name)
    rotations, _added, _too_fast = anim_io.export_rotation_keys(
        holder, obj, split=fix_long_turns)
    positions = anim_io.sampled(holder, "location", 3, (0.0, 0.0, 0.0))
    scales = anim_io.sampled(holder, "scale", 3, (1.0, 1.0, 1.0))

    # What a frame holds is the whole step from the frame above it, which in
    # Blender is more than an object's own transform: the offset parenting put
    # beneath it, and the joint's end a bone-parented object hangs from. The
    # model export measures through exactly the same anchor, so the two can
    # never place a frame differently. Worked out frame by frame, since the
    # anchor mixes the three channels. With no anchor the values go out
    # exactly as they were keyed, which is what a model opened from a file has.
    anchor = module("4ds.joint_space").frame_anchor(obj)
    local = (anim_io.frame_locals(
        obj, holder, rest, anchor,
        set(rotations) | set(positions) | set(scales))
        if anchor is not None else None)

    # Not normalized. What Blender hands back is a hair off unit because its
    # four components came out of the file as 32-bit floats, and scaling it
    # back to exactly one moves the rotation - which then swings the frame's
    # own offset and shifts the position by a thousand times more than the
    # rounding it was meant to tidy up. The file is written what the scene
    # holds, the way the rest of the addon does.
    for frame, values in rotations.items():
        turn = Quaternion(values)
        if local is not None:
            turn = local[frame][1]
        elif rest is not None:
            turn = anim_io.from_object_rotation(rest, turn)
        track.rotations.append((frame, anim_io.to_file_quaternion(turn)))
    for frame, values in positions.items():
        place = Vector(values)
        if local is not None:
            place = local[frame][0]
        elif rest is not None:
            place = anim_io.from_object_location(rest, place)
        track.positions.append((frame, anim_io.to_file_vector(place)))
    for frame, values in scales.items():
        size = Vector(values)
        if local is not None:
            size = local[frame][2]
        elif rest is not None:
            size = anim_io.from_object_scale(rest, size)
        track.scales.append((frame, anim_io.to_file_vector(size)))
    for frame, value in _notes_of(holder):
        track.notes.append((frame, value))
    for frame, text in named_events(obj):
        track.named_notes.append((frame, text))
    if track.is_empty and getattr(obj, "ls3d_anim_auto_flags", True):
        return None
    return _apply_flag_choice(obj, track)


def _bone_track(armature, holder, pose_bone, report, fix_long_turns=False):
    """One pose bone's animation as a track, or ``None``."""
    prefix = f'pose.bones["{pose_bone.name}"].'
    rotations, _added, _too_fast = anim_io.export_rotation_keys(
        holder, pose_bone, prefix, split=fix_long_turns)
    positions = anim_io.sampled(holder, prefix + "location", 3, (0.0, 0.0, 0.0))
    scales = anim_io.sampled(holder, prefix + "scale", 3, (1.0, 1.0, 1.0))
    if not (rotations or positions or scales):
        return None

    bone = armature.data.bones.get(pose_bone.name)
    if bone is None:
        return None
    rest = anim_io.bone_rest(armature, bone)

    track = Track(name=pose_bone.name)
    for frame, values in rotations.items():
        turn = anim_io.from_pose_rotation(rest, Quaternion(values))
        track.rotations.append((frame, anim_io.to_file_quaternion(turn)))
    for frame, values in positions.items():
        place = anim_io.from_pose_location(rest, Vector(values))
        track.positions.append((frame, anim_io.to_file_vector(place)))
    for frame, values in scales.items():
        size = anim_io.from_pose_scale(rest, Vector(values))
        track.scales.append((frame, anim_io.to_file_vector(size)))
    return _apply_flag_choice(pose_bone, track)


#: The export option's tooltip, shared by the animation and model exports.
#: The limit and the step, in whole degrees, from the constants the check and
#: the split actually use - so the words can never say one number while the
#: export acts on another.
TURN_LIMIT_DEGREES = round(math.degrees(anim_io.MAX_TURN_BETWEEN_KEYS))
TURN_STEP_DEGREES = round(math.degrees(anim_io.MAX_EXPORT_TURN))

FIX_LONG_TURNS_DESCRIPTION = (
    f"The game only plays a rotation the way you keyed it when two neighboring "
    f"keys are less than {TURN_LIMIT_DEGREES} degrees apart. It always blends "
    f"the shorter way round, so a turn of {TURN_LIMIT_DEGREES} degrees or more "
    f"between two keys plays backwards, and a full 360 not at all. This adds "
    f"keys along each such turn, read off your own curve, so no step is more "
    f"than {TURN_STEP_DEGREES} degrees. Nothing in Blender is touched, and "
    f"rotations keyed as quaternions are never split. Off, the keys are written "
    f"exactly as keyed and each such turn is named in a warning")


class Export5DS(bpy.types.Operator, ExportHelper):
    """Export the scene's animation as an LS3D .5ds file"""

    bl_idname = "export_scene.5ds"
    bl_label = "Export 5DS"
    filename_ext = ".5ds"
    filter_glob: StringProperty(default="*.5ds", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        # Grayed out with nothing to write, rather than offered and then
        # refused.
        return scene_has_animation(context.scene)

    selection_only: BoolProperty(
        name="Selected Objects Only",
        description=("Write tracks for the selected objects alone. Off writes "
                     "every animated object in the scene"),
        default=False)

    def chosen(self, context):
        """The objects this export may look at, or ``None`` for the scene."""
        return list(context.selected_objects) if self.selection_only else None

    write_motion: BoolProperty(
        name="Write Its Track",
        description=("Also write the .tck holding where the actor travels. "
                     "Available when the model sits under a movement empty - "
                     "one arrives with an animation that has a track, and can "
                     "be added by hand"),
        default=True)

    use_scene_range: BoolProperty(
        name="Use Scene Frame Range",
        description=("Write the scene's end frame as the animation's length. "
                     "Off ends the animation on its last key, which is what "
                     "2717 of the game's own 2726 animations do"),
        default=True)

    fix_long_turns: BoolProperty(
        name="Fix Long Rotation Turns",
        description=FIX_LONG_TURNS_DESCRIPTION,
        default=False)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="What To Write", icon="RESTRICT_SELECT_OFF")
        box.prop(self, "selection_only")
        if self.selection_only:
            note = box.column()
            note.scale_y = 0.8
            note.label(text=f"{len(context.selected_objects)} object(s) "
                            f"selected.", icon="INFO")

        box = layout.box()
        box.label(text="Animation", icon="ACTION")
        box.prop(self, "use_scene_range")

        box = layout.box()
        box.label(text="Rotation Corrections", icon="CON_ROTLIKE")
        box.prop(self, "fix_long_turns")

        in_scene = motion_ops.motion_holders(context.scene)
        holders = motion_ops.motion_holders(context.scene,
                                            self.chosen(context))
        box = layout.box()
        row = box.row()
        row.label(text="Movement", icon="ANIM")
        if holders:
            row.label(text=holders[0].name)
        elif in_scene:
            row.label(text="not selected")
        # Grayed out with nothing to write: the movement lives on an empty
        # above the model, and without one there is no .tck to make.
        column = box.column()
        column.enabled = bool(holders)
        column.prop(self, "write_motion")
        column.prop(context.scene, "ls3d_motion_period")
        if not holders:
            note = box.column()
            note.scale_y = 0.8
            if in_scene:
                note.label(text=f"'{in_scene[0].name}' carries the movement "
                                f"but is not selected.", icon="INFO")
            else:
                note.label(text="Nothing in the scene carries movement.",
                           icon="BLANK1")

    def _write_motion(self, context, result):
        """Write the .tck beside the animation, if there is one to write."""
        if self.write_motion:
            write_motion_beside(context, self.filepath, result,
                                objects=self.chosen(context),
                                selection_only=self.selection_only)

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Exporting {filename}", "5DS")

        result.stage("Tracks")
        result.span(0, 70)
        chosen = self.chosen(context)
        if chosen is not None:
            result.info(f"Selected objects: {len(chosen)} of "
                        f"{len(context.scene.objects)} in the scene")
        animation = build_animation(context, result, self.use_scene_range,
                                    chosen, self.fix_long_turns)
        if animation is None:
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        for index, track in enumerate(animation.tracks, start=1):
            result.item("Track", index, len(animation.tracks),
                        describe.track_line(track))

        result.stage("Writing")
        result.span(70, 100)
        try:
            size = write_animation_file(animation, self.filepath)
        except OSError as problem:
            result.error(f"Could not write the file: {problem}")
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        tracks = animation.tracks
        keys = sum(len(t.rotations) + len(t.positions) + len(t.scales)
                   + len(t.notes) for t in tracks)
        result.info(f"Wrote {size} bytes: {len(tracks)} track(s), {keys} key(s)")
        self._write_motion(context, result)
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Export {status}: {filename}")
        result.show()
        return {"FINISHED"}


def build_animation(context, result, use_scene_range=True,
                    objects=None, fix_long_turns=False):
    """The scene's animation, ready to write, or ``None`` when it is refused.

    Shared by the animation export and by the model export writing the .5ds
    beside its .4ds, so both apply the same checks to the same tracks.
    """
    tracks = _tracks_from_scene(context.scene, result, objects,
                                fix_long_turns)
    if not tracks:
        result.error("Nothing in the scene is animated."
                     if objects is None else
                     "Nothing among the selected objects is animated.",
                     fix="Key a joint or an object first - an animation "
                         "with no tracks would do nothing.")
        return None

    last_key = max(
        (frame
         for track in tracks
         for frame, _ in (track.rotations + track.positions
                          + track.scales + track.notes)),
        default=0)
    end_frame = (int(context.scene.frame_end) if use_scene_range
                 else int(last_key))
    if end_frame < last_key:
        result.warn(
            f"Keys run to frame {last_key} but the animation is "
            f"{end_frame} frame(s) long, so the tail is never played.",
            fix="Move the scene's end frame past the last key, or turn "
                "off Use Scene Frame Range.")
    if end_frame < 0 or end_frame > MAX_U16:
        result.error(f"An animation cannot be {end_frame} frames long; "
                     f"the format holds up to {MAX_U16}.")
        return None

    # The one thing that cannot be written down: a scene on another clock says
    # something different here than it would in game, and there is no rate in
    # the file to carry the difference.
    if anim_io.refuse_wrong_frame_rate(context.scene, result):
        return None
    # Every check below looks at the objects the tracks came from, so an object
    # Selected Objects Only left out can neither warn nor refuse the export.
    _warn_about_easing(context.scene, result, objects)
    _warn_about_long_turns(context.scene, result, fix_long_turns, objects)

    # The travel is a transform above the model, so the model has to be under
    # it. Checked here rather than only where the .tck is written, since an
    # animation authored outside the track carries the travel itself.
    if motion_ops.check_parenting(context.scene, result, objects):
        return None

    animation = Animation(end_frame=end_frame, tracks=tracks)
    refused = []
    validate_animation(animation,
                       lambda message, fix=None: (
                           refused.append(message),
                           result.error(message, fix)),
                       result.warn)
    return None if refused else animation


def write_animation_beside(context, filepath, result, use_scene_range=True,
                           fix_long_turns=False, objects=None):
    """Write the scene's animation to *filepath*. True when it was written.

    *objects* narrows it to a chosen set, so a model exported with Selected
    Objects Only gets an animation of those objects and nothing else.
    """
    animation = build_animation(context, result, use_scene_range,
                                objects=objects,
                                fix_long_turns=fix_long_turns)
    if animation is None:
        return False
    try:
        write_animation_file(animation, filepath)
    except OSError as problem:
        result.error(f"Could not write the animation: {problem}")
        return False
    return True


def write_motion_beside(context, filepath, result, objects=None,
                        selection_only=False):
    """Write the .tck holding the travel beside *filepath*.

    True when one was written. The travel is a transform above the model
    rather than a frame of it, so it is a file of its own that the game opens
    beside the animation - and it is written from the one empty carrying it.

    *objects* narrows it to a chosen set, the way Selected Objects Only does:
    the travel goes out only when the empty carrying it is part of what is
    being exported, and *selection_only* is what says so when it is not.
    """
    from ..tck.codec import validate_track, write_track_file

    with result.as_format("TCK"):
        holders = motion_ops.motion_holders(context.scene, objects)
        if not holders:
            if selection_only and motion_ops.motion_holders(context.scene):
                result.warn(
                    "The movement track is not selected, so no .tck was "
                    "written beside the animation.",
                    fix="Select the movement empty as well, or turn off "
                        "Selected Objects Only.")
            return False

        period = (holders[0].ls3d_motion_period
                  or context.scene.ls3d_motion_period)
        track = motion_ops.build_motion_track(holders[0], context.scene,
                                              period, result)
        if track is None:
            return False
        refused = []
        validate_track(track,
                       lambda message, fix=None: (refused.append(message),
                                                  result.error(message, fix)),
                       result.warn)
        if refused:
            return False
        beside = os.path.splitext(filepath)[0] + ".tck"
        try:
            written = write_track_file(track, beside)
        except OSError as problem:
            result.warn(f"Could not write the movement: {problem}")
            return False
        result.info(f"Wrote {written} bytes to "
                    f"'{os.path.basename(beside)}'")
        return True


def menu_func_import(self, context):
    self.layout.operator(Import5DS.bl_idname, text="5DS Mafia Animation (.5ds)")


def menu_func_export(self, context):
    self.layout.operator(Export5DS.bl_idname, text="5DS Mafia Animation (.5ds)")




class LS3D_OT_CheckAnimation(bpy.types.Operator):
    """Check the scene's animation against what a 5DS can hold, without writing"""

    bl_idname = "ls3d.check_animation"
    bl_label = "Check 5DS Animation"
    bl_options = {"REGISTER"}

    def execute(self, context):
        result = report_module.Report().begin("Checking the animation", "5DS")
        result.stage("Tracks")
        result.span(0, 60)
        tracks = _tracks_from_scene(context.scene, result)
        if not tracks:
            result.error("Nothing in the scene is animated.",
                         fix="Key a joint or an object first - an animation "
                             "with no tracks would do nothing.")
            result.finish("Check FAILED - there is no animation to write")
            result.show()
            return {"CANCELLED"}

        result.stage("Rules")
        result.span(60, 100)
        end_frame = int(context.scene.frame_end)
        animation = Animation(end_frame=end_frame, tracks=tracks)
        refused = []
        validate_animation(animation,
                           lambda message, fix=None: (
                               refused.append(message),
                               result.error(message, fix)),
                           result.warn)

        if anim_io.refuse_wrong_frame_rate(context.scene, result):
            refused.append("the scene is not on the game's frame rate")
        _warn_about_easing(context.scene, result)
        # With the export's Fix Long Rotation Turns at its default - off - which
        # is what an export from the menu will do, so every long turn is named.
        _warn_about_long_turns(context.scene, result, fix_long_turns=False)

        keys = sum(len(t.rotations) + len(t.positions) + len(t.scales)
                   + len(t.notes) + len(t.named_notes) for t in tracks)
        result.info(f"{len(tracks)} track(s), {keys} key(s), "
                    f"{end_frame + 1} frame(s)")
        if refused:
            result.finish("Check FAILED - this animation would not export")
            result.show()
            return {"CANCELLED"}
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Check {status} - the animation would export")
        result.show()
        return {"FINISHED"}


# ── Event cues ────────────────────────────────────────────────────────────────
def event_items(self, context):
    """The named events, for a dropdown. Blender wants this rebuilt each time."""
    from .codec import EVENT_NAMES
    return [(str(value), label, note) for value, label, note in EVENT_NAMES]


def is_notify_frame(obj):
    """Whether *obj* is the one frame the game reads event cues from.

    A Dummy called ``notify``, and nothing else. The name has to match exactly
    because that is what the export writes as the track name and what the game
    looks the cues up by; the frame type has to be Dummy because that is what
    every one of the 305 models carrying this frame ships. A plain Blender
    empty is not enough - one added straight from Blender's own menu comes in
    as a Target frame.
    """
    return (obj is not None and obj.name == NOTIFY_TRACK
            and int(getattr(obj, "ls3d_frame_type", C.FRAME_VISUAL))
            == C.FRAME_DUMMY)


def stray_cue_owners(scene):
    """Objects other than the notify frame that are carrying cues.

    The export refuses these, so the panel says so while there is still
    something to be done about it.
    """
    return [obj for obj in scene.objects
            if not is_notify_frame(obj) and event_cues(obj)]


def event_owner(context):
    """Which object's cues the panel should show.

    Cues live on one frame of the model - the game's own animations put them
    on a frame called 'notify' - not on whatever happens to be selected. The
    active object wins if it carries any, so several can be edited in turn;
    otherwise the one that has them is found wherever it sits, because looking
    at the armature and seeing an empty list reads as "events do not work".
    """
    active = context.object
    if active is not None and event_cues(active):
        return active
    for obj in context.scene.objects:
        if event_cues(obj):
            return obj
    named = context.scene.objects.get(NOTIFY_TRACK)
    return named if named is not None else active


def event_cues(obj):
    """Every cue on *obj*, as ``(frame, value)`` in frame order."""
    return anim_io.note_cues(anim_io.channelbag(obj))


def _write_cues(obj, cues):
    holder = anim_io.channelbag(obj, create=True)
    anim_io.ensure_note_slots(obj)
    anim_io.write_note_cues(holder, cues, "notify", NOTE_INTERPOLATION)


class LS3D_OT_AddEventCue(bpy.types.Operator):
    """Put an event cue on this frame of the animation"""

    bl_idname = "ls3d.add_event_cue"
    bl_label = "Add Event Cue"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return is_notify_frame(context.object)

    def execute(self, context):
        obj = context.object
        frame = round(context.scene.frame_current)
        value = int(context.scene.ls3d_event_kind)
        from .codec import event_label
        cues = event_cues(obj)
        # Two cues can share a frame - a few of the game's own animations do
        # it - so a second one stacks rather than replacing the first. Asking
        # twice for the same cue on the same frame changes nothing.
        if (frame, value) in cues:
            self.report({"INFO"},
                        f"{event_label(value)} is already on frame {frame}.")
            return {"CANCELLED"}
        here = sum(1 for at, _kind in cues if at == frame)
        if here >= anim_io.NOTE_SLOTS:
            self.report({"WARNING"},
                        f"Frame {frame} already carries "
                        f"{anim_io.NOTE_SLOTS} cues.")
            return {"CANCELLED"}
        cues.append((frame, value))
        _write_cues(obj, cues)

        # The game reads an even-numbered event track one value out of step,
        # so it is worth knowing before the export says so.
        parity = ("" if len(cues) % 2
                  else " - an even number of cues; the game misreads those")
        stacked = f" (stacked on {here} already there)" if here else ""
        self.report({"INFO"},
                    f"{event_label(value)} at frame {frame}{stacked}{parity}")
        return {"FINISHED"}


class LS3D_OT_RemoveEventCue(bpy.types.Operator):
    """Take an event cue off this frame, the last one first"""

    bl_idname = "ls3d.remove_event_cue"
    bl_label = "Remove Event Cue"
    bl_options = {"REGISTER", "UNDO"}

    frame: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        owner = event_owner(context)
        return owner is not None and bool(event_cues(owner))

    def execute(self, context):
        # Removal still reaches wherever the cues are, so a set put on the
        # wrong frame before can be taken off again.
        obj = event_owner(context)
        target = (self.frame if self.frame >= 0
                  else round(context.scene.frame_current))
        cues = event_cues(obj)
        # Peel one off at a time, newest first, so a stacked frame can be
        # taken apart rather than emptied in one go.
        for index in range(len(cues) - 1, -1, -1):
            if cues[index][0] == target:
                cues.pop(index)
                break
        else:
            self.report({"WARNING"}, f"No cue on frame {target}.")
            return {"CANCELLED"}
        _write_cues(obj, cues)
        left = sum(1 for at, _kind in cues if at == target)
        if left:
            self.report({"INFO"},
                        f"{left} cue(s) still on frame {target}.")
        return {"FINISHED"}


class LS3D_OT_JumpToEventCue(bpy.types.Operator):
    """Move the playhead to this cue"""

    bl_idname = "ls3d.jump_to_event_cue"
    bl_label = "Jump To Cue"
    bl_options = {"REGISTER", "UNDO"}

    frame: bpy.props.IntProperty(default=0)

    def execute(self, context):
        context.scene.frame_set(self.frame)
        return {"FINISHED"}


class LS3D_OT_ActivateAction(bpy.types.Operator):
    """Make this animation the one the scene plays"""

    bl_idname = "ls3d.activate_action"
    bl_label = "Make Active"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(default="")

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.actions)

    def execute(self, context):
        chosen = bpy.data.actions.get(self.name) if self.name else None
        if chosen is None:
            index = context.scene.ls3d_action_index
            if not 0 <= index < len(bpy.data.actions):
                self.report({"WARNING"}, "No animation is picked.")
                return {"CANCELLED"}
            chosen = bpy.data.actions[index]

        scene = context.scene
        # The whole animation goes on at once: a character and the prop it
        # carries are one animation in the file even though Blender keeps an
        # action for each.
        placed = []
        for action in anim_io.actions_in_animation(chosen):
            owner = anim_io.object_for_action(action, scene)
            if owner is None:
                continue
            anim_io.assign_action(owner, action)
            placed.append(owner.name)

        if not placed:
            # Nothing claims it - put it on whatever is selected, so an action
            # brought in on its own can still be tried out.
            if context.object is None:
                self.report({"WARNING"},
                            f"Nothing in the scene matches '{chosen.name}'.")
                return {"CANCELLED"}
            anim_io.assign_action(context.object, chosen)
            placed.append(context.object.name)

        # The scene range is left exactly as it is. It is the user's setting,
        # several animations can be on at once, and the 5DS export reads it -
        # so rewriting it here would quietly change what a later export writes.
        # The panel says when the range does not reach the keys, and Fit Scene
        # Range is one click away when that is what you want.
        span = anim_io.animation_frame_range(
            anim_io.actions_in_animation(chosen))
        length = ""
        if span is not None:
            length = (f", keyed {int(math.floor(span[0]))} to "
                      f"{int(math.ceil(span[1]))}")

        scene.ls3d_action_index = bpy.data.actions.find(chosen.name)
        self.report({"INFO"},
                    f"'{anim_io.animation_base_name(chosen)}' is now on "
                    + ", ".join(placed) + length)
        return {"FINISHED"}


class LS3D_OT_UnloadAnimation(bpy.types.Operator):
    """Take every animation off the scene, so the timeline is blank and the model stands at rest. The animations stay in the list, and Make Active puts one back"""

    bl_idname = "ls3d.unload_animation"
    bl_label = "Unload Animation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not any(obj.animation_data is not None
                   and obj.animation_data.action is not None
                   for obj in context.scene.objects):
            cls.poll_message_set("Nothing in the scene has an animation on.")
            return False
        return True

    def execute(self, context):
        # What each animation keyed goes back to rest: a pose bone to its
        # bone, an object to where its Delta Transform stands it.
        taken = [obj.name for obj in context.scene.objects
                 if anim_io.take_off_animation(obj) is not None]
        context.view_layer.update()
        self.report({"INFO"},
                    f"Took the animation off {len(taken)} object(s); the model "
                    f"stands at rest. The animations stay in the list.")
        return {"FINISHED"}


class LS3D_OT_FitSceneRange(bpy.types.Operator):
    """Set the scene's frame range to cover every key in the scene"""

    bl_idname = "ls3d.fit_scene_range"
    bl_label = "Fit Scene Range To Keys"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        summary = anim_io.scene_animation_summary(context.scene)
        return bool(summary["tracks"]) and summary["last"] > 0

    def execute(self, context):
        scene = context.scene
        summary = anim_io.scene_animation_summary(scene)
        last = int(math.ceil(summary["last"]))
        if last <= 0:
            self.report({"INFO"}, "Nothing keyed past frame 0 to fit to.")
            return {"CANCELLED"}

        # Every animated frame in the scene, not just the one that happens to
        # be picked: a character and the props around it are separate actions
        # and the range has to reach the last key of the lot.
        before = (scene.frame_start, scene.frame_end)
        scene.frame_start = 0
        scene.frame_end = last
        if not scene.frame_start <= scene.frame_current <= scene.frame_end:
            scene.frame_set(scene.frame_start)
        self.report({"INFO"},
                    f"Scene range {before[0]} to {before[1]} -> 0 to {last}")
        return {"FINISHED"}


class LS3D_OT_FollowAnimatedCount(bpy.types.Operator):
    """Let the animated object count follow what the scene has keyed again"""

    bl_idname = "ls3d.follow_animated_count"
    bl_label = "Follow The Scene"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.ls3d_animated_count_pinned)

    def execute(self, context):
        scene = context.scene
        scene.ls3d_animated_count_pinned = False
        before = scene.ls3d_animated_object_count
        anim_io.refresh_animated_object_count(scene)
        # Clearing the pin is the point; the refresh only ever raises, so a
        # count already past what is keyed stays where it is.
        scene.ls3d_animated_count_pinned = False
        after = scene.ls3d_animated_object_count
        self.report({"INFO"},
                    f"Animated object count follows the scene again"
                    + (f" ({before} -> {after})" if after != before else
                       f" (still {after})"))
        return {"FINISHED"}


class LS3D_OT_SetFrameRate(bpy.types.Operator):
    """Put the scene on the frame rate the game plays animations at"""

    bl_idname = "ls3d.set_frame_rate"
    bl_label = "Set Scene To 25 fps"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return not anim_io.on_game_frame_rate(context.scene)

    def execute(self, context):
        before = anim_io.scene_frame_rate(context.scene)
        if not anim_io.apply_frame_rate(context.scene):
            self.report({"INFO"},
                        f"Already {anim_io.FRAMES_PER_SECOND} fps.")
            return {"CANCELLED"}
        self.report({"INFO"},
                    f"{anim_io.FRAMES_PER_SECOND} fps, was {before:g} - one "
                    f"frame every {1000 // anim_io.FRAMES_PER_SECOND} ms, "
                    f"which is what the game plays.")
        return {"FINISHED"}


class LS3D_OT_AddAction(bpy.types.Operator):
    """Start a new animation on the selected objects"""

    bl_idname = "ls3d.add_action"
    bl_label = "New Animation"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Name", default="New Animation")

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects or context.object)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=260)

    def draw(self, context):
        self.layout.prop(self, "name")

    def execute(self, context):
        # Whatever is selected, or just the active object when nothing is.
        targets = [obj for obj in context.selected_objects
                   if not motion_io.is_motion_track(obj)]
        if not targets and context.object is not None:
            targets = [context.object]
        if not targets:
            self.report({"WARNING"}, "Select what the animation should drive.")
            return {"CANCELLED"}

        made = []
        for obj in targets:
            if obj.type != "ARMATURE":
                _rest_under_animation(obj)
            # A fresh action each time, named after the animation, so a whole
            # move set can sit side by side the way an import leaves it.
            name = anim_io.action_name_for(obj, self.name, targets)
            anim_io.channelbag(obj, create=True, action_name=name, fresh=True)
            action = obj.animation_data.action
            if action is not None:
                made.append(action.name)

        if not made:
            self.report({"WARNING"}, "Could not make an animation here.")
            return {"CANCELLED"}
        context.scene.ls3d_action_index = bpy.data.actions.find(made[0])
        self.report({"INFO"},
                    f"'{self.name}' started on " +
                    ", ".join(obj.name for obj in targets) +
                    " - key it, then export.")
        return {"FINISHED"}


class LS3D_OT_DeleteAction(bpy.types.Operator):
    """Remove this animation from the file, every part of it"""

    bl_idname = "ls3d.delete_action"
    bl_label = "Delete Animation"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(default="")

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.actions)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        chosen = bpy.data.actions.get(self.name) if self.name else None
        if chosen is None:
            index = context.scene.ls3d_action_index
            if not 0 <= index < len(bpy.data.actions):
                self.report({"WARNING"}, "No animation is picked.")
                return {"CANCELLED"}
            chosen = bpy.data.actions[index]

        # An animation is however many actions share its name, so all of them
        # go: leaving one behind would leave half an animation in the file.
        label = anim_io.animation_base_name(chosen)
        doomed = anim_io.actions_in_animation(chosen)
        for obj in context.scene.objects:
            data = obj.animation_data
            if data is not None and data.action in doomed:
                data.action = None
        for action in doomed:
            # Imports keep a fake user so a whole move set survives saving;
            # that has to be let go before Blender will drop the action.
            action.use_fake_user = False
            bpy.data.actions.remove(action)

        count = len(bpy.data.actions)
        context.scene.ls3d_action_index = min(
            context.scene.ls3d_action_index, max(0, count - 1))
        self.report({"INFO"},
                    f"Deleted '{label}' ({len(doomed)} action(s)); "
                    f"{count} left")
        return {"FINISHED"}


class LS3D_OT_MoveEventCue(bpy.types.Operator):
    """Move this cue to a frame you type"""

    bl_idname = "ls3d.move_event_cue"
    bl_label = "Move Event Cue"
    bl_options = {"REGISTER", "UNDO"}

    frame: bpy.props.IntProperty(name="From", default=0, min=0, max=MAX_U16)
    to_frame: bpy.props.IntProperty(name="To Frame", default=0, min=0,
                                    max=MAX_U16)

    @classmethod
    def poll(cls, context):
        owner = event_owner(context)
        return owner is not None and bool(event_cues(owner))

    def invoke(self, context, event):
        self.to_frame = self.frame
        return context.window_manager.invoke_props_dialog(self, width=220)

    def draw(self, context):
        self.layout.prop(self, "to_frame")

    def execute(self, context):
        obj = event_owner(context)
        cues = event_cues(obj)
        moving = [(at, kind) for at, kind in cues if at == self.frame]
        if not moving:
            self.report({"WARNING"}, f"No cue on frame {self.frame}.")
            return {"CANCELLED"}
        kept = [(at, kind) for at, kind in cues if at != self.frame]
        kept.extend((self.to_frame, kind) for _at, kind in moving)
        # Two cues may share a frame, but not two of the same kind.
        if len(set(kept)) != len(kept):
            self.report({"WARNING"},
                        f"Frame {self.to_frame} already carries that cue.")
            return {"CANCELLED"}
        _write_cues(obj, kept)
        self.report({"INFO"},
                    f"Moved {len(moving)} cue(s) to frame {self.to_frame}")
        return {"FINISHED"}


class LS3D_OT_AddNamedEvent(bpy.types.Operator):
    """Put a named cue on this frame of the animation"""

    bl_idname = "ls3d.add_named_event"
    bl_label = "Add Named Event"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return is_notify_frame(context.object)

    def execute(self, context):
        obj = context.object
        frame = round(context.scene.frame_current)
        entry = obj.ls3d_named_events.add()
        entry.frame = frame
        entry.text = "event"
        obj.ls3d_named_event_index = len(obj.ls3d_named_events) - 1
        self.report({"INFO"}, f"Named cue at frame {frame}")
        return {"FINISHED"}


class LS3D_OT_RemoveNamedEvent(bpy.types.Operator):
    """Take this named cue off the animation"""

    bl_idname = "ls3d.remove_named_event"
    bl_label = "Remove Named Event"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (is_notify_frame(obj) and len(obj.ls3d_named_events) > 0)

    def execute(self, context):
        obj = context.object
        index = obj.ls3d_named_event_index
        if not 0 <= index < len(obj.ls3d_named_events):
            self.report({"WARNING"}, "No named cue is picked.")
            return {"CANCELLED"}
        obj.ls3d_named_events.remove(index)
        obj.ls3d_named_event_index = min(index,
                                         len(obj.ls3d_named_events) - 1)
        return {"FINISHED"}


CLASSES = (Import5DS, Export5DS, LS3D_OT_CheckAnimation,
           LS3D_OT_MoveEventCue,
           LS3D_OT_AddNamedEvent, LS3D_OT_RemoveNamedEvent,
           LS3D_OT_AddAction, LS3D_OT_DeleteAction,
           LS3D_OT_SetFrameRate,
           LS3D_OT_FitSceneRange,
           LS3D_OT_FollowAnimatedCount,
           LS3D_OT_ActivateAction, LS3D_OT_UnloadAnimation,
           LS3D_OT_AddEventCue, LS3D_OT_RemoveEventCue,
           LS3D_OT_JumpToEventCue)
