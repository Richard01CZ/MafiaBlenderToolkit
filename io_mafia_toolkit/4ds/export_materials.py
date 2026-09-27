"""Choosing which materials and LODs a model writes.

Split out of :mod:`.exporter` for size; these are methods of the exporter
and only make sense mixed into it.
"""

import os
import re

import bpy
from mathutils import Quaternion, Vector

from ..common import constants as C
from .codec.material import has_alpha_texture
from .codec.types import Material
from .ops_lensflare import glow_entries
from .export_util import (_DUPLICATE_SUFFIX_RE, _IDENTITY_TRANSFORM, _LOD_RE,
                          _MATERIALLESS_FRAME_TYPES, _PORTAL_RE, _RESERVED_RE,
                          _int_prop, texture_filename)
from .validation import (animated_name_problem, texture_problems,
                         validate_text_length)

_LOD_RE = re.compile(C.LOD_SUFFIX_PATTERN, re.IGNORECASE)
_PORTAL_RE = re.compile(C.PORTAL_SUFFIX_PATTERN, re.IGNORECASE)
_RESERVED_RE = re.compile(C.RESERVED_MATERIAL_PATTERN)
#: Frame types written as bare geometry - a hull or an occlusion volume, with
#: no face groups to carry a material id. A material left on one of these used
#: to enter the file's material table anyway, so the game loaded its texture
#: for something that can never draw it.
_MATERIALLESS_FRAME_TYPES = (C.FRAME_SECTOR, C.FRAME_OCCLUDER)

#: Blender's uniquifying suffix, e.g. "WOOD.BMP.001".
_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")

#: (location, rotation, scale) for a frame that carries no transform.
_IDENTITY_TRANSFORM = (Vector((0.0, 0.0, 0.0)),
                       Quaternion((1.0, 0.0, 0.0, 0.0)),
                       Vector((1.0, 1.0, 1.0)))


class ExportError(Exception):
    """Raised when the scene cannot be exported. Nothing has been written."""


def texture_filename(image):
    """The name to write for *image*.

    Prefers the real file name over the datablock name: Blender appends ``.001``
    when a name collides, and writing ``WOOD.BMP.001`` gives the engine a
    texture it cannot find.
    """
    if image is None:
        return ""
    if image.filepath:
        base = os.path.basename(image.filepath_from_user() or image.filepath)
        if base:
            return base.upper()
    return _DUPLICATE_SUFFIX_RE.sub("", image.name).upper()


def texture_folder():
    """Where the addon has been told the game's textures live, or ``None``."""
    from .. import get_preferences
    preferences = get_preferences()
    folder = getattr(preferences, "textures_path", "") if preferences else ""
    return folder if folder and os.path.isdir(folder) else None


def texture_path(image, folder):
    """Where *image* actually is on disk, or ``None`` if it cannot be found.

    The image's own path first, because that is the file the artist is looking
    at; then the texture folder by name, which is where the game will look.
    """
    if image is None:
        return None
    for candidate in (image.filepath_from_user(), image.filepath):
        if candidate:
            resolved = bpy.path.abspath(candidate)
            if os.path.isfile(resolved):
                return os.path.abspath(resolved)
    if folder:
        name = os.path.basename(image.filepath_from_user() or image.filepath
                                or image.name)
        beside = os.path.join(folder, name)
        if os.path.isfile(beside):
            return beside
        # The game's names are upper case and Windows does not care; a
        # case-sensitive filesystem does.
        lowered = name.lower()
        try:
            for entry in os.listdir(folder):
                if entry.lower() == lowered:
                    return os.path.join(folder, entry)
        except OSError:
            pass
    return None


class MaterialsMixin:
    """Part of :class:`~.exporter.Exporter`."""

    # ── LODs ──────────────────────────────────────────────────────────────────
    def _collect_lods(self):
        """Group ``<base>`` with its ``<base>_lodN`` siblings.

        Matching is anchored to the end of the name, so an object legitimately
        called ``car_lodge`` is not mistaken for a LOD and dropped.
        """
        by_name = {o.name: o for o in self.source_objects if o.type == "MESH"}
        self.lod_members = set()

        for obj in self.source_objects:
            if obj.type != "MESH" or _LOD_RE.search(obj.name):
                continue
            chain = [obj]
            for level in range(1, C.MAX_LOD_LEVELS):
                candidate = by_name.get(f"{obj.name}_lod{level}")
                if candidate is None:
                    # A gap ends the chain: silently renumbering later LODs
                    # would change which distance each one switches at.
                    missing = [f"{obj.name}_lod{n}"
                               for n in range(level + 1, C.MAX_LOD_LEVELS)
                               if f"{obj.name}_lod{n}" in by_name]
                    if missing:
                        self.report.warn(
                            f"'{obj.name}' has no _lod{level} but does have "
                            f"{', '.join(missing)}; those are not exported.",
                            fix=f"Rename the later LODs so they run without gaps "
                                f"from _lod1.")
                    break
                chain.append(candidate)
                self.lod_members.add(candidate)
            self.lods[obj] = chain

    # ── materials ─────────────────────────────────────────────────────────────
    def _collect_materials(self):
        """Gather every material any exported mesh uses, LODs included.

        Scanning only the base objects meant a material used solely on a LOD
        never entered the table, and every face using it silently exported with
        material id 0.
        """
        seen = set()
        collected = []

        def take(material):
            if material is not None and material.name not in seen:
                seen.add(material.name)
                collected.append(material)

        for material in bpy.data.materials:
            if _RESERVED_RE.match(material.name):
                take(material)

        meshes = set()
        for base, chain in self.lods.items():
            meshes.update(chain)
        meshes.update(o for o in self.source_objects if o.type == "MESH")

        # Sectors, portals and occluders are hulls. Whatever is in their
        # material slots is Blender's business, not the file's.
        ignored = sorted(obj.name for obj in meshes
                         if _int_prop(obj, "ls3d_frame_type")
                         in _MATERIALLESS_FRAME_TYPES
                         and any(slot.material for slot in obj.material_slots))
        meshes = {obj for obj in meshes
                  if _int_prop(obj, "ls3d_frame_type")
                  not in _MATERIALLESS_FRAME_TYPES}

        for obj in meshes:
            for slot in obj.material_slots:
                take(slot.material)
        for obj in self.source_objects:
            for _position, material in glow_entries(obj):
                take(material)
            # A projector has no mesh and so no material slot, but it names a
            # material all the same and the file stores it as an index into the
            # same table.
            if (_int_prop(obj, "ls3d_frame_type") == C.FRAME_VISUAL
                    and _int_prop(obj, "visual_type") == C.VISUAL_PROJECTOR):
                take(obj.ls3d_projector_material)

        def sort_key(material):
            match = _RESERVED_RE.match(material.name)
            if match:
                return (0, int(match.group(1)), "")
            return (1, 0, material.name)

        collected.sort(key=sort_key)
        self.materials = collected
        self.material_index = {m.name: i + 1 for i, m in enumerate(collected)}
        self.report.info(f"Materials: {len(collected)}")
        if ignored:
            listed = ", ".join(f"'{name}'" for name in ignored[:4])
            if len(ignored) > 4:
                listed += f" and {len(ignored) - 4} more"
            self.report.warn(
                f"{len(ignored)} sector, portal or occluder mesh(es) have a "
                f"material: {listed}. Those are hulls, so their materials were "
                f"left out of the file.",
                fix="Clear the material slots on sector, portal and occluder "
                    "meshes - the 4DS Material panel has a button for it.")

    def _material_id(self, material):
        if material is None:
            return 0
        return self.material_index.get(material.name, 0)

    def _build_material(self, mat):
        flags = mat.ls3d_material_flags & 0xFFFFFFFF
        material = Material(
            flags=flags,
            ambient=tuple(mat.ls3d_ambient_color),
            diffuse=tuple(mat.ls3d_diffuse_color),
            emission=tuple(mat.ls3d_emission_color),
            opacity=mat.ls3d_opacity,
            env_amount=mat.ls3d_env_amount,
            env_texture=texture_filename(mat.ls3d_env_tex),
            diffuse_texture=texture_filename(mat.ls3d_diffuse_tex),
            alpha_texture=texture_filename(mat.ls3d_alpha_tex),
            anim_frames=mat.ls3d_anim_frames,
            anim_period=mat.ls3d_anim_period,
            anim_loop_start=mat.ls3d_anim_loop_start,
            env_anim_frames=mat.ls3d_env_anim_frames,
            env_anim_period=mat.ls3d_env_anim_period,
            env_anim_loop_start=mat.ls3d_env_anim_loop_start,
        )
        for kind, name in (("Diffuse", material.diffuse_texture),
                           ("Alpha", material.alpha_texture),
                           ("Environment", material.env_texture)):
            validate_text_length(
                f"{kind} texture name on material '{mat.name}'", name,
                self.fail)

        # The frame counter that names the files is only two digits wide, so a
        # count past 100 asks the game for names it cannot spell - it writes
        # three digits over the two and the dot, and the frame fails to load.
        if material.anim_frames > C.MAX_ANIM_FRAMES:
            self.report.warn(
                f"Material '{mat.name}' asks for {material.anim_frames} "
                f"animation frames, but frames are numbered in two digits, so "
                f"the game can only reach {C.MAX_ANIM_FRAMES}.",
                fix=f"Use {C.MAX_ANIM_FRAMES} frames or fewer.")

        # The format only stores a texture name when its flag is set, so an
        # assigned image with the flag cleared would vanish without a word.
        if mat.ls3d_diffuse_tex and not (flags & C.MTL_DIFFUSE_ENABLE):
            self.report.warn(
                f"Material '{mat.name}' has a diffuse texture but 'Use Diffuse "
                f"Texture' is off; the texture name will not be written.",
                fix="Enable 'Use Diffuse Texture' on the material.")
        if mat.ls3d_alpha_tex and not has_alpha_texture(flags):
            if not flags & C.MTL_ALPHATEX:
                self.report.warn(
                    f"Material '{mat.name}' has an alpha texture but 'Use Alpha "
                    f"Texture' is off; the name will not be written.",
                    fix="Enable 'Use Alpha Texture' on the material.")
            else:
                # The game reads no separate alpha name for these: their
                # transparency already rides in the diffuse texture.
                vetoed = ", ".join(
                    label for bit, label in
                    ((C.MTL_ALPHA_ADDITIVE, "Alpha Additive"),
                     (C.MTL_ALPHA_COLORKEY, "Color Key"),
                     (C.MTL_ALPHA_IN_TEX, "Truecolor Texture"))
                    if flags & bit)
                self.report.warn(
                    f"Material '{mat.name}' has an alpha texture, but "
                    f"{vetoed} takes its transparency from the diffuse "
                    f"texture instead; the name will not be written.",
                    fix=f"Turn off {vetoed}, or clear the alpha texture.")
        env_blend = (flags & C.MTL_ENV_BLEND_MASK) >> C.MTL_ENV_BLEND_SHIFT
        if flags & C.MTL_ENV_ENABLE and env_blend not in C.ENV_BLENDS_DRAWN:
            self.report.warn(
                f"Material '{mat.name}' uses its environment texture with "
                f"blend {env_blend}, which the game has no blend for. It logs "
                f"an error and draws the reflection however the surface before "
                f"it was blended, so it can change from one frame to the next.",
                fix="Pick Blend by Amount, Blend by Alpha, Multiply, Multiply "
                    "Twice or Add, or turn off 'Use Environment Texture'.")
        if mat.ls3d_env_tex and not (flags & C.MTL_ENV_ENABLE):
            self.report.warn(
                f"Material '{mat.name}' has an environment texture but 'Use "
                f"Environment Texture' is off; it will not be written.",
                fix="Enable 'Use Environment Texture' on the material.")
        # What the game's own loader would make of each texture. None of this
        # can spoil the model file, so it is all reported rather than refused -
        # but a texture the loader cannot read takes the surface with it, and
        # nothing in Blender gives any sign of it.
        folder = texture_folder()
        for kind, image, frames in (
                ("Diffuse", mat.ls3d_diffuse_tex, material.anim_frames),
                ("Alpha", mat.ls3d_alpha_tex, 0),
                ("Environment", mat.ls3d_env_tex, material.env_anim_frames)):
            if image is None:
                continue
            label = f"{kind} texture on material '{mat.name}'"
            name = os.path.basename(image.filepath_from_user()
                                    or image.filepath or image.name).upper()
            trouble = animated_name_problem(name, frames)
            if trouble is not None:
                self.report.warn(f"{label} {trouble[0]}.", fix=trouble[1])

            path = texture_path(image, folder)
            if path is None:
                # An image with no file behind it was made inside Blender, and
                # the game looks its name up in its own texture folder, so
                # there is nothing here to be right or wrong about. One that
                # names a file we cannot open is a different matter.
                if image.filepath:
                    self.report.warn(
                        f"{label}: '{name}' is not where the material says it "
                        f"is, so there is nothing to check and nothing for the "
                        f"game to load.",
                        fix=f"Point the image at the real file, or put {name} "
                            f"in the addon's texture folder.")
                continue
            for problem, fix in texture_problems(path):
                self.report.warn(f"{label}: '{name}' {problem}.", fix=fix)

        return material
