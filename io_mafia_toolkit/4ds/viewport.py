"""Viewport appearance driven by 4DS frame and visual type.

Coloring objects by their role makes a scene readable at a glance: sectors and
occluders draw as wireframes, portals in violet, skinned meshes in pink, and so
on. Purely cosmetic — nothing here affects what gets exported.
"""

import math
import re

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from ..common import constants as C

#: Color applied when the frame type is not one we recognize.
UNKNOWN_COLOR = (0.5, 0.5, 0.5, 1.0)

#: Color of the dummy box overlay at rest, and once the dummy is picked.
DUMMY_BOX_COLOR = (0.35, 0.55, 1.0, 0.9)
DUMMY_BOX_COLOR_SELECTED = C.COLOR_DUMMY_BOX_SELECTED
#: A mirror's bound is drawn in its own color, so the two do not read as the
#: same thing. It is the volume the game culls the mirror by, not a marker.
MIRROR_BOX_COLOR = (0.45, 1.0, 0.85, 0.85)
#: A joint's influence box, in a green quiet enough to sit on a character
#: without shouting: the boxes are there while a skeleton is weighted, and
#: there are as many of them as there are joints. The active joint's own box
#: is brighter, so it is clear which one the handles are on.
INFLUENCE_BOX_COLOR = (0.42, 0.72, 0.45, 0.85)
INFLUENCE_BOX_COLOR_SELECTED = (0.60, 0.95, 0.62, 1.0)
#: The marker where an armature has its skinned mesh's frame: amber, so it
#: reads apart from the joints and their green boxes.
MESH_FRAME_COLOR = (1.0, 0.72, 0.18, 0.95)
#: How far the marker reaches from the frame's point, in meters, before the
#: scene's Joint Size multiplies it.
MESH_FRAME_REACH = 0.08
#: A mirror's view box - what the mirror is allowed to reflect - in a color of
#: its own, and brighter while the mirror is selected.
MIRROR_VIEW_BOX_COLOR = (1.0, 0.45, 0.85, 0.8)
MIRROR_VIEW_BOX_COLOR_SELECTED = (1.0, 0.7, 0.95, 1.0)
#: How thick the outline is drawn.
DUMMY_BOX_WIDTH = 1.5
#: Ceiling on boxes drawn at once. A city scene ships thousands of dummies, and
#: outlining them all would bury the viewport in lines.
DUMMY_BOX_LIMIT = 400
#: Length of each dash, and of the gap after it, in a line joining a box to
#: what it hangs off - a dummy's box to its empty, a joint's box to its joint -
#: in pixels, so it reads the same at any zoom. Dashed the way Blender draws its
#: own relationship lines, so it reads as a link and not as another edge of the
#: box.
LINK_DASH_PIXELS = 4.0
#: Ceiling on dashes in one link, for a box sitting far off what it hangs off,
#: seen from close by.
LINK_DASH_LIMIT = 250
#: Width of the dot marking the middle of a box, in pixels. Twelve edges say
#: nothing about where the middle is, and the middle is a number being edited,
#: so it is marked the way Blender marks an origin.
BOX_CENTER_DOT_PIXELS = 5.0
#: The overlay's two shaders. Dots need their own: the line shader draws a
#: point one pixel wide however wide a point is asked to be, which is a dot
#: nobody can see.
LINE_SHADER = "UNIFORM_COLOR"
DOT_SHADER = "POINT_UNIFORM_COLOR"
#: The twelve edges of a box, as index pairs into its eight corners.
_BOX_EDGES = ((0, 1), (1, 3), (3, 2), (2, 0),
              (4, 5), (5, 7), (7, 6), (6, 4),
              (0, 4), (1, 5), (2, 6), (3, 7))

#: Color of the projector volume outline. Unlike the dummy and mirror boxes,
#: which mark a number the panel edits, this outline *is* the object - there is
#: no mesh behind it - so it follows the selection the way any other object's
#: wire does: black at rest, the theme's orange once picked. The values here are
#: Blender's own defaults, used when the theme cannot be read.
PROJECTOR_COLOR = (0.0, 0.0, 0.0, 1.0)
PROJECTOR_COLOR_SELECTED = (0.929412, 0.341176, 0.0, 1.0)
PROJECTOR_COLOR_ACTIVE = (1.0, 0.627451, 0.156863, 1.0)
#: Size given to the arrows on a projector the addon creates. One unit puts
#: each arrow tip on the face of the volume it points at - the Y arrow on the
#: far face, X and Z on the sides - so the marker reads as the volume's own
#: gizmo. Only ever applied at creation; resizing afterwards is the user's.
PROJECTOR_MARKER_SIZE = 1.0
#: Ceiling on projector volumes drawn at once, for the same reason as the
#: boxes above.
PROJECTOR_LIMIT = 200
#: Size given to the sphere marking a lens flare the addon creates or imports.
#: Small, because a flare is a point: its elements are drawn along the line to
#: the camera, not around the marker. Only ever applied at creation; resizing
#: afterwards is the user's.
LENS_FLARE_MARKER_SIZE = 0.05
#: Size of the cone marking a light - its width; it is twice as long. Small:
#: what the overlay draws around it is the part worth looking at.
LIGHT_MARKER_SIZE = 0.25
#: How many segments a drawn circle gets. Enough to read as round at the sizes
#: a light's range is usually set to.
LIGHT_CIRCLE_STEPS = 32
#: How far a directional light's arrow reaches, in its own local units. It has
#: no range of its own - the engine uses only its direction - so the length is
#: just enough to show which way it points.
LIGHT_DIRECTION_LENGTH = 2.0
#: How solid a light's outline is when the light is not selected.
LIGHT_REST_ALPHA = 0.65
#: Ceiling on light outlines drawn at once, as for the boxes.
LIGHT_LIMIT = 200

#: The four corners of the far face, counter-clockwise, as (x, z) signs in the
#: projector's local space. The projection axis is local +Y.
_PROJECTOR_FACE = ((-1, -1), (1, -1), (1, 1), (-1, 1))


def mesh_sharers(obj):
    """Every object in the scene sharing this one's mesh, itself included.

    Objects sharing a mesh datablock are what the exporter turns into instance
    references, so this is the set the indicator talks about.
    """
    if obj is None or obj.type != "MESH" or obj.data is None:
        return []
    return [other for other in bpy.data.objects
            if other.type == "MESH" and other.data is obj.data]


#: The last plan made, kept until the scene changes. Planning a 1700-frame
#: mission takes tens of milliseconds - far too long to repeat on every redraw
#: of the object panel, which asks constantly and almost always about a scene
#: that has not moved.
_plan_cache = None


def forget_instance_plan():
    """Drop the kept plan, so the next question plans the scene afresh."""
    global _plan_cache
    _plan_cache = None


def instance_plan():
    """``{copy: master}`` as the export would write the scene right now.

    Asked of the export's own planner rather than read off anything the
    importer left behind, so the object panel names a linked duplicate made in
    Blender exactly as it names an instance in an imported model - and what it
    says is what the export will do. A panel must never fail to draw, so
    anything the planner trips over simply means nothing is said.
    """
    global _plan_cache
    if _plan_cache is not None:
        # A kept plan naming an object that has since been deleted would hand
        # the panel a dead reference. Checking is as cheap as the plan is
        # small - one entry per copy - so it is checked rather than trusted.
        try:
            for copy, master in _plan_cache.items():
                copy.name, master.name
            return _plan_cache
        except ReferenceError:
            _plan_cache = None

    from .exporter import plan_instances
    try:
        _plan_cache = plan_instances()
    except Exception:                               # noqa: BLE001 - see above
        return {}
    return _plan_cache


def dummy_box(obj):
    """The box a dummy actually stores, as ``(low, high)`` in local space.

    Falls back to the empty's own size when the box is empty, matching what the
    exporter writes for a dummy built from scratch.
    """
    low = tuple(getattr(obj, "bbox_min", (0.0, 0.0, 0.0)))
    high = tuple(getattr(obj, "bbox_max", (0.0, 0.0, 0.0)))
    if any(low) or any(high):
        return low, high
    size = obj.empty_display_size
    return (-size, -size, -size), (size, size, size)


def dummy_box_is_drawable(obj):
    """True when the empty's own cube already shows the box exactly.

    An empty draws a cube centered on its origin, so it can only stand in for a
    box that is itself cubic and centered - 19177 of the game's 19323 dummies.
    The remaining 146 need the real thing outlining.
    """
    low, high = dummy_box(obj)
    sides = [high[axis] - low[axis] for axis in range(3)]
    cubic = max(sides) - min(sides) < 1e-6
    centered = all(abs(low[axis] + high[axis]) < 1e-6 for axis in range(3))
    return cubic and centered


def _dummy_box_corners(obj):
    low, high = dummy_box(obj)
    matrix = obj.matrix_world
    return [matrix @ Vector((low[0] if not (index & 1) else high[0],
                             low[1] if not (index & 2) else high[1],
                             low[2] if not (index & 4) else high[2]))
            for index in range(8)]


def _pixel_size(region, view, point):
    """World units one pixel covers at *point*, across the view.

    Works out the same for a perspective and an orthographic view: the depth
    term is 1 in the second, and the window matrix carries the zoom in both.
    """
    scale = view.window_matrix[0][0]
    if region.width <= 0 or scale == 0.0:
        return 0.0
    depth = (view.perspective_matrix @ point.to_4d()).w
    return abs(depth) * 2.0 / (region.width * scale)


def link_lines(start, end, region, view):
    """A dashed line from *start* to *end*, in world space, as point pairs.

    Dashed the way Blender draws its own relationship lines, so it reads as a
    link and not as another edge of the box, with a dash at each end so it
    visibly touches both. The dashes are measured on screen, so they stay the
    same length however far the view stands from the line.
    """
    span = end - start
    pixel = _pixel_size(region, view, (start + end) * 0.5)
    if pixel <= 0.0:
        dashes = 1
    else:
        on_screen = span.length / pixel
        dashes = int(on_screen / (2.0 * LINK_DASH_PIXELS) + 0.5)
        dashes = max(1, min(dashes, LINK_DASH_LIMIT))
    # n dashes and n - 1 gaps of the same length, so both ends are drawn.
    step = 1.0 / (2 * dashes - 1)
    lines = []
    for index in range(dashes):
        lines.append(start + span * (2 * index * step))
        lines.append(start + span * ((2 * index + 1) * step))
    return lines


def dummy_link_lines(obj, region, view):
    """A dashed line from the middle of a dummy's box to its empty.

    Empty for a box centered on the empty, which already sits around it. The
    line runs from the box's center to the empty's origin, the two points
    Blender's own relationship lines join.
    """
    low, high = dummy_box(obj)
    middle = (Vector(low) + Vector(high)) * 0.5
    if all(abs(value) < 1e-6 for value in middle):
        return []
    matrix = obj.matrix_world
    return link_lines(matrix @ middle, matrix.translation.copy(), region, view)


def box_center(corners):
    """The middle of a box, from the corners it is drawn through."""
    middle = Vector((0.0, 0.0, 0.0))
    for corner in corners:
        middle += corner
    return middle / len(corners)


def has_mirror_box(obj):
    """True for a mirror carrying the bound the file stores for it.

    A mirror keeps an authored box and sphere, and the game trusts them rather
    than rebuilding from the geometry, so it is worth being able to see it.
    Nothing in Blender draws it - a mesh shows its own shape, not its bound.
    """
    if obj.type != "MESH":
        return False
    if _int_prop(obj, "ls3d_frame_type", C.FRAME_VISUAL) != C.FRAME_VISUAL:
        return False
    if _int_prop(obj, "visual_type") != C.VISUAL_MIRROR:
        return False
    return any(obj.bbox_min) or any(obj.bbox_max)


def is_projector(obj):
    """True for an object that paints a projected material."""
    return bool(
        obj
        and _int_prop(obj, "ls3d_frame_type", C.FRAME_VISUAL) == C.FRAME_VISUAL
        and _int_prop(obj, "visual_type", -1) == C.VISUAL_PROJECTOR
    )


def projector_lines(obj):
    """The projected volume's edges in world space, as pairs of points.

    The shape is not stored anywhere and never was: it is a fixed unit shape
    the frame's transform carries, so it is rebuilt here from the transform and
    the one flag that decides which shape it is. Straight-on projection is a
    box the same width all the way through; the other kind is the pyramid
    inscribed in that box, spreading out from the object's own origin.
    """
    half = C.PROJECTOR_HALF_WIDTH
    reach = C.PROJECTOR_REACH
    matrix = obj.matrix_world
    far = [matrix @ Vector((x * half, reach, z * half))
           for x, z in _PROJECTOR_FACE]

    lines = []
    for index in range(4):
        lines.append(far[index])
        lines.append(far[(index + 1) % 4])

    if getattr(obj, "ls3d_projector_orthogonal", False):
        near = [matrix @ Vector((x * half, 0.0, z * half))
                for x, z in _PROJECTOR_FACE]
        for index in range(4):
            lines.append(near[index])
            lines.append(near[(index + 1) % 4])
            lines.append(near[index])
            lines.append(far[index])
    else:
        apex = matrix @ Vector((0.0, 0.0, 0.0))
        for corner in far:
            lines.append(apex)
            lines.append(corner)
    return lines


def _outlined_boxes(context):
    """``(object, color)`` for every box the viewport should outline."""
    found = []
    for obj in context.visible_objects:
        if obj.type == "EMPTY" and obj.empty_display_type == "CUBE":
            if _int_prop(obj, "ls3d_frame_type", C.FRAME_DUMMY) != C.FRAME_DUMMY:
                continue
            if dummy_box_is_drawable(obj):
                continue
            found.append((obj, DUMMY_BOX_COLOR_SELECTED if obj.select_get()
                          else DUMMY_BOX_COLOR))
        elif has_mirror_box(obj):
            found.append((obj, MIRROR_BOX_COLOR))
        else:
            continue
        if len(found) >= DUMMY_BOX_LIMIT:
            break
    return found


def _selection_colors(context):
    """``(active, selected)`` outline colors, from the user's own theme."""
    try:
        theme = context.preferences.themes[0].view_3d
        return ((*theme.object_active, 1.0), (*theme.object_selected, 1.0))
    except (AttributeError, IndexError, TypeError):
        return PROJECTOR_COLOR_ACTIVE, PROJECTOR_COLOR_SELECTED


def is_light(obj):
    """True for an object whose frame is a light."""
    return bool(obj and _int_prop(obj, "ls3d_frame_type", -1) == C.FRAME_LIGHT)


def _circle(matrix, radius, axis):
    """A ring of *radius* about the origin, in the plane across *axis*."""
    first, second = [a for a in range(3) if a != axis]
    points = []
    for step in range(LIGHT_CIRCLE_STEPS):
        for offset in (step, step + 1):
            angle = 2.0 * math.pi * offset / LIGHT_CIRCLE_STEPS
            local = [0.0, 0.0, 0.0]
            local[first] = math.cos(angle) * radius
            local[second] = math.sin(angle) * radius
            points.append(matrix @ Vector(local))
    return points


def _cone_lines(matrix, full_angle, reach, spokes=4):
    """A cone of *full_angle* reaching *reach* along local +Y, as line pairs."""
    radius = math.tan(max(full_angle, 0.0) * 0.5) * reach
    rim = []
    for step in range(LIGHT_CIRCLE_STEPS):
        for offset in (step, step + 1):
            angle = 2.0 * math.pi * offset / LIGHT_CIRCLE_STEPS
            rim.append(matrix @ Vector((math.cos(angle) * radius, reach,
                                       math.sin(angle) * radius)))
    apex = matrix @ Vector((0.0, 0.0, 0.0))
    for index in range(spokes):
        angle = 2.0 * math.pi * index / spokes
        rim.append(apex)
        rim.append(matrix @ Vector((math.cos(angle) * radius, reach,
                                   math.sin(angle) * radius)))
    return rim


def light_lines(obj):
    """What the viewport draws for a light, from its own settings.

    Blender has nothing that holds a 4DS light, so none of this comes from a
    light datablock - it is drawn from the eight values the object carries. What
    gets drawn depends on the kind: a point light's two radii, a spot's two
    cones, a directional light's heading, and for the three that set a sector's
    atmosphere rather than lighting anything, the range they cover.
    """
    matrix = obj.matrix_world.normalized()
    kind = (_int_prop(obj, "ls3d_light_type_value", C.DEFAULT_LIGHT_TYPE)
            & 0xFFFFFFFF)
    near = _float_prop(obj, "ls3d_light_range_near", C.DEFAULT_LIGHT_RANGE_NEAR)
    far = _float_prop(obj, "ls3d_light_range_far", C.DEFAULT_LIGHT_RANGE_FAR)
    scale = obj.scale

    # The frame's scale multiplies both ranges, exactly as the engine does.
    near *= abs(scale.x) or 1.0
    far *= abs(scale.x) or 1.0

    if kind == C.LIGHT_SPOT:
        lines = []
        outer = _float_prop(obj, "ls3d_light_cone_outer",
                            C.DEFAULT_LIGHT_CONE_OUTER)
        for angle in (_float_prop(obj, "ls3d_light_cone_inner",
                                  C.DEFAULT_LIGHT_CONE_INNER), outer):
            lines += _cone_lines(matrix, angle, far)
        # Where full strength ends: a ring across the outer cone at the near
        # range, which is where the near handle sits.
        if 0.0 < near < far:
            radius = math.tan(max(min(outer, math.pi * 0.999), 0.0) * 0.5) * near
            lines += _circle(matrix @ Matrix.Translation((0.0, near, 0.0)),
                             radius, 1)
        # The axis down the middle, which is where the range handles and the
        # aim handle sit: without it they look like loose beads rather than
        # marks along the beam.
        lines += [matrix @ Vector((0.0, 0.0, 0.0)),
                  matrix @ Vector((0.0, far, 0.0))]
        return lines

    if kind == C.LIGHT_DIRECTIONAL:
        # No position and no range of its own: only the heading matters.
        reach = LIGHT_DIRECTION_LENGTH
        tip = matrix @ Vector((0.0, reach, 0.0))
        lines = [matrix @ Vector((0.0, 0.0, 0.0)), tip]
        head = reach * 0.15
        for sx, sz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            lines += [tip, matrix @ Vector((sx * head, reach - head, sz * head))]
        lines += _circle(matrix @ Matrix.Translation((0.0, reach, 0.0)),
                         head, 1)
        return lines

    if kind == C.LIGHT_POINT:
        lines = []
        for radius in (near, far):
            if radius <= 0.0:
                continue
            for axis in range(3):
                lines += _circle(matrix, radius, axis)
        return lines

    # Ambient, layered fog and none have nothing to show - and nor do the three
    # fogs: their ranges are percentages of the view distance or a density, not
    # distances from the light, so there is no shape in the scene to draw.
    return []


def _visible_lights(context):
    """``(object, color)`` for every light to outline, selection-aware."""
    active_color, selected_color = _selection_colors(context)
    active = context.view_layer.objects.active if context.view_layer else None
    found = []
    for obj in context.visible_objects:
        if not is_light(obj):
            continue
        if obj.select_get():
            color = active_color if obj == active else selected_color
        else:
            # Its own color, dimmed, so a scene full of lights reads as lights.
            tint = tuple(obj.ls3d_light_color)
            color = (tint[0], tint[1], tint[2], LIGHT_REST_ALPHA)
        found.append((obj, color))
        if len(found) >= LIGHT_LIMIT:
            break
    return found


def _visible_projectors(context):
    """``(object, color)`` for every projector volume to outline."""
    active_color, selected_color = _selection_colors(context)
    active = context.view_layer.objects.active if context.view_layer else None
    found = []
    for obj in context.visible_objects:
        if not is_projector(obj):
            continue
        if obj.select_get():
            color = active_color if obj == active else selected_color
        else:
            color = PROJECTOR_COLOR
        found.append((obj, color))
        if len(found) >= PROJECTOR_LIMIT:
            break
    return found


def _draw_dummy_boxes():
    """Outline what the file describes but Blender draws nothing for.

    That is the dummy and mirror bounds and a mirror's view box, whose numbers
    no Blender object shows, each joint's influence box, drawn in its own green
    over the empty that holds it, the volume each projector covers, which has
    no geometry at all, and what each light reaches - Blender's own lights cannot
    hold a 4DS light's two-value falloff band or its pair of cone angles, so
    none of this comes from one.

    Every box carries a dot at its middle - the point the panel's Center
    numbers move - so the middle can be placed by eye and not only by number.
    A dummy box that is not centered on its empty also gets a dashed line to
    the empty, and a joint's box that sits off its joint one to the joint, so
    which box belongs to what can be seen in a crowded scene. Those lines
    follow the viewport's Relationship Lines overlay toggle, as Blender's own
    parent lines do.

    Read-only, like the tag overlay: it projects nothing and writes nothing,
    it just batches a handful of lines per object.
    """
    context = bpy.context
    space = getattr(context, "space_data", None)
    if space is None or space.type != "VIEW_3D":
        return
    if not space.overlay.show_overlays:
        return

    try:
        boxes = _outlined_boxes(context)
        view_boxes = _mirror_view_boxes(context)
        influence = _influence_boxes(context)
        projectors = _visible_projectors(context)
        lights = _visible_lights(context)
        frames = _mesh_frame_markers(context)
    except (AttributeError, ReferenceError):
        return
    if (not boxes and not view_boxes and not influence and not projectors
            and not lights and not frames):
        return

    region = getattr(context, "region", None)
    view = getattr(context, "region_data", None)
    links = (region is not None and view is not None
             and space.overlay.show_relationship_lines)

    by_color = {}
    # A dot at each box's middle, drawn with its box and in its color.
    dots_by_color = {}
    # Influence boxes drawn over everything are drawn apart, last, with
    # nothing hiding them; the rest are hidden by what stands in front.
    in_front = getattr(context.scene, C.INFLUENCE_BOXES_IN_FRONT_PROP, False)
    front_by_color = {} if in_front else by_color
    front_dots_by_color = {} if in_front else dots_by_color
    for obj, color in boxes:
        corners = _dummy_box_corners(obj)
        lines = by_color.setdefault(color, [])
        for start, end in _BOX_EDGES:
            lines.append(corners[start])
            lines.append(corners[end])
        dots_by_color.setdefault(color, []).append(box_center(corners))
        if links and obj.type == "EMPTY":       # a mirror's bound is its mesh's
            lines.extend(dummy_link_lines(obj, region, view))
    for obj, color in view_boxes:
        corners = mirror_view_box_corners(obj)
        lines = by_color.setdefault(color, [])
        for start, end in _BOX_EDGES:
            lines.append(corners[start])
            lines.append(corners[end])
        dots_by_color.setdefault(color, []).append(box_center(corners))
    for matrix, color, joint in influence:
        corners = influence_box_corners(matrix)
        lines = front_by_color.setdefault(color, [])
        for start, end in _BOX_EDGES:
            lines.append(corners[start])
            lines.append(corners[end])
        middle = matrix.translation.copy()
        front_dots_by_color.setdefault(color, []).append(middle)
        # The joint a box hangs off can be anywhere, the same way a dummy's
        # empty can, so the two are joined for the box to be told apart from
        # a neighbor's.
        if links and joint is not None:
            lines.extend(link_lines(middle, joint, region, view))
    for obj, color in projectors:
        by_color.setdefault(color, []).extend(projector_lines(obj))
    for obj, color in lights:
        by_color.setdefault(color, []).extend(light_lines(obj))
    for lines in frames:
        by_color.setdefault(MESH_FRAME_COLOR, []).extend(lines)

    line_shader = gpu.shader.from_builtin(LINE_SHADER)
    dot_shader = gpu.shader.from_builtin(DOT_SHADER)

    def draw(shader, kind, groups):
        """Draw each color's points, as lines or as dots."""
        for color, points in groups.items():
            if not points:
                continue
            batch = batch_for_shader(shader, kind, {"pos": points})
            shader.uniform_float("color", color)
            batch.draw(shader)

    gpu.state.line_width_set(DUMMY_BOX_WIDTH)
    gpu.state.point_size_set(BOX_CENTER_DOT_PIXELS)
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    try:
        draw(line_shader, "LINES", by_color)
        draw(dot_shader, "POINTS", dots_by_color)
        if front_by_color is not by_color:
            gpu.state.depth_test_set("NONE")
            draw(line_shader, "LINES", front_by_color)
            draw(dot_shader, "POINTS", front_dots_by_color)
    finally:
        gpu.state.depth_test_set("NONE")
        gpu.state.blend_set("NONE")
        gpu.state.line_width_set(1.0)
        gpu.state.point_size_set(1.0)


_VISUAL_COLORS = {
    C.VISUAL_OBJECT:      C.COLOR_VISUAL_OBJECT,
    C.VISUAL_LITOBJECT:   C.COLOR_VISUAL_LITOBJECT,
    C.VISUAL_SINGLEMESH:  C.COLOR_VISUAL_SINGLEMESH,
    C.VISUAL_SINGLEMORPH: C.COLOR_VISUAL_SINGLEMORPH,
    C.VISUAL_BILLBOARD:   C.COLOR_VISUAL_BILLBOARD,
    C.VISUAL_MORPH:       C.COLOR_VISUAL_MORPH,
    C.VISUAL_LENSFLARE:   C.COLOR_VISUAL_LENSFLARE,
    C.VISUAL_MIRROR:      C.COLOR_VISUAL_MIRROR,
    C.VISUAL_PROJECTOR:   C.COLOR_VISUAL_PROJECTOR,
}

_PORTAL_RE = re.compile(C.PORTAL_SUFFIX_PATTERN, re.IGNORECASE)


def _int_prop(obj, name, default=0):
    try:
        return int(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


def _float_prop(obj, name, default=0.0):
    """Tolerant on purpose: a draw handler must never raise, whatever it finds.

    The export's reader is the strict one - it refuses to invent a value. Here a
    missing number only means one fewer line drawn.
    """
    try:
        return float(getattr(obj, name, default))
    except (TypeError, ValueError):
        return default


def is_portal(obj):
    """A portal is a sector-typed mesh named ``*_portalN`` parented to a sector."""
    return bool(
        obj
        and obj.type == "MESH"
        and _int_prop(obj, "ls3d_frame_type") == C.FRAME_SECTOR
        and obj.parent
        and _int_prop(obj.parent, "ls3d_frame_type") == C.FRAME_SECTOR
        and _PORTAL_RE.search(obj.name)
    )


def carries_frame(obj):
    """True when this object stands for a frame the model can hold.

    Meshes and armatures always do. An empty only does when its shape says
    which frame it is - a cube is a dummy, a sphere a lens flare, arrows a
    projector, plain axes a target or one of the frames that store nothing.
    Any other empty is a helper somebody put in the scene for their own
    reasons, and gets no 4DS settings and no place in the file.
    """
    if obj is None:
        return False
    if obj.type != "EMPTY":
        return obj.type in ("MESH", "ARMATURE")
    return obj.empty_display_type in C.EMPTY_FRAME_DISPLAYS


def is_mirror(obj):
    """True for a mesh whose frame is a mirror."""
    return bool(
        obj
        and obj.type == "MESH"
        and _int_prop(obj, "ls3d_frame_type", C.FRAME_VISUAL) == C.FRAME_VISUAL
        and _int_prop(obj, "visual_type", -1) == C.VISUAL_MIRROR
    )


def mirror_view_box(obj):
    """A mirror's view box as ``(center, (x, y, z))`` in the mirror's space.

    Each axis runs from the box's center to the middle of a face, so the box
    reaches the length of an axis either way along it. The four are the file's
    own view matrix, column by column, which is why they are written back to
    the bit: nothing turns them into a rotation and a scale and back.
    """
    return (Vector(tuple(getattr(obj, "ls3d_mirror_box_center", (0.0, 0.0, 0.0)))),
            tuple(Vector(tuple(getattr(obj, name, (0.0, 0.0, 0.0))))
                  for name in ("ls3d_mirror_box_x", "ls3d_mirror_box_y",
                               "ls3d_mirror_box_z")))


def mirror_view_box_corners(obj):
    """The view box's eight corners in world space, in the dummy box's order."""
    center, (x, y, z) = mirror_view_box(obj)
    matrix = obj.matrix_world
    return [matrix @ (center
                      + x * (1.0 if index & 1 else -1.0)
                      + y * (1.0 if index & 2 else -1.0)
                      + z * (1.0 if index & 4 else -1.0))
            for index in range(8)]


def _mirror_view_boxes(context):
    """``(mirror, color)`` for every mirror whose view box should be outlined."""
    found = []
    for obj in context.visible_objects:
        if not is_mirror(obj):
            continue
        _center, axes = mirror_view_box(obj)
        if not any(axis.length for axis in axes):
            continue
        found.append((obj, MIRROR_VIEW_BOX_COLOR_SELECTED if obj.select_get()
                      else MIRROR_VIEW_BOX_COLOR))
        if len(found) >= DUMMY_BOX_LIMIT:
            break
    return found


def influence_box_corners(matrix):
    """The eight corners of a box, from the matrix that places it.

    The matrix takes a cube reaching one unit each way onto the box, so its
    columns already carry the box's own axes and half its width along each.
    """
    return [matrix @ Vector((1.0 if index & 1 else -1.0,
                             1.0 if index & 2 else -1.0,
                             1.0 if index & 4 else -1.0))
            for index in range(8)]


#: Each armature's joints in their own terms, worked out once and kept until
#: the scene changes. Building one walks every bone, which is too much to do
#: again for every redraw of every view.
_JOINT_SPACES = {}
_MESH_FRAMES = {}


def forget_joint_spaces():
    """Drop what is kept about where the joints are."""
    _JOINT_SPACES.clear()
    _MESH_FRAMES.clear()


def mesh_frame_world(armature):
    """Where *armature*'s skinned mesh has its frame, in the world, or ``None``.

    The armature stands there, so it is the armature's own place - moving with
    the body when an animation moves it. ``None`` for a skeleton with no
    skinned mesh standing at its origin: there is nothing there worth marking.
    """
    found = _MESH_FRAMES.get(armature.name)
    if found is None:
        from .joint_space import mesh_frame_channels, skinned_mesh_of
        try:
            found = (skinned_mesh_of(armature, list(bpy.context.scene.objects))
                     is not None
                     or mesh_frame_channels(armature) != (
                         (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0),
                         (1.0, 1.0, 1.0)))
        except (AttributeError, ReferenceError):
            found = False
        _MESH_FRAMES[armature.name] = found
    return armature.matrix_world.copy() if found else None


def _mesh_frame_markers(context):
    """Lines marking every visible armature's mesh frame.

    Three axes and a small diamond around the point, so it reads as a place
    rather than a joint, and turns with the frame.
    """
    reach = MESH_FRAME_REACH * getattr(context.scene,
                                       C.JOINT_DISPLAY_SCALE_PROP, 1.0)
    found = []
    for obj in context.visible_objects:
        if obj.type != "ARMATURE":
            continue
        world = mesh_frame_world(obj)
        if world is None:
            continue
        place = world.to_translation()
        axes = [(world.to_3x3() @ Vector(axis)).normalized() * reach
                for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
        lines = []
        for axis in axes:
            lines += [place - axis, place + axis]
        tips = [place + axis * sign * 0.5 for axis in axes for sign in (1, -1)]
        for a in range(0, 6, 2):
            for b in range(a + 2, 6):
                for first in (tips[a], tips[a + 1]):
                    lines += [first, tips[b]]
        found.append(lines)
    return found


def _joint_worlds(armature):
    """``{bone name: the joint's own space}``, in the armature's terms."""
    found = _JOINT_SPACES.get(armature.name)
    if found is None:
        from .joint_space import JointSpace, skinned_mesh_of
        try:
            skin = skinned_mesh_of(armature, list(bpy.context.scene.objects))
            found = dict(JointSpace(armature, skin).worlds)
        except (AttributeError, KeyError, ReferenceError, ValueError):
            found = {}
        _JOINT_SPACES[armature.name] = found
    return found


def joint_world_matrix(armature, bone_name, worlds=None):
    """A joint's own space in the world, pose and all.

    A box is written in the joint's own space, which is placed through that
    space rather than through the bone - the two differ wherever a joint
    carries a scale. The bone's pose is applied over its rest, so what hangs
    off a joint follows it while the model is posed.
    """
    worlds = _joint_worlds(armature) if worlds is None else worlds
    joint = worlds.get(bone_name)
    if joint is None:
        return None
    bone = armature.data.bones.get(bone_name)
    pose_bone = armature.pose.bones.get(bone_name) if armature.pose else None
    posed = Matrix.Identity(4)
    if bone is not None and pose_bone is not None:
        try:
            posed = pose_bone.matrix @ bone.matrix_local.inverted()
        except ValueError:                  # a bone of no length has no inverse
            posed = Matrix.Identity(4)
    return armature.matrix_world @ posed @ joint


def influence_box_matrix(armature, bone_name, box, worlds=None):
    """Where a joint's box sits in the world."""
    world = joint_world_matrix(armature, bone_name, worlds)
    return None if world is None else world @ Matrix(box)


def _influence_boxes(context):
    """``(matrix, color, joint)`` for every joint's box to outline.

    *joint* is where the joint itself sits, for the line joining the two, and
    is ``None`` for a box centered on its joint - there would be no line.
    """
    if not getattr(context.scene, C.SHOW_INFLUENCE_BOXES_PROP, True):
        return []
    from . import influence
    from .gizmo_influence import active_joint
    lit = active_joint(context)
    found = []
    for obj in context.visible_objects:
        if obj.type != "ARMATURE":
            continue
        # While the armature is being edited its bones are somewhere else
        # entirely - the rest the boxes are placed through is the one from
        # before the edit - so there is nothing worth drawing until it is over.
        if obj.mode == "EDIT":
            continue
        boxes = influence.boxes_of(obj)
        if not boxes:
            continue
        worlds = _joint_worlds(obj)
        # Brighter on the joint the handles are on: the active bone, while it
        # is selected as well, in Pose Mode - out of it the armature is
        # picked as a whole, whatever bone it last had selected.
        chosen = (lit[1] if lit is not None and lit[0] == obj else None)
        for bone_name, box in boxes.items():
            matrix = influence_box_matrix(obj, bone_name, box, worlds)
            if matrix is None:
                continue
            joint = joint_world_matrix(obj, bone_name, worlds).translation
            off_center = (matrix.translation - joint).length > 1e-6
            found.append((matrix, INFLUENCE_BOX_COLOR_SELECTED
                          if bone_name == chosen else INFLUENCE_BOX_COLOR,
                          joint.copy() if off_center else None))
            if len(found) >= DUMMY_BOX_LIMIT:
                return found
    return found


def update_viewport_display(obj):
    """Recolor *obj* to match its frame and visual type."""
    if obj is None:
        return

    obj.display_type = "TEXTURED"
    obj.show_wire = False
    obj.show_all_edges = False
    obj.show_axis = False
    obj.color = (1.0, 1.0, 1.0, 1.0)

    frame_type = _int_prop(obj, "ls3d_frame_type")

    if frame_type == C.FRAME_SECTOR and obj.type == "MESH":
        portal = is_portal(obj)
        obj.display_type = "WIRE"
        obj.show_wire = True
        obj.show_all_edges = not portal
        obj.color = C.COLOR_FRAME_PORTAL if portal else C.COLOR_FRAME_SECTOR
        return

    if frame_type == C.FRAME_OCCLUDER and obj.type == "MESH":
        obj.display_type = "WIRE"
        obj.show_wire = True
        obj.show_all_edges = True
        obj.color = C.COLOR_FRAME_OCCLUDER
        return

    if frame_type == C.FRAME_DUMMY:
        obj.color = C.COLOR_FRAME_DUMMY
        return

    if frame_type == C.FRAME_TARGET:
        obj.color = C.COLOR_FRAME_TARGET
        return

    if frame_type == C.FRAME_JOINT:
        obj.color = C.COLOR_FRAME_JOINT
        return

    if frame_type == C.FRAME_LIGHT:
        if obj.type == "EMPTY":
            obj.empty_display_type = C.LIGHT_EMPTY_DISPLAY
        obj.color = C.COLOR_FRAME_LIGHT
        return

    if frame_type in C.PAYLOADLESS_FRAME_TYPES:
        obj.color = C.COLOR_FRAME_BARE
        return

    if frame_type == C.FRAME_VISUAL:
        visual_type = _int_prop(obj, "visual_type")
        # For both kinds of empty the display type is the discriminator - it
        # is what decides the frame is a lens flare or a projector at all - so
        # it is kept in step. The size is not: it is the user's, and this runs
        # on every frame and visual type edit, so setting it here would shrink
        # an empty the moment it was given the type and undo every resize after.
        if visual_type == C.VISUAL_LENSFLARE and obj.type == "EMPTY":
            obj.empty_display_type = "SPHERE"
        elif visual_type == C.VISUAL_PROJECTOR and obj.type == "EMPTY":
            obj.empty_display_type = "ARROWS"
        elif visual_type == C.VISUAL_MIRROR:
            obj.show_axis = True
        obj.color = _VISUAL_COLORS.get(visual_type, C.COLOR_VISUAL_OBJECT)
        return

    obj.color = UNKNOWN_COLOR


#: Handle for the 3D-view draw callback, so it can be removed cleanly.
_box_handle = None


@bpy.app.handlers.persistent
def _on_scene_changed(*_args):
    """Forget what is kept about the scene whenever the scene changes.

    It writes nothing - it only lets go of Python references - which is what
    makes it safe where the handler described below was not.
    """
    forget_instance_plan()
    forget_joint_spaces()


def register_handlers():
    """Install the box overlay, and what keeps the instance plan current.

    An earlier version seeded ``ls3d_joint_scale`` on every pose bone from a
    ``depsgraph_update_post`` handler. That walked every object and every bone
    on every depsgraph update, and writing IDs from inside that callback can
    fire while Blender is tearing a scene down - which crashes it outright. The
    property is seeded where it is actually needed instead: the importer sets
    it, the joint panel fills it in on draw, and the exporter falls back to
    (1, 1, 1) when it is missing.

    The scene handler registered here is not that. It touches no ID and walks
    nothing: it drops the kept instance plan so the object panel's next redraw
    makes a fresh one. Marked persistent, because Blender empties the handler
    lists when a file is opened and the plan would otherwise stop following the
    scene.

    The box overlay's draw callback only reads the scene and draws lines, so it
    is safe too.
    """
    global _box_handle
    for handlers in (bpy.app.handlers.depsgraph_update_post,
                     bpy.app.handlers.load_post):
        if _on_scene_changed not in handlers:
            handlers.append(_on_scene_changed)
    forget_instance_plan()
    if _box_handle is None:
        _box_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_dummy_boxes, (), "WINDOW", "POST_VIEW")


def unregister_handlers():
    global _box_handle
    for handlers in (bpy.app.handlers.depsgraph_update_post,
                     bpy.app.handlers.load_post):
        while _on_scene_changed in handlers:
            handlers.remove(_on_scene_changed)
    forget_instance_plan()
    if _box_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_box_handle, "WINDOW")
        _box_handle = None
