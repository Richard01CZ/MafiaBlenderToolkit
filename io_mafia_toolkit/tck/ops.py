"""Moving an actor's travel in and out of Blender.

A .tck rides beside the animation of the same name: the animation says how the
model moves in itself, this says where it goes while that plays. The two are
loaded and written together, and separately when only one is wanted.
"""

import os

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Quaternion, Vector

from ..common import describe
from ..common import report as report_module
from ..packages import module
from . import io as motion_io
from .codec import MotionTrack, TrackError, read_track_file

anim_io = module("5ds.io")

MOVEMENT_INTERPOLATION = anim_io.GAME_INTERPOLATION


class ImportTCK(bpy.types.Operator, ImportHelper):
    """Import an LS3D .tck motion track onto the model already in the scene"""

    bl_idname = "import_scene.tck"
    bl_label = "Import TCK"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".tck"
    filter_glob: StringProperty(default="*.tck", options={"HIDDEN"})

    set_frame_rate: BoolProperty(
        name="Set Scene Frame Rate",
        description=("Put the scene on 25 fps, which is the rate the game "
                     "plays animations at. Off keeps the scene's own rate and "
                     "samples the movement to match it"),
        default=True)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Timing", icon="TIME")
        box.prop(self, "set_frame_rate")
        column = layout.column()
        column.scale_y = 0.8
        column.label(text="Where the actor travels while its", icon="ANIM")
        column.label(text="animation plays. Arrives as an empty", icon="BLANK1")
        column.label(text="above the model, named " + motion_io.MOTION_NAME,
                     icon="BLANK1")

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Importing {filename}", "TCK")
        holders = [obj for obj in context.scene.objects
                   if obj.type == "ARMATURE" or obj.parent is None]
        track = load_motion_track(self.filepath, context, result, holders,
                                  os.path.splitext(filename)[0],
                                  set_frame_rate=self.set_frame_rate,
                                  own_range=True)
        if track is None:
            result.finish(f"Import FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Import {status}: {filename}")
        result.show()
        return {"FINISHED"}


class ExportTCK(bpy.types.Operator, ExportHelper):
    """Export the scene's movement track as an LS3D .tck file"""

    bl_idname = "export_scene.tck"
    bl_label = "Export TCK"
    filename_ext = ".tck"
    filter_glob: StringProperty(default="*.tck", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return bool(motion_holders(context.scene))

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        # No selection option here: the scene holds one movement track, so
        # there is nothing for a selection to choose between.
        holders = motion_holders(context.scene)
        box = layout.box()
        row = box.row()
        row.label(text="Movement", icon="ANIM")
        if holders:
            row.label(text=holders[0].name)
        box.prop(context.scene, "ls3d_motion_period")
        if not holders:
            note = box.column()
            note.scale_y = 0.8
            note.label(text="Nothing here carries movement.", icon="ERROR")

    def execute(self, context):
        from .codec import validate_track, write_track_file

        # The menu grays this out with nothing to write; say why for anything
        # that reaches here another way, rather than writing an empty track.
        if not motion_holders(context.scene):
            filename = os.path.basename(self.filepath)
            result = report_module.Report().begin(f"Exporting {filename}", "TCK")
            result.error("Nothing in the scene carries movement.",
                         fix="Mark an empty as the movement track in the 5DS "
                             "Animation sidebar and key its travel, or import "
                             "a .tck to make one.")
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Exporting {filename}", "TCK")
        holders = motion_holders(context.scene)
        if not holders:
            result.error("Nothing in the scene carries movement.",
                         fix=f"Mark an empty as the movement track - the 5DS "
                             f"Animation sidebar has the switch - or name it "
                             f"{motion_io.MOTION_NAME}.")
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        if check_parenting(context.scene, result):
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        # A track's length and key spacing are milliseconds, worked out from
        # the scene's clock, while the animation it belongs to is played at
        # the game's. On any other clock the two would describe different
        # journeys.
        if anim_io.refuse_wrong_frame_rate(context.scene, result):
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        period = holders[0].ls3d_motion_period or context.scene.ls3d_motion_period
        track = build_motion_track(holders[0], context.scene, period, result)
        if track is None:
            result.error("The movement track has no keys to write.")
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        refused = []
        validate_track(track,
                       lambda message, fix=None: (refused.append(message),
                                                  result.error(message, fix)),
                       result.warn)
        if refused:
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        try:
            written = write_track_file(track, self.filepath)
        except OSError as problem:
            result.error(f"Could not write the file: {problem}")
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        result.info(f"Wrote {written} bytes")
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Export {status}: {filename}")
        result.show()
        return {"FINISHED"}


class LS3D_OT_MarkMotionTrack(bpy.types.Operator):
    """Use this empty as the animation's movement track, or stop using it"""

    bl_idname = "ls3d.mark_motion_track"
    bl_label = "Use As Movement Track"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == "EMPTY"

    def execute(self, context):
        obj = context.object
        if motion_io.is_motion_track(obj):
            setattr(obj, motion_io.MOTION_FLAG, False)
            self.report({"INFO"}, f"'{obj.name}' is no longer the movement "
                                  f"track.")
            return {"FINISHED"}

        # A .tck holds one track, so marking this one hands the job over
        # rather than refusing. The property does the unmarking itself.
        others = [o.name for o in context.scene.objects
                  if o is not obj and motion_io.is_motion_track(o)]
        setattr(obj, motion_io.MOTION_FLAG, True)
        obj.rotation_mode = "QUATERNION"
        # Renamed only so it stands out in the outliner; the switch above is
        # what actually marks it.
        if obj.name.startswith("Empty"):
            obj.name = motion_io.MOTION_NAME
        handed_over = f", taking over from '{others[0]}'" if others else ""
        self.report({"INFO"},
                    f"'{obj.name}' is now the movement track{handed_over}.")
        return {"FINISHED"}


def menu_func_import_track(self, context):
    self.layout.operator(ImportTCK.bl_idname, text="TCK Mafia Movement (.tck)")


def menu_func_export_track(self, context):
    self.layout.operator(ExportTCK.bl_idname, text="TCK Mafia Movement (.tck)")





# ── Motion tracks ─────────────────────────────────────────────────────────────
def motion_holders(scene, objects=None):
    """Every movement empty that actually carries movement.

    *objects* narrows it to a chosen set - the export's Selected Objects Only.
    """
    found = []
    for obj in (scene.objects if objects is None else objects):
        if not motion_io.is_motion_track(obj):
            continue
        bag = anim_io.channelbag(obj)
        if bag is not None and any(c.data_path == "location"
                                   for c in bag.fcurves):
            found.append(obj)
    return found


def under_motion_track(obj, holder):
    """Whether *obj* hangs anywhere below *holder*."""
    parent = obj.parent
    while parent is not None:
        if parent is holder:
            return True
        parent = parent.parent
    return False


def check_parenting(scene, report, objects=None):
    """The model has to hang under the movement track.

    The game lays an animation's travel on top of whatever the model is
    already doing, which is exactly what a parent transform is - and it is why
    the import puts the model under the empty it makes. Left outside, the
    empty's travel never reaches the model: what the animator sees is the
    animation standing still while the empty wanders off, and the .5ds ends up
    carrying whatever they keyed to make up for it.

    Returns the objects that are in the wrong place, empty when all is well.
    *objects* narrows the armatures checked to a chosen set - an export's
    Selected Objects Only. The movement track is looked for in the whole scene
    either way: an armature that is being exported and is not under it is wrong
    whether or not the track was selected too.
    """
    holder = next((obj for obj in scene.objects
                   if motion_io.is_motion_track(obj)), None)
    if holder is None:
        return []
    # Armatures are what a movement track is for; a prop that is not under it
    # is its own business.
    loose = [obj for obj in (scene.objects if objects is None else objects)
             if obj.type == "ARMATURE" and not under_motion_track(obj, holder)]
    for obj in loose:
        report.error(
            f"'{obj.name}' is not under the movement track "
            f"'{holder.name}', so the travel never reaches it.",
            fix=f"Parent '{obj.name}' to '{holder.name}' - the movement is a "
                f"transform above the model, which is how the game adds it.")
    return loose


def sibling_track(animation_path):
    """The ``.tck`` beside an animation, or ``None``.

    Every one of the game's 283 readable tracks sits next to an animation of
    the same name, so that is the only place worth looking.
    """
    stem = os.path.splitext(animation_path)[0]
    for extension in (".tck", ".TCK", ".Tck"):
        candidate = stem + extension
        if os.path.isfile(candidate):
            return candidate
    return None


@anim_io.holding_count
def load_motion_track(path, context, result, holders, action_name,
                      set_frame_rate=False, own_range=False):
    """Put a motion track above the model, as its parent's animation."""

    try:
        track = read_track_file(path)
    except TrackError as problem:
        result.warn(str(problem))
        return None
    except OSError as problem:
        result.warn(f"Could not read the motion track: {problem}")
        return None

    # One track is one actor's travel, so it goes above one thing: the
    # armature if the animation drives a character, otherwise whatever single
    # root it drives. Hanging a copy over every root would write several.
    roots = [obj for obj in holders
             if motion_io.motion_holder(obj) is not None or obj.parent is None]
    chosen = next((obj for obj in roots if obj.type == "ARMATURE"), None)
    if chosen is None:
        chosen = roots[0] if roots else None
    if chosen is None:
        result.warn("Nothing the animation drives sits at the top of the "
                    "scene, so there is nowhere to hang its movement.")
        return None

    if set_frame_rate:
        anim_io.apply_frame_rate(context.scene, result)

    # The export samples the scene's own clock, so both the range and the key
    # spacing follow that clock rather than the game's - a scene left on
    # another rate still gets its samples back where it put them.
    rate = anim_io.scene_frame_rate(context.scene)
    needed = motion_io.motion_end_frame(track, rate)
    context.scene.frame_start = 0
    if own_range:
        # Nothing else has said how long the scene is, so the track does.
        context.scene.frame_end = needed
    elif context.scene.frame_end < needed:
        # An animation loaded alongside has already set the length; only widen
        # it, so the movement is not cut short by a shorter clip.
        context.scene.frame_end = needed

    frames = motion_io.motion_frames(track, rate)
    for root in (chosen,):
        holder = motion_io.ensure_motion_holder(root, context.collection)
        holder.ls3d_motion_period = track.key_period
        # Neither of these can be worked back out of the samples, so they ride
        # on the holder rather than being guessed at on the way out.
        holder.ls3d_motion_base_position = anim_io.to_blender_vector(
            track.base_position)
        holder.ls3d_motion_base_direction = anim_io.to_blender_vector(
            track.base_direction)
        bag = anim_io.channelbag(holder, create=True, fresh=True,
                                 action_name=f"{action_name} (motion)")
        anim_io.write_curve(
            bag, "location", 3,
            [(frame, anim_io.to_blender_vector(value))
             for frame, value in zip(frames, track.positions)],
            "motion", MOVEMENT_INTERPOLATION)
        if track.has_direction:
            anim_io.write_curve(
                bag, "rotation_quaternion", 4,
                [(frame, motion_io.direction_to_rotation(
                    anim_io.to_blender_vector(value)))
                 for frame, value in zip(frames, track.directions)],
                "motion", MOVEMENT_INTERPOLATION)
    result.item("Movement", 1, 1, describe.movement_line(track))
    return track


def build_motion_track(holder, scene, period, result):
    """Read a motion holder's curves back into a track, sampled by the clock."""

    bag = anim_io.channelbag(holder)
    if bag is None:
        return None
    location = [anim_io.curve(bag, "location", axis) for axis in range(3)]
    turning = [anim_io.curve(bag, "rotation_quaternion", axis)
               for axis in range(4)]
    if not any(location):
        return None

    rate = anim_io.scene_frame_rate(scene)
    duration = int(round((scene.frame_end - scene.frame_start)
                         * 1000.0 / rate))
    track = MotionTrack(duration=duration, key_period=max(1, int(period)))
    for index in range(track.expected_key_count):
        frame = scene.frame_start + index * track.key_period * rate / 1000.0
        place = Vector(tuple(
            curve.evaluate(frame) if curve else 0.0 for curve in location))
        track.positions.append(anim_io.to_file_vector(place))
        if any(turning):
            turn = Quaternion(tuple(
                curve.evaluate(frame) if curve else default
                for curve, default in zip(turning, (1.0, 0.0, 0.0, 0.0))))
            track.directions.append(anim_io.to_file_vector(
                motion_io.rotation_to_direction(turn)))
    if track.positions:
        # The file keeps its own pair of reference points and the game blends
        # against them; the first sample is not what they hold. They come off
        # the holder, falling back to the start of the travel for a track
        # being written from scratch.
        base = tuple(holder.ls3d_motion_base_position)
        aim = tuple(holder.ls3d_motion_base_direction)
        blank = (0.0, 0.0, 0.0)
        track.base_position = (anim_io.to_file_vector(Vector(base))
                               if base != blank else track.positions[0])
        track.base_direction = (anim_io.to_file_vector(Vector(aim))
                                if aim != blank else track.base_position)
    result.item("Movement", 1, 1, describe.movement_line(track, duration))
    return track


CLASSES = (ImportTCK, ExportTCK, LS3D_OT_MarkMotionTrack)
