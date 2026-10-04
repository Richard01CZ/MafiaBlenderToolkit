"""Drag handles for a joint's influence box.

The box lives on the joint as sixteen numbers, so there is no object in the
scene to grab it by. The handles are what it is grabbed by instead: six on the
faces to resize it, three arrows to move it and three rings to turn it, drawn
on the active joint's box and nowhere else, so a skeleton's worth of boxes does
not fill the view with handles.

The face and move handles are the ones a dummy's box uses, pointed at
properties computed from the box rather than kept beside it. The rings are
their own: a turn has no single number to drag towards, so a ring remembers the
box it started on and turns that, which is what keeps a drag from winding the
box further on every step.

Everything is worked out in the joint's own space and shown in the world: the
numbers written are the joint's own, while a drag has to feel like a distance
on screen wherever the joint sits and whatever it is scaled by.
"""

import math

import bpy
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Matrix, Quaternion, Vector

from ..common import constants as C
from . import influence, viewport
from .gizmo_dummy import (FACES, HANDLE_ALPHA, HANDLE_ALPHA_HIGHLIGHT,
                          HANDLE_COLOR, HANDLE_SCALE, MIN_THICKNESS,
                          PRECISE_FACTOR, highlight_color, push_undo)

#: The property each face handle drags, one per face of the box.
FACE_PROPERTIES = tuple(f"ls3d_box_face_{index}" for index in range(len(FACES)))
#: The property each arrow drags: the box's place along one of its own axes.
MOVE_PROPERTIES = tuple(f"ls3d_box_move_{index}" for index in range(3))

#: The colors Blender gives its own axes, so an arrow reads as a move and a
#: ring as a turn without being labeled.
AXIS_COLORS = ((0.85, 0.24, 0.29), (0.47, 0.75, 0.16), (0.21, 0.46, 0.86))

#: How far out along its axis an arrow sits, as a share of the box's own reach.
ARROW_REACH = 1.45
#: The radius the ring is drawn with in its own terms, which the matrix it is
#: given then scales to the size wanted on screen.
RING_RADIUS = 0.72

#: How far from the middle of the box each kind of handle sits on screen, at
#: the least, in pixels. A joint's box is often a few centimeters across, and
#: handles placed on a box that small all land within a few pixels of each
#: other: six faces, three arrows and three rings in one clump, where every
#: click is a guess. Held apart like this they keep their own ground however
#: small the box is, and a box big enough on screen is left as it is.
FACE_PIXELS = 44.0
ARROW_GAP_PIXELS = 34.0
RING_PIXELS = 116.0

#: How nearly a handle's own axis may point at the camera before the handle is
#: taken away: end-on it draws over the middle of the box, on top of every
#: other handle, and a drag along it says nothing anyway. Held as the cosine of
#: the angle, so 0.97 is about fourteen degrees.
FACING_AWAY = 0.97
#: And how nearly a ring may stand edge-on - its own axis square to the view -
#: before the same is done with it: a ring seen edge-on is a line through
#: everything else.
RING_EDGE_ON = 0.09
#: How many segments a ring is drawn with.
RING_SEGMENTS = 48
#: The grab shape of a ring is a band rather than a line, or it would be
#: impossible to hit - wide enough to click without aiming, and no wider: the
#: ring stands at a fixed size on screen, so a band written as a share of its
#: radius would draw as a heavy tube across the box whatever the box is.
RING_BAND_PIXELS = 9.0
RING_WIDTH = RING_BAND_PIXELS * 0.5 * RING_RADIUS / RING_PIXELS

#: Below this a drag around the center says nothing about an angle.
MIN_RING_PIXELS = 4.0


# ── which joint the handles are on ──────────────────────────────────────────
#: The mode a joint is picked in, and the only one the 4DS panel shows a
#: joint's own settings in. Back in Object Mode the armature is picked as a
#: whole - Blender keeps the last bone selected, but nothing there picks it -
#: and anywhere else - editing the armature, painting weights, editing a mesh
#: - the handles are in the way of whatever is actually being done; while the
#: bones are being edited they are not even where the handles think they are.
HANDLE_MODES = ("POSE",)


def active_joint(context):
    """``(armature, bone name)`` whose box the handles work on, or ``None``.

    Only while that joint is the one being worked on: in Pose Mode, the
    armature selected, the bone active *and* selected. Blender leaves a bone
    active after it is deselected - clicking into empty space clears the
    selection and nothing else - and selected after Pose Mode is left, so
    either on its own would keep the handles on screen with nothing picked.
    """
    if getattr(context, "mode", "OBJECT") not in HANDLE_MODES:
        return None
    armature = getattr(context, "object", None)
    if (armature is None or armature.type != "ARMATURE"
            or not armature.select_get()):
        return None
    if not getattr(context.scene, C.SHOW_INFLUENCE_BOXES_PROP, True):
        return None
    bone = armature.data.bones.active
    if bone is None or bone.hide:
        return None
    # A bone's selection is kept on its pose bone, not on the bone itself.
    posed = armature.pose.bones.get(bone.name) if armature.pose else None
    if posed is None or not posed.select:
        return None
    if not influence.has_box(armature, bone.name):
        return None
    return armature, bone.name


def _box(armature, bone_name):
    """The box the handles work on, as a Blender matrix."""
    box = influence.box_of(armature, bone_name)
    return Matrix(box if box is not None
                  else influence.box_from_file(C.DEFAULT_JOINT_BOX))


def _axis(armature, bone_name, index):
    """``(center, axis, unit, stretch)`` of one axis, in the joint's own space.

    *stretch* is how long one unit of the joint's space is in the world along
    that axis, which is what turns a drag across the screen into a number the
    file can hold.
    """
    box = _box(armature, bone_name)
    center = box.translation.copy()
    axis = box.col[index].to_3d()
    unit = axis.normalized() if axis.length else Vector(
        tuple(1.0 if i == index else 0.0 for i in range(3)))
    world = viewport.joint_world_matrix(armature, bone_name)
    stretch = (world.to_3x3() @ unit).length if world is not None else 1.0
    return center, axis, unit, (stretch or 1.0)


def _write(armature, bone_name, center=None, size=None, turn=None):
    joint = influence.joint_of(armature, bone_name)
    influence.set_bone_parts(joint, center=center, size=size, turn=turn)


# ── the six faces ───────────────────────────────────────────────────────────
def face_offset(armature, bone_name, index):
    """How far one face sits from the joint's own origin, in world units."""
    axis_index, sign = FACES[index]
    center, axis, unit, stretch = _axis(armature, bone_name, axis_index)
    return (center.dot(unit) + sign * axis.length) * sign * stretch


def set_face_offset(armature, bone_name, index, value):
    """Move one face, leaving the opposite one where it is."""
    axis_index, sign = FACES[index]
    center, axis, unit, stretch = _axis(armature, bone_name, axis_index)
    middle = center.dot(unit)
    opposite = middle - sign * axis.length
    face = (value / stretch) * sign
    half = max((face - opposite) * sign * 0.5, MIN_THICKNESS * 0.5)
    joint = influence.joint_of(armature, bone_name)
    was_center, was_size, was_turn = influence.bone_parts(joint)
    size = list(was_size)
    size[axis_index] = half * 2.0 * (-1.0 if was_size[axis_index] < 0.0 else 1.0)
    new_center = Vector(was_center) + unit * ((opposite + sign * half) - middle)
    influence.set_bone_parts(joint, center=new_center, size=size,
                             turn=was_turn)


# ── moving the whole box ────────────────────────────────────────────────────
def move_offset(armature, bone_name, index):
    """Where the box sits along one of its own axes, in world units."""
    center, _axis_vector, unit, stretch = _axis(armature, bone_name, index)
    return center.dot(unit) * stretch


def set_move_offset(armature, bone_name, index, value):
    """Slide the whole box along one of its own axes."""
    center, _axis_vector, unit, stretch = _axis(armature, bone_name, index)
    _write(armature, bone_name,
           center=Vector(center) + unit * (value / stretch - center.dot(unit)))


# ── where a handle sits ─────────────────────────────────────────────────────
def world_per_pixel(context, point):
    """How much world distance one pixel covers at *point*, or ``None``.

    Measured across the view, so it holds whatever the camera is doing:
    nearer the box, fewer world units to the pixel.
    """
    region = getattr(context, "region", None) if context else None
    rv3d = getattr(context, "region_data", None) if context else None
    if region is None or rv3d is None:
        return None
    across = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
    here = location_3d_to_region_2d(region, rv3d, point)
    there = location_3d_to_region_2d(region, rv3d, point + across)
    if here is None or there is None:
        return None
    span = (there - here).length
    return (1.0 / span) if span > 1.0e-6 else None


def _on_screen(context, point):
    """*point* in pixels, or ``None`` where there is no view to measure in."""
    region = getattr(context, "region", None) if context else None
    rv3d = getattr(context, "region_data", None) if context else None
    if region is None or rv3d is None:
        return None
    return location_3d_to_region_2d(region, rv3d, point)


def _held_out(context, center, place, pixels):
    """*place*, pushed out from *center* until it is *pixels* away on screen.

    Measured on screen rather than in the world, so an axis leaning away from
    the view - which covers fewer pixels for the same distance - is pushed out
    as far as it needs rather than as far as the others. Two passes settle it;
    perspective moves the answer only a little over so short a step.
    """
    if not context or pixels is None:
        return place
    reach = (place - center).length
    if reach < 1.0e-9:
        return place
    for _pass in range(2):
        here = _on_screen(context, center)
        there = _on_screen(context, place)
        if here is None or there is None:
            return place
        span = (there - here).length
        if span >= pixels:
            return place
        if span < 1.0:
            # End-on to the camera: no distance on screen to grow from, and
            # the handle is taken away anyway. A world guess keeps it sane.
            pixel = world_per_pixel(context, center)
            if pixel is None:
                return place
            return center + (place - center).normalized() * (pixels * pixel)
        place = center + (place - center) * (pixels / span)
    return place


def points_at_camera(toward, middle, place):
    """Whether a handle out at *place* stands end-on to the view.

    End-on it draws over the middle of the box, on top of every other handle,
    and a drag along it says nothing about a distance anyway, so it is taken
    away until the view moves. *toward* is the way the camera looks.
    """
    if toward is None or middle is None:
        return False
    along = Vector(place) - Vector(middle)
    if along.length < 1.0e-9:
        return True
    return abs(along.normalized().dot(toward)) > FACING_AWAY


def stands_edge_on(toward, matrix):
    """Whether a ring placed by *matrix* stands edge-on to the view."""
    if toward is None:
        return False
    axis = matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if axis.length < 1.0e-9:
        return False
    return abs(axis.normalized().dot(toward)) < RING_EDGE_ON


def _pointing(direction):
    """A rotation taking a gizmo's own +Z onto *direction*."""
    if direction.length == 0.0:
        direction = Vector((0.0, 0.0, 1.0))
    return direction.normalized().to_track_quat("Z", "Y").to_matrix().to_4x4()


def face_handle_matrix(armature, bone_name, index, context=None):
    """Where a face handle sits - the middle of its face - and which way it points.

    With a view to measure against, a handle on a box too small to hold them
    apart is pushed out along its own axis until it has room. What is written
    does not change: the box's own outline is still where its faces are.
    """
    axis_index, sign = FACES[index]
    world = viewport.influence_box_matrix(armature, bone_name,
                                          _box(armature, bone_name))
    if world is None:
        return Matrix.Identity(4)
    local = Vector((0.0, 0.0, 0.0))
    local[axis_index] = float(sign)
    direction = world.to_3x3() @ local
    place = world @ local
    center = world.translation
    if context:
        if (place - center).length < 1.0e-9:
            place = center + (direction.normalized() if direction.length
                              else Vector((0.0, 0.0, 1.0))) * 1.0e-6
        place = _held_out(context, center, place, FACE_PIXELS)
    return Matrix.Translation(place) @ _pointing(direction)


def move_handle_matrix(armature, bone_name, index, context=None):
    """Where a move arrow sits: out beyond the box along one of its own axes.

    Always further out than the face handle on the same axis, so the two never
    share a spot on screen.
    """
    world = viewport.influence_box_matrix(armature, bone_name,
                                          _box(armature, bone_name))
    if world is None:
        return Matrix.Identity(4)
    local = Vector((0.0, 0.0, 0.0))
    local[index] = ARROW_REACH
    direction = world.to_3x3() @ Vector(
        tuple(1.0 if i == index else 0.0 for i in range(3)))
    place = world @ local
    center = world.translation
    if context:
        if (place - center).length < 1.0e-9:
            place = center + (direction.normalized() if direction.length
                              else Vector((0.0, 0.0, 1.0))) * 1.0e-6
        place = _held_out(context, center, place,
                          FACE_PIXELS + ARROW_GAP_PIXELS)
    return Matrix.Translation(place) @ _pointing(direction)


def ring_matrix(armature, bone_name, index, context=None):
    """Where a turn ring sits: on the box's center, square to one of its axes.

    With a view to measure against the ring is given a size in the matrix and
    drawn at it, out beyond the arrows, so the three kinds of handle sit in
    three rings of their own and a click can only mean one of them.
    """
    world = viewport.influence_box_matrix(armature, bone_name,
                                          _box(armature, bone_name))
    if world is None:
        return Matrix.Identity(4)
    direction = world.to_3x3() @ Vector(
        tuple(1.0 if i == index else 0.0 for i in range(3)))
    placed = Matrix.Translation(world.translation) @ _pointing(direction)
    pixel = world_per_pixel(context, world.translation)
    if pixel is None:
        return placed
    radius = RING_PIXELS * pixel
    return placed @ Matrix.Scale(radius / RING_RADIUS, 4)


def _ring_tris(radius=RING_RADIUS, width=RING_WIDTH, segments=RING_SEGMENTS):
    """A flat band around the gizmo's own Z, as triangles."""
    inner, outer = radius - width, radius + width
    tris = []
    for step in range(segments):
        first = (step / segments) * math.tau
        second = ((step + 1) / segments) * math.tau
        a = Vector((math.cos(first) * inner, math.sin(first) * inner, 0.0))
        b = Vector((math.cos(first) * outer, math.sin(first) * outer, 0.0))
        c = Vector((math.cos(second) * outer, math.sin(second) * outer, 0.0))
        d = Vector((math.cos(second) * inner, math.sin(second) * inner, 0.0))
        tris += [a, b, c, a, c, d]
    return tris


RING_TRIS = _ring_tris()


def screen_angle(context, matrix, start_mouse, mouse):
    """How far the pointer has swung around a ring, in radians.

    Measured on the screen around the ring's own center, and turned the other
    way when the ring's axis points away from the view, so a drag turns the box
    the way the pointer goes whichever side of it you are on.
    """
    region = getattr(context, "region", None)
    rv3d = getattr(context, "region_data", None)
    if region is None or rv3d is None:
        return None
    center = location_3d_to_region_2d(region, rv3d, matrix.translation)
    if center is None:
        return None
    start = Vector(start_mouse) - center
    now = Vector(mouse) - center
    if start.length < MIN_RING_PIXELS or now.length < MIN_RING_PIXELS:
        return None
    angle = math.atan2(now.y, now.x) - math.atan2(start.y, start.x)
    angle = (angle + math.pi) % math.tau - math.pi
    axis = (matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    toward = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
    return angle if axis.dot(toward) >= 0.0 else -angle


def turned_box(armature, bone_name, parts, index, angle):
    """*parts* turned by *angle* about the box's own axis *index*."""
    center, size, turn = parts
    basis = turn.to_matrix()
    axis = basis.col[index].normalized()
    spin = Quaternion(axis, angle).to_matrix()
    return (center, size, (spin @ basis).to_euler("XYZ"))


def redraw(context):
    """Ask the view and the panel to paint again after a drag."""
    area = getattr(context, "area", None)
    if area is not None:
        area.tag_redraw()
    manager = getattr(context, "window_manager", None)
    for window in getattr(manager, "windows", ()) or ():
        screen = getattr(window, "screen", None)
        for other in getattr(screen, "areas", ()) or ():
            if other.type in ("PROPERTIES", "VIEW_3D"):
                other.tag_redraw()


class LS3D_GT_BoxRing(bpy.types.Gizmo):
    """A ring turning the box about one of its own axes."""

    bl_idname = "LS3D_GT_box_ring"

    __slots__ = ("shape", "axis_index", "armature", "bone_name",
                 "start_parts", "start_mouse")

    def _band(self):
        if getattr(self, "shape", None) is None:
            self.shape = self.new_custom_shape("TRIS", RING_TRIS)
        return self.shape

    def draw(self, context):
        self.draw_custom_shape(self._band())

    def draw_select(self, context, select_id):
        self.draw_custom_shape(self._band(), select_id=select_id)

    def invoke(self, context, event):
        joint = influence.joint_of(self.armature, self.bone_name)
        self.start_parts = influence.bone_parts(joint)
        self.start_mouse = (event.mouse_region_x, event.mouse_region_y)
        return {"RUNNING_MODAL"}

    def modal(self, context, event, tweak):
        angle = screen_angle(context, self.matrix_basis, self.start_mouse,
                             (event.mouse_region_x, event.mouse_region_y))
        if angle is None:
            return {"RUNNING_MODAL"}
        if "PRECISE" in tweak:
            angle *= PRECISE_FACTOR
        if "SNAP" in tweak:
            step = math.radians(5.0)
            angle = round(angle / step) * step
        center, size, turn = turned_box(self.armature, self.bone_name,
                                        self.start_parts, self.axis_index,
                                        angle)
        _write(self.armature, self.bone_name, center=center, size=size,
               turn=turn)
        redraw(context)
        return {"RUNNING_MODAL"}

    def exit(self, context, cancel):
        if cancel:
            center, size, turn = self.start_parts
            _write(self.armature, self.bone_name, center=center, size=size,
                   turn=turn)
            return
        joint = influence.joint_of(self.armature, self.bone_name)
        if influence.bone_parts(joint) != self.start_parts:
            push_undo("Turn influence box")


def wanted_handles(scene):
    """``(faces, arrows, rings)`` - which kinds of handle to show.

    One toggle a kind rather than one choice between them, so any combination
    can be had: the arrows and the rings without the face handles was not a
    state a single choice could hold. All three move the joint's influence
    box; the joint itself is moved with Blender's own shortcuts.
    """
    return (bool(getattr(scene, C.INFLUENCE_RESIZE_HANDLES_PROP, True)),
            bool(getattr(scene, C.INFLUENCE_MOVE_HANDLES_PROP, True)),
            bool(getattr(scene, C.INFLUENCE_TURN_HANDLES_PROP, True)))


class LS3D_GGT_InfluenceBox(bpy.types.GizmoGroup):
    """Handles for the active joint's influence box."""

    bl_idname = "LS3D_GGT_influence_box"
    bl_label = "4DS Influence Box"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        return active_joint(context) is not None

    def setup(self, context):
        from .gizmo_dummy import LS3D_GT_DummyHandle
        self.faces = []
        for _index in range(len(FACES)):
            gizmo = self.gizmos.new(LS3D_GT_DummyHandle.bl_idname)
            gizmo.color = HANDLE_COLOR
            gizmo.alpha = HANDLE_ALPHA
            gizmo.alpha_highlight = HANDLE_ALPHA_HIGHLIGHT
            gizmo.scale_basis = HANDLE_SCALE
            gizmo.use_draw_modal = True
            self.faces.append(gizmo)
        self.arrows = []
        for index in range(3):
            gizmo = self.gizmos.new(LS3D_GT_DummyHandle.bl_idname)
            # An arrow rather than a grab box: the two kinds sit on the same
            # box a few dozen pixels apart, and a shape tells them apart at a
            # glance where a color alone does not.
            gizmo.pointer = True
            gizmo.color = AXIS_COLORS[index]
            gizmo.alpha = HANDLE_ALPHA
            gizmo.alpha_highlight = HANDLE_ALPHA_HIGHLIGHT
            gizmo.scale_basis = HANDLE_SCALE
            gizmo.use_draw_modal = True
            self.arrows.append(gizmo)
        self.rings = []
        for index in range(3):
            gizmo = self.gizmos.new(LS3D_GT_BoxRing.bl_idname)
            gizmo.color = AXIS_COLORS[index]
            gizmo.alpha = HANDLE_ALPHA
            gizmo.alpha_highlight = HANDLE_ALPHA_HIGHLIGHT
            gizmo.use_draw_modal = True
            # The matrix carries the size, worked out against the view, so
            # Blender is asked not to scale it a second time of its own.
            gizmo.use_draw_scale = False
            gizmo.axis_index = index
            self.rings.append(gizmo)
        self.draw_prepare(context)

    def draw_prepare(self, context):
        """Put every handle back on the box before drawing.

        A box is a number on a joint and nothing in the scene depends on it, so
        nothing tells refresh() when it moves: the handles are placed again on
        the way to every draw, the one being dragged included, because
        following the face it moves is exactly what it should do.
        """
        found = active_joint(context)
        if found is None:
            return
        armature, bone_name = found
        highlight = highlight_color(context)
        show_faces, show_arrows, show_rings = wanted_handles(context.scene)
        rv3d = getattr(context, "region_data", None)
        toward = (rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
                  if rv3d is not None else None)
        middle = viewport.influence_box_matrix(
            armature, bone_name, _box(armature, bone_name))
        middle = middle.translation if middle is not None else None

        # Re-pointed rather than bound once: the group outlives a change of
        # active bone, and a target bound in setup would edit the joint you
        # left.
        for index, gizmo in enumerate(self.faces):
            gizmo.target_set_prop("offset", armature, FACE_PROPERTIES[index])
            gizmo.matrix_basis = face_handle_matrix(armature, bone_name, index,
                                                    context)
            gizmo.hide = (not show_faces
                          or points_at_camera(toward, middle,
                                             gizmo.matrix_basis.translation))
            gizmo.color_highlight = highlight
        for index, gizmo in enumerate(self.arrows):
            gizmo.target_set_prop("offset", armature, MOVE_PROPERTIES[index])
            gizmo.matrix_basis = move_handle_matrix(armature, bone_name, index,
                                                    context)
            gizmo.hide = (not show_arrows
                          or points_at_camera(toward, middle,
                                             gizmo.matrix_basis.translation))
            gizmo.color_highlight = highlight
        for index, gizmo in enumerate(self.rings):
            gizmo.armature = armature
            gizmo.bone_name = bone_name
            gizmo.matrix_basis = ring_matrix(armature, bone_name, index,
                                             context)
            gizmo.hide = (not show_rings
                          or stands_edge_on(toward, gizmo.matrix_basis))
            gizmo.color_highlight = highlight


CLASSES = (LS3D_GT_BoxRing, LS3D_GGT_InfluenceBox)
