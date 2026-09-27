"""Blender property registration for 4DS data.

Every field the format carries but Blender has no native home for lives here as
a custom property, so an imported model can be re-exported without losing
anything the addon read.

Bit flags are stored in a single ``IntProperty`` and exposed as individual
booleans through generated get/set pairs. The storage is signed 32-bit (Blender
has no unsigned int property), so the setters round-trip through an unsigned
value before converting back — otherwise setting bit 31 raises.
"""

import math

import bpy

from .packages import module
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty, StringProperty)

from .common import constants as C


# ── Bit-flag plumbing ─────────────────────────────────────────────────────────
def _to_signed32(value):
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def flag_getter(prop_name, mask):
    def getter(self):
        return (getattr(self, prop_name, 0) & mask) != 0
    return getter


def flag_setter(prop_name, mask):
    def setter(self, value):
        current = getattr(self, prop_name, 0) & 0xFFFFFFFF
        updated = (current | mask) if value else (current & ~mask)
        setattr(self, prop_name, _to_signed32(updated))
    return setter


def flag_bool(prop_name, mask, name, description=""):
    return BoolProperty(
        name=name, description=description,
        get=flag_getter(prop_name, mask),
        set=flag_setter(prop_name, mask),
    )


def flag_enum(prop_name, mask, shift, items, name, description=""):
    """A dropdown backed by a multi-bit field inside an int property.

    The item identifiers are "0", "1", ... so Blender's implicit enum numbering
    matches the value stored in the bits.
    """
    def getter(self):
        return (getattr(self, prop_name, 0) & 0xFFFFFFFF & mask) >> shift

    def setter(self, value):
        current = getattr(self, prop_name, 0) & 0xFFFFFFFF
        updated = (current & ~mask) | ((value << shift) & mask)
        setattr(self, prop_name, _to_signed32(updated))

    return EnumProperty(name=name, description=description, items=items,
                        get=getter, set=setter)


def flag_int(prop_name, mask, shift, name, description="", minimum=0,
             maximum=255):
    """A number field backed by a run of bits inside an int property."""
    def getter(self):
        return (getattr(self, prop_name, 0) & 0xFFFFFFFF & mask) >> shift

    def setter(self, value):
        current = getattr(self, prop_name, 0) & 0xFFFFFFFF
        updated = (current & ~mask) | ((value << shift) & mask)
        setattr(self, prop_name, _to_signed32(updated))

    return IntProperty(name=name, description=description, min=minimum,
                       max=maximum, get=getter, set=setter)


def parse_hex_word(text, width=32):
    """The value typed into a raw flag field, or None if it is not one.

    The field shows hex, so what is typed back is read as hex too, with or
    without the ``0x`` in front and with spaces or underscores anywhere. A value
    that does not fit the word is refused rather than cut down to size.
    """
    if not isinstance(text, str):
        return None
    digits = text.strip().replace("_", "").replace(" ", "")
    if digits[:2].lower() == "0x":
        digits = digits[2:]
    if not digits:
        return None
    try:
        value = int(digits, 16)
    except ValueError:
        return None
    if value >> width:
        return None
    return value


def hex_string_property(prop_name, name, description, width=32):
    """A text field showing an int property as unsigned hex, e.g. ``0x40808001``."""
    digits = width // 4
    mask = (1 << width) - 1

    def getter(self):
        return f"0x{getattr(self, prop_name, 0) & mask:0{digits}X}"

    def setter(self, value):
        parsed = parse_hex_word(value, width)
        if parsed is None:
            return      # keep the previous value on input that is not a word
        setattr(self, prop_name, _to_signed32(parsed))

    return StringProperty(name=name, description=description, get=getter, set=setter)


def redraw_3d_views():
    """Ask every 3D viewport to paint again.

    The viewport outlines and handles are drawn from properties Blender knows
    nothing about, and changing one of those - from the panel or a script -
    does not repaint the viewport by itself. The outlines kept their old shape,
    and handles whose showing depends on the value (a light's range and cone
    boxes, its aim ball) stayed missing, until something else repainted the
    view, such as an export's report.
    """
    manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(manager, "windows", ()) or ():
        screen = getattr(window, "screen", None)
        for area in getattr(screen, "areas", ()) or ():
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _viewport_changed(self, context):
    # Looked up by name when called, so a test can stand in for it.
    redraw_3d_views()


def _joint_display_scale_changed(self, _context):
    """Paint the joint markers again at the size just set.

    The markers are driven from this setting, so nothing here has to reach
    them: what is left is to ask the views to draw.
    """
    redraw_3d_views()


def _show_influence_boxes(self, _context):
    """Show or hide the joints' influence boxes.

    Only what is drawn: a box hidden is still the joint's box and is written
    as it always was. Nothing stands in the scene for one, so this is a redraw
    and nothing else.
    """
    redraw_3d_views()


def _int_prop_value(obj, name, default=0):
    try:
        return int(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


#: The frame types that carry nothing but a transform, in the order the panel
#: offers them, with what each one is for. None is a shape - they are named
#: points the game hangs behavior off - so all of them are plain empties.
PAYLOADLESS_DESCRIPTIONS = (
    (C.FRAME_MODEL, "Groups the frames under it as one model"),
    (C.FRAME_SCENE, "Scene root"),
    (C.FRAME_CAMERA, "Camera position"),
    (C.FRAME_VOLUME, "Volume marker"),
    (C.FRAME_USER, "Marker with no meaning of its own"),
    (C.FRAME_LANDSCAPE, "Landscape marker"),
    (C.FRAME_SHADOW, "Shadow frame. Nothing to do with the .6ds shadow "
                     "beside the model - that is a separate file and a "
                     "separate switch"),
)


# ── Enum item callbacks ───────────────────────────────────────────────────────
def frame_type_items(self, context):
    """Frame types that make sense for this object's Blender type."""
    if self.type == "MESH":
        return [
            (str(C.FRAME_VISUAL), "Visual", "Standard mesh"),
            (str(C.FRAME_SECTOR), "Sector", "Sector volume or portal"),
            (str(C.FRAME_OCCLUDER), "Occluder", "Visibility occluder"),
            (str(C.FRAME_EMITOR), "Emitor",
             "Particle emitter. The mesh is the particle it spawns, not the "
             "shape of the emitter"),
        ]
    if self.type == "EMPTY":
        display = self.empty_display_type
        if display == "CUBE":
            return [(str(C.FRAME_DUMMY), "Dummy", "Helper / mount point")]
        if display == "SPHERE":
            return [(str(C.FRAME_VISUAL), "Visual", "Lens flare carrier")]
        if display == "ARROWS":
            return [(str(C.FRAME_VISUAL), "Visual", "Projector volume")]
        if display == C.LIGHT_EMPTY_DISPLAY:
            return [(str(C.FRAME_LIGHT), "Light", "A light of one of the "
                     "engine's nine kinds")]
        if display == "PLAIN_AXES":
            # Target stays first, so a plain empty that has never been
            # assigned a type still reads as one. The rest store nothing but
            # their transform, so a plain empty is all any of them can be.
            return [(str(C.FRAME_TARGET), "Target", "Look-at target")] + [
                (str(frame_type), C.FRAME_TYPE_NAMES[frame_type], description)
                for frame_type, description in PAYLOADLESS_DESCRIPTIONS
            ]
        # Any other shape is not a frame at all. The list still has to hold
        # something - an empty one is not a legal enum - but the panel does not
        # draw it and the export passes the object by, so nothing reads it.
        return [(str(C.FRAME_DUMMY), "Dummy", "Helper / mount point")]
    if self.type == "ARMATURE":
        return [(str(C.FRAME_JOINT), "Joint", "Skeleton root")]
    return [(str(C.FRAME_VISUAL), "Visual", "")]


def visual_type_items(self, context):
    """Visual sub-types available for this object."""
    try:
        frame_type = int(self.ls3d_frame_type)
    except (TypeError, ValueError):
        frame_type = C.FRAME_VISUAL
    if frame_type != C.FRAME_VISUAL:
        return [(str(C.VISUAL_OBJECT), "Standard", "")]

    if self.type == "MESH":
        return [
            (str(C.VISUAL_OBJECT), "Object", "Plain mesh"),
            (str(C.VISUAL_LITOBJECT), "Lit Object",
             "Plain mesh whose lighting is baked. The bake itself lives in the "
             "mission, not in the model"),
            (str(C.VISUAL_SINGLEMESH), "Single Mesh", "Skinned mesh"),
            (str(C.VISUAL_SINGLEMORPH), "Single Morph", "Skinned mesh with morph targets"),
            (str(C.VISUAL_MORPH), "Morph", "Unskinned mesh with morph targets"),
            (str(C.VISUAL_BILLBOARD), "Billboard", "Camera-facing mesh"),
            (str(C.VISUAL_MIRROR), "Mirror", "Reflective surface"),
        ]
    if self.type == "EMPTY":
        if self.empty_display_type == "SPHERE":
            return [(str(C.VISUAL_LENSFLARE), "Lens Flare", "")]
        if self.empty_display_type == "ARROWS":
            return [(str(C.VISUAL_PROJECTOR), "Projector",
                     "Paints a material onto whatever it covers")]
    return [(str(C.VISUAL_OBJECT), "Standard", "")]


def detect_initial_frame_type(obj):
    """Best-guess frame type for an object that has never been assigned one."""
    if obj.type == "ARMATURE":
        return str(C.FRAME_JOINT)
    if obj.type == "EMPTY":
        # By shape, since that is what says which frame an empty stands for.
        display = obj.empty_display_type
        if display == "PLAIN_AXES":
            return str(C.FRAME_TARGET)
        if display in ("SPHERE", "ARROWS"):
            return str(C.FRAME_VISUAL)
        if display == C.LIGHT_EMPTY_DISPLAY:
            return str(C.FRAME_LIGHT)
        return str(C.FRAME_DUMMY)
    if obj.type == "MESH":
        return str(C.FRAME_VISUAL)
    return str(C.FRAME_DUMMY)


# ── Target links ──────────────────────────────────────────────────────────────
class LS3DNamedEvent(bpy.types.PropertyGroup):
    """One event cue carrying a word rather than a number.

    Kept as a list rather than as keyframes because a curve holds numbers and
    this holds text. It is authored input with nowhere else to live - nothing
    about it can be worked out from the animation - so it rides on the frame
    the same way the movement track's reference points do.
    """

    frame: IntProperty(
        name="Frame", default=0, min=0, max=0xFFFF,
        description="When the cue fires, as a frame of the animation")
    text: StringProperty(
        name="Text", default="",
        description="What the cue says. The game passes this straight to "
                    "whatever is listening without reading it, so any wording "
                    "will do")


class LS3DTargetObject(bpy.types.PropertyGroup):
    """One entry in a target frame's link list.

    A link points either at an object or at a bone on an armature. Object
    pointers are used rather than name strings, so a rename does not break one.
    """

    name: StringProperty(name="Name", default="")
    target_object: PointerProperty(name="Object", type=bpy.types.Object)
    target_armature: PointerProperty(name="Armature", type=bpy.types.Object)
    bone_name: StringProperty(name="Bone", default="")


# ── Lens flare elements ───────────────────────────────────────────────────────
class LS3DGlowElement(bpy.types.PropertyGroup):
    """One element of a lens flare: a textured quad drawn on the flare axis.

    A flare with a single element is a lamp: it is drawn at the object's own
    position and grows or fades with distance. Two or more elements make a
    directional flare - the object's position is read as a direction, and the
    elements are spread along the line running from the middle of the screen
    out towards that direction.
    """

    name: StringProperty(name="Name", default="Glow")
    position: FloatProperty(
        name="Axis Offset", default=0.0, unit="LENGTH",
        description="Distance along the light direction, measured from a point "
                    "2 m in front of the camera, and that world point is what "
                    "gets projected to screen. 0 lands on the screen center. "
                    "Positive moves towards the light and closes on it without "
                    "arriving - half the way at 2 m, 90% at 18 m. Negative "
                    "mirrors it through the center, and past about -2 m the "
                    "point falls behind the camera and the element is dropped. "
                    "A one-element flare ignores this")
    material: PointerProperty(
        name="Material", type=bpy.types.Material,
        description="Material drawn as this element's sprite. Its texture "
                    "size sets how big the element appears")


# ── Morph groups ──────────────────────────────────────────────────────────────
def _on_active_target_changed(group, context):
    """Show only the active morph target in the viewport."""
    obj = context.object if context else None
    if not obj or obj.type != "MESH" or not obj.data.shape_keys:
        return
    index = group.active_target_index
    if not (0 <= index < len(group.targets)):
        return
    key_name = group.targets[index].shape_key_name
    key_blocks = obj.data.shape_keys.key_blocks
    if key_name in key_blocks:
        obj.active_shape_key_index = key_blocks.find(key_name)


def _on_active_morph_group_changed(obj, context):
    """Bring the chosen group's region forward as the active vertex group.

    Which vertices a morph group covers was once a choice in the panel, and
    it is not one anybody should have to make: a group that came out of a
    file has the region the file listed for it, and a group made here takes
    the vertices its targets move. Picking a group here just puts its region
    where Blender's own vertex group tools can reach it.
    """
    groups = getattr(obj, "ls3d_morph_groups", None)
    index = getattr(obj, "ls3d_active_morph_group", -1)
    if not groups or not (0 <= index < len(groups)):
        return
    name = groups[index].vertex_group
    found = obj.vertex_groups.get(name) if name else None
    if found is not None:
        obj.vertex_groups.active_index = found.index


class LS3DMorphTarget(bpy.types.PropertyGroup):
    """A reference to one shape key inside a morph group."""

    shape_key_name: StringProperty(name="Shape Key", default="")
    select: BoolProperty(name="Select", default=False)


class LS3DMorphGroup(bpy.types.PropertyGroup):
    """A morph region: an ordered list of shape keys sharing a basis.

    The first target is the group's basis; the remainder are the poses the
    engine blends towards. Every group in one object must end up with the same
    number of targets in the exported file, so shorter groups are padded with
    their own basis at write time.
    """

    name: StringProperty(name="Name", default="Group")
    targets: CollectionProperty(type=LS3DMorphTarget)
    active_target_index: IntProperty(default=0, update=_on_active_target_changed)
    vertex_group: StringProperty(
        name="Vertex Group", default="",
        description="Vertex group holding this region's vertices, moving or "
                    "not. Set by the import from what the file listed, and "
                    "not by hand: left empty, the region is every vertex "
                    "this group's targets move")


def active_morph_group(obj):
    """Return ``(group, active_target_index)`` or ``(None, -1)``."""
    if not obj or obj.type != "MESH":
        return None, -1
    index = getattr(obj, "ls3d_active_morph_group", -1)
    groups = getattr(obj, "ls3d_morph_groups", None)
    if groups is None or not (0 <= index < len(groups)):
        return None, -1
    group = groups[index]
    return group, group.active_target_index


# ── Registration ──────────────────────────────────────────────────────────────
def _register_scene_properties():
    # Typing a number here pins it. Without that the count follows the scene,
    # and the handler that raises it would put it straight back on the next
    # depsgraph update - so a model that really has no animation could not be
    # told to say zero, which is exactly what it has to say when there is no
    # .5DS beside it.
    def _pin_animated_count(self, _context):
        anim_io = module("5ds.io")
        if anim_io.count_is_held():
            return
        # Only a number the refresh would otherwise overwrite is worth pinning:
        # it only ever raises, so anything at or above what the scene has keyed
        # already stays put on its own. Pinning on every write would make the
        # refresh's own raise look like a hand-typed value and freeze it there.
        if self.ls3d_animated_object_count < anim_io.count_animated_objects(self):
            self.ls3d_animated_count_pinned = True

    bpy.types.Scene.ls3d_animated_object_count = IntProperty(
        name="Animated Objects", min=0, max=255, default=0,
        update=_pin_animated_count,
        description=("Number of animated objects. Any value above zero tells "
                     "the engine to look for a .5DS named like this .4DS and "
                     "play it, so leave it at zero until there is one. Follows "
                     "what the scene has keyed until you type a number, and "
                     "then stays where you put it"),
    )
    setattr(bpy.types.Scene, C.JOINT_DISPLAY_SCALE_PROP, FloatProperty(
        name="Joint Size", default=1.0, min=0.01, max=100.0,
        soft_min=0.1, soft_max=10.0, step=10, precision=2,
        description="How big the joint markers are drawn, against the size a "
                    "joint of scale 1 is drawn at. Nothing about the model "
                    "changes - a marker is drawn, never written",
        update=_joint_display_scale_changed))

    setattr(bpy.types.Scene, C.SHOW_INFLUENCE_BOXES_PROP, BoolProperty(
        name="Influence Boxes", default=True,
        description="Show the joints' influence boxes. They are only there to "
                    "be seen and moved - hiding them changes nothing that is "
                    "written",
        update=_show_influence_boxes))

    setattr(bpy.types.Scene, C.INFLUENCE_BOXES_IN_FRONT_PROP, BoolProperty(
        name="In Front", default=False,
        description="Draw the influence boxes over everything, so a box "
                    "inside the mesh can be seen whole. Only the drawing "
                    "changes - nothing that is written",
        update=_show_influence_boxes))

    setattr(bpy.types.Scene, C.INFLUENCE_HANDLES_PROP, EnumProperty(
        name="Handles", default="ALL",
        items=(
            ("ALL", "All", "The faces, the arrows and the rings together"),
            ("SIZE", "Resize", "Only the six handles on the box's faces"),
            ("MOVE", "Move", "Only the three arrows that slide the box"),
            ("TURN", "Turn", "Only the three rings that turn the box"),
        ),
        description="Which handles the joint being posed carries. One kind at "
                    "a time is easier to hit on a small box",
        update=_show_influence_boxes))

    bpy.types.Scene.ls3d_animated_count_pinned = BoolProperty(
        name="Count Set By Hand", default=False,
        description=("Whether the animated object count was typed rather than "
                     "counted from the scene. Authoring state - the file holds "
                     "the number, never this"),
    )


def _register_frame_properties():
    obj = bpy.types.Object
    update_viewport_display = module("4ds.viewport").update_viewport_display

    obj.ls3d_frame_type_override = IntProperty(default=0)
    def _on_frame_type_changed(self, context):
        # Picking Sector turns the mesh into a containment hull, and every
        # Blender primitive arrives with its normals pointing out - which would
        # leave the room reading as empty. Flip once, here, rather than making
        # everyone fix it by hand after the export refuses.
        if _int_prop_value(self, "ls3d_frame_type") == C.FRAME_SECTOR:
            face_sector_normals_inward = module(
                "4ds.validation").face_sector_normals_inward
            face_sector_normals_inward(self)
        update_viewport_display(self)

    obj.ls3d_frame_type = EnumProperty(
        name="Frame Type", items=frame_type_items, default=0,
        update=_on_frame_type_changed)
    obj.visual_type = EnumProperty(
        name="Visual Type", items=visual_type_items, default=0,
        update=lambda self, ctx: update_viewport_display(self))

    # The file stores the SQUARE of the switch distance - it is compared
    # against a squared camera distance, with no square root taken. The values
    # shipped with the game agree: Sam's eyes hold 0.484 (0.70 m) and the
    # zeppelin 789823 (889 m). The raw float stays the stored value so files
    # round-trip untouched; the meter field below is the one the panel shows.
    obj.ls3d_lod_dist = FloatProperty(
        name="LOD Distance (squared)", default=0.0, min=0.0,
        description="Raw stored value: the square of the switch distance")

    def _lod_meters_get(self):
        raw = getattr(self, "ls3d_lod_dist", 0.0)
        return math.sqrt(raw) if raw > 0.0 else 0.0

    def _lod_meters_set(self, value):
        self.ls3d_lod_dist = value * value

    obj.ls3d_lod_distance_m = FloatProperty(
        name="Switch Distance", default=0.0, min=0.0, unit="LENGTH",
        description="How far the camera can get before the engine switches to "
                    "this level of detail",
        get=_lod_meters_get, set=_lod_meters_set)
    obj.ls3d_user_props = StringProperty(
        name="User Props",
        description="Free-form text stored on the frame (255 bytes max)")
    # The box is stored in the same axes as the frame's own transform and its
    # geometry - the pipe in 'xv trubka2.4ds' has a dummy box of
    # (0.211, 3.697, 0.211) and a mesh whose vertices span exactly the same
    # figures. The importer applies the same Y/Z swap to it as to positions, so
    # what is held here is plain Blender XYZ and can be labeled as such.
    obj.bbox_min = FloatVectorProperty(
        name="BBox Min", subtype="XYZ", size=3, unit="LENGTH",
        description="Low corner of the dummy's box, in the object's own space",
        update=_viewport_changed)
    obj.bbox_max = FloatVectorProperty(
        name="BBox Max", subtype="XYZ", size=3, unit="LENGTH",
        description="High corner of the dummy's box, in the object's own space",
        update=_viewport_changed)

    # A mirror's view box: what the mirror is allowed to reflect. The file
    # stores it as a matrix taking a cube two units across onto the box, and
    # these are that matrix's columns - its center and its three axes - in the
    # mirror's own space, so they are written back exactly as set.
    obj.ls3d_mirror_box_center = FloatVectorProperty(
        name="Center", subtype="XYZ", size=3, unit="LENGTH", precision=5,
        description="Middle of the mirror's view box, in the mirror's own space",
        update=_viewport_changed)
    for axis in "xyz":
        setattr(obj, f"ls3d_mirror_box_{axis}", FloatVectorProperty(
            name=f"{axis.upper()} Axis", subtype="XYZ", size=3, unit="LENGTH",
            precision=5,
            description=(f"From the view box's center to the middle of one "
                         f"face, in the mirror's own space. The box reaches "
                         f"this far either way along it, so its length is "
                         f"half the box's size that way"),
            update=_viewport_changed))

    # Culling flags. Only the two bits the engine actually honors are shown;
    # the rest are its own transform bookkeeping, overwritten on load. They
    # still survive a round trip through the raw value below.
    obj.cull_flags = IntProperty(name="Culling Flags",
                                 default=C.DEFAULT_CULL_FLAGS, min=0, max=255)
    obj.cull_flags_str = hex_string_property(
        "cull_flags", "Raw Flags", "Frame flags as unsigned hex", width=8)
    obj.cf_enabled = flag_bool(
        "cull_flags", C.CF_ENABLED, "Enable",
        "The object is drawn in game. Off also removes it from collision, "
        "lighting and sound, and hides its children")
    obj.cf_pos_locked = flag_bool(
        "cull_flags", C.CF_POS_LOCKED, "Position Locked",
        "The engine refuses to move the object at runtime. Unused in the "
        "original game")

    # Render flags, byte 1
    obj.render_flags = IntProperty(name="Render Flags 1",
                                   default=C.DEFAULT_RENDER_FLAGS, min=0, max=255)
    obj.render_flags_str = hex_string_property(
        "render_flags", "Raw Flags", "Rendering flags as unsigned hex", width=8)
    obj.rf1_flat_light = flag_bool(
        "render_flags", C.RF_FLAT_LIGHT, "Flat Lighting",
        "Averages vertex lighting into one value across the whole object")
    obj.rf1_managed_lod = flag_bool(
        "render_flags", C.RF_MANAGED_LOD, "Managed LOD",
        "Picks the LOD from texture residency instead of camera distance. "
        "Unused in the original game")
    obj.rf1_no_mirror = flag_bool(
        "render_flags", C.RF_NO_MIRROR, "No Mirror",
        "Leaves the object out of mirror reflections. The game gathers what a "
        "mirror can see and skips anything carrying this")
    obj.rf1_world_space = flag_bool(
        "render_flags", C.RF_WORLD_SPACE, "World Space Geometry",
        "Skips the frame transform because the vertices are already in world "
        "space. Unused in the original game")

    # Render flags, byte 2
    obj.render_flags2 = IntProperty(name="Render Flags 2",
                                    default=C.DEFAULT_RENDER_FLAGS2, min=0, max=255)
    obj.render_flags2_str = hex_string_property(
        "render_flags2", "Raw Flags", "Visual flags as unsigned hex", width=8)
    obj.rf2_zbias = flag_bool(
        "render_flags2", C.LF_ZBIAS, "Z-Bias",
        "Depth bias for decals. Stops posters, road markings and stains "
        "z-fighting with the surface underneath")
    obj.rf2_shadow_diffuse = flag_bool(
        "render_flags2", C.LF_SHADOW_DIFFUSE, "Shadows on Diffuse",
        "Receive dynamic shadows on solid faces. Off means the object takes "
        "no shadow at all")
    obj.rf2_shadow_alpha = flag_bool(
        "render_flags2", C.LF_SHADOW_ALPHA, "Shadows on Alpha",
        "Let the shadow fall on see-through faces too. Without it the "
        "clipper drops every face group whose material uses color key, "
        "alpha blending, alpha test or an alpha below 1, so railings and "
        "foliage stay unshadowed. Needs Shadows on Diffuse as well")
    obj.rf2_is_mesh_object = flag_bool(
        "render_flags2", C.LF_IS_MESH_OBJECT, "Renderable Object",
        "Marks the visual as one of the engine's object family - every type "
        "except a mirror, lens flares included even though they carry no mesh. "
        "With it off the object is left out of every mirror's reflection and "
        "skipped when detail levels are pre-built; ordinary drawing is not "
        "affected. The exporter sets it to match the object's type")
    obj.rf2_no_twosided_collision = flag_bool(
        "render_flags2", C.LF_NO_TWOSIDED_COLLISION, "Skip Two-Sided Collision",
        "Excludes the object from collision queries tagged two-sided. The "
        "game never tags one, so on its own this changes nothing. It is not "
        "the setting that makes collision accept back faces - that is a "
        "separate option on the query itself. Unused in the original game")
    obj.rf2_projection_diffuse = flag_bool(
        "render_flags2", C.LF_PROJECTION_DIFFUSE, "Projection on Diffuse",
        "Receive projected textures - headlights, bullet holes, projected "
        "shadows - on solid faces")
    obj.rf2_projection_alpha = flag_bool(
        "render_flags2", C.LF_PROJECTION_ALPHA, "Projection on Alpha",
        "Let headlights and bullet holes land on see-through faces too. "
        "Without it the clipper drops every face group whose material uses "
        "color key, alpha blending, alpha test or an alpha below 1. Needs "
        "Projection on Diffuse as well")
    obj.rf2_no_fog = flag_bool(
        "render_flags2", C.LF_NO_FOG, "No Fog",
        "Excludes the object from the sector's fog")

    for mask, attr, label, desc in C.RESERVED_CULL_FLAGS:
        setattr(obj, attr, flag_bool("cull_flags", mask, label, desc))
    for mask, attr, label, desc in C.RESERVED_RENDER_FLAGS:
        setattr(obj, attr, flag_bool("render_flags", mask, label, desc))


def _register_sector_properties():
    obj = bpy.types.Object
    obj.ls3d_sector_flags1 = IntProperty(default=C.DEFAULT_SECTOR_FLAGS1)
    obj.ls3d_sector_flags2 = IntProperty(default=C.DEFAULT_SECTOR_FLAGS2)
    obj.ls3d_sector_flags1_str = hex_string_property(
        "ls3d_sector_flags1", "Raw Flags", "Sector flags as unsigned hex")
    obj.ls3d_sector_flags2_str = hex_string_property(
        "ls3d_sector_flags2", "Group Mask",
        "The 32 light groups this sector belongs to. They would decide which "
        "sectors a headlight or a projected shadow can reach, but Mafia never "
        "uses them: every sector in the game stores zero, and the moment a "
        "mission finishes loading the game turns every group on for every "
        "sector regardless. There are no switches for that reason. The value "
        "is written back exactly as it came in")
    obj.sf_occluder = flag_bool(
        "ls3d_sector_flags1", C.SF_OCCLUDER, "Occluder",
        "Use the sector's own shape to hide whatever is behind it, the way a "
        "dedicated occluder object would. Saves drawing a building interior "
        "you cannot see past")
    obj.sf_small_portal_cull = flag_bool(
        "ls3d_sector_flags1", C.SF_SMALL_PORTAL_CULL,
        "Skip Nested Objects When Barely Visible",
        "Cuts this sector back while you are only glimpsing it. It counts as "
        "barely visible when the opening you are looking through covers less "
        "than about ten pixels each way on screen, or when you are past that "
        "portal's View Distance and the portal has no Far Cull. In that "
        "state only the objects sitting directly in the sector are drawn - "
        "anything parented under them is skipped. Meant for cluttered "
        "interiors seen through a far-off window. Unused in the original "
        "game")
    obj.sf_sound_reverb = flag_bool(
        "ls3d_sector_flags1", C.SF_SOUND_REVERB, "Echo Settings Pending",
        "Says the sector's echo settings have not been applied yet. The game "
        "applies them when you walk in and switches this off again. A sector "
        "saved with it already off never has its echo applied")
    for mask, attr, label, desc in C.RESERVED_SECTOR_FLAGS:
        setattr(obj, attr, flag_bool("ls3d_sector_flags1", mask, label, desc))

    obj.ls3d_portal_flags = IntProperty(default=C.DEFAULT_PORTAL_FLAGS)
    obj.ls3d_portal_flags_str = hex_string_property(
        "ls3d_portal_flags", "Raw Flags", "Portal flags as unsigned hex")
    # Stored squared, exactly like the LOD switch distance: it is compared
    # against a squared camera distance with no square root taken. Every
    # shipped portal that uses it holds 400.0, which is 20 m.
    obj.ls3d_portal_near = FloatProperty(
        name="View Distance (squared)", default=0.0,
        description="Raw stored value: the square of the view distance")

    def _portal_view_meters_get(self):
        raw = getattr(self, "ls3d_portal_near", 0.0)
        return math.sqrt(raw) if raw > 0.0 else 0.0

    def _portal_view_meters_set(self, value):
        self.ls3d_portal_near = value * value

    obj.ls3d_portal_view_dist_m = FloatProperty(
        name="View Distance", default=0.0, min=0.0, unit="LENGTH",
        description="How far the camera can get before the view through this "
                    "opening is cut back. Past it the sector behind counts "
                    "as barely visible, or is not drawn at all if Far Cull "
                    "is on. Zero means no limit",
        get=_portal_view_meters_get, set=_portal_view_meters_set)
    obj.ls3d_portal_far = FloatProperty(
        name="Second Range", default=0.0,
        description="Stored in the file but never read by the game. Kept so "
                    "the portal is written back unchanged")
    # Reference only. The exporter always recomputes the plane from the portal's
    # current geometry, so moving or reshaping a portal stays correct.
    obj.ls3d_portal_normal = FloatVectorProperty(
        name="Imported Plane Normal", subtype="XYZ", size=3, precision=8,
        description="Plane normal as it was read from the file. Not used on "
                    "export - the plane is recomputed from the geometry")
    obj.ls3d_portal_dot = FloatProperty(
        name="Imported Plane Distance", precision=8,
        description="Plane offset as it was read from the file. Not used on "
                    "export - the plane is recomputed from the geometry")
    obj.pf_enabled = flag_bool(
        "ls3d_portal_flags", C.PF_ENABLED, "Enable",
        "The opening is open and can be seen through. Off seals it like a "
        "solid wall and the sector beyond stops being drawn. Enabling one "
        "side also enables the matching portal on the other")
    obj.pf_far_cull = flag_bool(
        "ls3d_portal_flags", C.PF_FAR_CULL, "Far Cull",
        "Past the View Distance above, stop looking through this opening "
        "altogether - the sector behind it is not drawn at all. Without it "
        "the game still looks through, but treats the sector behind as "
        "barely visible")
    for mask, attr, label, desc in C.RESERVED_PORTAL_FLAGS:
        setattr(obj, attr, flag_bool("ls3d_portal_flags", mask, label, desc))


def _register_special_object_properties():
    obj = bpy.types.Object

    obj.rot_mode = EnumProperty(
        name="Rotation Mode",
        items=(("1", "All axes", "Face the camera freely"),
               ("2", "Single axis", "Rotate around one fixed axis")))
    obj.rot_axis = EnumProperty(
        name="Rotation Axis",
        items=(("1", "X", ""), ("2", "Z", ""), ("3", "Y", "")))

    obj.ls3d_mirror_color = FloatVectorProperty(
        name="Mirror Color", subtype="COLOR", size=3, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0),
        description="Shown where the reflection is out of range")
    # Past this distance the mirror stops reflecting. Zero is not "no limit":
    # the game compares the camera's distance against it, so a mirror left at
    # zero never reflects at all.
    obj.ls3d_mirror_range = FloatProperty(
        name="Reflection Range", min=0.0, unit="LENGTH",
        default=C.DEFAULT_MIRROR_RANGE,
        description="Past this distance from the camera the mirror stops "
                    "reflecting and draws as flat color. Zero means it never "
                    "reflects, so this starts at 100 - the engine's own "
                    "default, and what every mirror the game ships uses")

    # ── Light ─────────────────────────────────────────────────────────────
    # Eight fields, and every one of them is the truth: Blender's own light
    # datablock can hold exactly one of them without loss - the color - so there
    # is no point keeping one in step. Where the light sits and which way it
    # faces are the object's transform, as they are in the file.
    # The type is a whole word in the file and the engine's own switch has a
    # default case that does nothing, so a file naming a tenth kind still loads
    # and that light simply has no effect. The stored word is therefore kept as
    # it came and the dropdown is a view onto it, the same way the projector's
    # mode byte works: an unknown word reads as None, which is what the engine
    # makes of it, and picking from the list writes a word the engine knows.
    obj.ls3d_light_type_value = IntProperty(
        name="Light Type Value", default=C.DEFAULT_LIGHT_TYPE,
        description="Raw stored type word. Nine values mean something; any "
                    "other is carried exactly as it came and does nothing in "
                    "game",
        update=_viewport_changed)

    def _light_type(self):
        """The kind to show: the stored word, or None when it names no kind."""
        stored = _int_prop_value(self, "ls3d_light_type_value") & 0xFFFFFFFF
        return stored if stored < C.LIGHT_TYPE_COUNT else 0

    obj.ls3d_light_type = EnumProperty(
        name="Light Type", items=C.LIGHT_TYPE_ITEMS,
        get=_light_type,
        set=lambda self, value: setattr(self, "ls3d_light_type_value", value),
        description="Which of the nine kinds the engine knows",
        update=_viewport_changed)
    obj.ls3d_light_power = FloatProperty(
        name="Power", default=C.DEFAULT_LIGHT_POWER,
        description="Multiplier on the color. Not a physical quantity - the "
                    "engine multiplies the two and uses the result directly")
    obj.ls3d_light_color = FloatVectorProperty(
        name="Color", subtype="COLOR", size=3, min=0.0, max=1.0,
        default=C.DEFAULT_LIGHT_COLOR, description="The light's color",
        update=_viewport_changed)
    # The same two stored numbers mean three different things by kind, so the
    # panel labels them for the kind in hand; these descriptions cover all
    # three.
    obj.ls3d_light_range_near = FloatProperty(
        name="Near Range", min=0.0,
        default=C.DEFAULT_LIGHT_RANGE_NEAR,
        description="Point and spot: full strength within this distance, which "
                    "the frame's scale multiplies. Fog: where the fog starts, "
                    "as a percentage of where it is thickest. Point Ambient and "
                    "Point Fog: the fog's density, exactly as typed",
        update=_viewport_changed)
    obj.ls3d_light_range_far = FloatProperty(
        name="Far Range", min=0.0,
        default=C.DEFAULT_LIGHT_RANGE_FAR,
        description="Point and spot: faded to nothing by this distance, which "
                    "the frame's scale multiplies - linearly in between, which "
                    "is not what Blender's lights do. Fog: where the fog is "
                    "thickest, as a percentage of the view distance. Unused by "
                    "Point Ambient and Point Fog",
        update=_viewport_changed)
    obj.ls3d_light_cone_inner = FloatProperty(
        name="Inner Cone", min=0.0, subtype="ANGLE",
        default=C.DEFAULT_LIGHT_CONE_INNER,
        description="Full angle of the cone that is lit at full strength. The "
                    "file stores the whole angle, not the half-angle",
        update=_viewport_changed)
    obj.ls3d_light_cone_outer = FloatProperty(
        name="Outer Cone", min=0.0, subtype="ANGLE",
        default=C.DEFAULT_LIGHT_CONE_OUTER,
        description="Full angle of the cone's outer edge. Between the two the "
                    "light falls off; past it there is none",
        update=_viewport_changed)
    obj.ls3d_light_mode = IntProperty(
        name="Mode", default=C.DEFAULT_LIGHT_MODE,
        description="Raw mode word. Six of its bits are read; the rest are "
                    "never tested and are carried exactly as they came")
    obj.ls3d_light_mode_str = hex_string_property(
        "ls3d_light_mode", "Mode", "The light's mode word as unsigned hex")
    for bit, attr, label, desc in C.LIGHT_MODE_FLAGS:
        setattr(obj, attr, flag_bool("ls3d_light_mode", bit, label, desc))

    # What the viewport handles drag: the near and far distances and the two
    # cone widths, measured the way they are drawn - in world units along and
    # across the beam. Worked out from the stored values on every read and
    # written straight back into them, so nothing new is kept.
    light_handles = module("4ds.gizmo_light")
    for attr, reads, writes, label, desc in (
            ("ls3d_light_near_reach", light_handles.near_reach,
             light_handles.set_near_reach, "Near Distance",
             "The near range as a distance from the light, in world units"),
            ("ls3d_light_far_reach", light_handles.far_reach,
             light_handles.set_far_reach, "Far Distance",
             "The far range as a distance from the light, in world units"),
            ("ls3d_light_inner_radius", light_handles.inner_radius,
             light_handles.set_inner_radius, "Inner Cone Radius",
             "How wide the inner cone is where the beam ends, in world units"),
            ("ls3d_light_outer_radius", light_handles.outer_radius,
             light_handles.set_outer_radius, "Outer Cone Radius",
             "How wide the outer cone is where the beam ends, in world units")):
        setattr(obj, attr, FloatProperty(
            name=label, description=desc, unit="LENGTH",
            get=reads, set=writes))

    # ── Projector ─────────────────────────────────────────────────────────
    # One byte carries both the fade and the blend, and the game reads it by
    # comparing the whole value against the six it knows rather than by masking
    # bits. The two dropdowns below decode it the same way, so a byte the game
    # does not know shows what the game would actually do with it. It is left
    # alone in storage until one of them is used, and then a value the game
    # knows is written in its place.
    obj.ls3d_projector_mode = IntProperty(
        name="Projection Mode", default=0, min=0, max=255,
        description="Raw stored mode byte: the depth falloff and the blend "
                    "mode together")

    def _decode_mode(self):
        byte = _int_prop_value(self, "ls3d_projector_mode") & 0xFF
        return C.PROJECTOR_MODES.get(byte, C.PROJECTOR_MODE_FALLBACK)

    def _encode_mode(self, fade, covers):
        self.ls3d_projector_mode = C.PROJECTOR_MODE_BYTES[(fade, covers)]

    obj.ls3d_projector_falloff = EnumProperty(
        name="Depth Falloff", items=C.PROJECTOR_FALLOFF_ITEMS,
        get=lambda self: _decode_mode(self)[0],
        set=lambda self, value: _encode_mode(self, value, _decode_mode(self)[1]),
        description="How the projected color is weighted across the depth of "
                    "the volume, from 0 at the near face to 1 at the far one")

    obj.ls3d_projector_blend = EnumProperty(
        name="Blend Mode", items=C.PROJECTOR_BLEND_ITEMS,
        get=lambda self: int(_decode_mode(self)[1]),
        set=lambda self, value: _encode_mode(self, _decode_mode(self)[0],
                                             bool(value)),
        description="How the projected triangles are combined with the surface "
                    "under them, and which channel the falloff weight drives")

    obj.ls3d_projector_orthogonal = BoolProperty(
        name="Straight On", default=True, update=_viewport_changed,
        description="Project straight ahead at one size all the way through, "
                    "rather than spreading out from the object's origin. All "
                    "five projectors the game makes for itself are set this "
                    "way")

    obj.ls3d_projector_material = PointerProperty(
        name="Material", type=bpy.types.Material,
        description="The material the projector paints with. A projector has "
                    "no mesh, so this never appears in Blender's Material tab")

    obj.ls3d_show_projector_material = BoolProperty(
        name="Projected Material", default=False,
        description="Edit the projected material's 4DS settings here. A "
                    "projector has no mesh, so its material never appears in "
                    "Blender's Material tab")

    obj.ls3d_glows = CollectionProperty(type=LS3DGlowElement)
    obj.ls3d_glows_index = IntProperty(default=0)
    obj.ls3d_show_glow_material = BoolProperty(
        name="Element Material", default=False,
        description="Edit the selected element's 4DS material here. A flare "
                    "has no mesh, so its materials never appear in Blender's "
                    "Material tab")
    obj.ls3d_target_flags = IntProperty(
        name="Flags", default=C.TF_LOOK_AT, min=0, max=65535,
        description="What the target does to the objects linked to it")
    obj.ls3d_target_flags_str = hex_string_property(
        "ls3d_target_flags", "Raw Flags", "Target flags as unsigned hex",
        width=16)
    for mask, attr, label, desc in C.TARGET_FLAGS:
        setattr(obj, attr, flag_bool("ls3d_target_flags", mask, label, desc))
    obj.ls3d_target_objects = CollectionProperty(
        name="Target Objects", type=LS3DTargetObject)
    obj.ls3d_target_objects_index = IntProperty(name="Active Index", default=0)
    obj.ls3d_target_add_name = StringProperty(
        name="Add Target", default="",
        description="Object name, or 'armature:bone' for a joint link")

    # ── Dummy box faces ───────────────────────────────────────────────────
    # One float per face, computed from the box rather than kept beside it, the
    # same way every flag checkbox in this addon is a view onto its flag word.
    # They exist so the box handles can hand Blender a real property to drag:
    # Blender's own gizmo template targets a property, and a target bound to
    # get/set handlers instead is read differently while a drag is running.
    gizmo_dummy = module("4ds.gizmo_dummy")

    def face_property(index):
        # Closed over by a factory, not by a default argument: Blender reads a
        # callback's signature and turns down a getter that takes more than the
        # one parameter it means to pass.
        def getter(self):
            return gizmo_dummy.face_offset(self, index)

        def setter(self, value):
            gizmo_dummy.set_face_offset(self, index, value)

        return FloatProperty(
            name="Face", unit="LENGTH", get=getter, set=setter,
            description="How far one face of a dummy's box sits from the "
                        "object's origin. Computed from the box, not stored "
                        "beside it")

    for index in range(len(gizmo_dummy.FACES)):
        setattr(obj, gizmo_dummy.FACE_PROPERTIES[index], face_property(index))

    def mirror_face_property(index):
        def getter(self):
            return gizmo_dummy.mirror_face_offset(self, index)

        def setter(self, value):
            gizmo_dummy.set_mirror_face_offset(self, index, value)

        return FloatProperty(
            name="Face", unit="LENGTH", get=getter, set=setter,
            description="How far one face of a mirror's view box sits from "
                        "the mirror's origin. Computed from the box, not "
                        "stored beside it")

    for index in range(len(gizmo_dummy.FACES)):
        setattr(obj, gizmo_dummy.MIRROR_FACE_PROPERTIES[index],
                mirror_face_property(index))

    # ── the active joint's influence box ──────────────────────────────────
    # On the armature rather than on the bone, because that is what a handle
    # can be pointed at: a gizmo target is a property on an object. Which
    # joint's box they work on is whichever bone is active, the one the
    # handles are drawn on.
    gizmo_influence = module("4ds.gizmo_influence")

    def active_bone_name(armature):
        bone = armature.data.bones.active if armature.type == "ARMATURE" else None
        return bone.name if bone is not None else None

    def box_handle_property(index, read, write, name, description):
        def getter(self):
            bone_name = active_bone_name(self)
            return read(self, bone_name, index) if bone_name else 0.0

        def setter(self, value):
            bone_name = active_bone_name(self)
            if bone_name:
                write(self, bone_name, index, value)

        return FloatProperty(name=name, unit="LENGTH", get=getter, set=setter,
                             description=description)

    for index in range(len(gizmo_dummy.FACES)):
        setattr(obj, gizmo_influence.FACE_PROPERTIES[index],
                box_handle_property(
                    index, gizmo_influence.face_offset,
                    gizmo_influence.set_face_offset, "Face",
                    "How far one face of the active joint's influence box "
                    "sits from the joint. Computed from the box, not stored "
                    "beside it"))
    for index in range(3):
        setattr(obj, gizmo_influence.MOVE_PROPERTIES[index],
                box_handle_property(
                    index, gizmo_influence.move_offset,
                    gizmo_influence.set_move_offset, "Move",
                    "Where the active joint's influence box sits along one of "
                    "its own axes. Computed from the box, not stored beside "
                    "it"))

    obj.ls3d_morph_groups = CollectionProperty(type=LS3DMorphGroup)
    obj.ls3d_active_morph_group = IntProperty(
        default=0, update=_on_active_morph_group_changed)


def _register_material_properties():
    materials = module("4ds.materials")
    rebuild_material_nodes = materials.rebuild_material_nodes
    sync_material_flags = materials.sync_material_flags

    mat = bpy.types.Material
    resync = lambda self, ctx: sync_material_flags(self)
    rebuild = lambda self, ctx: rebuild_material_nodes(self)

    mat.ls3d_ambient_color = FloatVectorProperty(
        name="Ambient", subtype="COLOR", min=0.0, max=1.0,
        default=(0.5, 0.5, 0.5), update=resync,
        description="Tints the light the surface picks up from its "
                    "surroundings, on a material with no texture, the same way "
                    "Diffuse tints the light falling on it. Blender lights its "
                    "own way, so the preview does not show it")
    mat.ls3d_diffuse_color = FloatVectorProperty(
        name="Diffuse", subtype="COLOR", min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0), update=resync,
        description="The color the game lights the surface in, on a material "
                    "with no texture - a texture replaces it outright, unless "
                    "Vertex Colors is on. The preview paints with it in the "
                    "same two cases")
    mat.ls3d_emission_color = FloatVectorProperty(
        name="Emission", subtype="COLOR", min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0), update=resync,
        description="Light the surface gives off. The game adds it to the "
                    "light landing on the surface and multiplies the texture "
                    "by the sum, so it brightens the texture rather than "
                    "laying a flat color over it - a lit window keeps its "
                    "panes. Any color but black also switches a faded surface "
                    "to additive blending, so it brightens what is behind it "
                    "rather than covering it")
    mat.ls3d_color_key = FloatVectorProperty(
        name="Color Key", size=3, default=(0.0, 0.0, 0.0))

    # The game reads BMP and TGA textures only, so a slot lists only those and
    # takes any other kind straight back out - see ops_texfix.
    texfix = module("4ds.ops_texfix")
    mat.ls3d_diffuse_tex = PointerProperty(
        name="Diffuse Texture", type=bpy.types.Image,
        poll=texfix.slot_poll, update=texfix.slot_update("ls3d_diffuse_tex"))
    mat.ls3d_alpha_tex = PointerProperty(
        name="Alpha Texture", type=bpy.types.Image,
        poll=texfix.slot_poll, update=texfix.slot_update("ls3d_alpha_tex"))
    mat.ls3d_env_tex = PointerProperty(
        name="Environment Texture", type=bpy.types.Image,
        poll=texfix.slot_poll, update=texfix.slot_update("ls3d_env_tex"))
    mat.ls3d_env_amount = FloatProperty(
        name="Env Intensity", default=0.0, min=0.0, max=1.0, update=resync)
    mat.ls3d_opacity = FloatProperty(
        name="Opacity", default=1.0, min=0.0, max=1.0, update=resync)

    # The file holds each frame count in a single byte, so 255 is the ceiling.
    # The counter that names the frames is only two digits wide, though, so a
    # count above 100 asks for names the game cannot spell - see the warning
    # the export raises.
    # The diffuse texture and the environment texture each have a full
    # animation channel, with the same three settings and their own copy of the
    # stepping code - so the two sets of descriptions below are deliberately
    # the same three sentences with the channel's name swapped, and differ only
    # in the last one, which says what the game's own files do with it.
    #
    # Nothing in the game animates its environment texture: across all 3237
    # shipping files that frame count never exceeds 1 and both of its timing
    # fields are always zero, which is exactly the state the game reads as "not
    # animated". Those three are carried so a file that does set them survives
    # a round trip, and the panel keeps them out of the way until one does.
    # Short descriptions: what the number is, and where an off-value does
    # something worth knowing. The naming rule is not repeated here - the panel
    # states it once, with an example, above the names it works out for this
    # material, and the ticks there say which channel is actually running.
    #
    # *channel* is the bare word, for where it reads as an adjective ("each
    # diffuse frame"); *texture* is the full noun. One string doing both jobs
    # gave "each diffuse texture frame" and "the texture on the texture".
    def animation_fields(channel, label, usage):
        texture = f"{channel} texture"
        frames, period, loop = usage
        return (
            IntProperty(
                name=f"{label}Frames", default=0, min=0, max=255,
                description=f"How many frames the {texture} animation "
                            f"has{frames}"),
            IntProperty(
                name=f"{label}Frame Time", default=0, min=0,
                description=f"How long each {channel} frame stays on screen, "
                            f"in milliseconds. 0 leaves the {texture} on its "
                            f"first frame{period}"),
            IntProperty(
                name=f"{label}Loop Back To", default=0, min=-1,
                description=f"The frame the {channel} animation jumps back to "
                            f"when it reaches the end, so an opening run can "
                            f"play once and the rest repeat. -1 shows a frame "
                            f"picked at random each time instead{loop}"),
        )

    (mat.ls3d_anim_frames, mat.ls3d_anim_period,
     mat.ls3d_anim_loop_start) = animation_fields("diffuse", "", ("", "", ""))

    (mat.ls3d_env_anim_frames, mat.ls3d_env_anim_period,
     mat.ls3d_env_anim_loop_start) = animation_fields(
        "environment", "Env ",
        (". Nothing in the game animates its environment texture", "", ""))

    # Panel state, not the file's: how the frames have to be named is worth
    # having to hand once and in the way thereafter.
    mat.ls3d_show_anim_info = BoolProperty(
        name="Info", default=False,
        description="Show how an animation's frame files have to be named")

    mat.ls3d_material_flags = IntProperty(
        name="Material Flags", default=0, update=resync)
    mat.ls3d_material_flags_str = hex_string_property(
        "ls3d_material_flags", "Raw Flags", "Material flags as unsigned hex")

    mat.ls3d_env_tiling = flag_int(
        "ls3d_material_flags", C.MTL_ENV_TILE_MASK, 0, "Env Tiling",
        "Environment texture repeat count across the surface",
        minimum=0, maximum=255)
    mat.ls3d_env_blend = flag_enum(
        "ls3d_material_flags", C.MTL_ENV_BLEND_MASK, C.MTL_ENV_BLEND_SHIFT,
        C.ENV_BLEND_ITEMS, "Env Blend Type",
        "How the environment texture is combined with the surface below")
    mat.ls3d_env_uv_mode = flag_enum(
        "ls3d_material_flags", C.MTL_ENV_UV_MASK, C.MTL_ENV_UV_SHIFT,
        C.ENV_UV_ITEMS, "Env Mapping Mode",
        "How environment texture coordinates are generated. Reflect modes "
        "follow the camera; planar modes are locked to world space. Axis "
        "letters are Blender's - each option also names the game's own")

    for bit, attr, label, desc in (
        (C.MTL_TEXTURE_MANAGER, "ls3d_flag_texture_manager", "Texture Manager",
         "Load through the engine's texture manager. Unused in the original "
         "game"),
        (C.MTL_ALPHA_ENABLE, "ls3d_flag_alpha_enable", "Alpha Enable",
         "Keep the texture's pixels in memory, which is what lets shots pass "
         "through see-through texels instead of hitting them. It is set "
         "alongside Use Alpha Texture on every transparent material in the "
         "game, but it is not what makes the surface transparent and the "
         "preview does not change with it"),
        (C.MTL_DISABLE_U_TILING, "ls3d_flag_disable_u_tiling", "Disable U Tiling",
         "Clamp texture addressing horizontally instead of repeating"),
        (C.MTL_DISABLE_V_TILING, "ls3d_flag_disable_v_tiling", "Disable V Tiling",
         "Clamp texture addressing vertically instead of repeating"),
        (C.MTL_DIFFUSE_ENABLE, "ls3d_flag_diffuse_enable", "Use Diffuse Texture",
         "Use the diffuse texture. Off omits its name from the exported "
         "file"),
        (C.MTL_ENV_ENABLE, "ls3d_flag_env_enable", "Use Environment Texture",
         "Use the environment texture for reflection and shine"),
        (C.MTL_DIFFUSE_MIPMAP, "ls3d_flag_diffuse_mipmap", "Diffuse MipMap",
         "Build mipmaps for the diffuse texture, so it stays smooth rather than "
         "shimmering in the distance. The environment texture gets them "
         "whatever this says. On a color-keyed material it also switches the "
         "material to alpha blending"),
        (C.MTL_FORCE_TRUECOLOR, "ls3d_flag_force_truecolor", "Force Truecolor",
         "Always load at full color depth and skip texture compression, "
         "ignoring the memory-saving setting"),
        (C.MTL_NO_COMPRESSION, "ls3d_flag_no_compression", "No Compression",
         "Keep the texture uncompressed even when texture compression is "
         "enabled"),
        (C.MTL_NO_CACHED_TEXTURE, "ls3d_flag_no_cached_texture", "No Presaved Texture",
         "Read the original texture file instead of the pre-converted "
         "cache"),
        (C.MTL_ALPHA_IN_TEX, "ls3d_flag_alpha_in_tex", "Truecolor Texture",
         "Load the texture at full color depth instead of through a "
         "palette"),
        (C.MTL_ALPHA_ANIMATED, "ls3d_flag_alpha_animated", "Anim Alpha",
         "Step the alpha texture through its own frames alongside the diffuse "
         "one, numbering it the same way. Off holds the alpha texture on the "
         "one name it carries while the diffuse animates under it. This does "
         "nothing on its own - Anim Diffuse is what makes a material animate "
         "at all"),
        (C.MTL_DIFFUSE_ANIMATED, "ls3d_flag_diffuse_animated", "Anim Diffuse",
         "Step the diffuse texture through numbered frames. The game numbers "
         "them by overwriting the two characters just before the extension "
         "with a count from 00 upward, so the name here is already frame 00 - "
         "'2FIRESM00.BMP' runs 2FIRESM00 to 2FIRESM15, and 'FLAME1_0000.BMP' "
         "runs FLAME1_0000 to FLAME1_0004, since only the last two digits are "
         "the counter. Two digits is the whole width, so 100 frames is the "
         "ceiling, and a name shorter than six characters has nowhere to put "
         "the number"),
        (C.MTL_DIFFUSE_COLORED, "ls3d_flag_diffuse_colored", "Vertex Colors",
         "Tint lighting with the material's ambient, diffuse and emission "
         "colors"),
        (C.MTL_DIFFUSE_DOUBLESIDED, "ls3d_flag_diffuse_doublesided", "Double Sided",
         "Disable backface culling"),
        (C.MTL_ALPHA_COLORKEY, "ls3d_flag_alpha_colorkey", "Color Key",
         "Treat the texture's key color as fully transparent. The key color is "
         "the first entry in an 8-bit texture's indexed color table (its "
         "palette), not a color picked here - repaint that entry to change it. "
         "A texture with no color table (16, 24 or 32-bit) has no key entry, "
         "and the game makes every pixel with no blue in it transparent "
         "instead"),
        (C.MTL_ALPHATEX, "ls3d_flag_alphatex", "Use Alpha Texture",
         "Use a separate alpha texture. Requires Alpha Enable"),
        (C.MTL_ALPHA_ADDITIVE, "ls3d_flag_alpha_additive", "Alpha Additive",
         "Adds the texture to what is behind it (source alpha plus "
         "destination), so black turns invisible. Suits fire, glass and "
         "light glows"),
    ):
        setattr(mat, attr, flag_bool("ls3d_material_flags", bit, label, desc))


def _register_bone_properties():
    bone = bpy.types.PoseBone
    bone.cull_flags = IntProperty(name="Culling Flags",
                                  default=C.DEFAULT_CULL_FLAGS_JOINT,
                                  min=0, max=255)
    bone.cull_flags_str = hex_string_property(
        "cull_flags", "Raw Flags", "Frame flags as unsigned hex", width=8)
    bone.user_props = StringProperty(name="User Props", default="")
    for bit, attr, label in (
        (C.CF_ENABLED, "cf_enabled", "Enable"),
        (C.CF_POS_LOCKED, "cf_pos_locked", "Position Locked"),
    ):
        setattr(bone, attr, flag_bool("cull_flags", bit, label))
    # A joint carries the same culling byte as any other frame.
    for mask, attr, label, desc in C.RESERVED_CULL_FLAGS:
        setattr(bone, attr, flag_bool("cull_flags", mask, label, desc))

    # ── the joint's influence box ─────────────────────────────────────────
    # The sixteen numbers as the file holds them, so a box nobody has touched
    # goes back out as it came in, and a switch for whether the joint has one:
    # a joint painted by hand needs none.
    influence = module("4ds.influence")
    setattr(bone, C.JOINT_BOX_PROP, FloatVectorProperty(
        name="Influence Box", size=16, default=C.DEFAULT_JOINT_BOX,
        description="The box a joint's weights are made from when it has none "
                    "painted, in the joint's own space, as the file holds it"))
    setattr(bone, C.HAS_JOINT_BOX_PROP, BoolProperty(
        name="Has Influence Box", default=False,
        description="Whether this joint carries an influence box"))

    # What the panel edits: the same box read as somewhere, a size and a turn.
    # Computed from the numbers rather than kept beside them, so there is one
    # box and no second copy of it to disagree.
    def part_property(index, part, name, description, **kind):
        def getter(self):
            return influence.bone_parts(self)[part][index]

        def setter(self, value):
            parts = list(influence.bone_parts(self)[part])
            parts[index] = value
            influence.set_bone_parts(self, **{("center", "size", "turn")[part]:
                                              parts})

        return FloatProperty(name=name, get=getter, set=setter,
                             description=description, **kind)

    for index, axis in enumerate("XYZ"):
        setattr(bone, f"ls3d_box_center_{index}", part_property(
            index, 0, f"Center {axis}", "Where the box sits in the joint's "
            "own space", unit="LENGTH"))
        setattr(bone, f"ls3d_box_size_{index}", part_property(
            index, 1, f"Size {axis}", "How wide the box is along its own "
            "axis; its local Y is the axis the weight runs along",
            unit="LENGTH"))
        setattr(bone, f"ls3d_box_turn_{index}", part_property(
            index, 2, f"Turn {axis}", "How the box is turned in the joint's "
            "own space", unit="ROTATION", subtype="ANGLE"))


#: PropertyGroups must be registered before anything points at them.
CLASSES = (LS3DNamedEvent, LS3DTargetObject, LS3DGlowElement,
           LS3DMorphTarget, LS3DMorphGroup)

#: Every reserved-bit and target switch name, so unregister cleans them up too.
_RESERVED_ATTRS = [attr for _mask, attr, _label, _desc in
                   (C.RESERVED_CULL_FLAGS + C.RESERVED_RENDER_FLAGS
                    + C.RESERVED_SECTOR_FLAGS + C.RESERVED_PORTAL_FLAGS
                    + C.TARGET_FLAGS)]

#: Attributes added to bpy.types, removed again on unregister.
_OWNED = {
    bpy.types.Scene: ["ls3d_animated_object_count",
                      C.SHOW_INFLUENCE_BOXES_PROP,
                      C.INFLUENCE_BOXES_IN_FRONT_PROP,
                      C.INFLUENCE_HANDLES_PROP, C.JOINT_DISPLAY_SCALE_PROP,
                      "ls3d_motion_period", "ls3d_action_index",
                      "ls3d_action_show_all", "ls3d_targets_ignored",
                      "ls3d_shadow_timestamp",
                      "ls3d_event_kind"],
    bpy.types.Object: [
        "ls3d_frame_type_override", "ls3d_frame_type", "visual_type",
        "ls3d_lod_dist", "ls3d_lod_distance_m", "ls3d_user_props",
        "bbox_min", "bbox_max",
        "cull_flags", "cull_flags_str", "cf_enabled", "cf_pos_locked",
        "render_flags", "render_flags_str", "rf1_managed_lod", "rf1_no_mirror",
        "rf1_world_space", "rf1_flat_light",
        "render_flags2", "render_flags2_str", "rf2_zbias", "rf2_shadow_diffuse", "rf2_shadow_alpha",
        "rf2_is_mesh_object", "rf2_no_twosided_collision",
        "rf2_projection_diffuse", "rf2_projection_alpha", "rf2_no_fog",
        "ls3d_sector_flags1", "ls3d_sector_flags2", "ls3d_sector_flags1_str",
        "ls3d_sector_flags2_str", "sf_occluder", "sf_small_portal_cull",
        "sf_sound_reverb",
        "ls3d_portal_flags", "ls3d_portal_flags_str", "ls3d_portal_near",
        "ls3d_portal_far",
        "ls3d_portal_normal", "ls3d_portal_dot", "pf_enabled", "pf_far_cull",
        "rot_mode", "rot_axis", "ls3d_mirror_color", "ls3d_mirror_range",
        "ls3d_mirror_box_center", "ls3d_mirror_box_x", "ls3d_mirror_box_y",
        "ls3d_mirror_box_z", *(f"ls3d_mirror_face_{i}" for i in range(6)),
        "ls3d_light_type", "ls3d_light_type_value",
        "ls3d_light_power", "ls3d_light_color",
        "ls3d_light_range_near", "ls3d_light_range_far",
        "ls3d_light_cone_inner", "ls3d_light_cone_outer",
        "ls3d_light_mode", "ls3d_light_mode_str",
        "ls3d_light_near_reach", "ls3d_light_far_reach",
        "ls3d_light_inner_radius", "ls3d_light_outer_radius",
        *(attr for _b, attr, _l, _d in C.LIGHT_MODE_FLAGS),
        "ls3d_projector_mode", "ls3d_projector_falloff",
        "ls3d_projector_blend", "ls3d_projector_orthogonal",
        "ls3d_projector_material", "ls3d_show_projector_material",
        *(f"ls3d_dummy_face_{i}" for i in range(6)),
        *(f"ls3d_box_face_{i}" for i in range(6)),
        *(f"ls3d_box_move_{i}" for i in range(3)),
        "ls3d_glows", "ls3d_glows_index", "ls3d_show_glow_material",
        "ls3d_target_flags", "ls3d_target_flags_str", "ls3d_target_objects",
        "ls3d_target_objects_index",
        "ls3d_target_add_name",
        "ls3d_morph_groups", "ls3d_active_morph_group",
        *_RESERVED_ATTRS,
        "ls3d_anim_auto_flags", "ls3d_anim_flags", "ls3d_anim_flags_str",
        "af_rotation", "af_position", "af_scale", "af_note",
        "af_note_string",
        "ls3d_is_shadow",
        "ls3d_named_events", "ls3d_named_event_index",
        "ls3d_motion_enabled", "ls3d_motion_period",
        "ls3d_motion_base_position", "ls3d_motion_base_direction",
        "ls3d_is_motion_track",
        "ls3d_target_enabled",
    ],
    bpy.types.Material: [
        "ls3d_ambient_color", "ls3d_diffuse_color", "ls3d_emission_color",
        "ls3d_color_key", "ls3d_diffuse_tex", "ls3d_alpha_tex", "ls3d_env_tex",
        "ls3d_env_tiling", "ls3d_env_blend", "ls3d_env_uv_mode",
        "ls3d_flag_texture_manager",
        "ls3d_env_amount", "ls3d_opacity", "ls3d_anim_frames",
        "ls3d_anim_period", "ls3d_anim_loop_start", "ls3d_env_anim_frames",
        "ls3d_env_anim_period", "ls3d_env_anim_loop_start",
        "ls3d_show_anim_info",
        "ls3d_material_flags", "ls3d_material_flags_str",
        "ls3d_flag_alpha_enable",
        "ls3d_flag_disable_u_tiling", "ls3d_flag_disable_v_tiling",
        "ls3d_flag_diffuse_enable", "ls3d_flag_env_enable",
        "ls3d_flag_diffuse_mipmap", "ls3d_flag_alpha_in_tex",
        "ls3d_flag_alpha_animated", "ls3d_flag_diffuse_animated",
        "ls3d_flag_diffuse_colored", "ls3d_flag_diffuse_doublesided",
        "ls3d_flag_alpha_colorkey", "ls3d_flag_alphatex",
        "ls3d_flag_alpha_additive",
    ],
    bpy.types.PoseBone: [
        "cull_flags", "cull_flags_str", "user_props", "cf_enabled",
        "cf_pos_locked",
        C.JOINT_BOX_PROP, C.HAS_JOINT_BOX_PROP,
        *(f"ls3d_box_{part}_{index}"
          for part in ("center", "size", "turn") for index in range(3)),
        *[a for _m, a, _l, _d in C.RESERVED_CULL_FLAGS],
        "ls3d_anim_auto_flags", "ls3d_anim_flags", "ls3d_anim_flags_str",
        "af_rotation", "af_position", "af_scale", "af_note",
        "af_note_string",
    ],
}


def _motion_toggled(self, _context):
    """Mute or unmute a movement empty's animation, live.

    Muting rather than deleting: the travel is still there and comes back the
    moment the box is ticked, but the model animates on the spot, which is what
    anyone editing a walk cycle wants to see.
    """
    animation_data = getattr(self, "animation_data", None)
    if animation_data is not None:
        animation_data.use_tweak_mode = False
        for track in animation_data.nla_tracks:
            track.mute = not self.ls3d_motion_enabled
        if animation_data.action is not None:
            anim_io = module("5ds.io")
            bag = anim_io.channelbag(self)
            if bag is not None:
                for fcurve in bag.fcurves:
                    fcurve.mute = not self.ls3d_motion_enabled
    # The transform itself is left alone. Muting is what stops the travel -
    # the empty simply holds where it had got to and the model animates on the
    # spot there. Writing a zero in would move the model, and worse, that zero
    # is what stays on screen when the box is ticked again until something
    # forces a re-evaluation.
    self.update_tag()
    scene = getattr(_context, "scene", None)
    if scene is not None:
        # Re-run the animation for the current frame, so the switch shows
        # immediately in both directions rather than on the next scrub.
        scene.frame_set(scene.frame_current)


def _register_target_aim_property():
    """Whether a target frame actually aims the things linked to it."""
    _on_target_enabled = module("4ds.ops_target")._on_target_enabled

    bpy.types.Object.ls3d_target_enabled = BoolProperty(
        name="Aim At This Target", default=True, update=_on_target_enabled,
        description="Turn the linked objects to face this target. Off leaves "
                    "the links alone - they still export - but stops them "
                    "steering anything, so an animation can turn those objects "
                    "itself while it is being worked on")


def _register_event_property():
    """Which event a new cue carries."""
    event_items = module("5ds.ops").event_items

    bpy.types.Scene.ls3d_event_kind = EnumProperty(
        name="Event", items=event_items,
        description="What the cue tells the game to do when the animation "
                    "reaches it")


def _register_motion_properties():
    """The movement track that rides above a model, and how it is sampled."""
    DEFAULT_KEY_PERIOD = module("tck.codec").DEFAULT_KEY_PERIOD

    def _motion_track_marked(self, context):
        """Keep the scene down to one movement track.

        A .tck holds a single track - the file has no count and no name table,
        just one set of keys - so a second could never be written. Marking one
        unmarks the rest rather than leaving the export to choose.
        """
        if not getattr(self, "ls3d_is_motion_track", False):
            return
        scene = getattr(context, "scene", None)
        for other in (scene.objects if scene is not None
                      else bpy.data.objects):
            if other is not self and getattr(other, "ls3d_is_motion_track",
                                             False):
                # Setting it False re-enters here and returns straight away.
                other.ls3d_is_motion_track = False

    bpy.types.Object.ls3d_is_shadow = BoolProperty(
        name="Shadow Piece", default=False,
        description="This mesh is part of the model's shadow rather than the "
                    "model. It is written into the .6ds beside the .4ds and "
                    "never into the model itself, and the game matches it to "
                    "a frame by name")

    bpy.types.Object.ls3d_is_motion_track = BoolProperty(
        name="Movement Track", default=False, update=_motion_track_marked,
        description="This empty carries where the actor travels while the "
                    "animation plays, and is written as the .tck beside it. "
                    "One per scene, because a .tck holds a single track, and "
                    "what marks it is this switch rather than its name")

    bpy.types.Object.ls3d_motion_enabled = BoolProperty(
        name="Show Track's Movement", default=True, update=_motion_toggled,
        description="Let this animation's travel move the model. Off holds it "
                    "where it stands, which is easier to animate against - "
                    "the movement is only muted, nothing is moved, and it "
                    "comes straight back")
    bpy.types.Object.ls3d_motion_period = IntProperty(
        name="Sample Every", default=DEFAULT_KEY_PERIOD, min=1, max=1000,
        subtype="TIME_ABSOLUTE",
        description="Milliseconds between one movement sample and the next. "
                    "The game's own tracks use 20, some 10 or 40, and an "
                    "animation's frames are 40 ms apart")
    bpy.types.Object.ls3d_motion_base_position = FloatVectorProperty(
        name="Hands Over From", size=3, subtype="XYZ", default=(0.0, 0.0, 0.0),
        description="Where this animation leaves the actor when another one "
                    "takes over. The game subtracts it from the incoming "
                    "animation's own entry point and eases the difference in, "
                    "so nothing jumps at the handover")
    bpy.types.Object.ls3d_motion_base_direction = FloatVectorProperty(
        name="Picked Up At", size=3, subtype="XYZ", default=(0.0, 0.0, 0.0),
        description="Where this animation wants the actor to be when it takes "
                    "over from another. Despite the file calling it a "
                    "direction it holds a point, not a facing")
    bpy.types.Scene.ls3d_action_index = IntProperty(
        name="Animation", default=0, min=0,
        description="Which animation the list is pointing at. Only the "
                    "highlight - use Make Active to actually play one")
    bpy.types.Object.ls3d_named_events = CollectionProperty(
        type=LS3DNamedEvent, name="Named Events")
    bpy.types.Object.ls3d_named_event_index = IntProperty(
        name="Named Event", default=0, min=0,
        description="Which named cue the list is pointing at")
    def _targets_ignored(self, context):
        """Re-aim, or stop aiming, everything the scene targets."""
        module("4ds.ops_target").apply_all_target_aims(self)

    bpy.types.Scene.ls3d_shadow_timestamp = StringProperty(
        name="Shadow Stamp", default="",
        description="The write time a .6ds carries, as hex. The game never "
                    "reads it - the loader passes no time to compare against "
                    "- but it is real data, so an imported shadow keeps its "
                    "own. Empty means stamp the file as it is written")

    bpy.types.Scene.ls3d_targets_ignored = BoolProperty(
        name="Ignore Target Frames", default=False, update=_targets_ignored,
        description="Stop every target frame steering the objects linked to "
                    "it, so an animation can turn them itself while it is "
                    "worked on. The links are untouched and still export - "
                    "this only decides whether they aim anything right now")

    bpy.types.Scene.ls3d_action_show_all = BoolProperty(
        name="Every Action", default=False,
        description="List every action rather than one per animation. An "
                    "animation driving several things keeps an action for "
                    "each, and normally only one stands for it")
    bpy.types.Scene.ls3d_motion_period = IntProperty(
        name="Movement Sampled Every", default=DEFAULT_KEY_PERIOD,
        min=1, max=1000, subtype="TIME_ABSOLUTE",
        description="Milliseconds between one movement sample and the next "
                    "when writing a .tck. The game's own tracks use 20, some "
                    "10 or 40")


def _register_animation_properties():
    """Which key kinds a 5DS track writes, on objects and on joints alike.

    An animation's flag word says which arrays a track carries. Worked out from
    the keys, it always agrees with them, which is what every animation the
    game ships does and what anyone editing one wants. Set by hand it can say
    something else - a flag over an empty array, or keys the flags leave out -
    which the game accepts and which is occasionally what a modder is after.
    """
    anim = module("5ds.codec")

    for owner in (bpy.types.Object, bpy.types.PoseBone):
        owner.ls3d_anim_auto_flags = BoolProperty(
            name="Automatic Key Flags", default=True,
            description="Work out which kinds of key this track writes from "
                        "the keys it actually has. Off lets the flags be set "
                        "by hand, which can write a flag over no keys or "
                        "leave keyed movement out of the file")
        # The file holds a 32-bit word, so the property does too: the panel
        # shows the whole field and the checkboxes are views onto its bits.
        owner.ls3d_anim_flags = IntProperty(name="Key Flags", default=0)
        for mask, attr, label, description in anim.KEY_FLAG_TABLE:
            setattr(owner, f"af_{attr}",
                    flag_bool("ls3d_anim_flags", mask, label, description))
        owner.ls3d_anim_flags_str = hex_string_property(
            "ls3d_anim_flags", "Key Flags",
            "The flag word written for this track")


def register_properties():
    _register_scene_properties()
    _register_frame_properties()
    _register_sector_properties()
    _register_special_object_properties()
    _register_material_properties()
    _register_bone_properties()
    _register_animation_properties()
    _register_motion_properties()
    _register_target_aim_property()
    _register_event_property()


def unregister_properties():
    for owner, names in _OWNED.items():
        for attr in names:
            if hasattr(owner, attr):
                try:
                    delattr(owner, attr)
                except (AttributeError, RuntimeError):
                    pass
