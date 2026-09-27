# SPDX-License-Identifier: GPL-3.0-or-later
"""Mafia Toolkit: the game's model, animation, movement and shadow formats.

Four formats, one package each, with what they share underneath::

    common/      binary.py    bounds-checked readers, buffered atomic writer
                 constants.py format enums, flag bits, limits, naming
                 convert.py   Mafia <-> Blender axis conversion, in one place
                 report.py    import/export result collection and popup

    4ds/         codec/       pure-Python: bytes <-> dataclasses, no bpy
                 importer.py  document -> Blender data (+ import_*.py)
                 exporter.py  Blender data -> document (+ export_*.py)
                 mesh.py      vertex splitting and bone-group ordering
                 materials.py preview shader graph for the material flags
                 validation.py export checks that do not need a file written
                 viewport.py  frame-type coloring
                 ops_*.py     operators that build 4DS objects set up right
                 ui.py        the model, material and morph panels

    5ds/         codec.py     the animation format itself
                 io.py        curves, actions and the scene's animation
                 ops.py       import, export, checking, event cues
                 ui.py        the 5DS Animation sidebar

    tck/         codec.py     the movement-track format
                 io.py        the empty an actor's travel rides on
                 ops.py       import and export

    6ds/         codec.py     the shadow format
                 io.py        shadow pieces as Blender meshes
                 ops.py       import and export

    packages.py  reaching the format packages, whose names start with a digit
    properties.py bpy property registration
    tools/       standalone corpus verifiers, and the Blender test suite

The codec layer round-trips what the game ships byte-for-byte: 3116 of 3118
models, 2726 animations, 283 movement tracks and all 37 shadows, the rest
being older format versions. ``python io_mafia_toolkit/tools/verify_corpus.py <game dir>`` checks the
models at any time, with verify_animations.py and verify_tracks.py beside it.
"""

import importlib
import os
import sys

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

# Installing a new version over a running Blender re-imports this file but
# leaves the submodules already in sys.modules alone, so a fresh __init__ can
# end up calling into last version's code - which surfaces as an
# AttributeError for a method the new source plainly has. Dropping our own
# submodules first forces them to be read from disk again.
_PACKAGE_PREFIX = __name__ + "."
for _stale in [_name for _name in sys.modules
               if _name.startswith(_PACKAGE_PREFIX)]:
    _module = sys.modules.get(_stale)
    if _module is not None and getattr(_module, "__file__", None):
        try:
            importlib.reload(_module)
        except Exception:                       # a half-loaded module: drop it
            sys.modules.pop(_stale, None)

from . import properties, ui
from .common import report as report_module
from .common.binary import FormatError
from .packages import module

# The three format packages are named after the formats themselves, and a name
# starting with a digit is not one Python's import statement can spell - so
# they come through the helper instead.
model = module("4ds")
joint_display = module("4ds.joint_display")
material_nodes = module("4ds.materials")
ops_create = module("4ds.ops_create")
gizmo_dummy = module("4ds.gizmo_dummy")
gizmo_influence = module("4ds.gizmo_influence")
gizmo_light = module("4ds.gizmo_light")
ops_lensflare = module("4ds.ops_lensflare")
ops_morph = module("4ds.ops_morph")
ops_texanim = module("4ds.ops_texanim")
ops_texfix = module("4ds.ops_texfix")
ops_target = module("4ds.ops_target")
viewport = module("4ds.viewport")
_exporter = module("4ds.exporter")
_importer = module("4ds.importer")
anim_io = module("5ds.io")
ops_anim = module("5ds.ops")
ops_track = module("tck.ops")
ops_shadow = module("6ds.ops")
ui_4ds = module("4ds.ui")
ui_5ds = module("5ds.ui")

ExportError = _exporter.ExportError
check_4ds = _exporter.check_4ds
export_4ds = _exporter.export_4ds
import_4ds = _importer.import_4ds
detect_initial_frame_type = properties.detect_initial_frame_type
update_viewport_display = viewport.update_viewport_display

# Blender reads bl_info WITHOUT importing the module - it parses the file and
# runs ast.literal_eval over the dict - so every value here has to be a plain
# literal. An f-string or a name makes the whole dict unreadable, and the
# add-on then installs without ever appearing in the add-on list.
bl_info = {
    "name": "Mafia Toolkit",
    "author": "Richard01_CZ",
    # Special thanks: Asa, Oravin, kirill_mapper, FlashX, sadness_smile,
    # huckleberrypie, jc
    # Numbers alone: Blender joins the tuple with dots whatever it holds, so
    # a label put here would ride along in the add-on list. There is no
    # warning key either - Blender draws an alert icon beside the name for
    # one, and there is nothing to warn about.
    "version": (1, 0, 0),
    # What it has been proven on. The suite has never run against anything
    # older, so nothing older is claimed.
    "blender": (5, 2, 0),
    "location": "File > Import/Export, Add > 4DS, and the 4DS Model, 4DS "
                "Morph and 5DS Animation sidebar tabs",
    "description": ("Mafia's models, animations, movement tracks and "
                    "shadows: .4ds, .5ds, .tck and .6ds"),
    "category": "Import-Export",
}

PACKAGE = __name__


def _redraw_panels(self, context):
    """Repaint the editors that read these preferences.

    Blender does not redraw the property panels when an addon preference
    changes, so without this a toggle here only appears to work after a
    restart.
    """
    window_manager = getattr(context, "window_manager", None)
    for window in getattr(window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type in {"PROPERTIES", "VIEW_3D"}:
                area.tag_redraw()


class LS3D_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = PACKAGE

    textures_path: StringProperty(
        name="Texture Folder",
        description="The game's 'maps' folder. Used by the importer to find textures",
        subtype="DIR_PATH", default="")

    show_raw_flags: BoolProperty(
        name="Show Raw Flag Values",
        description=("Show the whole flag value as a field above the "
                     "switches. Type into it in hex, with or without 0x in "
                     "front, and the switches follow. Every bit round-trips "
                     "either way"),
        default=True, update=_redraw_panels)

    show_reserved_flags: BoolProperty(
        name="Show Engine-Reserved Flags",
        description=("List every switch the game owns or never reads openly. "
                     "Off, the ones a file has on are still listed and the "
                     "rest wait in a folded section under each panel, so every "
                     "bit can be changed either way"),
        default=False, update=_redraw_panels)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Toolkit Configuration", icon="SETTINGS")
        layout.prop(self, "textures_path")
        layout.separator()
        layout.label(text="Flag Panels", icon="PROPERTIES")
        layout.prop(self, "show_raw_flags")
        layout.prop(self, "show_reserved_flags")
        note = layout.column()
        note.scale_y = 0.8
        note.label(text="What to export, and the weight corrections, are set "
                        "in the export dialog itself.", icon="INFO")


def get_preferences():
    addon = bpy.context.preferences.addons.get(PACKAGE)
    return addon.preferences if addon else None


class _ProgressBar:
    """Blender's progress bar and wait cursor, for one import or export.

    Wired to the Report so every stage advances it. Restores the cursor even
    when the run raises, which matters because a failed import otherwise leaves
    Blender stuck showing the busy pointer.
    """

    def __init__(self, context, report):
        self._window_manager = context.window_manager
        self._window = context.window
        self._report = report

    def __enter__(self):
        self._window_manager.progress_begin(0, 100)
        self._report.progress_fn = self._window_manager.progress_update
        if self._window is not None:
            self._window.cursor_set("WAIT")
        return self

    def __exit__(self, *exc):
        self._report.progress_fn = None
        self._window_manager.progress_end()
        if self._window is not None:
            self._window.cursor_set("DEFAULT")
        return False


class Import4DS(bpy.types.Operator, ImportHelper):
    """Import an LS3D .4ds model"""

    bl_idname = "import_scene.4ds"
    bl_label = "Import 4DS"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".4ds"
    filter_glob: StringProperty(default="*.4ds", options={"HIDDEN"})

    own_collection: BoolProperty(
        name="Collection Per Model",
        description=("Put the model's frames in a collection named after it, "
                     "so several can be open side by side and one can be "
                     "hidden without hunting through its frames"),
        default=True)

    load_animation: BoolProperty(
        name="Load Its Animation",
        description=("Also load the .5ds of the same name sitting beside the "
                     "model, which is where the game looks for a model's own "
                     "movement. Off brings in the model alone"),
        default=False)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="What To Build", icon="OUTLINER")
        box.prop(self, "own_collection")

        box = layout.box()
        box.label(text="Movement", icon="ANIM")
        box.prop(self, "load_animation")
        column = box.column()
        column.scale_y = 0.8
        column.label(text="A model that says it is animated keeps",
                     icon="BLANK1")
        column.label(text="its movement in the .5ds beside it.",
                     icon="BLANK1")

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Importing {filename}", "4DS")

        preferences = get_preferences()
        texture_dir = preferences.textures_path if preferences else None
        if texture_dir and not os.path.isdir(texture_dir):
            result.warn(f"Texture folder '{texture_dir}' does not exist; "
                        f"textures will not be loaded.",
                        fix="Set the texture folder in the addon preferences.")
            texture_dir = None
        elif not texture_dir:
            result.warn("No texture folder configured; textures will not be loaded.",
                        fix="Set the texture folder in the addon preferences.")

        with _ProgressBar(context, result):
            status = self._run(context, result, texture_dir, filename)
        result.show()
        return status

    def _load_animation(self, context, result):
        """Bring in the model's own animation, the way the game does.

        A 4DS ends with a count of animated objects, and the engine treats
        anything above zero as "there is a .5ds of this name beside me" - it
        swaps the extension and loads it onto stage 0, looping. So a model that
        says it is animated but arrives without its movement is worth saying
        something about: in game it would stand still.
        """
        declared = context.scene.ls3d_animated_object_count
        if declared <= 0:
            return

        beside = _sibling_animation(self.filepath)
        if beside is None:
            result.warn(
                f"This model says it has {declared} animated object(s), but "
                f"there is no animation beside it to move them.",
                fix=f"Put '{os.path.splitext(os.path.basename(self.filepath))[0]}"
                    f".5ds' next to the model - that is where the game looks - "
                    f"or set the animated object count to 0.")
            return
        if not self.load_animation:
            result.info(f"'{os.path.basename(beside)}' sits beside this model "
                        f"and holds its movement; tick Load Its Animation on "
                        f"the import to bring it in too.")
            return

        result.stage("Animation")
        result.span(100, 100)
        with result.as_format("5DS"):
            matched = ops_anim.load_animation(beside, context, result)
        if matched is None:
            result.warn(f"'{os.path.basename(beside)}' could not be read, so "
                        f"the model has no movement.")
        else:
            result.info(f"Loaded '{os.path.basename(beside)}' onto the model.")

    def _run(self, context, result, texture_dir, filename):
        try:
            import_4ds(self.filepath, result, texture_dir,
                       own_collection=self.own_collection)
        except (FormatError, OSError) as exc:
            result.error(str(exc))
            result.finish(f"Import FAILED: {filename}")
            return {"CANCELLED"}
        except Exception as exc:                       # unexpected: keep the trace
            import traceback
            traceback.print_exc()
            result.error(f"Unexpected error: {exc}")
            result.finish(f"Import FAILED: {filename}")
            return {"CANCELLED"}

        result.stage("Viewport")
        result.span(95, 100)
        for obj in context.scene.objects:
            if obj.ls3d_frame_type == "0":
                obj.ls3d_frame_type = detect_initial_frame_type(obj)
            update_viewport_display(obj)

        self._load_animation(context, result)

        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Import {status}: {filename}")
        return {"FINISHED"}


def _sibling_animation(model_path):
    """The .5ds the game would look for beside *model_path*, or ``None``.

    The engine takes the model's own path and swaps the extension - it does
    not search anywhere else - so the animation is always the file next door.
    The extension is matched either case, since the game compares it that way
    and the files on disk are not consistent about it.
    """
    stem = os.path.splitext(model_path)[0]
    for extension in (".5ds", ".5DS", ".5Ds", ".5dS"):
        candidate = stem + extension
        if os.path.isfile(candidate):
            return candidate
    return None


def _warn_missing_animation(scene, model_path, result):
    """Say so when a model claims animated objects but has no .5ds beside it.

    The game opens that animation as part of loading the model, and when it is
    not there the whole model load reports failure - after the model itself has
    loaded and appeared. The mission only records a model's frame names after
    a load that succeeded, so every scene2.bin entry naming one of its frames
    is then silently ignored. Nothing in game points back at the cause.
    """
    declared = scene.ls3d_animated_object_count
    if declared <= 0 or not model_path:
        return False
    if _sibling_animation(model_path) is not None:
        return False
    stem = os.path.splitext(os.path.basename(model_path))[0]
    result.warn(
        f"The model says it has {declared} animated object(s), so the game "
        f"also opens '{stem}.5ds' from the same folder, and there is none. The "
        f"model still appears in game, but its load counts as failed, so the "
        f"mission cannot find any of its frames by name: scene2.bin entries "
        f"for them, such as a light's sectors or a frame switched off, do "
        f"nothing.",
        fix=f"Put '{stem}.5ds' next to the model (Write Its Animation does "
            f"that), or set Animated Objects to 0 under Model.")
    return True


class Export4DS(bpy.types.Operator, ExportHelper):
    """Export the scene as an LS3D .4ds model"""

    bl_idname = "export_scene.4ds"
    bl_label = "Export 4DS"
    filename_ext = ".4ds"
    filter_glob: StringProperty(default="*.4ds", options={"HIDDEN"})

    selection_only: BoolProperty(
        name="Selected Objects Only",
        description=("Export only the selected objects. Leave this off unless "
                     "you know the rest of the model is already in the file"),
        default=False)

    fix_multi_influences: BoolProperty(
        name="Fix >2 Joint Influences",
        description=("Reduce any vertex with more than two joint influences "
                     "to its two strongest and renormalize them. Off, such a "
                     "vertex is a hard error naming the joints involved"),
        default=False)

    fix_non_parent_child: BoolProperty(
        name="Fix Non-Parent-Child Weights",
        description=("Resolve a vertex weighted to two joints that are not a "
                     "direct parent-child pair by keeping the stronger one. "
                     "Off, it is a hard error"),
        default=False)

    fix_long_turns: BoolProperty(
        name="Fix Long Rotation Turns",
        description=ops_anim.FIX_LONG_TURNS_DESCRIPTION,
        default=False)

    write_animation: BoolProperty(
        name="Write Its Animation",
        description=("Also write the .5ds holding the model's animation, "
                     "under the same name as the model. Available when "
                     "something in the scene is animated. Off by default: "
                     "exporting a model is usually about the model, and this "
                     "would overwrite the animation sitting beside it"),
        default=False)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="What To Write", icon="RESTRICT_SELECT_OFF")
        box.prop(self, "selection_only")
        if self.selection_only:
            chosen = len(context.selected_objects)
            note = box.column()
            note.scale_y = 0.8
            note.label(text=f"{chosen} object(s) selected of "
                            f"{len(context.scene.objects)}.", icon="INFO")

        box = layout.box()
        box.label(text="Weight Corrections", icon="MOD_VERTEX_WEIGHT")
        box.prop(self, "fix_multi_influences")
        box.prop(self, "fix_non_parent_child")

        # The animation goes into a .5ds of its own, named after the model,
        # which is how the game finds the two together.
        animated = ops_anim.scene_has_animation(context.scene)
        box = layout.box()
        box.label(text="Animation", icon="ACTION")
        column = box.column()
        column.enabled = animated
        column.prop(self, "write_animation")
        fix = box.column()
        fix.enabled = animated and self.write_animation
        fix.prop(self, "fix_long_turns")
        note = box.column()
        note.scale_y = 0.8
        if animated:
            stem = os.path.splitext(os.path.basename(self.filepath))[0]
            note.label(text=f"Written as '{stem or 'model'}.5ds'.",
                       icon="BLANK1")
        else:
            note.label(text="Nothing in the scene is animated.",
                       icon="BLANK1")

        box = layout.box()
        box.label(text="Model", icon="SCENE_DATA")
        box.prop(context.scene, "ls3d_animated_object_count")
        column = box.column()
        column.scale_y = 0.8
        column.label(text="Counted from the scene as you key it.",
                     icon="BLANK1")

    def execute(self, context):
        filename = os.path.basename(self.filepath)
        result = report_module.Report().begin(f"Exporting {filename}", "4DS")

        preferences = get_preferences()
        if self.selection_only:
            objects = list(context.selected_objects)
            result.info(f"Selected objects: {len(objects)} of "
                        f"{len(context.scene.objects)} in the scene")
        else:
            objects = list(context.scene.objects)
            result.info(f"Scene objects: {len(objects)}")

        with _ProgressBar(context, result):
            status = self._run(objects, result, preferences, filename)
        if status == {"FINISHED"}:
            self._write_animation(context, result)
        result.show()
        return status

    def _write_animation(self, context, result):
        """Write the model's animation beside it, under the model's name."""
        if not self.write_animation:
            return
        if not ops_anim.scene_has_animation(context.scene):
            return
        beside = os.path.splitext(self.filepath)[0] + ".5ds"
        # The same objects the model was written from: with Selected Objects
        # Only, an animation of the whole scene beside a model of part of it
        # would drive frames the model does not have.
        chosen = list(context.selected_objects) if self.selection_only else None
        with result.as_format("5DS"):
            written = ops_anim.write_animation_beside(
                context, beside, result, fix_long_turns=self.fix_long_turns,
                objects=chosen)
            if written:
                result.info(f"Wrote the animation to "
                            f"'{os.path.basename(beside)}'")

    def _run(self, objects, result, preferences, filename):
        try:
            export_4ds(self.filepath, objects, result,
                       fix_multi_influences=self.fix_multi_influences,
                       fix_non_parent_child=self.fix_non_parent_child)
        except ExportError as exc:
            result.info(str(exc))
            result.finish(f"Export FAILED: {filename} (nothing was written)")
            return {"CANCELLED"}
        except (FormatError, OSError) as exc:
            result.error(str(exc))
            result.finish(f"Export FAILED: {filename} (nothing was written)")
            return {"CANCELLED"}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result.error(f"Unexpected error: {exc}")
            result.finish(f"Export FAILED: {filename} (nothing was written)")
            return {"CANCELLED"}

        # Checked before the summary line. When this export is about to write
        # the animation beside the model, it will be there; that write reports
        # its own failure if it has one.
        scene = bpy.context.scene
        if not (self.write_animation and ops_anim.scene_has_animation(scene)):
            _warn_missing_animation(scene, self.filepath, result)

        status = "with warnings" if result.warning_count else "OK"
        result.finish(f"Export {status}: {filename}")
        return {"FINISHED"}


class LS3D_OT_CheckScene(bpy.types.Operator):
    """Run every export check on the scene without writing a file"""

    bl_idname = "ls3d.check_scene"
    bl_label = "Check 4DS Scene"
    bl_options = {"REGISTER"}

    def execute(self, context):
        result = report_module.Report().begin("Checking the scene", "4DS")
        objects = list(context.scene.objects)
        result.info(f"Scene objects: {len(objects)}")
        status = {"FINISHED"}
        try:
            with _ProgressBar(context, result):
                # Nothing is corrected here on purpose: a check reports what
                # is wrong, and the export dialog is where the weight fixes
                # are switched on.
                document = check_4ds(objects, result,
                                     fix_multi_influences=False,
                                     fix_non_parent_child=False)
        except ExportError as exc:
            result.info(str(exc))
            result.finish("Check FAILED - this scene would not export")
            result.show()
            return {"CANCELLED"}
        except Exception as exc:                # noqa: BLE001 - reported below
            import traceback
            traceback.print_exc()
            result.error(f"Unexpected error: {exc}")
            result.finish("Check FAILED")
            result.show()
            return {"CANCELLED"}

        result.info(f"{len(document.frames)} frame(s), "
                    f"{len(document.materials)} material(s)")
        # The check writes no file, so it looks beside the last one exported.
        declared = context.scene.ls3d_animated_object_count
        if declared > 0:
            last = context.window_manager.operator_properties_last(
                Export4DS.bl_idname)
            model_path = getattr(last, "filepath", "") if last else ""
            if model_path.lower().endswith(".4ds"):
                result.info(f"Animated Objects is {declared}: looking for its "
                            f"animation beside the last export, "
                            f"'{os.path.basename(model_path)}'.")
                _warn_missing_animation(context.scene, model_path, result)
            else:
                result.info(f"Animated Objects is {declared}, so the game will "
                            f"need a .5ds named like the model next to it. "
                            f"Export once and this check looks for it.")
        if result.warning_count:
            result.finish(f"Check OK with {result.warning_count} warning(s) - "
                          f"this scene would export")
        else:
            result.finish("Check OK - nothing would block the export")
        result.show()
        return status


def menu_func_import(self, context):
    self.layout.operator(Import4DS.bl_idname, text="4DS Mafia Model (.4ds)")


def menu_func_export(self, context):
    self.layout.operator(Export4DS.bl_idname, text="4DS Mafia Model (.4ds)")


#: Registered in order; PropertyGroups must come before anything pointing at them.
_CLASSES = (
    *properties.CLASSES,
    *report_module.CLASSES,
    *material_nodes.CLASSES,
    *ops_morph.CLASSES,
    *ops_texanim.CLASSES,
    *ops_texfix.CLASSES,
    *ops_target.CLASSES,
    *ops_lensflare.CLASSES,
    *ops_create.CLASSES,
    *gizmo_dummy.CLASSES,
    *gizmo_influence.CLASSES,
    *gizmo_light.CLASSES,
    *ops_anim.CLASSES,
    *ops_track.CLASSES,
    *ops_shadow.CLASSES,
    *ui_4ds.CLASSES,
    *ui_5ds.CLASSES,
    LS3D_AddonPreferences,
    LS3D_OT_CheckScene,
    Import4DS,
    Export4DS,
)


def _menu_entries():
    """Every menu this addon adds to, and what it adds."""
    return ((bpy.types.TOPBAR_MT_file_import, menu_func_import),
            (bpy.types.TOPBAR_MT_file_export, menu_func_export),
            (bpy.types.TOPBAR_MT_file_import, ops_anim.menu_func_import),
            (bpy.types.TOPBAR_MT_file_export, ops_anim.menu_func_export),
            (bpy.types.TOPBAR_MT_file_import, ops_track.menu_func_import_track),
            (bpy.types.TOPBAR_MT_file_export, ops_track.menu_func_export_track),
            (bpy.types.TOPBAR_MT_file_import, ops_shadow.menu_func_import),
            (bpy.types.TOPBAR_MT_file_export, ops_shadow.menu_func_export),
            (bpy.types.VIEW3D_MT_add, ops_create.menu_func_add))


def _append_once(menu, entry):
    """Add *entry* to *menu*, first taking off any copy already there.

    Registering twice - a reload, or an unregister that did not finish - would
    otherwise leave the same line in the menu twice over.
    """
    while entry in getattr(menu, "_dyn_ui_initialize", list)():
        menu.remove(entry)
    menu.append(entry)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    properties.register_properties()
    viewport.register_handlers()
    for menu, entry in _menu_entries():
        _append_once(menu, entry)
    anim_io.register_handlers()


def unregister():
    anim_io.unregister_handlers()
    # Stop any texture animation and take its timer down, or a reload leaves
    # the old callback running against materials this build no longer owns.
    ops_texanim.unregister_playback()
    for menu, entry in reversed(_menu_entries()):
        while entry in getattr(menu, "_dyn_ui_initialize", list)():
            menu.remove(entry)
    viewport.unregister_handlers()
    properties.unregister_properties()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
