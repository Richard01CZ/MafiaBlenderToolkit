"""Pure-Python reader and writer for the LS3D ``.5ds`` animation format.

Like the rest of this package, nothing here imports ``bpy``: it converts
between raw bytes and the plain dataclasses below, so it can be exercised
against the game's own animations without launching Blender.

The file is a short header followed by one blob the game reads whole and then
walks by pointer. Inside the blob is a count of tracks, the length of the
animation, and a table of two offsets per track - one to its name, one to its
keys. Every offset is measured from the end of the header rather than the start
of the file, which is why :data:`HEADER_SIZE` turns up on both sides.

A track's keys are stored as separate arrays rather than interleaved: a count,
then that many frame numbers, then that many values. Rotations, positions,
scales and note events each get their own array, always in that order, and only
the ones the track's flag word says are present. A two-byte pad follows the
frame numbers when there is an even count of them, so the values that follow
start on a four-byte boundary.

Key times are frame numbers, and the game spaces frames 40 ms apart - twenty
five to the second.
"""

import struct
from dataclasses import dataclass, field
from typing import List, Tuple

from ..common.constants import STRING_ENCODING

Vec3 = Tuple[float, float, float]
Vec4 = Tuple[float, float, float, float]

#: What the first four bytes of an animation say.
FILE_MAGIC = b"5DS\x00"
#: The only version Mafia ships and the only one the game will load.
ANIMATION_VERSION = 20
#: Magic (4) + version (2) + timestamp (8) + blob length (4). Every offset
#: stored inside the blob is relative to the end of this.
HEADER_SIZE = 18

#: Which kinds of key a track holds. Bit 0 and everything above bit 5 are
#: unused - the game recognizes exactly these.
KEY_POSITION = 1 << 1       # three floats per key
KEY_ROTATION = 1 << 2       # four floats per key, (w, x, y, z)
KEY_SCALE = 1 << 3          # three floats per key
KEY_NOTE = 1 << 4           # one 16-bit number per key
KEY_NOTE_STRING = 1 << 5    # one string per key, stored as an offset
#: Either kind of note. A track may carry one or the other, never both.
KEY_NOTE_ANY = KEY_NOTE | KEY_NOTE_STRING
#: Every bit the game knows about.
KEY_FLAGS = (KEY_POSITION | KEY_ROTATION | KEY_SCALE | KEY_NOTE_ANY)

#: Label and description per bit, for the panel that lets these be set by hand.
KEY_FLAG_TABLE = (
    (KEY_ROTATION, "rotation", "Rotation",
     "The track turns its frame. Written first, whatever order the panel "
     "shows"),
    (KEY_POSITION, "position", "Position",
     "The track moves its frame"),
    (KEY_SCALE, "scale", "Scale",
     "The track resizes its frame. Rare - barely a hundred tracks in the "
     "whole game use it"),
    (KEY_NOTE, "note", "Events",
     "The track carries numbered cues rather than movement, for the game to "
     "act on. The game's own animations put these on a track called "
     "'notify'"),
    (KEY_NOTE_STRING, "note_string", "Named Events",
     "Cues carrying a word rather than a number. The game can read them, but "
     "nothing it ships uses them"),
)

#: The order a track's key arrays appear in, which the game relies on.
KEY_ORDER = (KEY_ROTATION, KEY_POSITION, KEY_SCALE, KEY_NOTE, KEY_NOTE_STRING)

#: How long one frame lasts in the game, in milliseconds.
FRAME_MILLISECONDS = 40
#: The frame rate that follows from it.
FRAMES_PER_SECOND = 1000 // FRAME_MILLISECONDS

#: The track name the game's own animations use for their event cues. It is a
#: track like any other, but it carries notes rather than movement.
NOTIFY_TRACK = "notify"

#: The file stores a spare word after an even number of note frames, where the
#: game's reader expects it *before* the values. The game therefore reads an
#: even-numbered note track one value out of step: it drops the first cue and
#: invents a zero at the end. Every other key kind puts the word where the
#: reader looks for it, so only notes are affected. Nothing can be written that
#: satisfies both, so the addon writes what the files hold and warns instead.
NOTE_PAD_IS_TRAILING = True

#: The most a count field can hold. Tracks, keys and the end frame are all
#: 16-bit.
MAX_U16 = 0xFFFF

#: What each event value means. The game watches the notify track go by and
#: acts on the value: some of these play a sound, some open a car door, some
#: only set a flag. Anything not listed is still written and read - a couple of
#: the game's own animations use values of their own, and PUMPAR marks where
#: fueling starts and stops with the characters '*' and '+'.
EVENT_NAMES = (
    (1, "Right Footstep", "The right foot lands. The game picks the sound "
                          "from whatever surface is underfoot"),
    (2, "Left Footstep", "The left foot lands"),
    (8, "Clothing", "Cloth rustle, at a fixed sound"),
    (22, "Open Car Door", "Swings open the door of the car being used"),
    (24, "Close Car Door", "Swings it shut again"),
    (26, "Melee Starts", "From here the actor's swing can connect"),
    (27, "Melee Ends", "And from here it cannot"),
    (30, "Weapon Target Added", "The weapon takes aim"),
    (31, "Weapon Target Dropped", "And lets go of it"),
    (32, "Weapon To Left Hand", "Moves the weapon into the left hand"),
    (33, "Weapon To Right Hand", "And into the right"),
    (36, "Change Weapon", "Swaps to the weapon being drawn"),
    (40, "Shoot", "The shot goes off"),
    (41, "Throw Grenade", "The grenade leaves the hand"),
    (42, "Fueling Starts", "Only in PUMPAR: the pump handle goes in"),
    (43, "Fueling Ends", "And comes back out"),
    (53, "Blend Finished", "The animation has finished blending in"),
    (73, "Weapon Effect I", "One of five effect slots the weapon code uses"),
    (74, "Weapon Effect J", "One of five effect slots the weapon code uses"),
    (75, "Weapon Effect K", "One of five effect slots the weapon code uses"),
    (76, "Weapon Effect L", "One of five effect slots the weapon code uses"),
    (77, "Weapon Effect M", "One of five effect slots the weapon code uses"),
    (80, "Door Sound", "Plays the door sound"),
    (82, "Body Sound", "Plays the body sound - a fall or a hit landing"),
    (86, "Reached Car Door", "Where the game jumps a car-entry animation to"),
    (95, "Police Shout 1", "One of three police shouts"),
    (96, "Police Shout 2", "One of three police shouts"),
    (97, "Police Shout 3", "One of three police shouts"),
    (98, "Scream", "The actor screams"),
)

#: The same, keyed by value.
EVENT_BY_VALUE = {value: label for value, label, _note in EVENT_NAMES}


def event_label(value):
    """A readable name for an event value, or the number when it has none."""
    known = EVENT_BY_VALUE.get(int(value))
    if known:
        return known
    printable = chr(int(value)) if 32 <= int(value) < 127 else None
    return f"Event {int(value)}" + (f" ('{printable}')" if printable else "")


class AnimationError(Exception):
    """The bytes are not an animation this addon can read."""


@dataclass
class Track:
    """One animated thing - a joint, an object, or the note track.

    Each list is ``(frame, value)`` pairs in frame order. Which lists are
    filled is what the file's flag word records, so the word is worked out on
    the way back out rather than kept here: a stored one could disagree with
    the keys beside it.
    """

    name: str = ""
    rotations: List[Tuple[int, Vec4]] = field(default_factory=list)
    positions: List[Tuple[int, Vec3]] = field(default_factory=list)
    scales: List[Tuple[int, Vec3]] = field(default_factory=list)
    notes: List[Tuple[int, int]] = field(default_factory=list)
    named_notes: List[Tuple[int, str]] = field(default_factory=list)
    #: Set to write a flag word other than the one the keys imply. Left None,
    #: the word is worked out from what the track actually holds, which is what
    #: every animation the game ships does.
    forced_flags: int = None

    @property
    def flags(self):
        """The file's flag word for this track."""
        if self.forced_flags is not None:
            return self.forced_flags
        word = 0
        for flag in KEY_ORDER:
            if self.keys_for(flag):
                word |= flag
        return word

    def keys_for(self, flag):
        """The key list a flag bit stands for."""
        return {KEY_ROTATION: self.rotations, KEY_POSITION: self.positions,
                KEY_SCALE: self.scales, KEY_NOTE: self.notes,
                KEY_NOTE_STRING: self.named_notes}[flag]

    @property
    def is_empty(self):
        return not any(self.keys_for(flag) for flag in KEY_ORDER)


@dataclass
class Animation:
    """A whole ``.5ds`` file."""

    version: int = ANIMATION_VERSION
    #: Windows FILETIME, as stored. The game compares it against a cached copy
    #: to decide whether its own cache is stale.
    timestamp: int = 0
    #: The last frame of the animation. Frame 0 is the first, so an animation
    #: that ends here is ``end_frame + 1`` frames long.
    end_frame: int = 0
    tracks: List[Track] = field(default_factory=list)

    @property
    def duration_seconds(self):
        return (self.end_frame * FRAME_MILLISECONDS) / 1000.0


# ── Reading ───────────────────────────────────────────────────────────────────
def _string_at(raw, offset):
    end = raw.find(b"\x00", offset)
    if end < 0:
        raise AnimationError(f"a track name at {offset} runs off the end")
    return raw[offset:end].decode(STRING_ENCODING, errors="replace")


def _keys(raw, offset, count, size, unpack):
    """Read one key array: frames, an alignment pad, then values."""
    frames = struct.unpack_from(f"<{count}H", raw, offset)
    offset += 2 * count
    if count % 2 == 0:
        offset += 2                  # pad so the values start 4-byte aligned
    values = [unpack(raw, offset + index * size) for index in range(count)]
    return list(zip(frames, values)), offset + size * count


def _value_reader(layout):
    """A reader for an array of plain values, *layout* being one value's."""
    size = struct.calcsize(layout)

    def read(raw, offset, keys):
        count = struct.unpack_from("<H", raw, offset)[0]
        read_keys, offset = _keys(
            raw, offset + 2, count, size,
            lambda buf, at: struct.unpack_from(layout, buf, at))
        keys.extend(read_keys)
        return offset
    return read


def _read_notes(raw, offset, keys):
    # Notes break the pattern the game's own reader expects: the file puts its
    # spare word *after* the values rather than before them. See
    # NOTE_PAD_IS_TRAILING for what that costs.
    count = struct.unpack_from("<H", raw, offset)[0]
    offset += 2
    frames = struct.unpack_from(f"<{count}H", raw, offset)
    offset += 2 * count
    values = struct.unpack_from(f"<{count}H", raw, offset)
    offset += 2 * count + 2
    keys.extend(zip(frames, values))
    return offset


def _read_named_notes(raw, offset, keys):
    count = struct.unpack_from("<H", raw, offset)[0]
    offset += 2
    frames = struct.unpack_from(f"<{count}H", raw, offset)
    offset += 2 * count
    if count % 2 == 0:
        offset += 2
    for index in range(count):
        at = struct.unpack_from("<I", raw, offset + 4 * index)[0]
        keys.append((frames[index], _string_at(raw, at + HEADER_SIZE)))
    return offset + 4 * count


def read_animation(raw, name="<animation>"):
    """Parse *raw* into an :class:`Animation`."""
    if len(raw) < HEADER_SIZE + 4:
        raise AnimationError(f"{name}: too short to be an animation")
    if raw[:4] != FILE_MAGIC:
        raise AnimationError(f"{name}: not a 5DS file (starts {raw[:4]!r})")
    version = struct.unpack_from("<H", raw, 4)[0]
    if version != ANIMATION_VERSION:
        raise AnimationError(
            f"{name}: 5DS version {version} is not supported (this addon "
            f"reads version {ANIMATION_VERSION})")

    timestamp = struct.unpack_from("<Q", raw, 6)[0]
    track_count, end_frame = struct.unpack_from("<HH", raw, HEADER_SIZE)

    animation = Animation(version=version, timestamp=timestamp,
                          end_frame=end_frame)
    table = HEADER_SIZE + 4
    links = [struct.unpack_from("<II", raw, table + 8 * index)
             for index in range(track_count)]

    for name_offset, data_offset in links:
        track = Track(name=_string_at(raw, name_offset + HEADER_SIZE))
        offset = data_offset + HEADER_SIZE
        flags = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        unknown = flags & ~KEY_FLAGS
        if unknown:
            raise AnimationError(
                f"{name}: track '{track.name}' has unknown key flags "
                f"0x{unknown:X}")

        # Only the arrays the flag word names are present, always in the one
        # order the game walks them.
        for flag in KEY_ORDER:
            if flags & flag:
                offset = KEY_ARRAYS[flag][0](raw, offset, track.keys_for(flag))

        # A flag set over an empty array is not something the game ships, but
        # it is legal and has to survive: keep the word as written.
        derived = Track(rotations=track.rotations, positions=track.positions,
                        scales=track.scales, notes=track.notes,
                        named_notes=track.named_notes).flags
        if flags != derived:
            track.forced_flags = flags

        animation.tracks.append(track)
    return animation


def read_animation_file(path):
    with open(path, "rb") as handle:
        return read_animation(handle.read(), name=str(path))


# ── Writing ───────────────────────────────────────────────────────────────────
def _write_keys(out, keys, pack):
    out += struct.pack("<H", len(keys))
    out += struct.pack(f"<{len(keys)}H", *(frame for frame, _ in keys))
    if len(keys) % 2 == 0:
        out += struct.pack("<H", 0)
    for _, value in keys:
        out += pack(value)


def _value_writer(layout):
    """A writer for an array of plain values, *layout* being one value's."""
    def write(out, keys, _string_patches):
        _write_keys(out, keys, lambda value: struct.pack(layout, *value))
    return write


def _write_notes(out, keys, _string_patches):
    out += struct.pack("<H", len(keys))
    out += struct.pack(f"<{len(keys)}H", *(frame for frame, _ in keys))
    out += struct.pack(f"<{len(keys)}H", *(value for _, value in keys))
    out += struct.pack("<H", 0)                 # trailing; see NOTE_PAD_IS_TRAILING


def _write_named_notes(out, keys, string_patches):
    count = len(keys)
    out += struct.pack("<H", count)
    out += struct.pack(f"<{count}H", *(frame for frame, _ in keys))
    if count % 2 == 0:
        out += struct.pack("<H", 0)
    for _, text in keys:
        string_patches.append((len(out), text))  # the offset is filled in later
        out += struct.pack("<I", 0)


#: How each kind of key array is read and written, by its flag bit. Only the
#: layout differs between kinds; which are present and in what order is the
#: flag word's and KEY_ORDER's business.
KEY_ARRAYS = {
    KEY_ROTATION: (_value_reader("<4f"), _value_writer("<4f")),
    KEY_POSITION: (_value_reader("<3f"), _value_writer("<3f")),
    KEY_SCALE: (_value_reader("<3f"), _value_writer("<3f")),
    KEY_NOTE: (_read_notes, _write_notes),
    KEY_NOTE_STRING: (_read_named_notes, _write_named_notes),
}


def write_animation(animation):
    """Serialize *animation* back to bytes."""
    out = bytearray()
    out += FILE_MAGIC
    out += struct.pack("<H", animation.version)
    out += struct.pack("<Q", animation.timestamp)
    size_at = len(out)
    out += struct.pack("<I", 0)              # blob length, patched at the end

    out += struct.pack("<HH", len(animation.tracks), animation.end_frame)
    table_at = len(out)
    out += b"\x00" * (8 * len(animation.tracks))

    # Keys first, then every name, which is the order the game's own files use.
    data_offsets = []
    string_patches = []            # (where the offset goes, the string)
    for track in animation.tracks:
        data_offsets.append(len(out) - HEADER_SIZE)
        flags = track.flags
        out += struct.pack("<I", flags)
        for flag in KEY_ORDER:
            if flags & flag:
                KEY_ARRAYS[flag][1](out, track.keys_for(flag), string_patches)

    name_offsets = []
    for track in animation.tracks:
        name_offsets.append(len(out) - HEADER_SIZE)
        out += track.name.encode(STRING_ENCODING, errors="replace") + b"\x00"

    for where, text in string_patches:
        struct.pack_into("<I", out, where, len(out) - HEADER_SIZE)
        out += text.encode(STRING_ENCODING, errors="replace") + b"\x00"

    for index in range(len(animation.tracks)):
        struct.pack_into("<II", out, table_at + 8 * index,
                         name_offsets[index], data_offsets[index])
    struct.pack_into("<I", out, size_at, len(out) - HEADER_SIZE)
    return bytes(out)


def write_animation_file(animation, path):
    """Write *animation* to *path*, leaving any existing file alone on failure."""
    data = write_animation(animation)
    temporary = f"{path}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    import os
    os.replace(temporary, path)
    return len(data)


# ── Rules ─────────────────────────────────────────────────────────────────────
def validate_animation(animation, fail, warn):
    """Check an animation against what the game's own reader will accept.

    Everything here comes from how the engine walks the blob rather than from
    taste: a rule is here because breaking it makes the game read something
    other than what was written.
    """
    if len(animation.tracks) > MAX_U16:
        fail(f"An animation holds up to {MAX_U16} tracks; this one has "
             f"{len(animation.tracks)}.")
    if not 0 <= animation.end_frame <= MAX_U16:
        fail(f"An animation can be at most {MAX_U16} frames long; this one "
             f"says {animation.end_frame}.")

    seen = {}
    for track in animation.tracks:
        _validate_track(animation, track, fail, warn)
        if track.name in seen:
            warn(f"Two tracks are both called '{track.name}'. The game looks "
                 f"tracks up by name and stops at the first, so the second "
                 f"never plays.",
                 fix="Give every track the name of a different frame.")
        seen[track.name] = True


def _validate_track(animation, track, fail, warn):
    flags = track.flags
    unknown = flags & ~KEY_FLAGS
    if unknown:
        fail(f"Track '{track.name}' sets flag bits 0x{unknown:X}, which the "
             f"game does not know.",
             fix="Clear them, or switch the track back to automatic flags.")

    if (track.notes or track.named_notes) and track.name != NOTIFY_TRACK:
        if track.name.lower() == NOTIFY_TRACK:
            fail(f"Track '{track.name}' carries event cues, but the name has "
                 f"to be spelled '{NOTIFY_TRACK}' exactly - every frame and "
                 f"track the game ships is lower case.",
                 fix=f"Rename it to '{NOTIFY_TRACK}'.")
        else:
            fail(f"Track '{track.name}' carries event cues, but the game only "
                 f"looks for them on a frame called '{NOTIFY_TRACK}' - cues "
                 f"anywhere else are never read.",
                 fix=f"Move the cues onto a dummy named '{NOTIFY_TRACK}'. "
                     f"Every one of the 642 animations the game ships puts "
                     f"them there, and 305 of its models carry that dummy "
                     f"for the purpose.")

    if flags & KEY_NOTE and flags & KEY_NOTE_STRING:
        fail(f"Track '{track.name}' is marked as carrying both numbered and "
             f"named events; the game reads one or the other.",
             fix="Leave only one of the two event flags on.")

    for flag, attribute, label, _ in KEY_FLAG_TABLE:
        keys = track.keys_for(flag)
        if keys and not flags & flag:
            warn(f"Track '{track.name}' has {len(keys)} {label.lower()} key(s) "
                 f"that its flags leave out, so they are not written.",
                 fix=f"Turn {label} on for this track, or put it back on "
                     f"automatic flags.")
        if flags & flag and not keys:
            warn(f"Track '{track.name}' is flagged for {label.lower()} but "
                 f"has no such keys, so an empty list is written.",
                 fix=f"Turn {label} off, or put the track back on automatic "
                     f"flags.")

        if len(keys) > MAX_U16:
            fail(f"Track '{track.name}' has {len(keys)} {label.lower()} keys; "
                 f"the format holds {MAX_U16}.")
        frames = [frame for frame, _ in keys]
        if any(frame > MAX_U16 or frame < 0 for frame in frames):
            fail(f"Track '{track.name}' has a {label.lower()} key outside "
                 f"frame 0..{MAX_U16}.")
        if frames != sorted(frames):
            warn(f"Track '{track.name}' has {label.lower()} keys out of order. "
                 f"The game walks them forwards and never sorts them.",
                 fix="Sort the keys by frame.")
        if len(set(frames)) != len(frames):
            warn(f"Track '{track.name}' has two {label.lower()} keys on the "
                 f"same frame; the game uses whichever it reaches first.")
        if frames and frames[-1] > animation.end_frame:
            warn(f"Track '{track.name}' has {label.lower()} keys past frame "
                 f"{animation.end_frame}, where the animation ends, so they "
                 f"never play.",
                 fix="Move the end of the animation past the last key.")

    if len(track.notes) % 2 == 0 and track.notes:
        warn(f"Track '{track.name}' has an even number of event cues "
             f"({len(track.notes)}). The game reads an even-numbered event "
             f"track one value out of step - it drops the first cue and adds "
             f"a zero at the end.",
             fix="Use an odd number of cues, which the game reads correctly.")
