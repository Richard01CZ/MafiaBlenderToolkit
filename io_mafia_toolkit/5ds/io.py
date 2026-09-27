"""Moving a ``.5ds`` animation between the file and a Blender scene.

An animation is a list of named tracks and nothing else - no skeleton, no
model. What each track animates is worked out by name at load time against
whatever is already in the scene, which is why an animation is imported *onto*
a model rather than on its own.

A name matches, in this order:

* a pose bone in any armature - the usual case, since almost every animation
  the game ships drives a character's joints;
* a skinned mesh, which is redirected to the armature that deforms it, because
  the game animates the mesh frame itself where Blender animates the armature
  object standing in for it;
* any other object, animated directly.

The awkward part is that the file stores each key as the node's whole local
transform, while a Blender pose bone stores a *displacement from its rest
pose*. Everything in :func:`pose_rotation` and its neighbors exists to convert
between the two.
"""

import math

import bpy
from mathutils import Euler, Quaternion, Vector

from ..common import convert
from ..packages import module
from .codec import FRAMES_PER_SECOND

#: Custom property the note track's event ids are keyframed onto.
NOTE_PROPERTY = "ls3d_note_id"
#: How many cues may share a single frame. One curve cannot hold two keys at
#: the same frame, so the property is an array and each slot gets its own
#: curve. The game's own animations stack at most two; the rest is headroom.
NOTE_SLOTS = 4

# ── Interpolation ─────────────────────────────────────────────────────────────
#: How the game gets from one key to the next: a straight line for movement,
#: and a slerp for rotation, with no easing at either end. Blender's own
#: default is Bezier, which slows into every key and speeds out of it - the
#: animation would preview at speeds the game never plays.
GAME_INTERPOLATION = "LINEAR"
#: Note cues hold their value until the next one instead of sliding.
CUE_INTERPOLATION = "CONSTANT"

#: How the file's channels map onto Blender's, and how many components each
#: has. Names are the ones a data path ends in.
CHANNELS = (("rotations", "rotation_quaternion", 4),
            ("positions", "location", 3),
            ("scales", "scale", 3))

#: Which curve a rotation is keyed on, by the owner's rotation mode. Blender
#: keys whichever one the mode selects and leaves the others alone, so reading
#: the quaternion channel finds nothing at all on a frame left in the default
#: Euler mode. The file has no Euler, so those keys are converted here rather
#: than looked for somewhere else.
ROTATION_CHANNELS = {
    "QUATERNION": ("rotation_quaternion", 4, (1.0, 0.0, 0.0, 0.0)),
    "AXIS_ANGLE": ("rotation_axis_angle", 4, (0.0, 0.0, 1.0, 0.0)),
}
EULER_CHANNEL = ("rotation_euler", 3, (0.0, 0.0, 0.0))

#: Past this much turn between two keys a pair of quaternions no longer says
#: which way round the rotation went. Half a turn is where the two arcs are the
#: same length; beyond it the game takes the shorter one.
MAX_TURN_BETWEEN_KEYS = math.pi

#: The most a stretch is left to turn once the export has split a long one.
#: The engine's rotation blending always takes the shorter arc, so anything
#: under half a circle plays the authored way; a quarter leaves room for a turn
#: that bends across axes rather than about just one.
MAX_EXPORT_TURN = math.pi / 2.0


def rotation_channel(owner):
    """``(data path, component count, rest value)`` for *owner*'s rotation."""
    mode = getattr(owner, "rotation_mode", "QUATERNION")
    return ROTATION_CHANNELS.get(mode, EULER_CHANNEL)


def _as_quaternion(values, path, mode):
    """One key's value from *path*, as a quaternion."""
    if path == "rotation_quaternion":
        return Quaternion(values)
    if path == "rotation_axis_angle":
        angle, x, y, z = values
        axis = Vector((x, y, z))
        if axis.length < 1e-9:
            return Quaternion((1.0, 0.0, 0.0, 0.0))
        return Quaternion(axis, angle)
    return Euler(values, mode).to_quaternion()


def sampled_rotation(holder, owner, prefix=""):
    """Every keyed frame's rotation as ``{frame: (w, x, y, z)}``.

    The file stores quaternions and nothing else, so a rotation keyed in Euler
    or axis-angle is converted rather than ignored. Consecutive keys are then
    put on the same side of the double cover: a quaternion and its negative are
    the same rotation but interpolate opposite ways round, and converting each
    key on its own can hand back either one.
    """
    path, count, rest = rotation_channel(owner)
    mode = getattr(owner, "rotation_mode", "QUATERNION")
    raw = sampled(holder, prefix + path, count, rest)

    turns = {}
    previous = None
    for frame in sorted(raw):
        turn = _as_quaternion(raw[frame], path, mode)
        if previous is not None and turn.dot(previous) < 0.0:
            turn = Quaternion((-turn.w, -turn.x, -turn.y, -turn.z))
        turns[frame] = (turn.w, turn.x, turn.y, turn.z)
        previous = turn
    return turns


def _authored_turn(first, second, path):
    """How far an Euler or axis-angle curve turns between two keyed values.

    Measured from the values as keyed rather than from their quaternions: two
    quaternions only say where a turn starts and ends, which is the very thing
    that loses a turn past half a circle.
    """
    if path == "rotation_euler":
        # An upper bound on the real angle, which is what the split needs: a
        # rotation's angle is never more than the sum of its Euler turns.
        return sum(abs(b - a) for a, b in zip(first, second))
    # Axis-angle: the angle is keyed as a number of its own and moves linearly.
    return abs(second[0] - first[0])


def turns_to_split(holder, owner, prefix=""):
    """Stretches the long-turn fix would split, as ``[(from, to, radians)]``.

    The one place that decides it, so the fix and the warning given when the
    fix is off can never disagree about which turns are too long. Quaternion
    curves never appear: see :func:`export_rotation_keys`.
    """
    path, count, rest = rotation_channel(owner)
    if path == "rotation_quaternion":
        return []
    curves = [curve(holder, prefix + path, index) for index in range(count)]
    if not any(curves):
        return []

    def value_at(frame):
        return tuple(rest[index] if curves[index] is None
                     else curves[index].evaluate(frame)
                     for index in range(count))

    keyed = sorted({round(point.co[0]) for fcurve in curves
                    if fcurve is not None
                    for point in fcurve.keyframe_points})
    found = []
    for first, second in zip(keyed, keyed[1:]):
        travel = _authored_turn(value_at(first), value_at(second), path)
        if travel >= MAX_TURN_BETWEEN_KEYS - 1e-6:
            found.append((first, second, travel))
    return found


def export_rotation_keys(holder, owner, prefix="", split=True):
    """The rotation keys the file needs, as ``(turns, added, too_fast)``.

    The keys Blender has, plus whatever the engine needs to play them the same
    way. Its blending between two rotation keys always takes the shorter arc,
    so a turn of half a circle or more between two keys comes out the other way
    round - and a whole turn not at all. Each such stretch is split by
    evaluating the authored curve at evenly spaced frames in between. For a
    turn about one axis that reproduces Blender's own interpolation exactly:
    both move at an even angular speed from one key to the next.

    *turns* is ``{frame: (w, x, y, z)}``, consecutive keys on the same side of
    the double cover. *added* counts the keys the split put in. *too_fast* lists
    ``(from frame, to frame, radians)`` for any stretch with too few frames in
    it to split finely enough - keys sit on whole frames, so a half-turn
    squeezed into one frame has nowhere to put the key it needs.

    With *split* off - the export's Fix Long Rotation Turns unticked - the keys
    are written exactly as they were keyed and nothing is added.
    """
    path, count, rest = rotation_channel(owner)
    mode = getattr(owner, "rotation_mode", "QUATERNION")
    curves = [curve(holder, prefix + path, index) for index in range(count)]
    if not any(curves):
        return {}, 0, []

    # A quaternion curve is never split. It is already in the file's own terms,
    # and the engine reads two neighboring quaternion keys by their shorter
    # arc - so that is what the keys mean, and there is nothing further to
    # carry. It matters beyond tidiness: thousands of the game's own key pairs
    # store neighboring rotations in opposite halves, a turn of nearly nothing
    # with its sign flipped. Blender blends those the long way round, and
    # splitting along that path would make every re-exported game animation
    # spin where the original barely moved.
    splittable = path != "rotation_quaternion"

    def value_at(frame):
        return tuple(rest[index] if curves[index] is None
                     else curves[index].evaluate(frame)
                     for index in range(count))

    keyed = sorted({round(point.co[0]) for fcurve in curves
                    if fcurve is not None
                    for point in fcurve.keyframe_points})

    frames = list(keyed)
    too_fast = []
    for first, second, travel in (turns_to_split(holder, owner, prefix)
                                  if split and splittable else ()):
        span = second - first
        pieces = min(max(2, math.ceil(travel / MAX_EXPORT_TURN)), span)
        for step in range(1, pieces):
            frames.append(first + round(span * step / pieces))
        if travel / pieces >= MAX_TURN_BETWEEN_KEYS - 1e-6:
            too_fast.append((first, second, travel))

    turns = {}
    previous = None
    for frame in sorted(set(frames)):
        turn = _as_quaternion(value_at(frame), path, mode)
        if previous is not None and turn.dot(previous) < 0.0:
            turn = Quaternion((-turn.w, -turn.x, -turn.y, -turn.z))
        turns[frame] = (turn.w, turn.x, turn.y, turn.z)
        previous = turn
    added = len(turns) - len(keyed)
    return turns, added, too_fast


def long_turns(holder, owner, prefix=""):
    """Consecutive rotation keys too far apart for a quaternion pair to carry.

    Returns ``[(from frame, to frame, radians)]``. Two quaternions give the two
    ends of a turn and not the path between them, so past half a turn the game
    goes the short way round - and a full revolution keyed as two keys is the
    same rotation twice, which does not turn at all.
    """
    path, count, rest = rotation_channel(owner)
    mode = getattr(owner, "rotation_mode", "QUATERNION")
    raw = sampled(holder, prefix + path, count, rest)
    turns = sampled_rotation(holder, owner, prefix)

    found = []
    frames = sorted(turns)
    for first, second in zip(frames, frames[1:]):
        dot = max(-1.0, min(1.0, Quaternion(turns[first])
                            .dot(Quaternion(turns[second]))))
        angle = 2.0 * math.acos(abs(dot))
        if path == "rotation_euler":
            # What was actually authored, which is the figure that surprises
            # people: Blender turns the long way and the game will not.
            angle = max(angle, max(abs(b - a) for a, b
                                   in zip(raw[first], raw[second])))
        if angle >= MAX_TURN_BETWEEN_KEYS - 1e-6:
            found.append((first, second, angle))
    return found


# ── Axis conversion ───────────────────────────────────────────────────────────
def to_blender_quaternion(value):
    """A stored ``(w, x, y, z)`` rotation in Blender's axes."""
    return Quaternion(convert.to_blender_quaternion(value))


def to_file_quaternion(quaternion):
    return convert.to_mafia_quaternion(quaternion)


def to_blender_vector(value):
    return Vector(convert.to_blender_vector(value))


def to_file_vector(vector):
    return convert.to_mafia_vector(vector)


# ── Finding what a track drives ───────────────────────────────────────────────
def deforming_armature(obj):
    """The armature that skins *obj*, or ``None``."""
    if obj.parent is not None and obj.parent.type == "ARMATURE":
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and modifier.object is obj.parent:
                return obj.parent
    return None


class Target:
    """What one track drives, and the rest pose its keys are relative to."""

    def __init__(self, holder, path_prefix, rest=None, bone=None):
        #: The object whose animation data the keys land on.
        self.holder = holder
        #: What every data path starts with - "" or 'pose.bones["x"].'.
        self.prefix = path_prefix
        #: The rest transform the file's absolute keys are measured against.
        self.rest = rest
        self.bone = bone

    @property
    def is_bone(self):
        return self.bone is not None


class BoneRest:
    """What a bone's keys are measured against, in the file's terms.

    A 5DS key is a frame's whole local transform. Blender's pose channels are
    measured from the bone's rest instead, and the bone rests where the game
    puts the joint - which is the file's local position stretched by every
    scale above it. So a key's position moves the bone by that same stretch:
    ``basis`` turns a change in the file's position into a change in the pose
    location, and back.

    ``location``, ``rotation`` and ``scale`` are the frame's own rest in the
    file, worked out from the scene by :class:`JointSpace` the same way the
    model export writes them, so a key equal to the rest is no pose at all.
    """

    def __init__(self, armature, bone, space):
        self.location, self.rotation, self.scale = space.locals[bone.name]
        parent_rest = (bone.parent.matrix_local.to_quaternion()
                       if bone.parent is not None
                       else Quaternion((1.0, 0.0, 0.0, 0.0)))
        relative = parent_rest.inverted() @ bone.matrix_local.to_quaternion()
        _parent, parent_world, _turn = space.parent_space(bone)
        stretch = parent_rest.to_matrix().inverted() @ parent_world.to_3x3()
        self.basis = relative.to_matrix().inverted() @ stretch
        self.basis_inverse = self.basis.inverted()


def joint_space(armature, scene=None):
    """The file's view of *armature*'s joints, with the mesh it skins."""
    space_module = module("4ds.joint_space")
    objects = (scene or bpy.context.scene).objects
    return space_module.JointSpace(
        armature, space_module.skinned_mesh_of(armature, objects))


def bone_rest(armature, bone, scene=None):
    """The rest a bone's keys are measured against."""
    return BoneRest(armature, bone, joint_space(armature, scene))


def skinning_armature(obj):
    """The armature *obj* is the skinned mesh of, or ``None``."""
    space_module = module("4ds.joint_space")
    armature = deforming_armature(obj)
    if armature is None and space_module.is_skinned_mesh(obj):
        armature = space_module.find_armature(obj)
    return armature


def find_target(name, scene=None):
    """Work out what a track called *name* drives, or ``None``."""
    objects = (scene or bpy.context.scene).objects

    # Objects before bones, and deliberately: a track named after a skinned
    # mesh means the mesh frame, which is the one that moves in game.
    obj = objects.get(name)
    if obj is not None:
        armature = skinning_armature(obj)
        if armature is not None:
            # The mesh frame carries the whole body - the mesh and every joint
            # hanging from it - and in Blender that is the armature moving.
            # It rests where the armature's setting for the frame puts it, so
            # that has to come back out of every key.
            return Target(armature, "", rest=module(
                "4ds.joint_space").mesh_frame_matrix(armature))
        return Target(obj, "")

    for candidate in objects:
        if candidate.type != "ARMATURE":
            continue
        bone = candidate.data.bones.get(name)
        if bone is not None:
            return Target(candidate, f'pose.bones["{name}"].',
                          rest=bone_rest(candidate, bone, scene), bone=bone)
    return None


# ── Absolute keys to Blender's relative channels ──────────────────────────────
def pose_rotation(rest, rotation):
    """An absolute local rotation as a pose bone's rotation_quaternion."""
    return rest.rotation.inverted() @ rotation


def pose_location(rest, position):
    """An absolute local position as a pose bone's location."""
    return rest.basis @ (position - rest.location)


def pose_scale(rest, scale):
    """An absolute local scale as a pose bone's scale.

    Measured against the joint's own rest scale: a key equal to it leaves the
    bone unscaled, and one of 1.0 on a joint resting at 1.0506 shrinks it and
    everything below it, as the game does.
    """
    return Vector(tuple(
        value / base if abs(base) > 1e-9 else value
        for value, base in zip(scale, rest.scale)))


def object_rotation(rest, rotation):
    """A mesh frame's rotation as the armature's own rotation.

    The armature stands on the frame's rest through its Delta Transform, which
    Blender turns it by outside its own rotation, so the rest comes off the
    front. A constant, so the turn between two keys is the same either way.
    """
    return rest.to_quaternion().inverted() @ rotation


def object_location(rest, position):
    """A mesh frame's position as the armature's own location.

    Blender adds an object's Delta Transform place to its location, so the
    frame's rest place comes off - a constant, so the armature slides between
    two keys exactly as the frame does.
    """
    return position - rest.to_translation()


def object_scale(rest, scale):
    """A mesh frame's scale as the armature's own scale: measured against the
    frame's rest scale, which the Delta Transform already holds."""
    return Vector(tuple(value / base
                        for value, base in zip(scale, rest.to_scale())))


def without_duplicate_frames(keys):
    """Collapse keys sharing a frame, keeping the last, as the game does.

    81 of the game's animations put two keys on one frame - 26117 times in all,
    almost always with different values. An fcurve cannot hold both, and the
    game cannot use both either: its search walks forward to the first key
    *past* the frame it wants, so the earlier of a pair is stepped over and
    never read. Keeping the last one is therefore what the game plays.
    """
    kept = {}
    for frame, value in keys:
        kept[frame] = value
    dropped = len(keys) - len(kept)
    return [(frame, kept[frame]) for frame in sorted(kept)], dropped


# ── Blender's relative channels back to absolute keys ─────────────────────────
def from_pose_rotation(rest, rotation):
    return rest.rotation @ rotation


def from_pose_location(rest, location):
    return rest.location + rest.basis_inverse @ location


def from_pose_scale(rest, scale):
    return Vector(tuple(value * base
                        for value, base in zip(scale, rest.scale)))


def from_object_rotation(rest, rotation):
    return rest.to_quaternion() @ rotation


def from_object_location(rest, location):
    return location + rest.to_translation()


def from_object_scale(rest, scale):
    return Vector(tuple(value * base
                        for value, base in zip(scale, rest.to_scale())))


# ── Action plumbing ───────────────────────────────────────────────────────────
def action_name_for(obj, animation_name, holders=()):
    """What to call the action holding *animation_name* on *obj*.

    An animation usually drives one thing, and then its action is simply named
    after the file. Where it drives several - a character's armature and a prop
    hanging off it - Blender keeps one action per object, so the armature takes
    the plain name, being the one anybody goes looking for, and the rest are
    spelled out.
    """
    others = [holder for holder in holders if holder is not obj]
    if not others or obj.type == "ARMATURE":
        return animation_name
    return f"{animation_name} ({obj.name})"


def channelbag(obj, create=False, action_name=None, fresh=False):
    """The fcurve holder for *obj*, across Blender's slotted-action API.

    Blender 4.4 put fcurves behind an action slot; older versions keep them on
    the action itself. Both are handled so the addon does not pin a version.

    With *fresh*, a new action is made and assigned rather than the current one
    reused, so loading a second animation onto a model does not overwrite the
    first - both stay in the file and can be picked in the Action Editor.
    """
    animation_data = obj.animation_data
    if animation_data is None:
        if not create:
            return None
        animation_data = obj.animation_data_create()

    action = animation_data.action
    if create and (fresh or action is None):
        action = bpy.data.actions.new(action_name or f"{obj.name}Action")
        # Kept alive when nothing points at it, so a whole move set survives
        # saving and reloading even though only one action is assigned.
        action.use_fake_user = True
        animation_data.action = action
        animation_data.action_slot = None
    if action is None:
        return None

    if not hasattr(action, "slots"):
        return action                      # pre-slot Blender: fcurves are here

    from bpy_extras import anim_utils
    if animation_data.action_slot is None:
        if not create:
            existing = [action.slots[0]] if len(action.slots) else []
            if not existing:
                return None
            animation_data.action_slot = existing[0]
        else:
            animation_data.action_slot = action.slots.new(
                id_type="OBJECT", name=obj.name)
    if create:
        return anim_utils.action_ensure_channelbag_for_slot(
            action, animation_data.action_slot)
    return anim_utils.action_get_channelbag_for_slot(
        action, animation_data.action_slot)


def curve(holder, data_path, index, create=False, group=""):
    """One fcurve out of a channelbag, made if asked for and missing."""
    if holder is None:
        return None
    for existing in holder.fcurves:
        if existing.data_path == data_path and existing.array_index == index:
            return existing
    if not create:
        return None
    return holder.fcurves.new(data_path, index=index, group_name=group)


#: How cue keys are drawn. Blender colors a keyframe by its type, so giving
#: cues one of their own sets them apart from movement keys wherever keys are
#: shown. Nothing reads it back - the export takes only the frame and the
#: value - so it is presentation, set fresh on every write.
CUE_KEY_TYPE = "EXTREME"


def write_one_curve(holder, data_path, index, keys, group="",
                    interpolation=GAME_INTERPOLATION, key_type=None):
    """Replace a single curve of a channel with *keys*, a list of
    ``(frame, value)``. Emptying it takes the curve away rather than leaving a
    curve with nothing on it."""
    fcurve = curve(holder, data_path, index, create=bool(keys), group=group)
    if fcurve is None:
        return
    if not keys:
        holder.fcurves.remove(fcurve)
        return
    fcurve.keyframe_points.clear()
    flat = []
    for frame, value in keys:
        flat.append(float(frame))
        flat.append(float(value))
    fcurve.keyframe_points.add(count=len(keys))
    fcurve.keyframe_points.foreach_set("co", flat)
    for point in fcurve.keyframe_points:
        point.interpolation = interpolation
        if key_type is not None:
            point.type = key_type
    fcurve.update()


def ensure_note_slots(obj):
    """Give *obj* the array the cue curves are keyed onto."""
    existing = obj.get(NOTE_PROPERTY)
    if existing is None or not hasattr(existing, "__len__")             or len(existing) != NOTE_SLOTS:
        obj[NOTE_PROPERTY] = [0] * NOTE_SLOTS


def note_cues(holder):
    """Every cue on *holder* as ``(frame, value)``, stacked ones included.

    A cue is a key on one of the slot curves, so a value of zero counts like
    any other - the game's own animations do use it.
    """
    path = f'["{NOTE_PROPERTY}"]'
    found = []
    for slot in range(NOTE_SLOTS):
        fcurve = curve(holder, path, slot)
        if fcurve is None:
            continue
        for point in fcurve.keyframe_points:
            found.append((round(point.co[0]), slot,
                          int(round(point.co[1]))))
    found.sort()
    return [(frame, value) for frame, _slot, value in found]


def write_note_cues(holder, cues, group="notify",
                    interpolation=CUE_INTERPOLATION):
    """Lay *cues* across the slots, stacking any that share a frame.

    Returns however many would not fit, so the caller can say so.
    """
    lanes = [[] for _ in range(NOTE_SLOTS)]
    taken = {}
    overflow = 0
    for frame, value in sorted(cues, key=lambda cue: cue[0]):
        slot = taken.get(frame, 0)
        if slot >= NOTE_SLOTS:
            overflow += 1
            continue
        taken[frame] = slot + 1
        lanes[slot].append((frame, value))
    path = f'["{NOTE_PROPERTY}"]'
    for slot, keys in enumerate(lanes):
        write_one_curve(holder, path, slot, keys, group, interpolation,
                        key_type=CUE_KEY_TYPE)
    return overflow


def write_curve(holder, data_path, components, keys, group="",
                interpolation=GAME_INTERPOLATION):
    """Replace one channel's curves with *keys*, a list of (frame, values)."""
    for index in range(components):
        fcurve = curve(holder, data_path, index, create=True, group=group)
        fcurve.keyframe_points.clear()
        flat = []
        for frame, values in keys:
            flat.append(float(frame))
            flat.append(float(values[index]))
        fcurve.keyframe_points.add(count=len(keys))
        fcurve.keyframe_points.foreach_set("co", flat)
        for point in fcurve.keyframe_points:
            point.interpolation = interpolation
        fcurve.update()


def sampled(holder, data_path, components, default):
    """Every keyed frame of one channel, as ``{frame: (values...)}``.

    Only frames that carry a key are returned; a channel keyed on some
    components and not others is filled in from the curve so the file gets a
    complete value at every frame it mentions.
    """
    curves = [curve(holder, data_path, index) for index in range(components)]
    if not any(curves):
        return {}
    frames = set()
    for fcurve in curves:
        if fcurve is not None:
            frames.update(round(point.co[0]) for point in fcurve.keyframe_points)
    result = {}
    for frame in sorted(frames):
        values = []
        for index, fcurve in enumerate(curves):
            if fcurve is None:
                values.append(default[index])
            else:
                values.append(fcurve.evaluate(frame))
        result[frame] = tuple(values)
    return result


def scene_frame_rate(scene):
    """The scene's frames per second, however the render settings express it."""
    render = scene.render
    return render.fps / render.fps_base if render.fps_base else render.fps


def apply_frame_rate(scene, report=None):
    """Put the scene on the game's own frame rate, saying so if it moved."""
    before = scene_frame_rate(scene)
    if abs(before - FRAMES_PER_SECOND) < 1e-6:
        return False
    scene.render.fps = FRAMES_PER_SECOND
    scene.render.fps_base = 1.0
    if report is not None:
        report.info(f"Scene frame rate set to {FRAMES_PER_SECOND} fps "
                    f"(was {before:g}); the game plays one frame every "
                    f"{1000 // FRAMES_PER_SECOND} ms.")
    return True


def count_animated_objects(scene, objects=None):
    """How many tracks the scene would write, which is the 4DS's own count.

    A 4DS ends with a count of animated objects, and in every one of the 31
    models the game ships with an animation that number is exactly the number
    of tracks in the animation beside it. So it is not a flag with a spare
    byte around it - it is a count, and it is worked out from the scene rather
    than remembered.
    """
    return len(scene_animation_summary(scene, frames=False,
                                       objects=objects)["tracks"])


def scene_animation_summary(scene, frames=True, objects=None):
    """What the scene would export, counted without sampling a single curve.

    Cheap on purpose: this runs from a draw handler, so it counts curves rather
    than evaluating them. The numbers are the shape of the animation - how many
    tracks, how many channels - not the keys themselves. Pass ``frames=False``
    to skip walking the keys, when only the track count is wanted. *objects*
    narrows it to a chosen set - an export's Selected Objects Only.
    """
    from ..tck.io import is_motion_track

    tracks = []
    first = None
    last = None
    for obj in (scene.objects if objects is None else objects):
        holder = channelbag(obj)
        if holder is None:
            continue
        # The movement track goes to the .tck, never into the animation, so
        # counting it here would promise a track the export does not write.
        if is_motion_track(obj):
            continue
        found = {}
        for fcurve in holder.fcurves:
            path = fcurve.data_path
            name = obj.name
            if path.startswith('pose.bones["'):
                name = path[len('pose.bones["'):path.index('"]')]
                path = path[path.index('"].') + 3:]
            elif obj.type == "ARMATURE":
                skinned = module("4ds.joint_space").skinned_mesh_of(
                    obj, scene.objects)
                if skinned is None:
                    # An armature is not a frame. Only with a skinned mesh
                    # does it stand in for one: the mesh's.
                    continue
                name = skinned.name
            kind = next((label for _flag, _attr, label, _d
                         in _CHANNEL_LABELS if path.startswith(_d)), None)
            if kind is None:
                continue
            found.setdefault(name, set()).add(kind)
            if not frames:
                continue
            for point in fcurve.keyframe_points:
                frame = round(point.co[0])
                first = frame if first is None else min(first, frame)
                last = frame if last is None else max(last, frame)
        for name, kinds in found.items():
            tracks.append((name, sorted(kinds)))
    tracks.sort()
    return {"tracks": tracks,
            "first": first if first is not None else 0,
            "last": last if last is not None else 0}


#: Which data path each channel shows up under, for the summary above. The
#: rotation entry is the bare prefix on purpose: it has to match whichever of
#: rotation_quaternion, rotation_euler and rotation_axis_angle the owner's
#: rotation mode keys, or a frame keyed in Euler counts as having no animation
#: and the export refuses to run at all.
_CHANNEL_LABELS = (
    (None, None, "Rotation", "rotation_"),
    (None, None, "Position", "location"),
    (None, None, "Scale", "scale"),
    (None, None, "Events", f'["{NOTE_PROPERTY}"]'),
)


#: Nesting depth of :func:`holding_animated_count`.
_count_held = 0


class holding_animated_count:
    """Leave the animated-object count exactly as it is for this stretch.

    The count is the model's own word about itself, carried in the .4DS.
    Loading an animation onto that model says nothing about it - the animation
    may drive two of its frames or twenty - so an import must pass through
    without touching the number.
    """

    def __enter__(self):
        global _count_held
        _count_held += 1
        return self

    def __exit__(self, *_exception):
        global _count_held
        _count_held -= 1
        return False


def holding_count(function):
    """Run *function* with the animated-object count held.

    Put on the loaders rather than the operators, so the model import loading
    an animation for itself is covered by the same rule.
    """
    import functools

    @functools.wraps(function)
    def held(*args, **kwargs):
        with holding_animated_count():
            return function(*args, **kwargs)
    return held


def count_is_held():
    """Whether the animated-object count is being left alone right now."""
    return bool(_count_held)


def refresh_animated_object_count(scene):
    """Raise the scene's animated-object count to match what is animated.

    Only ever upwards, and only when there is something to count. Lowering it
    would throw away the model's own word: a .4DS that declares five animated
    frames still declares five when an animation driving two of them is
    loaded, or when none has been loaded at all.

    A number typed by hand wins outright. The count is what tells the engine to
    go looking for a .5DS, so "none" has to be sayable even in a scene full of
    keys - that is the state a model is in while its animation is still being
    built.
    """
    if _count_held or getattr(scene, "ls3d_animated_count_pinned", False):
        return False
    counted = count_animated_objects(scene)
    if counted <= scene.ls3d_animated_object_count:
        return False
    # Held while it writes, so its own raise is never mistaken for a number
    # somebody typed.
    with holding_animated_count():
        scene.ls3d_animated_object_count = counted
    return True


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, _depsgraph=None):
    try:
        refresh_animated_object_count(scene)
    except (AttributeError, ReferenceError):
        # Blender tearing the scene down mid-update; nothing to keep in step.
        pass


def register_handlers():
    """Keep the animated-object count in step as the scene is keyed.

    Marked persistent: Blender empties the handler lists when a file is opened
    or a new one started, and without that the count quietly stops following
    the scene from the first New File onwards.
    """
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister_handlers():
    while _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)


def use_game_interpolation(report=None):
    """Make new keyframes linear, the way the game reads them.

    Blender inserts Bezier keys by default, so a key added by hand in the
    middle of an imported animation eases in and out while its neighbors do
    not. Set once, when an animation arrives.
    """
    try:
        editing = bpy.context.preferences.edit
    except AttributeError:
        return False
    if editing.keyframe_new_interpolation_type == GAME_INTERPOLATION:
        return False
    was = editing.keyframe_new_interpolation_type
    editing.keyframe_new_interpolation_type = GAME_INTERPOLATION
    if report is not None:
        report.info(f"New keyframes are now {GAME_INTERPOLATION.lower()} "
                    f"(was {was.lower()}); the game walks straight from one "
                    f"key to the next.")
    return True


def animation_base_name(action):
    """The animation an action belongs to, read off its own name.

    An animation driving several things keeps one action each, the extras
    spelled out in brackets. Stripping the bracket gives what they share.
    """
    name = action.name
    if name.endswith(")") and " (" in name:
        return name[:name.rindex(" (")]
    return name


def _slot_labels(slot):
    """What an action slot might be called, however Blender spells it.

    A slot's own identifier carries a two-letter id-type prefix; the display
    name does not. Both are offered so the lookup does not depend on which
    one this Blender exposes.
    """
    labels = set()
    for attribute in ("name_display", "identifier", "name"):
        value = getattr(slot, attribute, None)
        if isinstance(value, str) and value:
            labels.add(value)
            labels.add(value[2:])
    return labels


def object_for_action(action, scene):
    """Which object in *scene* an action was made for.

    Worked out from the action's own slot rather than anything remembered, so
    renaming either one cannot leave a wrong answer behind.
    """
    labels = set()
    for slot in getattr(action, "slots", ()):
        labels.update(_slot_labels(slot))
    for obj in scene.objects:
        if obj.name in labels:
            return obj
    for obj in scene.objects:
        data = obj.animation_data
        if data is not None and data.action is action:
            return obj
    return None


def actions_in_animation(action):
    """Every action making up the same animation as *action*."""
    base = animation_base_name(action)
    return [other for other in bpy.data.actions
            if animation_base_name(other) == base]


def leading_actions(actions=None):
    """One action per animation - the one that stands for it in the list.

    The unbracketed action speaks for its animation where there is one, since
    that is the one an armature gets; otherwise the first to turn up. Worked
    out from the names as they stand, so a rename moves the row with it.
    """
    if actions is None:
        actions = bpy.data.actions
    standing = {}
    for action in actions:
        base = animation_base_name(action)
        if action.name == base or base not in standing:
            standing[base] = action
    return set(standing.values())


def action_channelbags(action):
    """Every fcurve holder in *action*, across Blender's slotted-action API."""
    if not hasattr(action, "slots"):
        return [action]
    from bpy_extras import anim_utils
    bags = []
    for slot in action.slots:
        bag = anim_utils.action_get_channelbag_for_slot(action, slot)
        if bag is not None:
            bags.append(bag)
    return bags


def animation_frame_range(actions):
    """The first and last keyed frame across *actions*, or ``None``.

    Read off the keys themselves, so switching to an animation gives the
    length it actually has rather than one left over from the last.
    """
    first = last = None
    for action in actions:
        for holder in action_channelbags(action):
            for fcurve in holder.fcurves:
                for point in fcurve.keyframe_points:
                    at = point.co[0]
                    first = at if first is None else min(first, at)
                    last = at if last is None else max(last, at)
    return None if first is None else (first, last)


# ── Taking an animation off, and putting it back ─────────────────────────────
_OWN_CHANNELS = {"location": "location", "rotation_quaternion": "rotation",
                 "rotation_euler": "rotation", "rotation_axis_angle": "rotation",
                 "scale": "scale"}


def keyed_channels(obj):
    """What *obj*'s animation keys: ``(own, {bone name: kinds})``.

    Each is a set of ``"location"``, ``"rotation"`` and ``"scale"`` - the
    object's own channels, and each pose bone's.
    """
    own, bones = set(), {}
    holder = channelbag(obj)
    if holder is None:
        return own, bones
    for fcurve in holder.fcurves:
        path = fcurve.data_path
        if path in _OWN_CHANNELS:
            own.add(_OWN_CHANNELS[path])
        elif path.startswith('pose.bones["') and '"].' in path:
            name = path[len('pose.bones["'):path.index('"].')]
            kind = _OWN_CHANNELS.get(path[path.index('"].') + 3:])
            if kind is not None:
                bones.setdefault(name, set()).add(kind)
    return own, bones


def _rest_pose_bone(pose_bone, kinds):
    if "location" in kinds:
        pose_bone.location = (0.0, 0.0, 0.0)
    if "rotation" in kinds:
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
    if "scale" in kinds:
        pose_bone.scale = (1.0, 1.0, 1.0)


def take_off_animation(obj):
    """Take *obj*'s animation off and put what it keyed back at rest.

    Returns what is needed to put it back, or ``None`` when there was none.
    Taken off, not muted: Blender plays an object's action at full strength
    whatever its influence says unless there is an NLA stack to blend into,
    so the only way to stop it is to take it away. What it keyed then holds
    whatever frame was showing, so that goes back to rest: a pose bone's rest
    is its bone, and an object's own location, rotation and scale rest at
    nothing - where it stands is its Delta Transform.
    """
    data = obj.animation_data
    if data is None or data.action is None:
        return None
    own, bones = keyed_channels(obj)
    kept = {"action": data.action,
            "slot": getattr(data, "action_slot", None),
            "own": (obj.location.copy(), obj.rotation_quaternion.copy(),
                    obj.rotation_euler.copy(), tuple(obj.rotation_axis_angle),
                    obj.scale.copy()),
            "bones": {}}
    pose = getattr(obj, "pose", None)
    for name in bones:
        pose_bone = pose.bones.get(name) if pose is not None else None
        if pose_bone is not None:
            kept["bones"][name] = pose_bone.matrix_basis.copy()
    data.action = None
    module("4ds.joint_space").rest_own_channels(
        obj, "location" in own, "rotation" in own, "scale" in own)
    for name, kinds in bones.items():
        pose_bone = pose.bones.get(name) if pose is not None else None
        if pose_bone is not None:
            _rest_pose_bone(pose_bone, kinds)
    return kept


def put_back_animation(obj, kept):
    """Undo :func:`take_off_animation`."""
    location, quaternion, euler, axis_angle, scale = kept["own"]
    obj.location, obj.rotation_quaternion = location, quaternion
    obj.rotation_euler, obj.rotation_axis_angle = euler, axis_angle
    obj.scale = scale
    for name, matrix in kept["bones"].items():
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is not None:
            pose_bone.matrix_basis = matrix
    data = obj.animation_data or obj.animation_data_create()
    data.action = kept["action"]
    if kept["slot"] is not None and hasattr(data, "action_slot"):
        data.action_slot = kept["slot"]


def assign_action(obj, action):
    """Put *action* on *obj*, picking the slot that belongs to it."""
    data = obj.animation_data or obj.animation_data_create()
    data.action = action
    if not hasattr(action, "slots"):
        return True
    for slot in action.slots:
        if obj.name in _slot_labels(slot):
            data.action_slot = slot
            return True
    if len(action.slots):
        data.action_slot = action.slots[0]
    return True


def derived_key_flags(obj, bone=None):
    """Which key kinds a frame carries, read off its curves and nothing else.

    The panel shows this rather than the word the file arrived with: keying a
    channel or clearing one changes the answer, and a number left over from
    the import would go on claiming the old one.
    """
    from .codec import (KEY_NOTE, KEY_POSITION, KEY_ROTATION,
                                   KEY_SCALE)
    holder = channelbag(obj)
    if holder is None:
        return 0
    prefix = f'pose.bones["{bone.name}"].' if bone is not None else ""
    owner = bone if bone is not None else obj
    # Rotation is read off whichever channel the owner's rotation mode keys,
    # not off the quaternion channel: in Euler mode that one is empty, and the
    # panel used to report no rotation on a frame that plainly spins.
    rotation_path, rotation_count, _rest = rotation_channel(owner)
    flags = 0
    for flag, path, count in ((KEY_ROTATION, rotation_path, rotation_count),
                              (KEY_POSITION, "location", 3),
                              (KEY_SCALE, "scale", 3)):
        if any(curve(holder, prefix + path, index) for index in range(count)):
            flags |= flag
    # Cues sit on the object itself, never on a joint.
    if bone is None and note_cues(holder):
        flags |= KEY_NOTE
    return flags


def eased_curves(obj, prefix="", owner=None):
    """Curves under *prefix* whose keys are not the game's interpolation.

    Returns ``{interpolation: count}``. Anything but linear on a movement
    channel is a shape the game cannot play: it reads the keys and walks
    between them in a straight line whatever the curve looks like here.
    """
    holder = channelbag(obj)
    if holder is None:
        return {}
    rotation_path, _count, _rest = rotation_channel(
        owner if owner is not None else obj)
    wanted = {prefix + name for _, name, _ in CHANNELS}
    wanted.add(prefix + rotation_path)
    found = {}
    for fcurve in holder.fcurves:
        if fcurve.data_path not in wanted:
            continue
        for point in fcurve.keyframe_points:
            if point.interpolation != GAME_INTERPOLATION:
                found[point.interpolation] = found.get(point.interpolation,
                                                       0) + 1
    return found
