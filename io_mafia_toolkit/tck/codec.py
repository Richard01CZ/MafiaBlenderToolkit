"""Pure-Python reader and writer for the LS3D ``.tck`` motion track.

A ``.tck`` is the world placement that goes with a ``.5ds``: where the actor
travels while the animation plays, kept out of the joint animation so the game
can move the character through the level. Every one the game ships sits beside
an animation of the same name, and 272 of the 283 run for exactly as long.

Unlike an animation it stores no key times. It is a flat array sampled every
``key_period`` milliseconds, which the game indexes by time and interpolates
between neighbors::

    index = time / key_period
    value = (keys[index + 1] * (time % key_period)
             + keys[index] * (key_period - time % key_period)) / key_period

The period is usually 20 ms - twice as dense as the animation's own 40 ms
frames - so a track does not map one key to one Blender frame.

Direction keys are unit vectors, not rotations: the actor faces along them.
Every one of them is horizontal and exactly unit length.
"""

import struct
from dataclasses import dataclass, field
from typing import List

Vec3 = tuple

#: The only version the game will load - it checks for this exactly and
#: refuses anything else, five of its own files included.
TRACK_VERSION = 4

#: Milliseconds between samples, where a file does not say otherwise. Most of
#: the game's tracks use this; the animation's own frames are 40 ms apart.
DEFAULT_KEY_PERIOD = 20


class TrackError(Exception):
    """The bytes are not a motion track this addon can read."""


@dataclass
class MotionTrack:
    """A whole ``.tck`` file."""

    version: int = TRACK_VERSION
    #: Anchor points the game uses when one animation hands over to the next.
    #: Both are positions despite the second one's name in the engine.
    base_position: Vec3 = (0.0, 0.0, 0.0)
    base_direction: Vec3 = (0.0, 0.0, 0.0)
    #: How long the track runs, in milliseconds.
    duration: int = 0
    #: Milliseconds between one sample and the next.
    key_period: int = DEFAULT_KEY_PERIOD
    positions: List[Vec3] = field(default_factory=list)
    #: Unit facing vectors, one per sample, or empty for a track that only
    #: moves. 147 of the game's tracks have them, 141 do not.
    directions: List[Vec3] = field(default_factory=list)
    #: Whatever follows the two key arrays. The game stops reading there and
    #: closes the file, so this is data it never looks at - 59 of its own
    #: tracks carry some. Kept so a file comes back out as it went in.
    trailing: bytes = b""

    @property
    def has_direction(self):
        return bool(self.directions)

    @property
    def expected_key_count(self):
        """How many samples the duration and period call for."""
        if self.key_period <= 0:
            return 0
        return self.duration // self.key_period + 1


def read_track(raw, name="<track>"):
    """Parse *raw* into a :class:`MotionTrack`."""
    if len(raw) < 40:
        raise TrackError(f"{name}: too short to be a motion track")
    version = struct.unpack_from("<i", raw, 0)[0]
    if version != TRACK_VERSION:
        raise TrackError(
            f"{name}: motion track version {version} is not supported (the "
            f"game reads version {TRACK_VERSION} and refuses the rest)")

    offset = 4
    base_position = struct.unpack_from("<3f", raw, offset)
    offset += 12
    base_direction = struct.unpack_from("<3f", raw, offset)
    offset += 12
    duration, key_period = struct.unpack_from("<Ii", raw, offset)
    offset += 8

    track = MotionTrack(version=version, base_position=base_position,
                        base_direction=base_direction, duration=duration,
                        key_period=key_period)

    for into in (track.positions, track.directions):
        count = struct.unpack_from("<i", raw, offset)[0]
        offset += 4
        if count < 0 or offset + 12 * count > len(raw):
            raise TrackError(f"{name}: a key count of {count} runs off the end")
        for index in range(count):
            into.append(struct.unpack_from("<3f", raw, offset + 12 * index))
        offset += 12 * count
    track.trailing = bytes(raw[offset:])
    return track


def read_track_file(path):
    with open(path, "rb") as handle:
        return read_track(handle.read(), name=str(path))


def write_track(track):
    """Serialize *track* back to bytes."""
    out = bytearray()
    out += struct.pack("<i", track.version)
    out += struct.pack("<3f", *track.base_position)
    out += struct.pack("<3f", *track.base_direction)
    out += struct.pack("<Ii", track.duration, track.key_period)
    for keys in (track.positions, track.directions):
        out += struct.pack("<i", len(keys))
        for value in keys:
            out += struct.pack("<3f", *value)
    out += track.trailing
    return bytes(out)


def write_track_file(track, path):
    """Write *track* to *path*, leaving any existing file alone on failure."""
    data = write_track(track)
    temporary = f"{path}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    import os
    os.replace(temporary, path)
    return len(data)


def validate_track(track, fail, warn):
    """Check a track against what the game's reader will accept."""
    if track.key_period <= 0:
        fail(f"A motion track samples every so many milliseconds, and this one "
             f"says {track.key_period}.",
             fix="Set the key period to a positive number - the game's own "
                 "tracks use 10, 20 or 40.")
        return
    if not track.positions:
        fail("A motion track with no position samples moves nothing.",
             fix="Key the motion, or leave the track out of the export.")
        return

    # Every facing the game ships is unit length. One that is not says the
    # rotation it came from was scaled, and it is written as it stands rather
    # than quietly rescaled.
    stretched = [index for index, facing in enumerate(track.directions)
                 if abs(sum(axis * axis for axis in facing) - 1.0) > 1e-3]
    if stretched:
        warn(f"{len(stretched)} facing sample(s) are not unit length, the "
             f"first at sample {stretched[0]}. They are written as they "
             f"stand.",
             fix="Clear the scale on the movement track so its rotation is a "
                 "plain turn.")

    expected = track.expected_key_count
    if len(track.positions) != expected:
        warn(f"This track holds {len(track.positions)} position sample(s) but "
             f"its length and period call for {expected}. The game reads it by "
             f"time, so the tail would never be reached.",
             fix="Match the duration to the samples, or the period to both.")
    if track.directions and len(track.directions) != len(track.positions):
        fail(f"A track carries the same number of facing samples as positions; "
             f"this one has {len(track.directions)} against "
             f"{len(track.positions)}.")
    for index, direction in enumerate(track.directions):
        length = sum(component * component for component in direction) ** 0.5
        if abs(length - 1.0) > 1e-3:
            warn(f"Facing sample {index} is {length:.4f} long where the game's "
                 f"own are all exactly 1. The actor would still face that way, "
                 f"but nothing in the game writes one like it.")
            break
