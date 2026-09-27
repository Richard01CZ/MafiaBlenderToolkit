"""Moving a model's shadow in and out of Blender.

A .6ds sits beside the .4ds of the same name and holds the coarse mesh the
engine casts from - one piece per frame of the model, far simpler than the
geometry it stands in for.
"""

import os

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from ..common import describe
from ..common import report as report_module

#: One zero byte, spelled out so the escape survives every editing pass.
PAD = bytes(1)
from . import io as shadow_io
from .codec import (
    ShadowError, Shadow, ShadowGroup, read_shadow_file, validate_shadow,
    write_shadow_file,
)


def load_shadow(filepath, context, result, parent_to_frames=False):
    """Build a scene's worth of shadow pieces. Returns how many arrived."""
    try:
        shadow = read_shadow_file(filepath)
    except ShadowError as problem:
        result.error(str(problem))
        return None
    except OSError as problem:
        result.error(f"Could not read the shadow: {problem}")
        return None

    # The file holds no hierarchy of its own: each piece's vertices are local
    # to the frame of the model whose name it carries, which the engine looks
    # up and multiplies by. Hanging the pieces off those frames is therefore
    # what puts them where the game draws them - offered rather than done,
    # since it only makes sense with that model already in the scene.
    stem = os.path.splitext(os.path.basename(filepath))[0]
    collection = bpy.data.collections.new(stem)
    context.scene.collection.children.link(collection)

    built = 0
    homeless = []
    total = len(shadow.groups)
    for index, group in enumerate(shadow.groups, start=1):
        result.item("Piece", index, total, describe.shadow_line(
            group.name or "shadow piece", len(group.vertices),
            len(group.faces)))
        mesh = shadow_io.build_mesh(group, group.name or "shadow piece")
        piece = bpy.data.objects.new(group.name or "shadow piece", mesh)
        setattr(piece, shadow_io.SHADOW_FLAG, True)
        collection.objects.link(piece)
        if parent_to_frames and group.name:
            if not shadow_io.hang_from_frame(piece, context.scene,
                                             group.name):
                homeless.append(group.name)
        built += 1

    # The stamp cannot be worked out from the geometry and the game never
    # reads it, but it is what the file holds, so it is kept rather than
    # replaced with a fresh one on the way back out.
    context.scene.ls3d_shadow_timestamp = shadow.timestamp.hex()
    result.info(f"{built} piece(s), {shadow.vertex_count} vertices, "
                f"{shadow.index_count // 3} triangle(s)")
    if homeless:
        result.warn(
            f"{len(homeless)} piece(s) found no frame of their name to hang "
            f"from, starting with '{homeless[0]}', so they sit at the origin.",
            fix="Import the model this shadow belongs to first - each piece "
                "is placed by the frame whose name it carries.")
    elif not parent_to_frames:
        result.info("The pieces sit at the origin, which is where the file "
                    "puts them: each one's coordinates are local to the frame "
                    "it is named after.")
    return built


def _stamp(scene):
    """The stamp to write: the one that came in, or now for a new shadow."""
    kept = getattr(scene, "ls3d_shadow_timestamp", "")
    if kept:
        try:
            return bytes.fromhex(kept)[:8].rjust(8, PAD)
        except ValueError:
            pass
    from ..packages import module
    filetime = module("4ds.codec.document").current_filetime()
    return int(filetime).to_bytes(8, "little")


def build_shadow(scene, result, objects=None):
    """The scene's shadow pieces as a :class:`Shadow`, or ``None``."""
    pieces = shadow_io.shadow_pieces(scene, objects)
    if not pieces:
        result.error("Nothing in the scene is marked as a shadow piece.",
                     fix="Mark the coarse meshes with Use As Shadow Piece in "
                         "the 4DS panel, or import a .6ds to start from.")
        return None

    shadow = Shadow(timestamp=_stamp(scene))
    skipped = 0
    total = len(pieces)
    for index, obj in enumerate(pieces, start=1):
        vertices, faces, untriangulated = shadow_io.read_mesh(obj)
        skipped += untriangulated
        name = shadow_io.frame_name(obj)
        result.item("Piece", index, total,
                    describe.shadow_line(name, len(vertices), len(faces)))
        shadow.groups.append(
            ShadowGroup(name=name, vertices=vertices, faces=faces))
    if skipped:
        result.warn(f"{skipped} face(s) have more than three corners and were "
                    f"left out; a shadow holds triangles only.",
                    fix="Triangulate the shadow meshes.")
    return shadow


class Import6DS(bpy.types.Operator, ImportHelper):
    """Import an LS3D .6ds shadow"""

    bl_idname = "import_scene.6ds"
    bl_label = "Import 6DS"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".6ds"
    filter_glob: StringProperty(default="*.6ds", options={"HIDDEN"})

    parent_to_frames: BoolProperty(
        name="Hang On Model Frames",
        description=("Parent each piece to the frame of the model whose name "
                     "it carries, which is what puts it where the game draws "
                     "it. Off leaves every piece at the origin, which is "
                     "literally what the file holds. Needs the model open"),
        default=False)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Placing", icon="MOD_CAST")
        box.prop(self, "parent_to_frames")
        note = box.column()
        note.scale_y = 0.8
        if self.parent_to_frames:
            note.label(text="Import the model first, or the pieces")
            note.label(text="have nothing to hang from.")
        else:
            note.label(text="Every piece lands at the origin: its")
            note.label(text="coordinates are local to the frame")
            note.label(text="it is named after.")

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Importing {filename}", "6DS")
        built = load_shadow(self.filepath, context, result,
                            parent_to_frames=self.parent_to_frames)
        if built is None:
            result.finish(f"Import FAILED: {filename}")
            result.show()
            return {"CANCELLED"}
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Import {status}: {filename}")
        result.show()
        return {"FINISHED"}


class Export6DS(bpy.types.Operator, ExportHelper):
    """Export the scene's shadow pieces as an LS3D .6ds file"""

    bl_idname = "export_scene.6ds"
    bl_label = "Export 6DS"
    filename_ext = ".6ds"
    filter_glob: StringProperty(default="*.6ds", options={"HIDDEN"})

    selection_only: BoolProperty(
        name="Selected Objects Only",
        description=("Write the shadow pieces among the selection alone. Off "
                     "writes every piece in the scene"),
        default=False)

    @classmethod
    def poll(cls, context):
        return bool(shadow_io.shadow_pieces(context.scene))

    def chosen(self, context):
        """The objects this export may look at, or ``None`` for the scene."""
        return list(context.selected_objects) if self.selection_only else None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="What To Write", icon="RESTRICT_SELECT_OFF")
        box.prop(self, "selection_only")

        pieces = shadow_io.shadow_pieces(context.scene, self.chosen(context))
        box = layout.box()
        box.label(text="Shadow", icon="MOD_CAST")
        note = box.column()
        note.scale_y = 0.8
        if pieces:
            triangles = sum(len(obj.data.polygons) for obj in pieces)
            note.label(text=f"{len(pieces)} piece(s), about {triangles} "
                            f"triangle(s).", icon="BLANK1")
        else:
            note.label(text="Nothing here is marked as a shadow piece.",
                       icon="ERROR")

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Exporting {filename}", "6DS")

        chosen = self.chosen(context)
        if chosen is not None:
            result.info(f"Selected objects: {len(chosen)} of "
                        f"{len(context.scene.objects)} in the scene")
        shadow = build_shadow(context.scene, result, chosen)
        if shadow is None:
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        refused = []
        validate_shadow(shadow,
                        lambda message, fix=None: (refused.append(message),
                                                   result.error(message, fix)),
                        result.warn)
        if refused:
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        try:
            size = write_shadow_file(shadow, self.filepath)
        except OSError as problem:
            result.error(f"Could not write the file: {problem}")
            result.finish(f"Export FAILED: {filename}")
            result.show()
            return {"CANCELLED"}

        result.info(f"Wrote {size} bytes: {len(shadow.groups)} piece(s), "
                    f"{shadow.vertex_count} vertices, "
                    f"{shadow.index_count // 3} triangle(s)")
        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Export {status}: {filename}")
        result.show()
        return {"FINISHED"}


class LS3D_OT_MarkShadowPiece(bpy.types.Operator):
    """Use this mesh as part of the model's shadow, or stop using it"""

    bl_idname = "ls3d.mark_shadow_piece"
    bl_label = "Use As Shadow Piece"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "MESH"

    def execute(self, context):
        obj = context.object
        marked = not shadow_io.is_shadow_piece(obj)
        setattr(obj, shadow_io.SHADOW_FLAG, marked)
        self.report({"INFO"},
                    f"'{obj.name}' is {'now' if marked else 'no longer'} a "
                    f"shadow piece.")
        return {"FINISHED"}


def menu_func_import(self, context):
    self.layout.operator(Import6DS.bl_idname, text="6DS Mafia Shadow (.6ds)")


def menu_func_export(self, context):
    self.layout.operator(Export6DS.bl_idname, text="6DS Mafia Shadow (.6ds)")


CLASSES = (Import6DS, Export6DS, LS3D_OT_MarkShadowPiece)
