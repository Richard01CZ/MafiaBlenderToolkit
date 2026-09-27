"""Drag handles for a dummy's box.

A dummy is a named box, and the box is the whole of what it stores. Blender
draws an empty's cube uniformly and centered on the origin, which is exactly
18996 of the game's 19142 dummies and none of the other 146 - and even for the
ones it does draw, the only way to change the box was to type six numbers into
the panel.

So the box gets six handles instead, one per face, and dragging one moves that
face. The numbers stay in the panel, because typing an exact figure is still
the right way to enter an exact figure; this is for the times you are eyeing it
up against the model.

The handle is drawn and dragged here rather than by one of Blender's stock
gizmos. The stock arrow shows where a drag began as feedback, which on a handle
already sitting on the thing it moves reads as a second handle stuck behind the
first until you let go. Owning the draw means one handle is drawn, once,
wherever the face currently is.

Nothing here is stored. The handles are placed from the object's transform and
its box every time the view redraws, and dragging writes straight back into the
box the exporter reads.
"""

import math

import bpy
from bpy_extras.view3d_utils import (region_2d_to_origin_3d,
                                   region_2d_to_vector_3d)
from mathutils import Matrix, Vector

from ..common import constants as C

#: The six faces, as (axis, outward sign). Blender axis order.
FACES = ((0, -1), (0, 1), (1, -1), (1, 1), (2, -1), (2, 1))

#: The property each handle drags, one per face. Registered in ``properties``,
#: computed from the box rather than kept beside it.
FACE_PROPERTIES = tuple(f"ls3d_dummy_face_{index}" for index in range(len(FACES)))

#: How the handles are drawn at rest, and how solid they are. Muted on purpose:
#: at rest they should read as somewhere to grab, not compete with the box.
HANDLE_COLOR = (0.55, 0.55, 0.55)
HANDLE_ALPHA = 0.7
#: ... and under the cursor they take the theme's own selection color, so
#: grabbing one looks like grabbing anything else in Blender. The fallback is
#: Blender's default active orange, for when the theme cannot be read.
HANDLE_COLOR_HIGHLIGHT = (1.0, 0.627451, 0.156863)
HANDLE_ALPHA_HIGHLIGHT = 1.0

#: Size of the grab box, before Blender scales it to hold a steady size on
#: screen. Small enough to sit on a face without hiding it.
HANDLE_SCALE = 0.09

#: How close two opposite faces may come. A box that has been dragged inside
#: out is not something the format can express, so the drag stops here instead.
MIN_THICKNESS = 1.0e-4

#: How much finer a drag gets while the precise modifier is held.
PRECISE_FACTOR = 0.1


def push_undo(message):
    """Make what a drag just did a step of its own, for Ctrl+Z to take back.

    A gizmo writes straight into what it drags and nothing of that reaches the
    undo stack by itself: without this, taking a drag back steps over it to
    whatever was done before it instead. Pushed once the drag is let go, so a
    drag is one step however many times the pointer moved.
    """
    try:
        bpy.ops.ed.undo_push(message=message)
    except (RuntimeError, AttributeError):
        pass

#: How nearly the handle's axis may point at the camera - the square of the
#: sine of the angle between it and the view - before a place on screen says
#: nothing about a place along it, and the drag holds still rather than leaping.
EDGE_ON = 1.0e-6


def _rotation_onto(axis, sign):
    """A rotation taking the gizmo's own +Z onto *axis* in the given direction."""
    direction = Vector((0.0, 0.0, 0.0))
    direction[axis] = float(sign)
    return direction.to_track_quat("Z", "Y").to_matrix().to_4x4()


#: Built once: the six rotations never change, only the transform they hang off.
FACE_ROTATIONS = tuple(_rotation_onto(axis, sign) for axis, sign in FACES)


def _cube_tris(half=1.0):
    """A cube as triangles, which is what a custom gizmo shape wants."""
    corners = [Vector((x * half, y * half, z * half))
               for z in (-1.0, 1.0) for y in (-1.0, 1.0) for x in (-1.0, 1.0)]
    quads = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
             (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))
    tris = []
    for a, b, c, d in quads:
        tris += [corners[a], corners[b], corners[c],
                 corners[a], corners[c], corners[d]]
    return tris


def _cone_tris(radius=0.8, back=0.8, tip=1.6, segments=12):
    """A cone pointing along the gizmo's own +Z, as triangles.

    The handles that slide a box carry this instead of the grab box the ones
    that resize it carry: two kinds of handle sitting side by side on the same
    box should not be the same shape, or the only thing telling them apart is
    their color.
    """
    apex = Vector((0.0, 0.0, tip))
    middle = Vector((0.0, 0.0, -back))
    rim = []
    for step in range(segments):
        angle = math.tau * step / segments
        rim.append(Vector((math.cos(angle) * radius, math.sin(angle) * radius,
                           -back)))
    tris = []
    for step in range(segments):
        first, second = rim[step], rim[(step + 1) % segments]
        tris += [first, second, apex]           # the side
        tris += [second, first, middle]         # and the base, facing back
    return tris


HANDLE_TRIS = _cube_tris()
ARROW_TRIS = _cone_tris()


def is_dummy(obj):
    """True for a cube empty that stands for a dummy, and nothing else.

    A dummy is a box, so the cube is the only shape it can be. An empty of any
    other shape is somebody's own helper and gets no handles.
    """
    if obj is None or obj.type != "EMPTY":
        return False
    if obj.empty_display_type != "CUBE":
        return False
    try:
        return int(obj.ls3d_frame_type) == C.FRAME_DUMMY
    except (AttributeError, TypeError, ValueError):
        return False


def _box(obj):
    """The box the handles work on, from the one place that decides it.

    Read through the viewport's own accessor rather than off the properties, so
    an empty that has never been given a box - one you added yourself and set
    to Dummy - falls back to the size it is drawn at instead of collapsing every
    handle onto the origin.
    """
    from .viewport import dummy_box
    return dummy_box(obj)


def _axis_scale(obj, axis):
    return abs(obj.scale[axis]) or 1.0


def face_offset(obj, index):
    """How far this face is from the object's origin, in world units.

    Measured along the face's own outward direction, so a handle always pulls
    outward and both faces of a pair read positive. The origin is the reference
    because it is the one point in the box no drag can move: measure a face
    from the opposite face or from the box center and dragging one face shifts
    the number every other handle is working from.
    """
    axis, sign = FACES[index]
    low, high = _box(obj)
    face = high[axis] if sign > 0 else low[axis]
    return face * sign * _axis_scale(obj, axis)


def set_face_offset(obj, index, value):
    """Move one face to *value*, without letting it pass its opposite."""
    axis, sign = FACES[index]
    low, high = _box(obj)
    local = (value / _axis_scale(obj, axis)) * sign

    # Written back in full: the box may have been standing in for an empty that
    # never had one, and the moment it is dragged it becomes a real box.
    new_low, new_high = list(low), list(high)
    if sign > 0:
        new_high[axis] = max(local, low[axis] + MIN_THICKNESS)
    else:
        new_low[axis] = min(local, high[axis] - MIN_THICKNESS)
    obj.bbox_min = new_low
    obj.bbox_max = new_high


def handle_matrix(obj, index):
    """Where one handle sits and which way it points, in world space.

    Centered on the face it moves: a handle belongs to a face, so it sits in the
    middle of that face rather than wherever the object's origin happens to put
    it. The whole placement lives in this matrix and the handle is drawn at its
    origin, so there is one position to get right and nothing to displace it.

    The object's own scale is left out of the rotation and folded into the
    translation instead: a gizmo matrix carrying a non-uniform scale skews the
    handle and makes it awkward to hit.
    """
    axis, sign = FACES[index]
    low, high = _box(obj)

    center = Vector(((low[0] + high[0]) * 0.5 * _axis_scale(obj, 0),
                     (low[1] + high[1]) * 0.5 * _axis_scale(obj, 1),
                     (low[2] + high[2]) * 0.5 * _axis_scale(obj, 2)))
    center[axis] = ((high if sign > 0 else low)[axis]) * _axis_scale(obj, axis)

    return (obj.matrix_world.normalized()
            @ Matrix.Translation(center)
            @ FACE_ROTATIONS[index])


def highlight_color(context):
    """The color a handle takes under the cursor, from the user's own theme."""
    try:
        return tuple(context.preferences.themes[0].view_3d.object_active)
    except (AttributeError, IndexError, TypeError):
        return HANDLE_COLOR_HIGHLIGHT


def _along(region, rv3d, origin, axis, mouse):
    """How far along the line through *origin* the pointer at *mouse* is.

    The point on the line nearest the view's ray through the pointer, so the
    handle sits under the pointer wherever it is on screen and however the
    view is turned. ``None`` when the line points at the camera, where the
    pointer says nothing about a place along it.
    """
    start = region_2d_to_origin_3d(region, rv3d, mouse)
    ray = region_2d_to_vector_3d(region, rv3d, mouse)
    if start is None or ray is None:
        return None
    across = origin - start
    a, b, c = axis.dot(axis), axis.dot(ray), ray.dot(ray)
    d, e = axis.dot(across), ray.dot(across)
    square = a * c - b * b
    if square <= EDGE_ON * a * c:
        return None
    return (b * e - c * d) / square


def drag_distance(context, matrix, start_mouse, mouse):
    """How far along the handle's axis the pointer has been dragged.

    *matrix* is where the handle stood when the drag began: its own +Z is the
    axis, one world unit long. The pointer is followed to the nearest point on
    that axis, at the start and now, so the face under it stays under it. It
    used to be measured as pixels along the axis drawn a whole unit out, and in
    perspective a unit out is not the same size as near the handle: a face
    turned toward the view lagged the pointer and the face opposite ran ahead
    of it. Returns ``None`` when there is no view to measure in, or the axis
    points at the camera.
    """
    region = getattr(context, "region", None)
    rv3d = getattr(context, "region_data", None)
    if region is None or rv3d is None:
        return None
    origin = matrix.translation
    axis = matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))
    at_start = _along(region, rv3d, origin, axis, start_mouse)
    now = _along(region, rv3d, origin, axis, mouse)
    if at_start is None or now is None:
        return None
    return now - at_start


def _redraw_everything(context):
    """Ask every editor showing this box to paint again.

    Tagging the 3D view alone is not enough: the box's numbers are in the panel
    too, and a property written from a drag does not move the scene, so nothing
    tells the properties editor anything changed. It would sit on the old figures
    until something else happened to redraw it - deselecting the dummy, say.
    """
    area = getattr(context, "area", None)
    if area is not None:
        area.tag_redraw()
    manager = getattr(context, "window_manager", None)
    for window in getattr(manager, "windows", ()) or ():
        screen = getattr(window, "screen", None)
        for other in getattr(screen, "areas", ()) or ():
            if other.type in ("PROPERTIES", "VIEW_3D"):
                other.tag_redraw()


class LS3D_GT_DummyHandle(bpy.types.Gizmo):
    """One face of a dummy's box: a grab box, dragged along its own axis."""

    bl_idname = "LS3D_GT_dummy_handle"
    bl_target_properties = ({"id": "offset", "type": "FLOAT", "array_length": 1},)

    __slots__ = ("shape", "pointer", "start_value", "start_mouse",
                 "start_matrix")

    def _grab_box(self):
        if getattr(self, "shape", None) is None:
            self.shape = self.new_custom_shape(
                "TRIS",
                ARROW_TRIS if getattr(self, "pointer", False) else HANDLE_TRIS)
        return self.shape

    def draw(self, context):
        self.draw_custom_shape(self._grab_box())

    def draw_select(self, context, select_id):
        self.draw_custom_shape(self._grab_box(), select_id=select_id)

    def invoke(self, context, event):
        self.start_value = self.target_get_value("offset")
        self.start_mouse = (event.mouse_region_x, event.mouse_region_y)
        # The axis the drag runs along, fixed where the handle stood: the
        # handle moves with its face, but along this same line.
        self.start_matrix = self.matrix_basis.copy()
        return {"RUNNING_MODAL"}

    def modal(self, context, event, tweak):
        distance = drag_distance(context, self.start_matrix, self.start_mouse,
                                 (event.mouse_region_x, event.mouse_region_y))
        if distance is None:
            return {"RUNNING_MODAL"}
        if "PRECISE" in tweak:
            distance *= PRECISE_FACTOR
        value = self.start_value + distance
        if "SNAP" in tweak:
            value = round(value)
        self.target_set_value("offset", value)
        _redraw_everything(context)
        return {"RUNNING_MODAL"}

    def exit(self, context, cancel):
        if cancel:
            self.target_set_value("offset", self.start_value)
            return
        if self.target_get_value("offset") != self.start_value:
            push_undo("Drag handle")


class LS3D_GGT_DummyBox(bpy.types.GizmoGroup):
    """Face handles for the selected dummy's box."""

    bl_idname = "LS3D_GGT_dummy_box"
    bl_label = "4DS Dummy Box"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        # Active *and* selected: an object stays active after you click away
        # from it, and handles hanging off something you have deselected are
        # just clutter over whatever you moved on to.
        obj = getattr(context, "object", None)
        return is_dummy(obj) and obj.select_get()

    def setup(self, context):
        self.handles = []
        for _index in range(len(FACES)):
            gizmo = self.gizmos.new(LS3D_GT_DummyHandle.bl_idname)
            gizmo.color = HANDLE_COLOR
            gizmo.alpha = HANDLE_ALPHA
            gizmo.alpha_highlight = HANDLE_ALPHA_HIGHLIGHT
            gizmo.scale_basis = HANDLE_SCALE
            gizmo.use_draw_modal = True     # ours is the only drawing there is
            self.handles.append(gizmo)
        self.draw_prepare(context)

    def draw_prepare(self, context):
        """Put each handle back on the middle of its face before drawing.

        A face center moves whenever any face moves, and a box is a property
        nothing in the scene depends on, so nothing tells refresh() about it -
        this has to run on the way to every draw. The handle being dragged is
        included rather than skipped, because its whole position lives in this
        matrix and following the face is exactly what it should be doing.
        """
        obj = context.object
        if obj is None:
            return
        highlight = highlight_color(context)
        for index, gizmo in enumerate(self.handles):
            # Re-pointed rather than bound once: the group outlives a change of
            # active object, and a target bound in setup would go on editing
            # the dummy you left.
            gizmo.target_set_prop("offset", obj, FACE_PROPERTIES[index])
            gizmo.matrix_basis = handle_matrix(obj, index)
            gizmo.color_highlight = highlight


# ── a mirror's view box ───────────────────────────────────────────────────────
#: The property each of a view box's handles drags, one per face.
MIRROR_FACE_PROPERTIES = tuple(f"ls3d_mirror_face_{index}"
                               for index in range(len(FACES)))

#: The mirror property holding each of the view box's axes.
MIRROR_AXIS_PROPERTIES = ("ls3d_mirror_box_x", "ls3d_mirror_box_y",
                          "ls3d_mirror_box_z")


def _mirror_axis(obj, axis):
    """``(center, axis vector, unit direction, world length of that direction)``."""
    center = Vector(tuple(obj.ls3d_mirror_box_center))
    vector = Vector(tuple(getattr(obj, MIRROR_AXIS_PROPERTIES[axis])))
    if vector.length == 0.0:
        return center, vector, None, 1.0
    unit = vector.normalized()
    stretch = (obj.matrix_world.to_3x3() @ unit).length or 1.0
    return center, vector, unit, stretch


def mirror_face_offset(obj, index):
    """How far one face of a mirror's view box is from the mirror's origin.

    In world units, along the face's own outward direction, the way a dummy's
    face is measured.
    """
    axis, sign = FACES[index]
    center, vector, unit, stretch = _mirror_axis(obj, axis)
    if unit is None:
        return 0.0
    face = center.dot(unit) + sign * vector.length
    return face * sign * stretch


def set_mirror_face_offset(obj, index, value):
    """Move one face of the view box, keeping the opposite face where it is."""
    axis, sign = FACES[index]
    center, vector, unit, stretch = _mirror_axis(obj, axis)
    if unit is None:
        return
    middle = center.dot(unit)
    opposite = middle - sign * vector.length
    face = (value / stretch) * sign
    half = max((face - opposite) * sign * 0.5, MIN_THICKNESS * 0.5)
    new_middle = opposite + sign * half
    obj.ls3d_mirror_box_center = center + unit * (new_middle - middle)
    setattr(obj, MIRROR_AXIS_PROPERTIES[axis], unit * half)


def mirror_handle_matrix(obj, index):
    """Where a view box handle sits - the middle of its face - and where it points."""
    axis, sign = FACES[index]
    center, vector, unit, _stretch = _mirror_axis(obj, axis)
    world = obj.matrix_world
    position = world @ (center + vector * sign)
    direction = (world.to_3x3() @ (vector * sign))
    if direction.length == 0.0:
        direction = Vector((0.0, 0.0, 1.0))
    turn = direction.normalized().to_track_quat("Z", "Y").to_matrix().to_4x4()
    return Matrix.Translation(position) @ turn


class LS3D_GGT_MirrorViewBox(bpy.types.GizmoGroup):
    """Face handles for the selected mirror's view box."""

    bl_idname = "LS3D_GGT_mirror_view_box"
    bl_label = "4DS Mirror View Box"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        from .viewport import is_mirror
        obj = getattr(context, "object", None)
        return is_mirror(obj) and obj.select_get() and obj.mode == "OBJECT"

    def setup(self, context):
        self.handles = []
        for _index in range(len(FACES)):
            gizmo = self.gizmos.new(LS3D_GT_DummyHandle.bl_idname)
            gizmo.color = HANDLE_COLOR
            gizmo.alpha = HANDLE_ALPHA
            gizmo.alpha_highlight = HANDLE_ALPHA_HIGHLIGHT
            gizmo.scale_basis = HANDLE_SCALE
            gizmo.use_draw_modal = True
            self.handles.append(gizmo)
        self.draw_prepare(context)

    def draw_prepare(self, context):
        """Put each handle back on the middle of its face before drawing."""
        obj = context.object
        if obj is None:
            return
        highlight = highlight_color(context)
        for index, gizmo in enumerate(self.handles):
            gizmo.target_set_prop("offset", obj, MIRROR_FACE_PROPERTIES[index])
            gizmo.matrix_basis = mirror_handle_matrix(obj, index)
            gizmo.color_highlight = highlight


CLASSES = (LS3D_GT_DummyHandle, LS3D_GGT_DummyBox, LS3D_GGT_MirrorViewBox)
