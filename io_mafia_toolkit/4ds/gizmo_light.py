"""Handles for a light: one to aim it, and four to size it.

A 4DS light has no direction field: a spot or a directional light shines
the way its frame faces, and the engine reads that off the frame's own
transform. So aiming one means turning the object, which you can do with R -
but pointing a beam at something by typing a rotation is not how anyone wants
to work. The aim handle is a ball out along the beam that turns the light to
face wherever you drag it; what moves is the object's rotation, which is the
thing the file stores.

The other four are what Blender's own spot light has handles for: the near and
far range, as boxes on the beam that slide along it, and the inner and outer
cone, as boxes on the rim of each cone at the far end that slide out across it.
A point light gets the two range boxes. Each drags one stored value and nothing
else, through a view of it measured the way it is drawn - see
``near_reach`` and the rest - so nothing about the handles is kept.

The boxes are the dummy's own handle, dragged the same way; only where they sit
and what they move is new here.
"""

import math

import bpy
from bpy_extras.view3d_utils import (region_2d_to_origin_3d,
                                     region_2d_to_vector_3d)
from mathutils import Matrix, Vector

from ..common import constants as C

#: What the handle sits at when the beam drawn for the light has no length to
#: speak of - a spot whose far range was left at zero, say. Without this the
#: handle would be inside the light's own marker and impossible to grab.
AIM_REACH_FALLBACK = 2.0

#: How the handle is drawn at rest, and under the cursor.
AIM_COLOR = (1.0, 0.88, 0.4)
AIM_COLOR_HIGHLIGHT = (1.0, 0.627451, 0.156863)
AIM_ALPHA = 0.8
AIM_ALPHA_HIGHLIGHT = 1.0

#: Size of the grab ball, before Blender scales it to hold a steady size on
#: screen.
AIM_SCALE = 0.12

#: The kinds that have a direction worth aiming. A point light shines every way
#: at once and the rest are not lighting at all.
AIMABLE = (C.LIGHT_SPOT, C.LIGHT_DIRECTIONAL)

#: How far past the far range handle the aim ball sits on a spot, in pixels, so
#: the two never cover each other however far the view is zoomed out.
AIM_GAP_PIXELS = 36.0

#: The widest a cone handle drags a cone to, as a full angle. A cone of half a
#: turn has no rim to put a handle on - its edge runs off to infinity - so the
#: drag stops just short of it. A wider angle can still be typed.
CONE_HANDLE_MAX = math.radians(179.0)

#: How short the beam can get before the cone handles have nothing to measure
#: a width against, and step out of the way.
MIN_HANDLE_REACH = 1.0e-4


def _int_prop(obj, name, default=0):
    try:
        return int(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


def _float_prop(obj, name, default=0.0):
    try:
        return float(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


def highlight_color(context):
    """The color the ball takes under the cursor, from the user's own theme."""
    try:
        return tuple(context.preferences.themes[0].view_3d.object_active)
    except (AttributeError, IndexError, TypeError):
        return AIM_COLOR_HIGHLIGHT


def is_aimable_light(obj):
    """True for a light whose heading means something."""
    return bool(
        obj is not None
        and _int_prop(obj, "ls3d_frame_type", -1) == C.FRAME_LIGHT
        and (_int_prop(obj, "ls3d_light_type_value",
                       C.DEFAULT_LIGHT_TYPE) & 0xFFFFFFFF) in AIMABLE
    )


def aim_reach(obj):
    """How far along the beam the handle sits, in world units.

    On the end of what is actually drawn for the light, so the handle reads as
    the tip of that beam rather than a bead floating near it: the middle of
    a spot's far cap, or the point of a directional light's arrow.
    """
    from .viewport import LIGHT_DIRECTION_LENGTH

    kind = (_int_prop(obj, "ls3d_light_type_value", C.DEFAULT_LIGHT_TYPE)
            & 0xFFFFFFFF)
    if kind == C.LIGHT_DIRECTIONAL:
        # A directional light has no range - its arrow is drawn at a fixed
        # length, and the handle belongs on the point of it.
        return LIGHT_DIRECTION_LENGTH

    far = _float_prop(obj, "ls3d_light_range_far", C.DEFAULT_LIGHT_RANGE_FAR)
    far *= abs(obj.scale.x) or 1.0
    return far if far > 1.0e-4 else AIM_REACH_FALLBACK


def aim_position(obj, context=None):
    """Where the handle sits in world space: out along the light's own +Y.

    On a spot it steps a little past the tip, where the far range handle is,
    by a distance measured on screen - given a *context* to measure it in.
    """
    reach = aim_reach(obj)
    base = obj.matrix_world.normalized()
    kind = (_int_prop(obj, "ls3d_light_type_value", C.DEFAULT_LIGHT_TYPE)
            & 0xFFFFFFFF)
    region = getattr(context, "region", None)
    view = getattr(context, "region_data", None)
    if kind == C.LIGHT_SPOT and region is not None and view is not None:
        from .viewport import _pixel_size
        tip = base @ Vector((0.0, reach, 0.0))
        reach += AIM_GAP_PIXELS * _pixel_size(region, view, tip)
    return base @ Vector((0.0, reach, 0.0))


# ── Range and cone, as the handles measure them ──────────────────────────────
def is_ranged_light(obj):
    """True for a light whose near and far are distances: point and spot."""
    return bool(
        obj is not None
        and _int_prop(obj, "ls3d_frame_type", -1) == C.FRAME_LIGHT
        and (_int_prop(obj, "ls3d_light_type_value", C.DEFAULT_LIGHT_TYPE)
             & 0xFFFFFFFF) in C.LIGHT_RANGED_TYPES
    )


def _beam_scale(obj):
    """What the frame's scale multiplies both ranges by, as the drawing uses."""
    return abs(obj.scale.x) or 1.0


def near_reach(obj):
    """The near range as a distance from the light, in world units."""
    return _float_prop(obj, "ls3d_light_range_near") * _beam_scale(obj)


def set_near_reach(obj, value):
    """Move the near range to *value* world units, never past the far one."""
    far = _float_prop(obj, "ls3d_light_range_far")
    obj.ls3d_light_range_near = min(max(value / _beam_scale(obj), 0.0), far)


def far_reach(obj):
    """The far range as a distance from the light, in world units."""
    return _float_prop(obj, "ls3d_light_range_far") * _beam_scale(obj)


def set_far_reach(obj, value):
    """Move the far range to *value* world units, never inside the near one."""
    near = _float_prop(obj, "ls3d_light_range_near")
    obj.ls3d_light_range_far = max(value / _beam_scale(obj), near)


def _radius(angle, reach):
    return math.tan(min(max(angle, 0.0), CONE_HANDLE_MAX) * 0.5) * reach


def _angle(radius, reach):
    return 2.0 * math.atan(max(radius, 0.0) / reach)


def inner_radius(obj):
    """How wide the inner cone is where the beam ends, in world units."""
    return _radius(_float_prop(obj, "ls3d_light_cone_inner"), far_reach(obj))


def set_inner_radius(obj, value):
    """Widen the inner cone to *value* at the beam's end, never past the outer."""
    reach = far_reach(obj)
    if reach <= MIN_HANDLE_REACH:
        return
    outer = _float_prop(obj, "ls3d_light_cone_outer")
    obj.ls3d_light_cone_inner = min(_angle(value, reach), outer)


def outer_radius(obj):
    """How wide the outer cone is where the beam ends, in world units."""
    return _radius(_float_prop(obj, "ls3d_light_cone_outer"), far_reach(obj))


def set_outer_radius(obj, value):
    """Widen the outer cone to *value* at the beam's end, never inside the inner."""
    reach = far_reach(obj)
    if reach <= MIN_HANDLE_REACH:
        return
    inner = _float_prop(obj, "ls3d_light_cone_inner")
    obj.ls3d_light_cone_outer = min(max(_angle(value, reach), inner),
                                    CONE_HANDLE_MAX)


#: The four handles: the property each drags, and whether only a spot has it.
RANGE_HANDLES = (
    ("ls3d_light_near_reach", False),
    ("ls3d_light_far_reach", False),
    ("ls3d_light_inner_radius", True),
    ("ls3d_light_outer_radius", True),
)


def range_handle_matrices(obj):
    """Where each handle sits and which way it slides, in world space.

    Along the beam for the two ranges; out across it, on the rim of each cone
    at the far end, for the two cones - the outer one toward local +X and the
    inner one toward +Z, so the pair never sit on top of each other.
    """
    from .gizmo_dummy import FACE_ROTATIONS

    along, across_x, across_z = FACE_ROTATIONS[3], FACE_ROTATIONS[1], FACE_ROTATIONS[5]
    base = obj.matrix_world.normalized()
    far = far_reach(obj)
    return {
        "ls3d_light_near_reach":
            base @ Matrix.Translation((0.0, near_reach(obj), 0.0)) @ along,
        "ls3d_light_far_reach":
            base @ Matrix.Translation((0.0, far, 0.0)) @ along,
        "ls3d_light_inner_radius":
            base @ Matrix.Translation((0.0, far, inner_radius(obj))) @ across_z,
        "ls3d_light_outer_radius":
            base @ Matrix.Translation((outer_radius(obj), far, 0.0)) @ across_x,
    }


def _sphere_tris(segments=8, rings=6, radius=1.0):
    """A ball, as the triangles a custom gizmo shape wants."""
    def point(ring, segment):
        phi = math.pi * ring / rings
        theta = 2.0 * math.pi * segment / segments
        return Vector((math.sin(phi) * math.cos(theta) * radius,
                       math.cos(phi) * radius,
                       math.sin(phi) * math.sin(theta) * radius))

    tris = []
    for ring in range(rings):
        for segment in range(segments):
            a = point(ring, segment)
            b = point(ring + 1, segment)
            c = point(ring + 1, segment + 1)
            d = point(ring, segment + 1)
            tris += [a, b, c, a, c, d]
    return tris


AIM_TRIS = _sphere_tris()


def aim_at(obj, target):
    """Turn *obj* so its local +Y points at *target*, keeping everything else.

    Only the rotation changes. The light's position, its scale and every one of
    its eight settings are left exactly as they were - what is edited here is
    the one thing the file reads a direction from.
    """
    pivot = obj.matrix_world.translation
    direction = Vector(target) - pivot
    if direction.length <= 1.0e-6:
        return False

    # Track Y onto the direction, keeping Z as the up reference, which is the
    # same convention the rest of the addon aims frames with.
    rotation = direction.normalized().to_track_quat("Y", "Z")
    if obj.parent is not None:
        # The rotation that reaches the file is the local one, so the parent's
        # own turn has to come back out of it.
        parent = (obj.parent.matrix_world
                  @ obj.matrix_parent_inverse).to_quaternion()
        rotation = parent.inverted() @ rotation

    previous = obj.rotation_mode
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = rotation
    if previous != "QUATERNION":
        obj.rotation_mode = previous
    return True


def _redraw_everything(context):
    """Ask every editor showing the light to paint again."""
    area = getattr(context, "area", None)
    if area is not None:
        area.tag_redraw()
    manager = getattr(context, "window_manager", None)
    for window in getattr(manager, "windows", ()) or ():
        screen = getattr(window, "screen", None)
        for other in getattr(screen, "areas", ()) or ():
            if other.type in ("PROPERTIES", "VIEW_3D"):
                other.tag_redraw()


class LS3D_OT_LightAim(bpy.types.Operator):
    """Aim the active light without touching the viewport handle."""

    bl_idname = "ls3d.light_aim"
    bl_label = "Aim At Target"
    bl_description = ("Turn the light to face the other selected object, or "
                      "the 3D cursor when the light is all that is selected")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return is_aimable_light(getattr(context, "object", None))

    def execute(self, context):
        obj = context.object
        others = [other for other in context.selected_objects
                  if other is not obj]
        if len(others) > 1:
            self.report({"ERROR"},
                        "Select one thing to aim at, or nothing and the 3D "
                        "cursor is used.")
            return {"CANCELLED"}

        if others:
            target = others[0].matrix_world.translation
            what = others[0].name
        else:
            target = context.scene.cursor.location
            what = "the 3D cursor"

        if not aim_at(obj, target):
            self.report({"ERROR"}, f"'{obj.name}' is already at {what}, so "
                                   f"there is no direction to face.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"'{obj.name}' now faces {what}.")
        return {"FINISHED"}


class LS3D_GT_LightAim(bpy.types.Gizmo):
    """A ball out along the beam; drag it to turn the light toward it."""

    bl_idname = "LS3D_GT_light_aim"

    __slots__ = ("shape", "start_rotation")

    def _ball(self):
        if getattr(self, "shape", None) is None:
            self.shape = self.new_custom_shape("TRIS", AIM_TRIS)
        return self.shape

    def draw(self, context):
        self.draw_custom_shape(self._ball())

    def draw_select(self, context, select_id):
        self.draw_custom_shape(self._ball(), select_id=select_id)

    def invoke(self, context, event):
        obj = context.object
        self.start_rotation = (obj.rotation_mode,
                               tuple(obj.rotation_quaternion),
                               tuple(obj.rotation_euler))
        return {"RUNNING_MODAL"}

    def modal(self, context, event, tweak):
        obj = context.object
        region = getattr(context, "region", None)
        rv3d = getattr(context, "region_data", None)
        if obj is None or region is None or rv3d is None:
            return {"RUNNING_MODAL"}

        mouse = (event.mouse_region_x, event.mouse_region_y)
        # Aim at where the pointer is, read at the distance the handle already
        # sits at: the beam turns to follow the cursor without its length
        # wandering in and out as the view is orbited.
        origin = region_2d_to_origin_3d(region, rv3d, mouse)
        heading = region_2d_to_vector_3d(region, rv3d, mouse)
        if origin is None or heading is None:
            return {"RUNNING_MODAL"}

        pivot = obj.matrix_world.translation
        # The point on the pointer's ray nearest the sphere the handle is on.
        along = (pivot - origin).dot(heading)
        target = origin + heading * max(along, 1.0e-4)
        if aim_at(obj, target):
            _redraw_everything(context)
        return {"RUNNING_MODAL"}

    def exit(self, context, cancel):
        if not cancel:
            from .gizmo_dummy import push_undo
            push_undo("Aim light")
            return
        obj = context.object
        if obj is None or not hasattr(self, "start_rotation"):
            return
        mode, quaternion, euler = self.start_rotation
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = quaternion
        obj.rotation_mode = mode
        if mode != "QUATERNION":
            obj.rotation_euler = euler


class LS3D_GGT_LightAim(bpy.types.GizmoGroup):
    """The aim handle for the selected light."""

    bl_idname = "LS3D_GGT_light_aim"
    bl_label = "4DS Light Aim"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "object", None)
        return is_aimable_light(obj) and obj.select_get()

    def setup(self, context):
        gizmo = self.gizmos.new(LS3D_GT_LightAim.bl_idname)
        gizmo.color = AIM_COLOR
        gizmo.alpha = AIM_ALPHA
        gizmo.alpha_highlight = AIM_ALPHA_HIGHLIGHT
        gizmo.scale_basis = AIM_SCALE
        gizmo.use_draw_modal = True
        self.handle = gizmo
        self.draw_prepare(context)

    def draw_prepare(self, context):
        obj = context.object
        if obj is None:
            return
        # Rebuilt every draw: the handle rides the beam, so turning the light
        # moves it, and so does changing the far range it is placed from.
        self.handle.matrix_basis = Matrix.Translation(aim_position(obj, context))
        self.handle.color_highlight = highlight_color(context)


class LS3D_GGT_LightRange(bpy.types.GizmoGroup):
    """Near, far and cone handles for the selected point or spot light."""

    bl_idname = "LS3D_GGT_light_range"
    bl_label = "4DS Light Range"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "object", None)
        return is_ranged_light(obj) and obj.select_get()

    def setup(self, context):
        from .gizmo_dummy import (HANDLE_ALPHA, HANDLE_ALPHA_HIGHLIGHT,
                                  HANDLE_COLOR, HANDLE_SCALE,
                                  LS3D_GT_DummyHandle)
        self.handles = []
        for _prop, _spot_only in RANGE_HANDLES:
            gizmo = self.gizmos.new(LS3D_GT_DummyHandle.bl_idname)
            gizmo.color = HANDLE_COLOR
            gizmo.alpha = HANDLE_ALPHA
            gizmo.alpha_highlight = HANDLE_ALPHA_HIGHLIGHT
            gizmo.scale_basis = HANDLE_SCALE
            gizmo.use_draw_modal = True
            self.handles.append(gizmo)
        self.draw_prepare(context)

    def draw_prepare(self, context):
        """Put every handle back where its value now puts it.

        Each one is re-pointed at the active light rather than bound once, as
        the dummy's are, since the group outlives a change of active object.
        """
        obj = context.object
        if obj is None:
            return
        spot = ((_int_prop(obj, "ls3d_light_type_value", C.DEFAULT_LIGHT_TYPE)
                 & 0xFFFFFFFF) == C.LIGHT_SPOT)
        short = far_reach(obj) <= MIN_HANDLE_REACH
        matrices = range_handle_matrices(obj)
        highlight = highlight_color(context)
        for gizmo, (prop, spot_only) in zip(self.handles, RANGE_HANDLES):
            gizmo.target_set_prop("offset", obj, prop)
            gizmo.matrix_basis = matrices[prop]
            gizmo.color_highlight = highlight
            gizmo.hide = spot_only and (not spot or short)


CLASSES = (LS3D_OT_LightAim, LS3D_GT_LightAim, LS3D_GGT_LightAim,
           LS3D_GGT_LightRange)
