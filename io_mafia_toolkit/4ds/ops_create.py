"""Operators that build 4DS objects already set up correctly.

Several frame types carry conventions the exporter only checks after the fact:
a portal has to be named ``<sector>_portalN``, parented to its sector, flat and
sitting on the sector's wall; a mirror needs a view box in front of it. Getting
any of that wrong is an export error and nothing in the panels helped you get
it right, so these make the correct thing in one step.
"""

import math
import re

import bmesh
import bpy
from mathutils import Matrix, Vector

from ..common import constants as C
from ..common.report import Report
from . import influence, joint_display, joint_math, viewport
from .joint_space import (JointSpace, edit_rest, find_armature,
                          is_skinned_mesh, numbered_joints, share_group,
                          shares_of, skinned_mesh_of, skinned_meshes_of)
from .mesh import joint_parents

#: A share smaller than this is nothing at all; the same figure the weight
#: checks use for what counts as a whole weight.
WEIGHT_EPSILON = 1.0e-6

#: How much of the sector wall a new portal covers, as a fraction of the face
#: it is placed on. Short of the full face so the outline is visibly inside it.
PORTAL_FACE_FRACTION = 0.6
#: Fallback half-size for a portal when the sector has no usable face.
PORTAL_FALLBACK_SIZE = 0.5
#: How far in front of a mirror a new view box reaches, as a multiple of the
#: mirror's own half-width. The game's own boxes are several times the mirror.
VIEW_BOX_REACH = 2.0
#: Reach for a mirror with no geometry to measure.
VIEW_BOX_FALLBACK = 1.0

_PORTAL_RE = re.compile(C.PORTAL_SUFFIX_PATTERN, re.IGNORECASE)


def _link(obj, context):
    context.collection.objects.link(obj)
    return obj


def _seed_flags(obj, cull=C.DEFAULT_CULL_FLAGS,
                render1=C.DEFAULT_RENDER_FLAGS,
                render2=C.DEFAULT_RENDER_FLAGS2):
    """Give a new frame the flag bytes the game's own models of its type carry.

    Spelled out here rather than left to the property defaults so the menu
    stays the one place that says what each kind of frame starts as.
    """
    obj.cull_flags = cull
    obj.render_flags = render1
    obj.render_flags2 = render2


def _make_active(context, obj):
    for other in context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _next_portal_number(sector):
    """The lowest ``_portalN`` number not already used under *sector*."""
    used = set()
    for child in sector.children:
        match = _PORTAL_RE.search(child.name)
        if match:
            digits = re.search(r"(\d+)$", match.group(0))
            if digits:
                used.add(int(digits.group(1)))
    number = 1
    while number in used:
        number += 1
    return number


def _view_box_reach(mirror):
    """Half-size for a view box that comfortably covers *mirror*.

    Measured across the mirror's own surface - its local X and Z - since local
    Y is the direction it reflects along and has no width to speak of.
    """
    if mirror.type != "MESH" or not mirror.data.vertices:
        return VIEW_BOX_FALLBACK
    coords = [v.co for v in mirror.data.vertices]
    width = max(max(c[axis] for c in coords) - min(c[axis] for c in coords)
                for axis in (0, 2))
    return max(width * 0.5 * VIEW_BOX_REACH, VIEW_BOX_FALLBACK)


def _largest_face(obj):
    """``(center, normal, size)`` of the object's biggest face, in local space."""
    mesh = obj.data
    if not getattr(mesh, "polygons", None):
        return None
    polygon = max(mesh.polygons, key=lambda p: p.area)
    if polygon.area <= 0.0:
        return None
    corners = [mesh.vertices[i].co for i in polygon.vertices]
    spread = max((a - b).length for a in corners for b in corners)
    return polygon.center.copy(), polygon.normal.copy(), spread


class LS3D_OT_AddSector(bpy.types.Operator):
    """Add a sector: a closed convex box with its normals facing inward"""

    bl_idname = "ls3d.add_sector"
    bl_label = "Sector"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        bpy.ops.mesh.primitive_cube_add(
            location=context.scene.cursor.location)
        sector = context.object
        sector.name = "sector"
        _seed_flags(sector, cull=C.DEFAULT_CULL_FLAGS_SECTOR)
        sector.ls3d_sector_flags1 = C.DEFAULT_SECTOR_FLAGS1
        sector.ls3d_sector_flags2 = C.DEFAULT_SECTOR_FLAGS2
        # Setting the type flips the normals inward for us.
        sector.ls3d_frame_type = str(C.FRAME_SECTOR)
        self.report({"INFO"}, "Sector added. Shape it, then add portals to it.")
        return {"FINISHED"}


class LS3D_OT_AddPortal(bpy.types.Operator):
    """Add a portal on the active sector's largest wall"""

    bl_idname = "ls3d.add_portal"
    bl_label = "Portal"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (obj is not None and obj.type == "MESH"
                and int(getattr(obj, "ls3d_frame_type", C.FRAME_VISUAL))
                == C.FRAME_SECTOR
                and not _PORTAL_RE.search(obj.name))

    def execute(self, context):
        sector = context.object
        name = f"{sector.name}_portal{_next_portal_number(sector)}"
        mesh = bpy.data.meshes.new(name)
        portal = _link(bpy.data.objects.new(name, mesh), context)
        portal.parent = sector
        portal.matrix_parent_inverse = Matrix.Identity(4)

        # On the wall from the start: the export warns about a portal more than
        # 0.1 m off the sector's surface, and every portal the game ships sits
        # on it.
        face = _largest_face(sector)
        if face is None:
            center = Vector((0.0, 0.0, 0.0))
            normal = Vector((0.0, 0.0, 1.0))
            half = PORTAL_FALLBACK_SIZE
        else:
            center, normal, spread = face
            half = max(spread * PORTAL_FACE_FRACTION * 0.5, 1e-3)

        rotation = normal.to_track_quat("Z", "Y").to_matrix()
        across, up = rotation.col[0] * half, rotation.col[1] * half
        corners = [center - across - up, center + across - up,
                   center + across + up, center - across + up]

        builder = bmesh.new()
        try:
            verts = [builder.verts.new(c) for c in corners]
            builder.faces.new(verts)
            builder.to_mesh(mesh)
        finally:
            builder.free()
        mesh.update()

        portal.ls3d_portal_flags = C.DEFAULT_PORTAL_FLAGS
        portal.ls3d_frame_type = str(C.FRAME_SECTOR)
        _make_active(context, portal)
        self.report({"INFO"}, f"'{name}' placed on the sector's largest wall.")
        return {"FINISHED"}


class LS3D_OT_AddOccluder(bpy.types.Operator):
    """Add an occluder: a closed convex box that hides what is behind it"""

    bl_idname = "ls3d.add_occluder"
    bl_label = "Occluder"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        bpy.ops.mesh.primitive_cube_add(
            location=context.scene.cursor.location)
        occluder = context.object
        occluder.name = "occluder"
        _seed_flags(occluder)
        occluder.ls3d_frame_type = str(C.FRAME_OCCLUDER)
        return {"FINISHED"}


class LS3D_OT_AddDummy(bpy.types.Operator):
    """Add a dummy: a named marker with a box the game uses for collision"""

    bl_idname = "ls3d.add_dummy"
    bl_label = "Dummy"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        dummy = _link(bpy.data.objects.new("dummy", None), context)
        dummy.location = context.scene.cursor.location
        dummy.empty_display_type = "CUBE"
        dummy.empty_display_size = 0.5
        _seed_flags(dummy)
        dummy.ls3d_frame_type = str(C.FRAME_DUMMY)
        dummy.bbox_min = (-0.5, -0.5, -0.5)
        dummy.bbox_max = (0.5, 0.5, 0.5)
        _make_active(context, dummy)
        return {"FINISHED"}


class LS3D_OT_AddTarget(bpy.types.Operator):
    """Add a target: an anchor other frames can be made to face"""

    bl_idname = "ls3d.add_target"
    bl_label = "Target"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target = _link(bpy.data.objects.new("target", None), context)
        target.location = context.scene.cursor.location
        target.empty_display_type = "PLAIN_AXES"
        target.empty_display_size = 0.25
        _seed_flags(target)
        target.ls3d_frame_type = str(C.FRAME_TARGET)
        _make_active(context, target)
        return {"FINISHED"}


class LS3D_OT_AddLensFlare(bpy.types.Operator):
    """Add a lens flare with one element ready for a material"""

    bl_idname = "ls3d.add_lens_flare"
    bl_label = "Lens Flare"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        flare = _link(bpy.data.objects.new("lensflare", None), context)
        flare.location = context.scene.cursor.location
        flare.empty_display_type = "SPHERE"
        flare.empty_display_size = viewport.LENS_FLARE_MARKER_SIZE
        _seed_flags(flare)
        flare.ls3d_frame_type = str(C.FRAME_VISUAL)
        flare.visual_type = str(C.VISUAL_LENSFLARE)
        entry = flare.ls3d_glows.add()
        entry.name = "Glow 1"
        _make_active(context, flare)
        self.report({"INFO"}, "Give the element a material - export needs one.")
        return {"FINISHED"}


#: The sun flare the game's daytime skies carry, taken off `denjasno1.4ds`
#: and shared by some twenty of its sky models. Each entry is the element's
#: offset along the flare axis and the texture it draws.
SUN_GLOW = ((0.0, "4BLACK.BMP"),
            (50.0, "2FLARE1.BMP"),
            (0.0, "4BLACK.BMP"))
SUN_FLARE = ((0.0, "4BLACK.BMP"),
             (-1.0, "2FLARE2.BMP"),
             (-0.6, "2FLARE5.BMP"),
             (0.3, "2FLARE6.BMP"),
             (-0.2, "2FLARE5.BMP"),
             (0.0, "2FLARE6.BMP"),
             (-0.5, "2FLARE4.BMP"),
             (-1.1, "2FLARE3.BMP"))

#: What the game's own flare materials carry: a mip-mapped diffuse texture, and
#: the environment repeat count of 1 every material starts with. The flare
#: sprites are left uncompressed; the black one is not.
_FLARE_FLAGS = (C.MTL_DIFFUSE_ENABLE | C.MTL_DIFFUSE_MIPMAP
                | C.MTL_ENV_TILE_DEFAULT | C.MTL_NO_COMPRESSION)
_BLACK_FLAGS = (C.MTL_DIFFUSE_ENABLE | C.MTL_DIFFUSE_MIPMAP
                | C.MTL_ENV_TILE_DEFAULT)
#: The gray the game's flare materials use as their diffuse color.
_FLARE_DIFFUSE = (0.498039, 0.498039, 0.498039)


def _flare_texture(name):
    """The image for a flare sprite, loaded if the texture folder has it.

    A texture that cannot be found still gets a datablock carrying its name,
    the way the import does, so the material references the right file and the
    name survives an export whether or not the pixels are to hand.
    """
    import os

    existing = bpy.data.images.get(name)
    if existing is not None:
        return existing

    from .. import get_preferences
    preferences = get_preferences()
    folder = getattr(preferences, "textures_path", "") if preferences else ""
    if folder and os.path.isdir(folder):
        for entry in os.listdir(folder):
            if entry.lower() == name.lower():
                try:
                    return bpy.data.images.load(os.path.join(folder, entry),
                                                check_existing=True)
                except RuntimeError:
                    break
    try:
        image = bpy.data.images.new(name, 1, 1)
    except RuntimeError:
        return None
    image.source = "FILE"
    image.filepath = os.path.join(folder, name) if folder else f"//{name}"
    return image


def flare_material(name):
    """One of the sun flare's materials, reused when it is already here."""
    from .materials import rebuild_material_nodes

    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    material = bpy.data.materials.new(name)
    material.ls3d_material_flags = (_BLACK_FLAGS if name == "4BLACK.BMP"
                                    else _FLARE_FLAGS)
    material.ls3d_diffuse_color = _FLARE_DIFFUSE
    material.ls3d_diffuse_tex = _flare_texture(name)
    rebuild_material_nodes(material)
    return material


def _build_flare(context, name, elements):
    """One lens-flare frame carrying *elements*, as (offset, texture)."""
    flare = _link(bpy.data.objects.new(name, None), context)
    flare.location = context.scene.cursor.location
    flare.empty_display_type = "SPHERE"
    flare.empty_display_size = viewport.LENS_FLARE_MARKER_SIZE
    _seed_flags(flare)
    flare.ls3d_frame_type = str(C.FRAME_VISUAL)
    flare.visual_type = str(C.VISUAL_LENSFLARE)
    for index, (offset, texture) in enumerate(elements):
        entry = flare.ls3d_glows.add()
        entry.name = f"Glow {index + 1}"
        entry.position = offset
        entry.material = flare_material(texture)
    return flare


class LS3D_OT_AddSunFlare(bpy.types.Operator):
    """Add the sun flare the game's daytime skies use, elements and all"""

    bl_idname = "ls3d.add_sun_flare"
    bl_label = "Sun Flare (preset)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        glow = _build_flare(context, "glow", SUN_GLOW)
        # Spelled as the game's own models spell it. Nothing found so far
        # matches a flare frame by name, but the cost of keeping the original
        # spelling is nothing and the cost of being wrong is a flare the game
        # ignores.
        flare = _build_flare(context, "lensflere", SUN_FLARE)
        _make_active(context, flare)
        missing = sorted({image.name for image in bpy.data.images
                          if image.name.endswith(".BMP") and not image.has_data
                          and image.name in
                          {t for _o, t in SUN_GLOW + SUN_FLARE}})
        if missing:
            self.report({"WARNING"},
                        f"{len(missing)} flare texture(s) were not found - set "
                        f"the Texture Folder in the addon preferences.")
        else:
            self.report({"INFO"},
                        f"'{glow.name}' with {len(SUN_GLOW)} element(s) and "
                        f"'{flare.name}' with {len(SUN_FLARE)}.")
        return {"FINISHED"}


class LS3D_OT_AddMirror(bpy.types.Operator):
    """Add a mirror plane facing local +Y, with the view box in front of it"""

    bl_idname = "ls3d.add_mirror"
    bl_label = "Mirror"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        bpy.ops.mesh.primitive_plane_add(
            location=context.scene.cursor.location)
        mirror = context.object
        mirror.name = "mirror"
        # Blender's plane lies in XY facing +Z; the game reflects along the
        # object's local +Y. The mesh is turned rather than the object so the
        # object's own rotation is left free to aim the finished mirror.
        mirror.data.transform(Matrix.Rotation(math.radians(-90.0), 4, "X"))
        mirror.data.update()
        _seed_flags(mirror, render2=C.DEFAULT_RENDER_FLAGS2_MIRROR)
        mirror.ls3d_frame_type = str(C.FRAME_VISUAL)
        mirror.visual_type = str(C.VISUAL_MIRROR)
        # Spelled out here as well as in the property default, so the menu
        # stays the one place that says what each kind of frame starts as.
        mirror.ls3d_mirror_range = C.DEFAULT_MIRROR_RANGE
        fit_view_box(mirror)
        _make_active(context, mirror)
        return {"FINISHED"}


class LS3D_OT_AddBillboard(bpy.types.Operator):
    """Add a billboard: an upright quad that turns to face the camera"""

    bl_idname = "ls3d.add_billboard"
    bl_label = "Billboard"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        bpy.ops.mesh.primitive_plane_add(
            location=context.scene.cursor.location)
        billboard = context.object
        billboard.name = "billboard"
        # Blender's plane lies flat; a billboard is a standing quad, so the
        # mesh is turned upright and the object's own rotation left free.
        billboard.data.transform(Matrix.Rotation(math.radians(-90.0), 4, "X"))
        billboard.data.update()
        _seed_flags(billboard, render2=C.DEFAULT_RENDER_FLAGS2_BILLBOARD)
        billboard.ls3d_frame_type = str(C.FRAME_VISUAL)
        billboard.visual_type = str(C.VISUAL_BILLBOARD)
        # Turning freely around the upright axis: 199 of the game's 293
        # billboards are set this way, more than all the others together.
        billboard.rot_mode = "1"
        billboard.rot_axis = "3"
        _make_active(context, billboard)
        return {"FINISHED"}


class LS3D_OT_AddProjector(bpy.types.Operator):
    """Add a projector that paints a material onto what it covers"""

    bl_idname = "ls3d.add_projector"
    bl_label = "Projector"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        projector = _link(bpy.data.objects.new("projector", None), context)
        projector.location = context.scene.cursor.location
        projector.empty_display_type = "ARROWS"
        projector.empty_display_size = viewport.PROJECTOR_MARKER_SIZE
        _seed_flags(projector, render2=C.DEFAULT_RENDER_FLAGS2_PROJECTOR)
        projector.ls3d_frame_type = str(C.FRAME_VISUAL)
        projector.visual_type = str(C.VISUAL_PROJECTOR)
        # A straight-on box, falling off with depth, drawn additively: what the
        # game's own headlights and railway lamps are, and the only combination
        # it builds for itself besides the constant alpha-blended one it uses
        # for tyre marks.
        projector.ls3d_projector_orthogonal = True
        projector.ls3d_projector_falloff = str(C.PROJECTOR_FALLOFF_LINEAR)
        projector.ls3d_projector_blend = "0"
        _make_active(context, projector)
        self.report({"INFO"},
                    "Give it a material with a diffuse texture - that is what "
                    "it paints. It projects along local +Y.")
        return {"FINISHED"}


class LS3D_OT_AddLight(bpy.types.Operator):
    """Add a light, as a spot pointing down at the floor"""

    bl_idname = "ls3d.add_light"
    bl_label = "Light"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        light = _link(bpy.data.objects.new("light", None), context)
        light.location = context.scene.cursor.location
        # The cone points along local +Y, the way the light shines - which is
        # what the frame's forward axis becomes once the axes are swapped.
        light.empty_display_type = C.LIGHT_EMPTY_DISPLAY
        light.empty_display_size = viewport.LIGHT_MARKER_SIZE
        _seed_flags(light)
        light.ls3d_frame_type = str(C.FRAME_LIGHT)
        # A spot, because that is the one kind where every field means
        # something and so the one worth starting from. Everything else is the
        # engine's own starting point, which is also what the properties
        # default to - they are spelled out here so the menu says what it makes.
        light.ls3d_light_type = str(C.LIGHT_SPOT)
        light.ls3d_light_mode = C.DEFAULT_LIGHT_MODE
        light.ls3d_light_power = C.DEFAULT_LIGHT_POWER
        light.ls3d_light_color = C.DEFAULT_LIGHT_COLOR
        light.ls3d_light_range_near = C.DEFAULT_LIGHT_RANGE_NEAR
        light.ls3d_light_range_far = C.DEFAULT_LIGHT_RANGE_FAR
        light.ls3d_light_cone_inner = C.DEFAULT_LIGHT_CONE_INNER
        light.ls3d_light_cone_outer = C.DEFAULT_LIGHT_CONE_OUTER
        # Pointing down, the way a lamp usually does. An unrotated frame shines
        # along +Y, which is sideways.
        light.rotation_euler = (math.radians(-90.0), 0.0, 0.0)
        _make_active(context, light)
        self.report({"INFO"},
                    "Shines along local +Y. Drag the ball on the end of the "
                    "beam to aim it, or use Aim At Target.")
        return {"FINISHED"}


#: The slots a projector's material can keep an image in, and what each one
#: is called where the panel shows it. A projector paints the picture the
#: material builds, so any of them can be the one worth fitting to.
PROJECTOR_IMAGE_SLOTS = (
    ("ls3d_diffuse_tex", "Diffuse"),
    ("ls3d_alpha_tex", "Transparency"),
    ("ls3d_env_tex", "Environment"),
)


def projector_images(projector):
    """``[(slot, label, image)]`` for every image a projector's material has."""
    material = getattr(projector, "ls3d_projector_material", None)
    if material is None:
        return []
    found = []
    for slot, label in PROJECTOR_IMAGE_SLOTS:
        image = getattr(material, slot, None)
        if image is not None and image.size[0] and image.size[1]:
            found.append((slot, label, image))
    return found


def _fit_items(self, context):
    """The images the active projector's material carries, to choose from."""
    obj = getattr(context, "object", None)
    items = []
    for slot, label, image in projector_images(obj) if obj else []:
        items.append((slot, f"{label}: {image.name}",
                      f"{image.size[0]} by {image.size[1]} pixels"))
    return items or [("NONE", "No image", "The material carries none")]


class LS3D_OT_FitProjectorToTexture(bpy.types.Operator):
    """Set how wide the projector's volume is from the proportions of one of its material's pictures, so the picture is painted without being stretched. The volume keeps the height it has and the width follows from it; the reach along the projection axis is left alone. Only the frame's own scale is set, the way dragging it would be"""

    bl_idname = "ls3d.fit_projector_to_texture"
    bl_label = "Fit Size To Texture"
    bl_options = {"REGISTER", "UNDO"}

    texture: bpy.props.EnumProperty(
        name="Picture", items=_fit_items,
        description="Which of the material's pictures to take the "
                    "proportions from")

    @classmethod
    def poll(cls, context):
        from .viewport import is_projector
        obj = getattr(context, "object", None)
        if not is_projector(obj):
            cls.poll_message_set("Select a projector.")
            return False
        if not getattr(obj, "ls3d_projector_material", None):
            cls.poll_message_set("This projector names no material, so there "
                                 "is no picture to measure.")
            return False
        if not projector_images(obj):
            cls.poll_message_set("This projector's material carries no "
                                 "picture to measure.")
            return False
        return True

    def invoke(self, context, _event):
        found = projector_images(context.object)
        if len(found) == 1:
            self.texture = found[0][0]
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        obj = context.object
        found = {slot: (label, image)
                 for slot, label, image in projector_images(obj)}
        chosen = found.get(self.texture)
        if chosen is None:
            self.report({"ERROR"},
                        "That picture is not on this projector's material any "
                        "more. Pick one that is.")
            return {"CANCELLED"}
        label, image = chosen
        wide, tall = image.size[0], image.size[1]

        # The picture goes across the volume: its width along the frame's own
        # X and its height along Z, which is the way the paint is sampled. So
        # the two have to stand in the picture's own proportions. The height
        # is kept and the width follows, because keeping the height is the one
        # that leaves a wall-mounted projector reaching as far down as it did.
        held = obj.scale.z
        if abs(held) < 1e-9:
            self.report({"ERROR"},
                        f"'{obj.name}' is scaled to nothing across its "
                        f"volume, so there is no height to measure the width "
                        f"against. Give it a size first, then fit it.")
            return {"CANCELLED"}
        was = obj.scale.x
        obj.scale.x = held * (wide / tall)
        context.view_layer.update()
        self.report({"INFO"},
                    f"'{obj.name}' is {wide} by {tall} across now, from "
                    f"{label.lower()} '{image.name}': its width went from "
                    f"{was:.4g} to {obj.scale.x:.4g}, keeping the height at "
                    f"{held:.4g}. The reach is untouched.")
        return {"FINISHED"}

def fit_view_box(mirror):
    """Set *mirror*'s view box to a cube standing in front of its surface."""
    reach = _view_box_reach(mirror)
    # In front of the surface, near face on it: the box is what the mirror is
    # allowed to see, and everything worth reflecting is on that side.
    mirror.ls3d_mirror_box_center = (0.0, reach, 0.0)
    mirror.ls3d_mirror_box_x = (reach, 0.0, 0.0)
    mirror.ls3d_mirror_box_y = (0.0, reach, 0.0)
    mirror.ls3d_mirror_box_z = (0.0, 0.0, reach)


class LS3D_OT_FitViewBox(bpy.types.Operator):
    """Set the active mirror's view box to a cube in front of its surface"""

    bl_idname = "ls3d.fit_view_box"
    bl_label = "Fit View Box"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and int(getattr(obj, "visual_type", -1)) == C.VISUAL_MIRROR

    def execute(self, context):
        fit_view_box(context.object)
        self.report({"INFO"}, "View box set in front of the mirror. Drag its "
                              "faces, or type its axes, to cover what should "
                              "be reflected.")
        return {"FINISHED"}


class LS3D_OT_AddJoint(bpy.types.Operator):
    """Add a joint, on the active armature or on a new one"""

    bl_idname = "ls3d.add_joint"
    bl_label = "Joint"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = context.object
        if armature is None or armature.type != "ARMATURE":
            armature = next((o for o in context.selected_objects
                             if o.type == "ARMATURE"), None)
        created = armature is None
        if created:
            data = bpy.data.armatures.new("Armature")
            # The armature object itself stays at the world origin and the
            # bone goes to the cursor: a model is written from its bones, and
            # a turn or a move on the armature object is not part of it.
            armature = _link(bpy.data.objects.new("Armature", data), context)

        previous_mode = armature.mode
        previous_active = context.view_layer.objects.active
        context.view_layer.objects.active = armature
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bones = armature.data.edit_bones
            parent = bones.active if bones.active in list(bones) else None
            bone = bones.new("joint")
            # Unturned against what it hangs from, as the game's own tool
            # made a joint, and as long as an imported one.
            if parent is not None:
                bone.head = parent.tail.copy()
                bone.tail = bone.head + ((parent.tail - parent.head).normalized()
                                         * joint_math.BONE_LENGTH)
                bone.roll = parent.roll
                bone.parent = parent
            else:
                start = (context.scene.cursor.location.copy() if created
                         else Vector((0.0, 0.0, 0.0)))
                bone.head = start
                bone.tail = start + Vector((0.0, joint_math.BONE_LENGTH, 0.0))
            name = bone.name
            bones.active = bone
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
            if previous_active is not None and not created:
                context.view_layer.objects.active = previous_active

        # The same shared empty the importer hands every joint: one object,
        # kept out of every collection so it is never written as a frame.
        joint_display.apply(armature, name)
        pose_bone = armature.pose.bones.get(name)
        if pose_bone is not None:
            pose_bone.cull_flags = C.DEFAULT_CULL_FLAGS_JOINT
        add_influence_box(context, armature, name)

        if previous_mode == "EDIT" and not created:
            context.view_layer.objects.active = armature
            bpy.ops.object.mode_set(mode="EDIT")
        self.report({"INFO"}, f"Joint '{name}' added"
                              + (" on a new armature." if created else "."))
        return {"FINISHED"}


def fit_influence_box(context, armature, bone_name):
    """Give *bone_name* a box fitted to where it bends, or ``False``.

    Sat on the joint and aimed at the child it bends toward, the way the game's
    own boxes are, and as wide as the mesh around the joint where there is a
    skinned mesh to measure. The last joint of a chain aims onward, away from
    its parent; a joint with neither child nor parent has nothing to aim at.
    """
    skin = skinned_mesh_of(armature, list(context.scene.objects))
    space = JointSpace(armature, skin, edit_rest(armature))
    child = influence.fitting_child(armature, bone_name)
    box = influence.fitted_box(
        space, armature, bone_name, child,
        mesh=skin.data if skin is not None else None,
        mesh_frame=space.mesh_placement(skin) if skin is not None else None)
    if box is None:
        return False
    influence.set_box(armature, bone_name, box)
    return True


def add_influence_box(context, armature, bone_name):
    """Give *bone_name* an influence box: fitted where it can be.

    A joint with a child gets a box fitted to the bend between them. One with
    nothing below it has nothing to aim a box at, and gets the box the game's
    own tool gave a joint nothing was linked to.
    """
    if not fit_influence_box(context, armature, bone_name):
        influence.set_file_box(armature, bone_name, C.DEFAULT_JOINT_BOX)
    return influence.joint_of(armature, bone_name)


class LS3D_OT_AddInfluenceBox(bpy.types.Operator):
    """Give the active joint an influence box: the box its skin weights are made from when it has none painted"""

    bl_idname = "ls3d.add_influence_box"
    bl_label = "Add Influence Box"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = context.object
        if armature is None or armature.type != "ARMATURE":
            return False
        bone = armature.data.bones.active
        return bone is not None and not influence.has_box(armature, bone.name)

    def execute(self, context):
        armature = context.object
        bone_name = armature.data.bones.active.name
        add_influence_box(context, armature, bone_name)
        self.report({"INFO"}, f"Joint '{bone_name}' has an influence box.")
        return {"FINISHED"}


class LS3D_OT_FitInfluenceBox(bpy.types.Operator):
    """Fit the active joint's box to where it bends: on the joint, aimed at the joint below it, as wide as the mesh around it"""

    bl_idname = "ls3d.fit_influence_box"
    bl_label = "Fit Influence Box"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = context.object
        return (armature is not None and armature.type == "ARMATURE"
                and armature.data.bones.active is not None)

    def execute(self, context):
        armature = context.object
        name = armature.data.bones.active.name
        if not fit_influence_box(context, armature, name):
            self.report({"WARNING"}, f"Nothing to fit '{name}' to: a box is "
                                     f"aimed down the chain, and it has "
                                     f"neither a joint below it nor above.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Joint '{name}' has its box fitted.")
        return {"FINISHED"}


class LS3D_OT_AddInfluenceBoxes(bpy.types.Operator):
    """Give every joint of the active armature that has no influence box the default one"""

    bl_idname = "ls3d.add_influence_boxes"
    bl_label = "Add Missing Influence Boxes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = context.object
        return armature is not None and armature.type == "ARMATURE"

    def execute(self, context):
        armature = context.object
        missing = [bone.name for bone in armature.data.bones
                   if not influence.has_box(armature, bone.name)]
        for bone_name in missing:
            add_influence_box(context, armature, bone_name)
        self.report({"INFO"}, f"{len(missing)} influence box(es) added."
                    if missing else "Every joint already has an influence box.")
        return {"FINISHED"}


class LS3D_OT_WeightsFromBoxes(bpy.types.Operator):
    """Paint the weights the influence boxes would make, so they can be seen and changed"""

    bl_idname = "ls3d.weights_from_boxes"
    bl_label = "Make Weights From Boxes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        from .joint_space import is_posed
        rigs = _box_weighted_rigs(context)
        if not rigs:
            return False
        # Weights on a skeleton that is still posed cannot look right, however
        # they are worked out: Blender's armature deform moves a vertex by the
        # step from where its joint rests to where it is posed, so the mesh is
        # pulled about by that step the moment anything is weighted. The boxes
        # are worked out from where the joints rest as well, so they are
        # measured in the wrong place on top of it. Both go away once the pose
        # is the rest, so it is said here rather than after the damage.
        posed = [armature.name for armature, _skin in rigs
                 if is_posed(armature)]
        if posed:
            cls.poll_message_set(
                "'%s' is posed, so weights made now would pull the mesh out "
                "of shape by however far each joint was moved. Press Set Rest "
                "From Pose first, above - or, if that is an animation rather "
                "than a skeleton being fitted, go to a frame where nothing is "
                "posed, or use Unload Animation." % posed[0])
            return False
        return True

    def execute(self, context):
        done, refused, idle = [], [], []
        for armature, skin in _box_weighted_rigs(context):
            space = JointSpace(armature, skin, edit_rest(armature))
            numbered = numbered_joints(space)
            parents = joint_parents(armature, numbered)
            ancestors = influence.ancestors_of(armature)
            for obj in skinned_meshes_of(armature, list(context.scene.objects)):
                mesh = obj.data
                if mesh is None or not mesh.vertices:
                    continue
                made, trouble, why = _paint_from_boxes(
                    obj, mesh, armature, space, numbered, parents, ancestors)
                if trouble:
                    refused.append(trouble)
                elif made:
                    done.append(made)
                elif why:
                    idle.append(why)

        for message in refused:
            self.report({"ERROR"}, message)
        if refused:
            return {"CANCELLED"}
        if not done:
            # Saying "already painted" whatever the reason sent people looking
            # for weights that were never there. Each mesh says its own.
            for message in idle:
                self.report({"WARNING"}, message)
            if not idle:
                self.report({"INFO"}, "Nothing to make: every joint that "
                                      "could be weighted from a box already "
                                      "has weights painted.")
            return {"CANCELLED"}
        self.report({"INFO"}, "; ".join(done))
        return {"FINISHED"}


class LS3D_OT_ClearWeights(bpy.types.Operator):
    """Take every weight off the selected meshes, so their joints are weighted from the influence boxes. The vertex groups stay, emptied, and so does the order that numbers the joints; a morph region's vertex group is not a weight and is left as it is"""

    bl_idname = "ls3d.clear_weights"
    bl_label = "Clear All Weights"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode == "EDIT_MESH":
            cls.poll_message_set("Leave Edit Mode first: the weights are "
                                 "cleared from the mesh itself.")
            return False
        if not _weighted_meshes(context):
            cls.poll_message_set("Select a mesh with vertex groups.")
            return False
        return True

    def execute(self, context):
        cleared = emptied = 0
        for obj in _weighted_meshes(context):
            everything = [vertex.index for vertex in obj.data.vertices]
            regions = {group.vertex_group for group in obj.ls3d_morph_groups
                       if group.vertex_group}
            weights = [group for group in obj.vertex_groups
                       if group.name not in regions]
            for group in weights:
                group.remove(everything)
            if weights:
                cleared += 1
                emptied += len(weights)
            obj.data.update()
        if not cleared:
            self.report({"INFO"}, "Nothing to clear: the selected meshes' only "
                                  "vertex groups are morph regions.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Cleared the weights of {cleared} mesh(es), "
                              f"{emptied} vertex group(s) in all. A joint with "
                              f"no weights is weighted from its influence box.")
        return {"FINISHED"}


class LS3D_OT_RestFromPose(bpy.types.Operator):
    """Make the pose the skeleton: every joint moved in Pose Mode becomes where that joint rests. A skeleton fitted to a character by posing it has to be told so before the mesh is weighted - until it is, the weights pull the mesh by the distance from each joint's resting place to where it was posed, and the influence boxes sit at the resting places rather than at the joints. Changes nothing a file keeps: a posed skeleton is already written as it is posed"""

    bl_idname = "ls3d.rest_from_pose"
    bl_label = "Set Rest From Pose"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        from .joint_space import is_posed
        armature = character_armature(context)
        if armature is None:
            cls.poll_message_set("Select the skeleton, or the mesh bound to "
                                 "it.")
            return False
        if context.mode not in ("OBJECT", "POSE"):
            cls.poll_message_set("Go back to Object Mode or Pose Mode first.")
            return False
        if not armature.data.bones:
            cls.poll_message_set("This skeleton has no joints.")
            return False
        if not is_posed(armature):
            cls.poll_message_set("Nothing is posed: every joint already rests "
                                 "where it stands.")
            return False
        return True

    def execute(self, context):
        from .joint_space import is_posed, settle_bones
        from ..packages import module
        armature = character_armature(context)
        # Where a bone is posed is worked-out data, stale until the scene is
        # brought up to date.
        context.view_layer.update()
        posed = sum(1 for bone in armature.pose.bones
                    if not _at_rest_pose(bone))
        aimed = sum(1 for bone in armature.pose.bones if bone.constraints)

        previous_active = context.view_layer.objects.active
        previous_mode = armature.mode
        context.view_layer.objects.active = armature
        # Only what somebody put there becomes the rest. A joint aimed at a
        # target is moved by its link, not by anybody posing it - the add-on
        # has never counted that as a pose, and Blender's own Apply Pose as
        # Rest Pose would bake it in, which would turn the joint for good and
        # leave the link aiming from somewhere new. Held off while the pose is
        # taken, and put back after, so the link goes on working from where
        # the joint now rests.
        held = [(constraint, constraint.mute)
                for bone in armature.pose.bones
                for constraint in bone.constraints]
        for constraint, _was in held:
            constraint.mute = True
        context.view_layer.update()
        try:
            bpy.ops.object.mode_set(mode="POSE")
            bpy.ops.pose.armature_apply(selected=False)
        except RuntimeError as problem:
            self.report({"ERROR"},
                        f"'{armature.name}' could not be told that its pose is "
                        f"where its joints rest: {problem}. Make sure the "
                        f"skeleton is visible and can be edited, then try "
                        f"again.")
            return {"CANCELLED"}
        finally:
            try:
                bpy.ops.object.mode_set(
                    mode="POSE" if previous_mode == "POSE" else "OBJECT")
            except RuntimeError:
                pass
            for constraint, was in held:
                constraint.mute = was
            context.view_layer.objects.active = previous_active

        # A bone is 32-bit, and a pose made the rest can land a rounding step
        # from where the values the export writes would put it. Settled, so the
        # next import rebuilds the same skeleton.
        settled = settle_bones(armature)

        # The joints' spaces are worked out from where they rest, so every one
        # of them has just changed.
        module("4ds.viewport").forget_joint_spaces()
        context.view_layer.update()

        still = is_posed(armature)
        self.report(
            {"INFO"},
            f"{posed} joint(s) of '{armature.name}' now rest where they were "
            f"posed"
            + (f"; {settled} settled onto the values an export writes"
               if settled else "")
            + (f"; {aimed} aimed at a target, left aiming"
               if aimed else "")
            + (". Something is still posed, so it was not all of them."
               if still else ". The mesh can be weighted now."))
        return {"FINISHED"}


def _at_rest_pose(pose_bone):
    """True while *pose_bone* is where it rests, to the tolerance we use."""
    from .joint_space import POSE_TOLERANCE
    from mathutils import Matrix as _Matrix
    rest = _Matrix.Identity(4)
    return max(abs(a - b)
               for row, rest_row in zip(pose_bone.matrix_basis, rest)
               for a, b in zip(row, rest_row)) <= POSE_TOLERANCE


class LS3D_OT_FixWeights(bpy.types.Operator):
    """Put the selected meshes' weights under the three rules the game plays them by: at most two influences on a vertex, those two a joint and its own parent, and the two totalling one. Done in that order, because each step changes what the next has to work with. The joints keep what is painted on them and the mesh frame's share becomes whatever they leave; only where the joints alone come to more than one are they brought down"""

    bl_idname = "ls3d.fix_weights"
    bl_label = "Fix Weights"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode == "EDIT_MESH":
            cls.poll_message_set("Leave Edit Mode first: the weights are set "
                                 "on the mesh itself.")
            return False
        if not _weights_to_fix(context):
            cls.poll_message_set("Select a skinned mesh with weights on it.")
            return False
        return True

    def execute(self, context):
        from .validation import repair_skin_weights
        done = {"influences": 0, "pairs": 0, "totals": 0}
        touched = 0
        stopped = []
        for obj, armature in _weights_to_fix(context):
            counts, why_not = repair_skin_weights(obj, armature)
            if why_not:
                stopped.append(why_not)
                continue
            if any(counts.values()):
                touched += 1
            for step, count in counts.items():
                done[step] += count
        if stopped:
            self.report({"ERROR"}, " ".join(stopped))
            return {"CANCELLED"}
        if not any(done.values()):
            self.report({"INFO"},
                        "Nothing to put right: every vertex is already under "
                        "all three rules.")
            return {"CANCELLED"}
        said = []
        if done["influences"]:
            said.append(f"cut {done['influences']} vertex/vertices to their "
                        f"two strongest")
        if done["pairs"]:
            said.append(f"resolved {done['pairs']} weighted to joints that "
                        f"are not a pair")
        if done["totals"]:
            said.append(f"settled {done['totals']} so their weights total one")
        self.report({"INFO"},
                    f"Across {touched} mesh(es): " + ", ".join(said) + ".")
        return {"FINISHED"}


def _weights_to_fix(context):
    """``(mesh, armature)`` for every selected skinned mesh carrying weights."""
    found = []
    for obj in getattr(context, "selected_objects", ()):
        if obj.type != "MESH" or obj.data is None or not obj.vertex_groups:
            continue
        armature = find_armature(obj)
        if armature is None:
            continue
        bones = set(armature.data.bones.keys())
        if any(group.name in bones for group in obj.vertex_groups):
            found.append((obj, armature))
    return found


def _weighted_meshes(context):
    """The selected meshes that have vertex groups to clear."""
    return [obj for obj in getattr(context, "selected_objects", ())
            if obj.type == "MESH" and obj.data is not None
            and obj.vertex_groups]


def _box_weighted_rigs(context):
    """``(armature, skinned mesh)`` for every rig a box could weight."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return []
    objects = list(scene.objects)
    found = []
    for armature in objects:
        if armature.type != "ARMATURE":
            continue
        skin = skinned_mesh_of(armature, objects)
        if skin is not None and influence.boxes_of(armature):
            found.append((armature, skin))
    return found


def _paint_from_boxes(obj, mesh, armature, space, numbered, parents, ancestors):
    """Weight one mesh from the boxes. ``(what was done, what stopped it)``.

    The joints with weights painted on them are left alone, exactly as the
    export leaves them: this paints what the export would have made, so that
    what is written does not change by pressing it.
    """
    painted, taken = influence.painted_joints(obj, mesh, set(numbered),
                                              shares_of(obj, mesh, armature))
    wanting = [bone.name for bone in space.joints()
               if bone.name in numbered and bone.name not in painted]
    boxes = []
    flat = []
    for name in wanting:
        box = influence.box_of(armature, name)
        if box is None or influence.is_flat(box):
            flat.append(name)
            continue
        boxes.append((name, influence.box_world(space, name, box)))
    if not boxes:
        # Nothing was made, and why matters: a joint already painted is a
        # joint left alone on purpose, a box with no thickness covers nothing
        # anybody can weight from, and neither is the same as a box that
        # simply misses the mesh.
        if not wanting:
            return "", "", ""
        return "", "", (f"'{obj.name}': every joint's box is flat "
                        f"({', '.join(sorted(flat))}), and a box with no "
                        f"thickness holds no vertices. Resize one, or press "
                        f"Fit Influence Box.")

    weights = influence.box_weights(mesh, space.mesh_placement(obj), boxes,
                                    parents, ancestors, taken)
    if not weights:
        return "", "", (f"'{obj.name}': no vertex of it lies in any joint's "
                        f"box, so the boxes weight nothing. Move or resize "
                        f"them over the mesh - Fit Influence Box aims a box "
                        f"down the chain and sizes it to the mesh around the "
                        f"joint.")

    # A vertex shared with the mesh frame keeps the mesh's share in the vertex
    # group the Armature modifier holds back from moving, so Blender shows it
    # moving as far as the game will.
    shared = [vertex for vertex, parts in weights.items()
              if sum(parts.values()) < 1.0 - WEIGHT_EPSILON]
    held, told = None, ""
    if shared:
        held, told = _share_holder(obj, armature)
        if held is None:
            return "", told, ""

    groups = {}

    def group_of(name):
        if name not in groups:
            groups[name] = (obj.vertex_groups.get(name)
                            or obj.vertex_groups.new(name=name))
        return groups[name]

    for vertex, shares in weights.items():
        total = 0.0
        for name, weight in shares.items():
            group_of(name).add([vertex], weight, "REPLACE")
            total += weight
        rest = 1.0 - total
        if rest > WEIGHT_EPSILON:
            group, inverted = held
            group.add([vertex], rest if inverted else 1.0 - rest, "REPLACE")
    return (f"'{obj.name}': {len(weights)} vertex/vertices weighted from the "
            f"boxes of {len(boxes)} joint(s){told}"), "", ""


def _share_holder(obj, armature):
    """``((group, inverted), what was set up)`` for the mesh's own share.

    The group the Armature modifier already names, or - where it names none -
    a group called after the mesh, which the modifier is then set to hold
    back, inverted. ``(None, why not)`` where there is no modifier to show it.
    """
    held = share_group(obj, armature)
    if held is not None:
        return held, ""
    modifier = next((m for m in obj.modifiers
                     if m.type == "ARMATURE" and m.object is armature), None)
    if modifier is None:
        return None, (f"'{obj.name}' has vertices shared with its mesh frame, "
                      f"and no Armature modifier to show that share: it is "
                      f"bound to '{armature.name}' by parenting alone. Give "
                      f"it an Armature modifier pointing at the armature, "
                      f"then try again.")
    if obj.name in armature.data.bones:
        return None, (f"'{obj.name}' has vertices shared with its mesh frame, "
                      f"and the group that would hold the mesh's share is "
                      f"named like the joint '{obj.name}'. Set the Armature "
                      f"modifier's Vertex Group to a group of another name, "
                      f"with Invert on, then try again.")
    group = (obj.vertex_groups.get(obj.name)
             or obj.vertex_groups.new(name=obj.name))
    modifier.vertex_group = group.name
    modifier.invert_vertex_group = True
    return (group, True), (f"; the mesh's own share is in the vertex group "
                           f"'{group.name}', which the Armature modifier now "
                           f"holds back")


class LS3D_OT_RemoveInfluenceBox(bpy.types.Operator):
    """Take the active joint's influence box away, leaving its weights to what is painted"""

    bl_idname = "ls3d.remove_influence_box"
    bl_label = "Remove Influence Box"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = context.object
        if armature is None or armature.type != "ARMATURE":
            return False
        bone = armature.data.bones.active
        return bone is not None and influence.has_box(armature, bone.name)

    def execute(self, context):
        armature = context.object
        bone_name = armature.data.bones.active.name
        influence.clear_box(armature, bone_name)
        self.report({"INFO"}, f"Joint '{bone_name}' has no influence box now; "
                              f"its weights are whatever is painted on it.")
        return {"FINISHED"}


class _PresetReport(Report):
    """Keeps what goes wrong and prints nothing else.

    A preset is built by the importer, whose running commentary - stages,
    frame by frame - means something for a file and nothing for a preset.
    """

    def info(self, message):
        pass

    def stage(self, message):
        pass

    def substage(self, message):
        pass

    def item(self, kind, index, total, description=""):
        pass


class LS3D_OT_AddCharacterSkeleton(bpy.types.Operator):
    """Add Tommy's skeleton: the eighteen joints every human character moves by, and the frames for weapons, animation events and turning the head"""

    bl_idname = "ls3d.add_character_skeleton"
    bl_label = "Character Skeleton (preset)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        # The skeleton is built in edit mode on the armature, which Blender
        # only allows from object mode.
        return context.mode == "OBJECT"

    def execute(self, context):
        from .character_preset import BASE_NAME, character_skeleton
        from .importer import Importer

        report = _PresetReport(fmt="4DS")
        builder = Importer("", report, own_collection=False)
        doc = character_skeleton()
        builder.build(doc)

        base = builder.objects_by_frame.get(1)
        armature = builder.armature_by_mesh.get(base)

        # Placed at the world origin, not the cursor: a model's frames are
        # measured from its origin, and the game stands a character on it.
        renamed = []
        for frame_id, frame in enumerate(doc.frames, start=1):
            obj = builder.objects_by_frame.get(frame_id)
            if obj is not None and obj.name != frame.name:
                renamed.append((frame.name, obj.name))

        # The preset is a skeleton. The mesh it is built from is not part of
        # it - a character's own mesh takes that name - so the empty one is
        # not left in the scene to be joined into or written out by mistake.
        # Nothing hangs off it: the joints are bones on the armature, and the
        # armature stands where the mesh frame is.
        base_name = base.name if base is not None else BASE_NAME
        if base is not None and armature is not None:
            mesh = base.data
            bpy.data.objects.remove(base, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            base = None
        _make_active(context, armature if armature is not None else base)
        if renamed:
            listed = ", ".join(f"'{old}' is '{new}'" for old, new in renamed[:3])
            if len(renamed) > 3:
                listed += f" and {len(renamed) - 3} more"
            self.report({"WARNING"},
                        f"Names already in use were taken, so {listed}. The "
                        f"game finds these frames by name - rename them back "
                        f"before exporting.")
        elif report.warning_count:
            self.report({"WARNING"}, report.entries[0][1])
        else:
            self.report({"INFO"},
                        f"Tommy's skeleton added. Name your character's mesh "
                        f"'{base_name}' and bind it to this armature - its "
                        f"origin can be anywhere. The armature stands at the "
                        f"default mesh origin, at Tommy's hip height, which "
                        f"the game's animations move the body from.")
        return {"FINISHED"}


def character_armature(context):
    """The armature the active object is, or the one its skinned mesh binds to."""
    obj = context.object
    if obj is None:
        return None
    if obj.type == "ARMATURE":
        return obj
    return find_armature(obj) if is_skinned_mesh(obj) else None


class LS3D_OT_DefaultMeshOrigin(bpy.types.Operator):
    """Stand the character's armature at the default mesh origin: hip height, the point the game's animations move and turn the body about, found from the character's own skeleton. One already standing there, within 10 cm as the game's own characters do, is left where it is. The bones, the mesh and everything hung on them stay where they are"""

    bl_idname = "ls3d.default_mesh_origin"
    bl_label = "Set Default Mesh Origin"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = character_armature(context)
        if armature is None:
            cls.poll_message_set("Select the character's armature or its "
                                 "skinned mesh.")
            return False
        if context.mode not in ("OBJECT", "POSE"):
            cls.poll_message_set("Go back to Object Mode first.")
            return False
        from .character_preset import THIGHS
        if any(name not in armature.data.bones for name in THIGHS):
            cls.poll_message_set(f"This skeleton has no '{THIGHS[0]}' and "
                                 f"'{THIGHS[1]}' joints, which the default "
                                 f"mesh origin is found from.")
            return False
        return True

    def execute(self, context):
        from ..common import convert
        from .character_preset import BASE_POSITION, THIGHS
        from .joint_space import (default_mesh_origin, origin_at_hips,
                                  own_channels_at_rest, rest_in_delta,
                                  rest_own_channels, set_delta)
        from .validation import MESH_FRAME_DRIFT
        from ..packages import module
        armature = character_armature(context)
        # Where a bone is posed is worked-out data, stale until the scene is
        # brought up to date: a skeleton just changed would read at its old
        # place and be taken for one already standing where it belongs.
        context.view_layer.update()
        place = default_mesh_origin(armature)
        if place is None:
            self.report({"ERROR"},
                        f"'{armature.name}' has no '{THIGHS[0]}' and "
                        f"'{THIGHS[1]}' joints, which the default mesh origin "
                        f"is found from.")
            return {"CANCELLED"}
        own, keyed = module("5ds.io").keyed_channels(armature)
        if own:
            self.report({"ERROR"},
                        f"'{armature.name}' has an animation moving it as a "
                        f"whole, measured from where it stands now. Press "
                        f"Unload Animation in the 5DS Animation tab first.")
            return {"CANCELLED"}
        if any(name in keyed for name in THIGHS):
            self.report({"ERROR"},
                        f"'{armature.name}' has an animation posing the "
                        f"joints the default mesh origin is found from, so "
                        f"they are wherever the frame showing puts them, not "
                        f"where the model has them. Press Unload Animation in "
                        f"the 5DS Animation tab first.")
            return {"CANCELLED"}

        if origin_at_hips(armature, place):
            # Already standing at its hips, as every character the game
            # ships does - each placed by hand, up to 6.6 cm from where the
            # thighs alone would put it - so it stays exactly where it is. A
            # move made to the armature as an object goes into where it
            # stands, the way Blender adds the two, so nothing shifts.
            if not own_channels_at_rest(armature):
                rest_in_delta(armature)
                context.view_layer.update()
            self.report({"INFO"}, f"'{armature.name}' already stands at the "
                                  f"default mesh origin; it is left where it "
                                  f"is.")
            return {"FINISHED"}

        # Everything the armature places goes over to the new place, so where
        # anything is in the world does not change: the armature's whole
        # present placement - its own location, rotation and scale and its
        # Delta Transform, as Blender puts them together - comes off the
        # bones' side, and the new one goes on. The bones are moved as one,
        # the way Blender applies a transform, so a bone connected to its
        # parent is not moved twice.
        standing = Matrix.Translation(place)
        step = standing.inverted() @ armature.matrix_basis
        hung = {}
        for child in armature.children:
            if child.parent_type == "BONE" and child.parent_bone:
                hung[child] = self._bone_place(armature, child.parent_bone)
        armature.data.transform(step)
        rest_own_channels(armature)
        set_delta(armature, place, (1.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        # Blender keeps hanging objects off a bone at its old length until
        # the armature has been through Edit Mode, so it goes through it here,
        # before anything is measured against the bones' new places.
        previous_active = context.view_layer.objects.active
        previous_mode = armature.mode
        context.view_layer.objects.active = armature
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.object.mode_set(mode=previous_mode)
        finally:
            context.view_layer.objects.active = previous_active
        context.view_layer.update()
        for child in armature.children:
            if child.parent_type == "OBJECT":
                child.matrix_parent_inverse = step @ child.matrix_parent_inverse
            elif child in hung:
                now = self._bone_place(armature, child.parent_bone)
                child.matrix_parent_inverse = (now.inverted() @ hung[child]
                                               @ child.matrix_parent_inverse)
        context.view_layer.update()
        # The game's own animations put the origin where Tommy's is, so a
        # character whose hips are elsewhere is carried by the difference.
        off = place - Vector(convert.to_blender_vector(BASE_POSITION))
        if off.length <= MESH_FRAME_DRIFT:
            self.report({"INFO"}, f"'{armature.name}' stands at the default "
                                  f"mesh origin, where Tommy's does.")
        else:
            rise = "lifts" if off.z < 0.0 else "lowers"
            self.report({"WARNING"},
                        f"'{armature.name}' stands at its own default mesh "
                        f"origin, {off.length:.2f} m from where Tommy's is "
                        f"({abs(off.z):.2f} m {'lower' if off.z < 0.0 else 'higher'}"
                        f"). The game's own animations put the origin at "
                        f"Tommy's, so they {rise} the whole body by that "
                        f"much.")
        return {"FINISHED"}

    @staticmethod
    def _bone_place(armature, bone_name):
        """Where Blender hangs an object on *bone_name*, in the world."""
        bone = armature.data.bones[bone_name]
        pose_bone = armature.pose.bones[bone_name]
        return (armature.matrix_world @ pose_bone.matrix
                @ Matrix.Translation((0.0, bone.length, 0.0)))


class LS3D_MT_AddMenu(bpy.types.Menu):
    """The 4DS entries in the viewport's Add menu."""

    bl_idname = "LS3D_MT_add"
    bl_label = "4DS"

    def draw(self, context):
        layout = self.layout
        layout.operator(LS3D_OT_AddSector.bl_idname, icon="SCENE_DATA")
        layout.operator(LS3D_OT_AddPortal.bl_idname, icon="OUTLINER_OB_LIGHT")
        layout.operator(LS3D_OT_AddOccluder.bl_idname, icon="MOD_BOOLEAN")
        layout.separator()
        layout.operator(LS3D_OT_AddDummy.bl_idname, icon="MESH_CUBE")
        layout.operator(LS3D_OT_AddTarget.bl_idname, icon="EMPTY_ARROWS")
        layout.operator(LS3D_OT_AddJoint.bl_idname, icon="BONE_DATA")
        layout.operator(LS3D_OT_AddCharacterSkeleton.bl_idname,
                        icon="ARMATURE_DATA")
        layout.separator()
        layout.operator(LS3D_OT_AddMirror.bl_idname, icon="MOD_MIRROR")
        layout.operator(LS3D_OT_AddBillboard.bl_idname, icon="IMAGE_PLANE")
        layout.operator(LS3D_OT_AddLensFlare.bl_idname, icon="LIGHT")
        layout.operator(LS3D_OT_AddProjector.bl_idname,
                        icon="OUTLINER_OB_LIGHT")
        layout.operator(LS3D_OT_AddLight.bl_idname, icon="LIGHT_SPOT")
        layout.operator(LS3D_OT_AddSunFlare.bl_idname, icon="LIGHT_SUN")


def menu_func_add(self, context):
    self.layout.menu(LS3D_MT_AddMenu.bl_idname, icon="SNAP_VOLUME")


CLASSES = (LS3D_OT_AddSector, LS3D_OT_AddPortal, LS3D_OT_AddOccluder,
           LS3D_OT_AddDummy, LS3D_OT_AddTarget, LS3D_OT_AddJoint,
           LS3D_OT_AddInfluenceBox, LS3D_OT_AddInfluenceBoxes,
           LS3D_OT_RemoveInfluenceBox, LS3D_OT_WeightsFromBoxes,
           LS3D_OT_ClearWeights, LS3D_OT_FixWeights,
           LS3D_OT_FitInfluenceBox,
           LS3D_OT_AddCharacterSkeleton, LS3D_OT_DefaultMeshOrigin,
           LS3D_OT_RestFromPose,
           LS3D_OT_AddSunFlare,
           LS3D_OT_AddLensFlare, LS3D_OT_FitViewBox, LS3D_OT_AddMirror,
           LS3D_OT_FitProjectorToTexture,
           LS3D_OT_AddBillboard, LS3D_OT_AddProjector, LS3D_OT_AddLight,
           LS3D_MT_AddMenu)
