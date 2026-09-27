"""Keeping a material's textures to the two formats the game reads, and fixing
the texture files its loader would misread.

Two jobs, both about the file behind a texture rather than anything stored in
Blender:

- **Refusing other formats.** The game reads BMP and TGA and nothing else, so a
  texture slot only takes an image whose file name ends in one of those. The
  slot's picker lists no others, and an image of any other kind that arrives
  anyway - opened into the slot, or assigned from a script - is taken straight
  back out with a word about why. An imported model naming one gets no texture
  there, and the import says so.
- **Repairing BMP and TGA files.** :mod:`.texture_repair` does the rewriting;
  this is the Blender side of it: finding the file, deciding whether the panel
  should offer the button, copying the original to a backup folder first, and
  reloading every image that shows the file afterwards.

Nothing is repaired without being asked, and nothing is overwritten without a
copy: each original goes into a ``texture_backups`` folder beside it before the
fixed file replaces it under the same name - the name the model refers to.
"""

import os
import shutil
import time

import bpy
from bpy.props import EnumProperty, StringProperty

from ..common import constants as C
from ..common import report as report_module
from . import texture_repair

#: The folder, beside a repaired file, that its original is copied into.
BACKUP_FOLDER = "texture_backups"

#: How long the animation box trusts its last look at the frame files before
#: looking again. Up to a hundred frames a channel is too many files to stat on
#: every redraw of the panel, and a file fixed or broken outside Blender is
#: still noticed within this long.
ANIMATION_RECHECK_SECONDS = 2.0

#: The material properties behind the three texture slots, by operator slot.
SLOT_PROPERTIES = {
    "DIFFUSE": "ls3d_diffuse_tex",
    "ALPHA": "ls3d_alpha_tex",
    "ENV": "ls3d_env_tex",
}
SLOT_ITEMS = (
    ("DIFFUSE", "Diffuse", "The diffuse texture"),
    ("ALPHA", "Alpha", "The alpha texture"),
    ("ENV", "Environment", "The environment texture"),
)


# ── Which formats a slot takes ────────────────────────────────────────────────
def texture_name_supported(name):
    """Whether a texture file *name* is one the game can read."""
    return os.path.splitext(name or "")[1].lower() in C.TEXTURE_EXTENSIONS


def image_supported(image):
    """Whether *image* can go in a texture slot. An empty slot always can."""
    if image is None:
        return True
    from .export_materials import texture_filename
    return texture_name_supported(texture_filename(image))


def refusal(name):
    """The sentence that goes with refusing a texture called *name*."""
    kind = os.path.splitext(name)[1].upper().lstrip(".")
    shown = f"a {kind} file" if kind else "a file with no extension"
    return (f"Texture '{name}' is {shown}. The game reads only BMP and TGA "
            f"textures, so it was not assigned.")


def slot_update(prop):
    """The update for a texture slot: take a refused image back out.

    The picker already lists only BMP and TGA images, so this is the other way
    in - an image opened straight into the slot, or set from a script.
    """
    def update(self, context):
        from .materials import rebuild_material_nodes

        image = getattr(self, prop)
        if image is not None and not image_supported(image):
            from .export_materials import texture_filename
            message = refusal(texture_filename(image))
            setattr(self, prop, None)          # runs this again, with nothing
            _tell(message)
            return
        rebuild_material_nodes(self)
    return update


def slot_poll(_owner, image):
    """Only images the game can read are offered in a texture slot."""
    return image_supported(image)


def _tell(message):
    """Say *message* in the console and, with a window to hand, a popup."""
    print(f"{report_module.console_prefix()} WARNING: {message}")
    if bpy.app.background:
        return
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return

    def draw(menu, _context):
        menu.layout.label(text=message)

    window_manager.popup_menu(draw, title="Texture not assigned", icon="ERROR")


# ── What needs fixing ─────────────────────────────────────────────────────────
#: The plan last made for each file, with the size and time it was made from,
#: so a redraw costs a stat rather than a read.
_file_plans = {}

#: The frame files last found wanting for each material, with when they were
#: looked at and what the material asked for then.
_animation_plans = {}


def forget():
    """Drop every kept answer, so the next question looks at the files again."""
    _file_plans.clear()
    _animation_plans.clear()


def plan_for_path(path):
    """A :class:`~.texture_repair.Plan` for *path*, or ``None``. Cached."""
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        _file_plans.pop(path, None)
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    kept = _file_plans.get(path)
    if kept is not None and kept[0] == stamp:
        return kept[1]
    plan = texture_repair.plan_repair(path)
    _file_plans[path] = (stamp, plan)
    return plan


def image_path(image):
    """The file on disk behind *image*, the one the export checks, or ``None``."""
    if image is None:
        return None
    from .export_materials import texture_folder, texture_path
    return texture_path(image, texture_folder())


def plan_for_image(image):
    """What the panel's button under a texture slot would fix, or ``None``."""
    if image is None or not image_supported(image):
        return None
    return plan_for_path(image_path(image))


def animation_frames(mat):
    """``[(channel, file name, path or None)]`` for every frame in range.

    The range is what each channel's Frames field says: the diffuse frames, the
    alpha frames when the alpha is animated alongside them, and the environment
    frames when it has two or more.
    """
    from . import ops_texanim
    from .codec.material import has_alpha_texture
    from .export_materials import texture_filename

    channels = []
    flags = mat.ls3d_material_flags & 0xFFFFFFFF
    if mat.ls3d_flag_diffuse_animated:
        channels.append(("Diffuse", mat.ls3d_diffuse_tex, mat.ls3d_anim_frames))
        if mat.ls3d_flag_alpha_animated and has_alpha_texture(flags):
            channels.append(("Alpha", mat.ls3d_alpha_tex, mat.ls3d_anim_frames))
    if mat.ls3d_env_anim_frames >= 2:
        channels.append(("Environment", mat.ls3d_env_tex,
                         mat.ls3d_env_anim_frames))

    frames = []
    for channel, image, count in channels:
        if image is None or not image_supported(image):
            continue
        first = texture_filename(image)
        found = image_path(image)
        beside = os.path.dirname(found) if found else ""
        for name in ops_texanim.frame_names(first, count):
            frames.append((channel, name, ops_texanim._find_file(name, beside)))
    return frames


def animation_plans(mat, fresh=False):
    """The frame files in range the Fix Frame Files button would rewrite."""
    signature = tuple((channel, name, path)
                      for channel, name, path in animation_frames(mat))
    kept = _animation_plans.get(mat.name)
    now = time.monotonic()
    if (not fresh and kept is not None and kept[1] == signature
            and now - kept[0] < ANIMATION_RECHECK_SECONDS):
        return kept[2]
    seen = set()
    plans = []
    for _channel, _name, path in signature:
        if path is None or path in seen:
            continue
        seen.add(path)
        plan = plan_for_path(path)
        if plan is not None:
            plans.append(plan)
    _animation_plans[mat.name] = (now, signature, plans)
    return plans


# ── Doing it ──────────────────────────────────────────────────────────────────
def backup(path):
    """Copy *path* into the backup folder beside it, and say where it went.

    A backup already holding the same bytes is left as it is; one holding
    different bytes is kept too, and this copy takes the next free number, so
    the very first original is never lost.
    """
    folder = os.path.join(os.path.dirname(path), BACKUP_FOLDER)
    os.makedirs(folder, exist_ok=True)
    stem, extension = os.path.splitext(os.path.basename(path))
    with open(path, "rb") as handle:
        original = handle.read()
    number = 1
    while True:
        name = (f"{stem}{extension}" if number == 1
                else f"{stem} ({number}){extension}")
        target = os.path.join(folder, name)
        if not os.path.exists(target):
            shutil.copy2(path, target)
            return target
        with open(target, "rb") as handle:
            if handle.read() == original:
                return target
        number += 1


def fix_files(plans):
    """Rewrite every planned file. Returns ``(fixed, failed)``.

    *fixed* is ``[(path, backup)]``; *failed* is ``[(path, reason)]``. A file is
    only replaced once its backup exists and the repaired bytes are written out
    in full, and it is replaced in one step, so a failure part way leaves the
    original where it was.
    """
    fixed = []
    failed = []
    for plan in plans:
        path = plan.path
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            repaired = texture_repair.repair(data, os.path.splitext(path)[1])
            saved = backup(path)
            scratch = f"{path}.fixing"
            with open(scratch, "wb") as handle:
                handle.write(repaired)
            os.replace(scratch, path)
            fixed.append((path, saved))
        except (OSError, texture_repair.RepairError) as exc:
            failed.append((path, str(exc)))
    forget()
    _reload_images([path for path, _backup in fixed])
    return fixed, failed


def _reload_images(paths):
    """Reload every image showing one of *paths*, so the panel shows the fix."""
    if not paths:
        return
    wanted = {os.path.normcase(os.path.abspath(path)) for path in paths}
    for image in bpy.data.images:
        if not image.filepath:
            continue
        try:
            where = os.path.normcase(os.path.abspath(
                bpy.path.abspath(image.filepath_from_user() or image.filepath)))
        except (RuntimeError, ValueError):
            continue
        if where in wanted:
            image.reload()
    from . import ops_texanim
    ops_texanim._frames_cache.clear()


def _report_outcome(operator, fixed, failed):
    for path, reason in failed:
        operator.report({"WARNING"}, f"Could not fix '{os.path.basename(path)}': "
                                     f"{reason}")
    if fixed:
        operator.report({"INFO"}, f"Fixed {len(fixed)} texture file(s). The "
                                  f"originals are in '{BACKUP_FOLDER}' beside "
                                  f"them.")
    return {"FINISHED"} if fixed else {"CANCELLED"}


def _draw_plan_list(layout, plans, limit=8):
    column = layout.column(align=True)
    column.label(text=f"Rewrites {len(plans)} file(s) so the game reads them:")
    for plan in plans[:limit]:
        column.label(text=f"{os.path.basename(plan.path)} - {plan.summary}",
                     icon="FILE_IMAGE")
    if len(plans) > limit:
        column.label(text=f"... and {len(plans) - limit} more", icon="BLANK1")
    column.separator()
    column.label(text=f"The originals are copied into '{BACKUP_FOLDER}' "
                      f"beside them first.", icon="INFO")


class LS3D_OT_TextureFix(bpy.types.Operator):
    """Rewrite this texture file so the game reads it correctly. The original
    is copied into a texture_backups folder beside it first"""

    bl_idname = "ls3d.texture_fix"
    bl_label = "Fix Texture File"
    bl_options = {"REGISTER"}

    material: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    slot: EnumProperty(items=SLOT_ITEMS, options={"HIDDEN", "SKIP_SAVE"})

    def _plans(self):
        mat = bpy.data.materials.get(self.material)
        if mat is None:
            return []
        plan = plan_for_image(getattr(mat, SLOT_PROPERTIES[self.slot]))
        return [plan] if plan is not None else []

    def invoke(self, context, _event):
        if not self._plans():
            self.report({"INFO"}, "This texture file needs no fixing.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        _draw_plan_list(self.layout, self._plans())

    def execute(self, _context):
        plans = self._plans()
        if not plans:
            self.report({"INFO"}, "This texture file needs no fixing.")
            return {"CANCELLED"}
        return _report_outcome(self, *fix_files(plans))


class LS3D_OT_TextureAnimFix(bpy.types.Operator):
    """Rewrite every animation frame file in the range the Frames fields set,
    so the game reads them correctly. Each original is copied into a
    texture_backups folder beside it first"""

    bl_idname = "ls3d.texture_anim_fix"
    bl_label = "Fix Frame Files"
    bl_options = {"REGISTER"}

    material: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def _plans(self):
        mat = bpy.data.materials.get(self.material)
        return animation_plans(mat, fresh=True) if mat is not None else []

    def invoke(self, context, _event):
        if not self._plans():
            self.report({"INFO"}, "No frame file in range needs fixing.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        _draw_plan_list(self.layout, self._plans())

    def execute(self, _context):
        plans = self._plans()
        if not plans:
            self.report({"INFO"}, "No frame file in range needs fixing.")
            return {"CANCELLED"}
        return _report_outcome(self, *fix_files(plans))


CLASSES = (LS3D_OT_TextureFix, LS3D_OT_TextureAnimFix)
