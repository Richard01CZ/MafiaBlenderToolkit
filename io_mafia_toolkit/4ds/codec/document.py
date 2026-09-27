"""Whole-file read and write for the 4DS container.

File layout::

    "4DS\\0"
    u16   version (29 for Mafia)
    u64   FILETIME timestamp
    u16   material count, then that many material blocks
    u16   frame count, then that many frame blocks
    u8    animated object count

Frame header, common to every frame type::

    u8    frame type
    if frame type == VISUAL:
        u8  visual type
        u8  render flags
        u8  render flags 2
    u16   parent frame id (1-based, 0 = root)
    3f    position
    3f    scale
    4f    rotation quaternion (w, x, y, z)
    u8    culling flags
    str   name
    str   user properties
    ...   type-specific payload

Sound (4) and Area (14) frames are the exception: the game reads only the type
byte and the parent id for them and nothing after, so that is all a file holds
for one::

    u8    frame type
    u16   parent frame id

There is no length field on a frame and no resynchronization marker, so an
unrecognized frame or visual type cannot be skipped — the reader raises instead
of guessing, because guessing turns one unknown frame into a file full of noise.
"""

from ...common.binary import BinaryReader, BinaryWriter, FormatError
from ...common.constants import (
    FRAME_DUMMY, FRAME_EMITOR, FRAME_JOINT, FRAME_LIGHT, FRAME_OCCLUDER,
    FRAME_TYPE_COUNT, VISUAL_TYPE_COUNT,
    PAYLOADLESS_FRAME_TYPES, FRAME_SECTOR, FRAME_TARGET, SKIPPED_FRAME_TYPES,
    FRAME_TYPE_NAMES, FRAME_VISUAL, GEOMETRY_VISUAL_TYPES, MORPH_VISUAL_TYPES,
    SKINNED_VISUAL_TYPES, SUPPORTED_VERSIONS, VERSION_NAMES,
    VISUAL_BILLBOARD,
    VISUAL_LENSFLARE, VISUAL_MIRROR, VISUAL_PROJECTOR, VISUAL_TYPE_NAMES,
)
from . import scene as scene_blocks
from . import visual as visual_blocks
from .material import read_material, write_material
from .types import Document, Frame

MAGIC = b"4DS\0"


class UnsupportedFeatureError(FormatError):
    """The file uses a frame or visual type this addon cannot parse."""


def _describe_frame(index, frame_type, visual_type):
    label = FRAME_TYPE_NAMES.get(frame_type, f"unknown type {frame_type}")
    if visual_type is not None:
        label += f" / {VISUAL_TYPE_NAMES.get(visual_type, f'unknown visual {visual_type}')}"
    return f"frame {index} ({label})"


# ── Reading ───────────────────────────────────────────────────────────────────
def read_frame(reader, index):
    frame = Frame()
    frame.frame_type = reader.u8()
    if not 0 < frame.frame_type < FRAME_TYPE_COUNT:
        raise UnsupportedFeatureError(
            f"frame {index} at offset {reader.pos - 1}: frame type "
            f"{frame.frame_type} does not exist. The game refuses to load a "
            f"file naming one, and nothing after it can be read.")

    if frame.frame_type in SKIPPED_FRAME_TYPES:
        # Three bytes and done, exactly as the game reads it. The frame is kept
        # in the list so every later frame keeps its id; the importer skips it.
        frame.parent_id = reader.u16()
        return frame

    if frame.frame_type == FRAME_VISUAL:
        frame.visual_type = reader.u8()
        if frame.visual_type >= VISUAL_TYPE_COUNT:
            raise UnsupportedFeatureError(
                f"frame {index} at offset {reader.pos - 1}: visual type "
                f"{frame.visual_type} does not exist. The game refuses to load "
                f"a file naming one, and nothing after it can be read.")
        frame.render_flags = reader.u8()
        frame.render_flags2 = reader.u8()

    frame.parent_id = reader.u16()
    frame.position = reader.vec3()
    frame.scale = reader.vec3()
    frame.rotation = reader.vec4()
    frame.cull_flags = reader.u8()
    frame.name = reader.string()
    frame.user_props = reader.string()

    where = _describe_frame(index, frame.frame_type, frame.visual_type)

    if frame.frame_type == FRAME_JOINT:
        frame.joint = scene_blocks.read_joint(reader)

    elif frame.frame_type == FRAME_VISUAL:
        visual_type = frame.visual_type
        if visual_type == VISUAL_LENSFLARE:
            frame.lens_flare = visual_blocks.read_lens_flare(reader)
        elif visual_type == VISUAL_MIRROR:
            frame.mirror = visual_blocks.read_mirror(reader)
        elif visual_type == VISUAL_PROJECTOR:
            frame.projector = visual_blocks.read_projector(reader)
        elif visual_type in GEOMETRY_VISUAL_TYPES:
            frame.geometry = visual_blocks.read_geometry(reader)
            if not frame.geometry.instance_id:
                # Skin and morph data describe the mesh, so an instance that
                # borrows another frame's mesh borrows these too.
                lod_count = len(frame.geometry.lods)
                if visual_type in SKINNED_VISUAL_TYPES:
                    frame.skin = visual_blocks.read_skin(reader, lod_count)
                if visual_type in MORPH_VISUAL_TYPES:
                    frame.morph = visual_blocks.read_morph(reader)
            if visual_type == VISUAL_BILLBOARD:
                # The billboard block is per-frame, not per-mesh, and is present
                # even when the frame is an instance: 'glow03' in
                # MISE16-LETISTE/scene.4ds is an instanced billboard whose five
                # payload bytes follow its instance id. Skipping them here
                # desynchronizes every frame after it.
                frame.billboard = visual_blocks.read_billboard(reader)
        else:
            raise UnsupportedFeatureError(
                f"{where} at offset {reader.pos}: this visual type is not "
                f"supported. Its payload size is unknown, so the rest of the "
                f"file cannot be read reliably."
            )

    elif frame.frame_type == FRAME_SECTOR:
        frame.sector = scene_blocks.read_sector(reader)
    elif frame.frame_type == FRAME_DUMMY:
        frame.dummy = scene_blocks.read_dummy(reader)
    elif frame.frame_type == FRAME_TARGET:
        frame.target = scene_blocks.read_target(reader)
    elif frame.frame_type == FRAME_OCCLUDER:
        frame.occluder = scene_blocks.read_occluder(reader)
    elif frame.frame_type == FRAME_EMITOR:
        # A particle emitter keeps the mesh each of its particles is drawn
        # with, in the same block a visual keeps its own geometry in.
        frame.geometry = visual_blocks.read_geometry(reader)
    elif frame.frame_type == FRAME_LIGHT:
        frame.light = scene_blocks.read_light(reader)
    elif frame.frame_type in PAYLOADLESS_FRAME_TYPES:
        pass                    # the header above is the whole frame
    else:
        raise UnsupportedFeatureError(
            f"{where} at offset {reader.pos}: this frame type is not supported. "
            f"Its payload size is unknown, so the rest of the file cannot be "
            f"read reliably."
        )

    return frame


def read_document(data, name="<stream>"):
    reader = BinaryReader(data, name)

    if reader._take(4) != MAGIC:
        raise FormatError(f"{name}: not a 4DS file (bad magic bytes)")

    doc = Document()
    doc.version = reader.u16()
    if doc.version not in SUPPORTED_VERSIONS:
        # Naming the game a version belongs to saves the next person working out
        # whether they have a corrupt file or simply one from elsewhere.
        whose = VERSION_NAMES.get(doc.version)
        raise UnsupportedFeatureError(
            f"{name}: 4DS version {doc.version} is not supported"
            + (f" - that is {whose}'s own version" if whose else "")
            + f". This addon reads version {SUPPORTED_VERSIONS[0]}, "
              f"{VERSION_NAMES[SUPPORTED_VERSIONS[0]]}."
        )
    doc.timestamp = reader._unpack("<Q", 8)[0]

    for index in range(reader.u16()):
        try:
            doc.materials.append(read_material(reader))
        except FormatError as exc:
            raise FormatError(f"{name}: material {index + 1}: {exc}") from exc

    skipped = []
    for index in range(reader.u16()):
        try:
            frame = read_frame(reader, index + 1)
        except FormatError as exc:
            if not skipped:
                raise
            # The likeliest reason, and one the plain error cannot say: a tool
            # that wrote the usual frame header after a Sound or Area frame, which
            # the game never reads, so everything from there on is out of step.
            raise type(exc)(
                f"{exc}. Frame {skipped[0]} is a Sound or Area frame, which the "
                f"game reads as three bytes and nothing more; if whatever wrote "
                f"this file stored more for it, every frame after it is read "
                f"from the wrong place, in game too.") from exc
        if frame.frame_type in SKIPPED_FRAME_TYPES:
            skipped.append(index + 1)
        doc.frames.append(frame)

    if reader.remaining:
        doc.animated_object_count = reader.u8()
    if reader.remaining:
        doc.trailing = bytes(reader.buf[reader.pos:])

    return doc


def read_file(filepath):
    with open(filepath, "rb") as handle:
        return read_document(handle.read(), filepath)


# ── Writing ───────────────────────────────────────────────────────────────────
# A frame missing the payload its type calls for is a bug upstream, not a gap to
# paper over: writing an empty stand-in produces a file that loads and is wrong,
# which is worse than one that was never written. Every such case raises.
def write_frame(writer, frame, index):
    where = _describe_frame(index, frame.frame_type, frame.visual_type)

    writer.u8(frame.frame_type)
    if frame.frame_type == FRAME_VISUAL:
        if frame.visual_type is None:
            raise FormatError(
                f"{where}: a visual frame with no visual type. Nothing is "
                f"written for one - the writer does not stand in a value.")
        writer.u8(frame.visual_type)
        writer.u8(frame.render_flags)
        writer.u8(frame.render_flags2)

    writer.u16(frame.parent_id)
    writer.vec3(*frame.position)
    writer.vec3(*frame.scale)
    writer.vec4(*frame.rotation)
    writer.u8(frame.cull_flags)
    writer.string(frame.name, f"{where} name")
    writer.string(frame.user_props, f"{where} user properties")

    if frame.frame_type == FRAME_JOINT:
        scene_blocks.write_joint(writer, frame.joint)

    elif frame.frame_type == FRAME_VISUAL:
        visual_type = frame.visual_type
        if visual_type == VISUAL_LENSFLARE:
            visual_blocks.write_lens_flare(writer, frame.lens_flare)
        elif visual_type == VISUAL_MIRROR:
            visual_blocks.write_mirror(writer, frame.mirror, where)
        elif visual_type == VISUAL_PROJECTOR:
            if frame.projector is None:
                raise FormatError(f"{where}: a projector with no payload.")
            visual_blocks.write_projector(writer, frame.projector, where)
        elif visual_type in GEOMETRY_VISUAL_TYPES:
            visual_blocks.write_geometry(writer, frame.geometry, where)
            if not frame.geometry.instance_id:
                if visual_type in SKINNED_VISUAL_TYPES:
                    visual_blocks.write_skin(writer, frame.skin)
                if visual_type in MORPH_VISUAL_TYPES:
                    visual_blocks.write_morph(writer, frame.morph)
            if visual_type == VISUAL_BILLBOARD:
                # Written for instances too; see the matching note in read_frame.
                if frame.billboard is None:
                    raise FormatError(f"{where}: a billboard with no payload.")
                visual_blocks.write_billboard(writer, frame.billboard)
        else:
            raise UnsupportedFeatureError(
                f"{where}: this visual type cannot be written.")

    elif frame.frame_type == FRAME_SECTOR:
        scene_blocks.write_sector(writer, frame.sector, where)
    elif frame.frame_type == FRAME_DUMMY:
        scene_blocks.write_dummy(writer, frame.dummy)
    elif frame.frame_type == FRAME_TARGET:
        scene_blocks.write_target(writer, frame.target)
    elif frame.frame_type == FRAME_OCCLUDER:
        scene_blocks.write_occluder(writer, frame.occluder, where)
    elif frame.frame_type == FRAME_EMITOR:
        if frame.geometry is None:
            raise FormatError(f"{where}: an emitor with no particle mesh.")
        visual_blocks.write_geometry(writer, frame.geometry, where)
    elif frame.frame_type == FRAME_LIGHT:
        if frame.light is None:
            raise FormatError(f"{where}: a light with no settings.")
        scene_blocks.write_light(writer, frame.light)
    elif frame.frame_type in PAYLOADLESS_FRAME_TYPES:
        pass
    elif frame.frame_type in SKIPPED_FRAME_TYPES:
        raise UnsupportedFeatureError(
            f"{where}: Sound and Area frames are never written. The game builds "
            f"them itself and reads nothing of one from a file.")
    else:
        raise UnsupportedFeatureError(f"{where}: this frame type cannot be written.")


def write_document(doc, on_truncate=None):
    writer = BinaryWriter(on_truncate=on_truncate)
    writer.raw(MAGIC)
    writer.u16(doc.version)
    writer.raw(doc.timestamp.to_bytes(8, "little"))

    writer.u16(len(doc.materials))
    for index, material in enumerate(doc.materials):
        write_material(writer, material, f"material {index + 1}")

    writer.u16(len(doc.frames))
    for index, frame in enumerate(doc.frames):
        write_frame(writer, frame, index + 1)

    writer.u8(doc.animated_object_count)
    if doc.trailing:
        writer.raw(doc.trailing)
    return writer


def write_file(doc, filepath, on_truncate=None):
    return write_document(doc, on_truncate).save(filepath)


def current_filetime():
    """Windows FILETIME for right now, matching what the game tools write."""
    from datetime import datetime, timezone
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - epoch
    return int(delta.total_seconds() * 10_000_000)
