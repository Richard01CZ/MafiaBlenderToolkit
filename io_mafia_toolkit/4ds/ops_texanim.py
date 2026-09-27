"""Playing a material's texture animation in the viewport.

The game steps an animated material through numbered frame files on a timer of
its own, measured in milliseconds and unrelated to the scene's timeline. So
this plays it the same way - on a Blender timer - rather than by keyframing
anything. Nothing here is animation data: no keys are inserted, no action is
touched, and a scene that was not animated before is not animated after.

What moves is the **image on the preview node**, never
``material.ls3d_diffuse_tex``. That property is what the export writes the file
name from, so leaving playback parked on frame 07 would write ``FIRE07.BMP``
where the model said ``FIRE00.BMP``. The property stays on the first frame
throughout; only the node under it changes.
"""

import os
import random
import time

import bpy

from ..common import constants as C
from .codec.material import (
    ANIM_COUNTER_DIGITS, ANIM_NAME_TAIL, ANIM_RANDOM,
)

#: How often the timer wakes. The game's own frame times run from 6 ms to
#: 180 ms; waking faster than the screen refreshes would buy nothing, and each
#: material works out its own frame from the clock rather than from ticks, so
#: this only sets how finely that is followed.
TICK_SECONDS = 1.0 / 30.0

#: Materials currently playing, by name, each with the moment it started -
#: already shifted back by however far it had run when it was last paused, so
#: elapsed time is always "now minus this". Keyed by name rather than by the
#: material so a deleted one cannot keep a dangling reference alive.
_playing = {}

#: How far a paused material had run, in milliseconds, so Play picks it up
#: where Pause left it rather than snapping to the beginning.
_paused = {}

#: Frame images already found, keyed by the first frame's file name and the
#: frame count. Loading the same fire twice over is wasted work. Only a
#: complete set is kept: a lookup that had to stand frames in for missing files
#: is a snapshot of a folder still being filled, and keeping that was what left
#: an animation showing frame 00 for the rest of the session.
_frames_cache = {}

#: Case-folded listings of the folders frames are looked for in, each with the
#: folder's modification time. Copying the rest of the frames in changes that,
#: so the listing is taken again and the frames are found - without this, the
#: only way to pick them up was to restart Blender.
_folder_index = {}


def _folder_entries(folder):
    """``{lowercase name: real name}`` for *folder*, re-read when it changes."""
    try:
        stamp = os.stat(folder).st_mtime_ns
    except OSError:
        _folder_index.pop(folder, None)
        return {}
    cached = _folder_index.get(folder)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        entries = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        entries = {}
    _folder_index[folder] = (stamp, entries)
    return entries


def frame_names(first, count):
    """Every file name an animation of *count* frames asks for.

    The game numbers frames by overwriting the two characters just before the
    extension with a count from 00 upward, so the name stored in the file is
    already frame 00 and anything ahead of those two digits stays put.
    """
    if not first or len(first) < ANIM_NAME_TAIL or count < 1:
        return []
    stem = first[:-ANIM_NAME_TAIL]
    tail = first[-ANIM_NAME_TAIL + ANIM_COUNTER_DIGITS:]
    return [f"{stem}{number:0{ANIM_COUNTER_DIGITS}d}{tail}"
            for number in range(min(count, C.MAX_ANIM_FRAMES))]


def _texture_folder():
    from .. import get_preferences
    prefs = get_preferences()
    folder = getattr(prefs, "textures_path", "") if prefs else ""
    return bpy.path.abspath(folder) if folder else ""


def _find_file(name, beside=""):
    """Where *name* lives: beside the first frame, or in the texture folder.

    Both folders are looked up through their cached listing, so hunting for
    twenty frames costs two directory reads rather than twenty - and the
    listing is case-folded, because Mafia's own names are upper case and the
    files on disk are not.
    """
    for folder in (beside, _texture_folder()):
        if not folder:
            continue
        entries = _folder_entries(folder)
        actual = entries.get(name.lower())
        if actual is not None:
            return os.path.join(folder, actual)
    return None


def load_frames(image, count):
    """The images an animation plays, starting from *image*. Never ``None``.

    A frame whose file is missing is left as the one before it, so a partly
    present set still plays rather than flashing to nothing.
    """
    if image is None or count < 2:
        return []
    first = os.path.basename(image.filepath_from_user() or image.filepath
                             or image.name)
    key = (first.lower(), count)
    found = _frames_cache.get(key)
    if found is not None:
        # A datablock the user removed since would be dead, and so is every one
        # of them after another file is opened; drop the lot and look again
        # rather than handing back a broken reference. Reaching a freed one
        # raises, and getattr's default covers a missing attribute rather than
        # a dead reference, so the question has to be asked inside a try.
        try:
            alive = all(frame.name is not None for frame in found)
        except ReferenceError:
            alive = False
        if alive:
            return found
        _frames_cache.pop(key, None)

    beside = os.path.dirname(image.filepath_from_user() or image.filepath or "")
    frames = []
    complete = True
    for name in frame_names(first, count):
        path = _find_file(name, beside)
        if path is None:
            frames.append(frames[-1] if frames else image)
            complete = False
            continue
        try:
            frames.append(bpy.data.images.load(path, check_existing=True))
        except RuntimeError:
            frames.append(frames[-1] if frames else image)
            complete = False
    if complete:
        _frames_cache[key] = frames
    return frames


def _channels(mat):
    """``[(node label, first image, frame count, period, loop start)]``.

    The alpha has no timing of its own: the game renumbers it alongside the
    diffuse frames, so it borrows the diffuse channel's clock entirely.
    """
    from .materials import NL_ALPHA_TEX, NL_DIFFUSE_TEX, NL_ENV_TEX
    from .codec.material import has_alpha_texture

    flags = mat.ls3d_material_flags & 0xFFFFFFFF
    out = []
    if (mat.ls3d_flag_diffuse_animated and mat.ls3d_anim_frames >= 2
            and mat.ls3d_anim_period > 0):
        out.append((NL_DIFFUSE_TEX, mat.ls3d_diffuse_tex, mat.ls3d_anim_frames,
                    mat.ls3d_anim_period, mat.ls3d_anim_loop_start))
        if mat.ls3d_flag_alpha_animated and has_alpha_texture(flags):
            out.append((NL_ALPHA_TEX, mat.ls3d_alpha_tex, mat.ls3d_anim_frames,
                        mat.ls3d_anim_period, mat.ls3d_anim_loop_start))
    if mat.ls3d_env_anim_frames >= 2 and mat.ls3d_env_anim_period > 0:
        out.append((NL_ENV_TEX, mat.ls3d_env_tex, mat.ls3d_env_anim_frames,
                    mat.ls3d_env_anim_period, mat.ls3d_env_anim_loop_start))
    return out


def can_play(mat):
    """Whether *mat* has an animation the viewport could show."""
    return bool(mat is not None and getattr(mat, "ls3d_material_flags", None)
                is not None and _channels(mat))


def frame_at(elapsed_ms, count, period, loop_start):
    """Which frame is on screen *elapsed_ms* into the animation.

    Wrapping follows the game: past the last frame it goes back to *loop_start*
    rather than to the beginning, so an opening run can play once and the rest
    repeat. A loop start of -1 asks for a frame picked at random each interval.
    """
    if count < 2 or period <= 0:
        return 0
    step = int(elapsed_ms // period)
    if loop_start == ANIM_RANDOM:
        # Seeded off the step so the same moment gives the same frame, which
        # keeps the viewport steady rather than flickering as it redraws.
        return random.Random(step).randrange(count)
    if step < count:
        return step
    start = loop_start if 0 <= loop_start < count else 0
    return start + (step - start) % max(count - start, 1)


def show_frame(mat, elapsed_ms):
    """Put the frame belonging to *elapsed_ms* onto the material's nodes."""
    from .materials import find_node

    if mat.node_tree is None:
        return
    nodes = mat.node_tree.nodes
    for label, image, count, period, loop_start in _channels(mat):
        node = find_node(nodes, label)
        if node is None:
            continue
        frames = load_frames(image, count)
        if not frames:
            continue
        node.image = frames[frame_at(elapsed_ms, len(frames), period,
                                     loop_start) % len(frames)]


def rest(mat):
    """Put every animated channel back to the texture the material carries.

    This is the state a material is in with no animation running, which is what
    the file holds and what the export writes.
    """
    from .materials import find_node

    if mat is None or mat.node_tree is None:
        return
    nodes = mat.node_tree.nodes
    for label, image, _count, _period, _loop in _channels(mat):
        node = find_node(nodes, label)
        if node is not None:
            node.image = image or None


def is_playing(mat):
    return mat is not None and mat.name in _playing


def is_paused(mat):
    return mat is not None and mat.name in _paused


def start(mat):
    """Begin playing *mat*, resuming where a pause left it."""
    if not can_play(mat):
        return False
    resume_ms = _paused.pop(mat.name, 0.0)
    _playing[mat.name] = time.monotonic() - resume_ms / 1000.0
    _ensure_timer()
    return True


def pause(mat):
    """Hold *mat* on the frame it is showing, ready to carry on from there."""
    started = _playing.pop(getattr(mat, "name", ""), None)
    if started is not None:
        _paused[mat.name] = (time.monotonic() - started) * 1000.0


def stop(mat, to_rest=True):
    """Stop playing *mat* and forget where it was, back to its first frame."""
    name = getattr(mat, "name", "")
    _playing.pop(name, None)
    _paused.pop(name, None)
    if to_rest:
        rest(mat)


def stop_all(to_rest=True):
    for name in list(_playing) + list(_paused):
        _playing.pop(name, None)
        _paused.pop(name, None)
        material = bpy.data.materials.get(name)
        if to_rest and material is not None:
            rest(material)


def _tick():
    """Advance every playing material. Returns when to wake again."""
    if not _playing:
        return None                     # nothing left to do; the timer ends
    now = time.monotonic()
    for name, started in list(_playing.items()):
        material = bpy.data.materials.get(name)
        if material is None or not can_play(material):
            _playing.pop(name, None)
            continue
        try:
            show_frame(material, (now - started) * 1000.0)
        except Exception:
            # A broken material must not take the timer down with it, or every
            # other animation in the scene stops too.
            _playing.pop(name, None)
    _redraw_viewports()
    return TICK_SECONDS


def _redraw_viewports():
    """Ask the 3D views to redraw, since a timer's change alone will not.

    Swapping the image on a node marks the material, but nothing asks the
    viewport to paint again - so without this the animation only moves when
    something else happens to cause a redraw.
    """
    manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(manager, "windows", ()) or ():
        screen = getattr(window, "screen", None)
        for area in getattr(screen, "areas", ()) or ():
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _ensure_timer():
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK_SECONDS)


def on_flag_toggled(mat):
    """Stop a material that can no longer animate. Never starts one.

    Called from the material sync. Playing is something you ask for with the
    Play button, not something an import decides for you - opening a model
    should not set every fire and water surface running before you have looked
    at it. Switching the animation off, though, has to stop whatever is
    already running and put the texture back where it started.
    """
    if mat is not None and not can_play(mat):
        stop(mat)


class LS3D_OT_TextureAnimPlay(bpy.types.Operator):
    """Play this material's texture animation in the viewport"""

    bl_idname = "ls3d.texture_anim_play"
    bl_label = "Play"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        mat = getattr(context, "material", None) or (
            context.object.active_material if context.object else None)
        return can_play(mat) and not is_playing(mat)

    def execute(self, context):
        mat = getattr(context, "material", None) or (
            context.object.active_material if context.object else None)
        if not start(mat):
            self.report({"WARNING"},
                        "Nothing to play: needs 2 frames and a frame time.")
            return {"CANCELLED"}
        return {"FINISHED"}


class LS3D_OT_TextureAnimPause(bpy.types.Operator):
    """Hold the animation on the frame it is showing"""

    bl_idname = "ls3d.texture_anim_pause"
    bl_label = "Pause"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        mat = getattr(context, "material", None) or (
            context.object.active_material if context.object else None)
        return is_playing(mat)

    def execute(self, context):
        mat = getattr(context, "material", None) or (
            context.object.active_material if context.object else None)
        pause(mat)
        return {"FINISHED"}


CLASSES = (LS3D_OT_TextureAnimPlay, LS3D_OT_TextureAnimPause)


def unregister_playback():
    """Stop everything and take the timer down, for an addon reload."""
    stop_all()
    _paused.clear()
    _frames_cache.clear()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
