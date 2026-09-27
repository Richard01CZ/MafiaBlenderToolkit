"""One-line descriptions of the things an import or an export walks past.

Both directions name the same item the same way, so a line from an import and
the line from re-exporting it can be read side by side. Everything here is for
the console only - nothing reads these back.
"""

from . import constants as C


def frame_kind(frame_type, visual_type=None):
    """``"Visual/Billboard"``, ``"Dummy"``, or the raw number for an odd one."""
    name = C.FRAME_TYPE_NAMES.get(frame_type)
    if name is None:
        return f"type {frame_type}"
    if frame_type != C.FRAME_VISUAL or visual_type is None:
        return name
    sub = C.VISUAL_TYPE_NAMES.get(visual_type, f"type {visual_type}")
    return f"{name}/{sub}"


def material_line(name, flags, textures=()):
    """``'glass' WOOD.BMP 0x40808001`` - what it paints with, and its flags.

    *name* is left out when there is nothing worth saying: an imported material
    is called ``4ds_material_7`` and the textures identify it far better.

    The flag word earns its place either way. It is what decides whether the
    material is transparent, keyed, animated or reflective, and reading it off
    the console is how a material that draws wrong gets identified at all.
    """
    used = [text for text in textures if text]
    shown = ", ".join(used) if used else "no texture"
    lead = f"'{name}' " if name else ""
    return f"{lead}{shown} 0x{flags & 0xFFFFFFFF:08X}"


def track_line(track):
    """``'base' 24 rotation, 24 position`` for one animation track.

    Counted per kind rather than totaled, because which kinds a track carries
    is what its flag word records and what decides how the game plays it - a
    track with rotation only and one with rotation and position are different
    animations, and a single total hides that.
    """
    parts = []
    for label, keys in (("rotation", track.rotations),
                        ("position", track.positions),
                        ("scale", track.scales),
                        ("cue", track.notes),
                        ("named cue", track.named_notes)):
        if keys:
            parts.append(f"{len(keys)} {label}")
    return f"'{track.name}' " + (", ".join(parts) if parts else "no keys")


def shadow_line(name, vertices, faces):
    """``'l_foot' 24 vertices, 12 triangle(s)`` for one shadow piece."""
    return f"'{name}' {vertices} vertices, {faces} triangle(s)"


def movement_line(track, duration=None):
    """What a movement track holds: how far it travels and for how long."""
    span = track.duration if duration is None else duration
    line = (f"{len(track.positions)} sample(s) every {track.key_period} ms"
            f" over {span} ms")
    return line + (", with facing" if track.has_direction else "")
