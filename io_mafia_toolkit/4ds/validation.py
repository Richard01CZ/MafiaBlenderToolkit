"""Pre-export validation of skinning weights.

The engine's skin format is far more restrictive than Blender's. Each vertex
belongs to exactly one bone, optionally blended with that bone's *direct parent*
by a single weight. There is no room for a third influence and no way to express
a blend between two unrelated bones, so a mesh that breaks those rules cannot be
represented at all — the export would silently pick one bone and drop the rest.

Violations are reported as errors by default. The two addon preferences turn the
common ones into automatic corrections instead, which is what you want when
importing a mesh weighted in another tool.
"""

import math
import os
import struct

from mathutils import Quaternion, Vector

from ..common import constants as C
from . import influence
from .codec.material import has_diffuse_texture
from .joint_space import (JointSpace, find_armature, mesh_frame_channels,
                          object_channels, own_channels_at_rest, share_group)

#: Weights at or below this are treated as unassigned.
EPSILON = 1e-10
#: How far a weight total may stray from 1.0 before it counts as wrong.
WEIGHT_TOLERANCE = 1e-6
#: The format stores one bone plus an optional parent blend.
MAX_INFLUENCES = 2


#: How far a character's mesh frame may sit from Tommy's before the game's
#: animations would visibly carry the body off. The game's own characters sit
#: within 6 cm of it.
MESH_FRAME_DRIFT = 0.1
#: The joints of Tommy's skeleton the character animations turn, which make a
#: skeleton a character's.
CHARACTER_JOINTS = ("back1", "l_thigh", "r_thigh")


def validate_skin_weights(obj, report, on_error,
                          fix_multi_influences=False,
                          fix_non_parent_child=False):
    """Check (and optionally repair) the bone weights on a skinned mesh.

    ``on_error(message, fix)`` is called for each violation that could not be
    corrected; the caller decides whether that blocks the export.

    Auto-fixes rewrite the object's vertex groups in place, so they must run
    before the mesh is evaluated for export.
    """
    armature = find_armature(obj)
    if armature is None:
        return

    # The mesh frame's own share counts as an influence too. The joints at the
    # top of the skeleton hang from it, and a vertex blended between one of
    # them and the mesh frame is exactly what the format's blend against its
    # parent stores. Blender shows that share through the Armature modifier's
    # Vertex Group, so that is where it is read from.
    held = share_group(obj, armature)
    share_name = held[0].name if held is not None else None
    share_index = held[0].index if held is not None else None
    inverted = held[1] if held is not None else True
    mesh_frame = {share_name} if share_name is not None else set()
    # Every joint at the top of the skeleton hangs from the mesh frame.
    parent_of = {b.name: (b.parent.name if b.parent else share_name)
                 for b in armature.data.bones}
    bone_names = set(parent_of)

    group_name = {vg.index: vg.name for vg in obj.vertex_groups}
    # A share read the other way round - Invert off - cannot be set by weight
    # directly, so the repairs leave it alone.
    group_index = {vg.name: vg.index for vg in obj.vertex_groups
                   if vg.index != share_index or inverted}

    repaired_multi = 0
    repaired_pair = 0

    for vertex_index, vertex in enumerate(obj.data.vertices):
        influences = [
            (group_name[entry.group], entry.weight)
            for entry in vertex.groups
            if group_name.get(entry.group) in bone_names and entry.weight > EPSILON
        ]
        if not influences:
            continue        # unweighted: the engine binds it to the mesh frame
        if share_index is not None:
            weight = next((entry.weight for entry in vertex.groups
                           if entry.group == share_index), 0.0)
            share = weight if inverted else 1.0 - weight
            if share > EPSILON:
                influences.append((share_name, share))

        # ── at most two influences ────────────────────────────────────────────
        if len(influences) > MAX_INFLUENCES:
            if not fix_multi_influences:
                listing = ", ".join(f"'{n}' ({w:.4f})" for n, w in influences)
                on_error(
                    f"'{obj.name}', vertex {vertex_index}: {len(influences)} joint "
                    f"influences, but the format allows {MAX_INFLUENCES}: {listing}.",
                    "Enable 'Auto-fix >2 Joint Influences' in the addon "
                    "preferences, or limit each vertex to two joints.")
                continue
            influences = _keep_strongest(obj, vertex_index, influences,
                                         group_index, MAX_INFLUENCES)
            repaired_multi += 1

        # ── a pair must be parent and child ───────────────────────────────────
        if len(influences) == MAX_INFLUENCES:
            (name_a, weight_a), (name_b, weight_b) = influences
            related = (parent_of.get(name_a) == name_b
                       or parent_of.get(name_b) == name_a)
            if not related:
                if not fix_non_parent_child:
                    on_error(
                        f"'{obj.name}', vertex {vertex_index}: weighted to "
                        f"'{name_a}' and '{name_b}', which are not a direct "
                        f"parent-child pair.",
                        "Enable 'Auto-fix Non-Parent-Child Weights' in the addon "
                        "preferences, or re-weight to a parent-child joint pair.")
                    continue
                keep = name_a if weight_a >= weight_b else name_b
                influences = _keep_only(obj, vertex_index, influences,
                                        group_index, keep)
                repaired_pair += 1

        # ── weights must total 1.0 ────────────────────────────────────────────
        total = sum(weight for _, weight in influences)
        listing = ", ".join(f"'{n}' ({w:.4f})" for n, w in influences)

        if len(influences) == 1 and influences[0][0] in mesh_frame:
            continue        # bound to the mesh frame alone, which is whole

        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            # Blender's armature scales a vertex's weights to add up before it
            # moves the vertex; the game uses them as they are, giving whatever
            # is missing to the joint's parent. So a vertex that looks right in
            # Blender would move differently in game. Reported per vertex
            # rather than stopping at the first one, so a single pass tells you
            # everything that needs fixing.
            on_error(
                f"'{obj.name}', vertex {vertex_index}: joint weights total "
                f"{total:.6f}, not 1.0. Influences: {listing}. Blender scales "
                f"them to add up; the game uses them as they are, and gives "
                f"whatever is missing to the joint's parent.",
                "In Weight Paint mode, use Weights > Limit Total set to 2, "
                "then Weights > Normalize All. A vertex shared with the mesh "
                "keeps the mesh's share in the vertex group the Armature "
                "modifier names, with Invert on.")
            continue

        if total < EPSILON:
            report.warn(
                f"'{obj.name}', vertex {vertex_index}: joint weights total zero; "
                f"it will bind to the mesh frame.")

    if repaired_multi:
        report.warn(f"'{obj.name}': reduced {repaired_multi} vertex/vertices to "
                    f"their {MAX_INFLUENCES} strongest joint influences.")
    if repaired_pair:
        report.warn(f"'{obj.name}': resolved {repaired_pair} vertex/vertices "
                    f"weighted to unrelated joints by keeping the stronger one.")


def _keep_strongest(obj, vertex_index, influences, group_index, keep_count):
    """Drop the weakest influences and renormalize what remains."""
    ordered = sorted(influences, key=lambda pair: pair[1], reverse=True)
    kept, dropped = ordered[:keep_count], ordered[keep_count:]

    for name, _weight in dropped:
        index = group_index.get(name)
        if index is not None:
            obj.vertex_groups[index].remove([vertex_index])

    total = sum(weight for _, weight in kept)
    if total <= EPSILON:
        return kept
    normalized = [(name, weight / total) for name, weight in kept]
    for name, weight in normalized:
        index = group_index.get(name)
        if index is not None:
            obj.vertex_groups[index].add([vertex_index], weight, "REPLACE")
    return normalized


def _keep_only(obj, vertex_index, influences, group_index, keep_name):
    """Bind the vertex fully to one joint, removing the others."""
    for name, _weight in influences:
        if name == keep_name:
            continue
        index = group_index.get(name)
        if index is not None:
            obj.vertex_groups[index].remove([vertex_index])
    index = group_index.get(keep_name)
    if index is not None:
        obj.vertex_groups[index].add([vertex_index], 1.0, "REPLACE")
    return [(keep_name, 1.0)]

# ── Volume meshes: sectors and occluders ──────────────────────────────────────
#: A vertex further than this off a face's plane counts as non-convex.
#:
#: Measured against the game's own data: all 1129 sector hulls it ships are
#: closed, but their convexity is only approximate - 45 are off by more than a
#: millimeter, five by more than a centimeter, and 'sector Mesh153' in
#: MISE20-GALERY by 114 mm. That is modeling slop, not intent; a genuinely
#: concave room is out by a meter or more. The threshold sits above the worst
#: shipping hull so re-exporting the game's own missions stays silent, while
#: anything actually concave is still caught.
CONVEX_TOLERANCE = 0.15
#: A sector needs at least this many faces before it encloses anything.
MIN_SECTOR_FACES = 4


def _volume_report(obj, tolerance):
    """Measure a mesh's closedness and convexity.

    Returns ``(open_edges, face_count, inward_ok, outward_ok)``. A convex mesh
    has every vertex on one side of every face plane; which side tells us which
    way the normals point, which is the difference between a sector and a solid.
    """
    import bmesh

    mesh = bmesh.new()
    try:
        mesh.from_mesh(obj.data)
        mesh.verts.ensure_lookup_table()
        mesh.faces.ensure_lookup_table()

        open_edges = sum(1 for edge in mesh.edges if len(edge.link_faces) != 2)
        face_count = len(mesh.faces)

        inward_ok = outward_ok = True
        for face in mesh.faces:
            center = face.calc_center_median()
            normal = face.normal
            own = set(face.verts)
            for vert in mesh.verts:
                if vert in own:
                    continue
                distance = (vert.co - center).dot(normal)
                if distance < -tolerance:
                    inward_ok = False
                if distance > tolerance:
                    outward_ok = False
                if not inward_ok and not outward_ok:
                    return open_edges, face_count, False, False
        return open_edges, face_count, inward_ok, outward_ok
    finally:
        mesh.free()


def face_sector_normals_inward(obj):
    """Flip a closed convex mesh's faces inward. Returns True if it flipped.

    A sector answers "is this point inside?" by requiring the point to be on
    the inner side of every face plane, so outward normals make the room inert.
    Blender's primitives all come with normals out, which means every sector
    built from scratch starts wrong.
    """
    import bmesh

    if obj is None or obj.type != "MESH" or obj.mode != "OBJECT":
        return False
    open_edges, faces, inward_ok, outward_ok = _volume_report(
        obj, CONVEX_TOLERANCE)
    if open_edges or faces < MIN_SECTOR_FACES or inward_ok or not outward_ok:
        return False

    mesh = bmesh.new()
    try:
        mesh.from_mesh(obj.data)
        bmesh.ops.reverse_faces(mesh, faces=mesh.faces[:])
        mesh.to_mesh(obj.data)
    finally:
        mesh.free()
    obj.data.update()
    return True


def validate_sector(obj, fail, warn):
    """A sector must be a closed, convex volume with inward-facing normals.

    The mesh is stored as a convex hull: each face becomes a plane, and "is
    this point inside the sector?" is answered by requiring the point to sit on
    the inner side of every one of them. That is a convex test with no slack to
    spare, which is why a dented sector misreports where you are standing.
    """
    open_edges, faces, inward_ok, outward_ok = _volume_report(
        obj, CONVEX_TOLERANCE)
    # Face count first: a flat plane is both open and too small, and "has 1
    # face and does not enclose a volume" says more than "4 open edges". With
    # the order reversed the count check could never fire, since any closed
    # mesh already has at least four faces.
    if faces == 0:
        # Three of the game's own sectors are exactly this: a handful of loose
        # vertices and no hull at all. Nothing is ever inside one, so it sits
        # in the level doing nothing, and refusing it would leave three of the
        # game's missions unable to be written back out. A sector part-way
        # built, below, is a different thing - the game never ships one.
        warn(f"Sector '{obj.name}' has no faces, so nothing is ever inside it "
             f"and it does nothing in game.",
             fix="Give the sector a closed convex mesh, or delete it.")
        return
    if faces < MIN_SECTOR_FACES:
        fail(f"Sector '{obj.name}' has {faces} face(s) and does not enclose a "
             f"volume.",
             fix=f"A sector needs at least {MIN_SECTOR_FACES} faces.")
        return
    if open_edges:
        fail(f"Sector '{obj.name}' is not a closed mesh ({open_edges} open "
             f"edge(s)).",
             fix="Close every hole in sector meshes - they must seal a volume.")
        return
    if not inward_ok and not outward_ok:
        # A warning, not an error. The game copes with the near-convex hulls
        # it ships itself, so blocking the export would leave the addon unable
        # to round-trip Mafia's own missions.
        # Containment is decided by requiring a point to be on the inside of
        # every face plane. For a concave mesh that region is smaller than the
        # room, so the dented-in part reads as outside and the game picks the
        # wrong sector for fog, reverb and visibility while you stand there.
        warn(f"Sector '{obj.name}' is not convex; the game may treat part of "
             f"the room as being outside it.",
             fix="Make sector meshes convex - Mesh > Convex Hull, or split the "
                 "room into several sectors.")
        return
    if outward_ok and not inward_ok:
        # Containment is a plane test that assumes the normals point in. With
        # them out, every point in the room reads as outside it, so fog, reverb
        # and visibility all pick the wrong sector - the room is inert.
        fail(f"Sector '{obj.name}' has outward-facing normals; a sector's must "
             f"face inward or nothing is ever inside it.",
             fix="Flip them with Mesh > Normals > Flip, or re-pick Sector in "
                 "the frame type menu, which flips them for you.")


def validate_occluder(obj, fail, warn):
    """An occluder must be a closed, convex volume with outward-facing normals.

    An occluder is a convex occlusion volume. Every frame the game works out
    which faces point towards the camera and extrudes the outline between them
    into the shape that hides whatever is behind - which only describes the real
    shadow when that outline is a single loop, i.e. when the mesh is convex.
    """
    open_edges, _faces, inward_ok, outward_ok = _volume_report(
        obj, CONVEX_TOLERANCE)
    if open_edges:
        fail(f"Occluder '{obj.name}' is not a closed mesh ({open_edges} open "
             f"edge(s)).",
             fix="Close every hole in occluder meshes.")
        return
    if not inward_ok and not outward_ok:
        # The outline between front- and back-facing parts is extruded into
        # the hiding shape. A convex mesh has one such outline and gives a
        # correct result; a concave one has several, and the shape built from
        # them covers more than the real shadow - so geometry that should be
        # visible gets culled.
        warn(f"Occluder '{obj.name}' is not convex; it may hide geometry that "
             f"should still be visible.",
             fix="Make occluder meshes convex - Mesh > Convex Hull, or simplify "
                 "the shape.")
        return
    if inward_ok and not outward_ok:
        warn(f"Occluder '{obj.name}' has inward-facing normals; occluders are "
             f"expected to face outward.",
             fix="Flip the occluder's normals (Mesh > Normals > Flip).")


# ── Portals ───────────────────────────────────────────────────────────────────
#: How far a portal corner may sit off the outline's plane, as a fraction of
#: the portal's longest span.
#:
#: Measured against the game's own data: of 2755 shipping portals only one is
#: folded by more than 1% of its own size, and that one is in a file called
#: testtttt.4ds. The next worst real portal is 0.66%. So this catches a portal
#: that was genuinely bent without complaining about the modeling slop the
#: game itself ships.
PORTAL_FLATNESS_TOLERANCE = 0.01


def validate_portal_outline(name, vertices, normal, warn):
    """A portal is a flat polygon; the game builds one plane from it.

    The whole outline is projected onto a single plane at run time, so a folded
    portal opens onto a different shape than the one drawn in Blender.
    """
    if len(vertices) < 3:
        return
    if normal.length <= 1e-12:
        # The game ships one of these and copes: with no cross product to take,
        # it settles the plane on a unit axis and carries on. Nothing can be
        # seen through an opening with no area, so the portal is inert rather
        # than harmful, and refusing it would cost a whole mission scene.
        warn(f"Portal '{name}' has no area - its corners are in a straight "
             f"line, so there is nothing to see through.",
             fix="Reshape the portal so its corners enclose an area.")
        return

    plane = normal.normalized()
    count = len(vertices)
    centroid = [sum(v[axis] for v in vertices) / count for axis in range(3)]
    deviation = max(
        abs(sum((v[axis] - centroid[axis]) * plane[axis] for axis in range(3)))
        for v in vertices)
    span = max((a - b).length for a in vertices for b in vertices)
    if span > 0.0 and deviation / span > PORTAL_FLATNESS_TOLERANCE:
        warn(f"Portal '{name}' is not flat - a corner sits {deviation:.3f} m "
             f"off the plane of the others.",
             fix="Flatten the portal: select it in Edit Mode and scale to zero "
                 "along its thinnest axis, or rebuild it as one flat face.")


# ── Lens flares ───────────────────────────────────────────────────────────────
def validate_lens_flare(obj, entries, fail, warn):
    """A flare needs at least one element, and every element needs a material.

    The material is written as an index into the model's material table, and
    the game subtracts one from it before use - so an element with no material
    writes a zero the game reads off the front of that table. An empty flare
    draws nothing at all.
    """
    if not entries:
        fail(f"Lens flare '{obj.name}' has no elements, so it would draw "
             f"nothing.",
             fix="Add at least one element in the Lens Flare panel and give it "
                 "a material.")
        return

    missing = [number for number, (_position, material)
               in enumerate(entries, 1) if material is None]
    if missing:
        listed = ", ".join(str(n) for n in missing)
        fail(f"Lens flare '{obj.name}' element(s) {listed} have no material.",
             fix="Pick a material for every element in the Lens Flare panel.")
        return

    if len(entries) > 1 and all(position == entries[0][0]
                                for position, _material in entries):
        warn(f"Every element of lens flare '{obj.name}' sits at the same axis "
             f"offset, so they will be drawn on top of each other.",
             fix="Spread the elements out with Axis Offset - the head at the "
                 "light, the rest trailing back through the middle of the "
                 "screen.")


def validate_light(obj, light, warn):
    """Settings that make a light do nothing, which the file itself allows.

    None of these corrupt anything - the file is valid and the game loads it
    happily - so they are said out loud rather than refused.
    """
    # Each part of the game that lights things asks for its own mode bit -
    # ordinary objects for one, lit objects for another - so a lighting kind
    # with neither lights nothing at all. The fogs do not ask.
    lighting = (light.light_type in C.LIGHT_SHADING_TYPES
                or light.light_type == C.LIGHT_AMBIENT)
    if lighting and not light.mode & (C.LM_REALTIME | C.LM_LIT_OBJECTS):
        warn(f"Light '{obj.name}' has neither Lights Objects nor Lights Lit "
             f"Objects on, so it lights nothing while the game is running.",
             fix="Switch on Lights Objects, Lights Lit Objects or both in the "
                 "Light panel's Mode box.")

    if lighting and light.power == 0.0:
        warn(f"Light '{obj.name}' has no power, so it contributes nothing "
             f"whatever its color is.",
             fix="Give it a power above 0.")

    if light.light_type in C.LIGHT_RANGED_TYPES and light.range_far <= 0.0:
        warn(f"Light '{obj.name}' fades to nothing at {light.range_far:g}, so "
             f"it reaches nowhere.",
             fix="Set a far range above 0.")

    if light.light_type == C.LIGHT_SPOT and light.cone_outer <= 0.0:
        warn(f"Light '{obj.name}' is a spot with an outer cone of "
             f"{light.cone_outer:g}, so its beam has no width.",
             fix="Give the outer cone an angle above 0.")

    if light.light_type == C.LIGHT_SPOT and light.cone_inner > light.cone_outer:
        # The engine works the falloff out as the difference between the two
        # cosines and gives up when it is not positive, so the beam loses its
        # soft edge entirely rather than doing something surprising.
        warn(f"Light '{obj.name}' has an inner cone wider than its outer one, "
             f"so the beam has no falloff across its edge.",
             fix="Keep the inner cone inside the outer one.")


def validate_projector(obj, fail, warn):
    """A projector needs a material, and that material needs a diffuse texture.

    The material is written as an index into the model's material table, and
    the game subtracts one from it and follows it without checking the result
    against zero - so a projector with no material does not draw nothing, it
    reads a pointer off the word in front of the table and writes through it.

    The texture is a softer matter: the game checks for it and skips the
    projector, so a material without one costs nothing but draws nothing.
    """
    material = getattr(obj, "ls3d_projector_material", None)
    if material is None:
        fail(f"Projector '{obj.name}' has no material, so there is nothing for "
             f"it to paint with.",
             fix="Pick a material in the Projector panel.")
        return

    if (getattr(material, "ls3d_diffuse_tex", None) is None
            or not has_diffuse_texture(
                getattr(material, "ls3d_material_flags", 0) & 0xFFFFFFFF)):
        warn(f"Projector '{obj.name}' uses material '{material.name}', which "
             f"writes no diffuse texture. A projector paints with that texture "
             f"and nothing else, so it will draw nothing.",
             fix="Give the material a diffuse texture and switch Diffuse "
                 "Texture on.")


#: How far a portal corner may sit from the sector hull's surface.
#:
#: Measured against the game's own data: of 2770 shipping portals the median
#: corner is exactly on the hull, the 90th percentile is 79 um and the 99th is
#: 17 mm. Only five sit further than this, and every one of them looks like an
#: authoring slip - two at 0.65 m in MISE13-ZRADCE, one in a file called
#: testtttt.4ds, and one 1.7 km out.
PORTAL_ON_HULL_TOLERANCE = 0.1


def validate_portal_on_hull(name, vertices, hull_triangles, warn):
    """A portal is a hole in a wall, so it belongs on the wall.

    Measured as the distance from each corner to the nearest point of the hull
    surface, which catches a portal floating away from the sector and one lying
    in the right plane but hanging off the end of the face.
    """
    if not vertices or not hull_triangles:
        return
    worst = 0.0
    for vertex in vertices:
        near = min(_point_triangle_distance(vertex, *triangle)
                   for triangle in hull_triangles)
        worst = max(worst, near)
    if worst > PORTAL_ON_HULL_TOLERANCE:
        warn(f"Portal '{name}' sits {worst:.2f} m off the sector's wall.",
             fix="Move the portal onto the sector's surface - it is the "
                 "opening in that wall, not a panel floating near it.")


def _point_triangle_distance(p, a, b, c):
    """Shortest distance from a point to a triangle, edges and corners included."""
    def sub3(u, v): return (u[0] - v[0], u[1] - v[1], u[2] - v[2])
    def dot3(u, v): return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
    def add3(u, v): return (u[0] + v[0], u[1] + v[1], u[2] + v[2])
    def mul3(u, t): return (u[0] * t, u[1] * t, u[2] * t)
    def length(u): return math.sqrt(dot3(u, u))

    ab, ac, ap = sub3(b, a), sub3(c, a), sub3(p, a)
    d1, d2 = dot3(ab, ap), dot3(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return length(ap)
    bp = sub3(p, b)
    d3, d4 = dot3(ab, bp), dot3(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return length(bp)
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        t = d1 / (d1 - d3) if d1 != d3 else 0.0
        return length(sub3(p, add3(a, mul3(ab, t))))
    cp = sub3(p, c)
    d5, d6 = dot3(ab, cp), dot3(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return length(cp)
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        t = d2 / (d2 - d6) if d2 != d6 else 0.0
        return length(sub3(p, add3(a, mul3(ac, t))))
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        denominator = (d4 - d3) + (d5 - d6)
        t = (d4 - d3) / denominator if denominator else 0.0
        return length(sub3(p, add3(b, mul3(sub3(c, b), t))))
    normal = (ab[1] * ac[2] - ab[2] * ac[1],
              ab[2] * ac[0] - ab[0] * ac[2],
              ab[0] * ac[1] - ab[1] * ac[0])
    scale = length(normal)
    return abs(dot3(normal, ap)) / scale if scale > 1e-20 else length(ap)


# ── Sector and portal flag hazards ────────────────────────────────────────────


def validate_sector_flags(obj, flags, fail):
    """The two engine bits that break a sector when a file arrives with them set.

    The rest of the engine-owned sector bits are either wiped before use
    (SECTOR_PER_FRAME_BITS) or only make the game's own root sectors behave
    differently, so they are left alone.
    """
    if flags & C.SF_PARSING:
        fail(f"Sector '{obj.name}' has the engine's Parsing bit set. That bit "
             f"is the traversal's own recursion guard: a sector that already "
             f"has it is skipped and never entered, so the room would never "
             f"be drawn.",
             fix="Clear Parsing under Engine Internals in the Sector panel.")
    if flags & C.SF_PREPARED:
        fail(f"Sector '{obj.name}' has the engine's Prepared bit set. The game "
             f"takes that to mean the sector's one-time setup already ran and "
             f"skips it, so no occluder is built from the hull.",
             fix="Clear Prepared under Engine Internals in the Sector panel.")


def validate_portal_flags(name, flags, warn):
    """One engine portal bit survives loading and can stick."""
    if flags & C.PF_OCCLUDED:
        warn(f"Portal '{name}' has the engine's Occluded bit set. It is only "
             f"cleared while an occluder pass runs over the sector, so in a "
             f"sector with no occluders the portal stays blocked and nothing "
             f"is drawn through it.",
             fix="Clear Occluded under Engine Internals in the Portal panel.")


# ── Mirrors ───────────────────────────────────────────────────────────────────
#: How far a mirror's surface may lean off its local Y axis before it is
#: reported, in degrees. The game's own mirrors are exact to well under a
#: degree, so anything past this was not meant to be there.
MIRROR_FACING_TOLERANCE_DEG = 5.0


def mirror_surface_normal(vertices, faces):
    """The mirror surface's average normal in object space, or ``None``.

    Area-weighted, so a stray sliver triangle cannot swing the answer, and a
    mirror that curves still reports the way it faces overall.
    """
    normal = Vector((0.0, 0.0, 0.0))
    for a, b, c in faces:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        normal += (vb - va).cross(vc - va)
    if normal.length <= 1e-12:
        return None
    return normal.normalized()


#: Above this many faces the panel stops measuring which way a mirror points -
#: the export still checks it, and no real mirror is anywhere near this big.
MIRROR_PANEL_FACE_LIMIT = 4096


def mirror_facing(obj):
    """Which way a mirror's surface points, for the panel to say so live.

    Returns ``"FACING"``, ``"BACK"``, ``"TILTED"``, or ``None`` when there is
    nothing to measure. Built from Blender's own cached face normals and areas
    so it is cheap enough to call from a draw handler.
    """
    mesh = obj.data if obj.type == "MESH" else None
    if mesh is None or not mesh.polygons:
        return None
    if len(mesh.polygons) > MIRROR_PANEL_FACE_LIMIT:
        return None
    normal = Vector((0.0, 0.0, 0.0))
    for polygon in mesh.polygons:
        normal += polygon.normal * polygon.area
    if normal.length <= 1e-12:
        return None
    facing = normal.normalized().y
    limit = math.cos(math.radians(MIRROR_FACING_TOLERANCE_DEG))
    if facing >= limit:
        return "FACING"
    return "BACK" if facing <= -limit else "TILTED"


def validate_mirror_facing(obj, vertices, faces, warn):
    """A mirror reflects along its own local +Y, whatever its mesh does.

    The game never looks at the faces to work out which way a mirror points. It
    takes the object's local Y axis, reflects the world across the plane square
    to it, and uses the mesh only to say where that reflection is painted. So a
    mirror whose surface is turned any other way is drawn in one place and
    reflects from another, and one turned right round reflects what is behind
    it.
    """
    if len(vertices) < 3 or not faces:
        return
    normal = mirror_surface_normal(vertices, faces)
    if normal is None:
        return

    limit = math.cos(math.radians(MIRROR_FACING_TOLERANCE_DEG))
    facing = normal.y
    if facing <= -limit:
        warn(f"Mirror '{obj.name}' faces along its local -Y, so the game "
             f"reflects it from behind.",
             fix="Flip the mirror's faces (Mesh > Normals > Flip) so they "
                 "point along +Y.")
    elif facing < limit:
        angle = math.degrees(math.acos(max(-1.0, min(1.0, facing))))
        warn(f"Mirror '{obj.name}' sits {angle:.1f} degrees off its local Y "
             f"axis, but the game reflects along local +Y regardless.",
             fix="Rotate the mesh in Edit Mode until it lies in the local XZ "
                 "plane facing +Y, then turn the object itself to aim the "
                 "mirror.")


def validate_mirror_view_box(obj, center, axes, fail, warn):
    """The view box has to be a box, and belongs in front of the mirror.

    The game works out where the camera is inside the box by undoing its
    matrix, which a box with an axis of no length does not allow. And every
    mirror the game ships puts the box's near face on the mirror plane and the
    rest out along local +Y: a box left behind the surface encloses the wall
    instead of the room, and the mirror comes up empty.
    """
    flat = [name for name, axis in zip("XYZ", axes)
            if not any(axis)]
    if flat:
        fail(f"Mirror '{obj.name}' has a view box with no {', '.join(flat)} "
             f"axis, so it is not a box the game can look into.",
             fix="Set the view box in the mirror's 4DS panel, or press Fit "
                 "View Box.")
        return
    reach = sum(abs(axis[1]) for axis in axes)
    if center[1] + reach <= 0.0:
        warn(f"Mirror '{obj.name}' has its view box entirely behind it, but "
             f"it reflects along local +Y.",
             fix="Move the view box out in front of the mirror, with its near "
                 "face on the mirror surface.")


# ── Names and other text ─────────────────────────────────────
def text_length(text):
    """How many bytes *text* occupies in the file's own encoding.

    Not the same as its length on screen: the file stores text in a single-byte
    encoding, so a character it cannot represent still costs exactly one byte.
    """
    return len(text.encode(C.STRING_ENCODING, errors="replace"))


def validate_text_length(label, text, fail):
    """Refuse text too long for the single length byte in front of it.

    Every string in a 4DS - a frame's name, its user properties, a texture name
    - is written as one length byte followed by that many bytes, so 255 is the
    format's ceiling. Past it the string would be cut, and a cut frame name
    silently breaks everything that refers to the frame by name.
    """
    length = text_length(text)
    if length <= C.MAX_STRING_BYTES:
        return
    fail(f"{label} is {length} bytes long, and the format allows "
         f"{C.MAX_STRING_BYTES}.",
         fix=f"Shorten it by {length - C.MAX_STRING_BYTES} bytes or more.")


# ── Armatures and skinned meshes ──────────────────────────────────────────────
def _at_rest(matrix, tolerance=1e-6):
    """True for a matrix that moves, turns and scales nothing."""
    return all(abs(matrix[r][c] - (1.0 if r == c else 0.0)) <= tolerance
               for r in range(4) for c in range(4))


def validate_armature(armature, skinned, fail, warn):
    """At most one skinned mesh, and an armature the export can place.

    An armature with no skinned mesh is a skeleton of joints alone, which the
    game ships too; it stands for no frame, so it has to stand at the model's
    origin. With one, the armature stands where the mesh's frame is and holds
    that in its Delta Transform, and the vertex group holding the mesh's own
    share cannot also be a joint's.

    Either way the armature's own location, rotation and scale are for the
    body's movement - what the game's character animations key on the mesh
    frame - and at rest they move nothing. A move left on them would be
    written nowhere, so rather than write a skeleton standing somewhere it is
    not, the export says so.
    """
    location, rotation, scale = object_channels(armature)
    if not own_channels_at_rest(armature):
        turned = Quaternion(rotation).normalized().angle
        done = [word for word, yes in (
            ("moved", Vector(location).length > 1e-6),
            ("turned", min(turned, 2.0 * math.pi - turned) > 1e-6),
            ("scaled", any(abs(v - 1.0) > 1e-6 for v in scale))) if yes]
        fail(f"Armature '{armature.name}' has been {' and '.join(done)} as an "
             f"object, and that is written nowhere: its own location, "
             f"rotation and scale are left for the body's movement and move "
             f"nothing at rest. Where it stands is its mesh frame's place, "
             f"which it keeps in its Delta Transform.",
             fix="Clear its transform (Object > Clear > Location, Rotation "
                 "and Scale). To stand a character's armature at its default "
                 "mesh origin, where it is now, press Set Default Mesh Origin "
                 "in the 4DS Model sidebar tab.")
        return
    if not skinned and armature.parent is None and not _at_rest(
            armature.matrix_basis):
        fail(f"Armature '{armature.name}' has no skinned mesh, so it stands "
             f"for no frame and its joints hang from the model itself - but "
             f"its Delta Transform stands it off the model's origin, and that "
             f"would be written nowhere.",
             fix="Give it the character mesh it is for - named 'base', with "
                 "an Armature modifier pointing at this armature - and the "
                 "Delta Transform becomes that mesh's frame. For a skeleton "
                 "meant to stand on its own, clear the Delta Transform in "
                 "its Object properties instead.")
        return

    if len(skinned) > 1:
        names = ", ".join(f"'{o.name}'" for o in skinned)
        fail(f"Armature '{armature.name}' has {len(skinned)} skinned meshes "
             f"({names}); only one is allowed.",
             fix="Remove or reparent the extra skinned meshes.")
        return

    if not skinned:
        return

    skin = skinned[0]
    held = share_group(skin, armature)
    if held is not None and held[0].name in armature.data.bones:
        fail(f"The Armature modifier of '{skin.name}' holds the mesh's own "
             f"share in the vertex group '{held[0].name}', which is also the "
             f"group of the joint of that name. One group cannot be both.",
             fix="Rename the group the modifier names, and set the "
                 "modifier's Vertex Group to it.")
        return

    space = JointSpace(armature, skin)
    below = sum(1 for joint in space.joints() if space.below_mesh(joint))
    if below > C.MAX_SKIN_JOINTS:
        fail(f"Mesh '{skin.name}' has {below} joints hanging from it, but the "
             f"game takes only the first {C.MAX_SKIN_JOINTS} it finds below a "
             f"skinned mesh. The vertices on the others would never move, and "
             f"a joint numbered among the first {C.MAX_SKIN_JOINTS} but found "
             f"later leaves a gap the game still reads.",
             fix=f"Keep {C.MAX_SKIN_JOINTS} joints or fewer below the mesh.")
        return
    validate_character_frame(armature, skin, space, warn)


def validate_character_frame(armature, skin, space, warn):
    """A character's mesh frame where the game's animations put it.

    The character animations set the frame's place outright - hip height when
    standing, lower in a crouch - rather than move it from wherever it rests.
    A character whose frame rests anywhere else is carried there by every
    animation, and the whole body with it: one resting at its feet floats a
    metre up. So for a skeleton with Tommy's joints, the frame is measured
    against Tommy's.
    """
    if skin.name != C.CHARACTER_MESH_NAME or armature.parent_type == "BONE":
        return
    joints = armature.data.bones
    if not all(name in joints and space.below_mesh(joints[name])
               for name in CHARACTER_JOINTS):
        return
    from ..common import convert
    from .character_preset import BASE_POSITION
    from .character_preset import ORIGIN_FROM_THIGHS
    from .joint_space import default_mesh_origin
    tommy = Vector(convert.to_blender_vector(BASE_POSITION))
    location = Vector(mesh_frame_channels(armature)[0])
    drift = (location - tommy).length
    if drift <= MESH_FRAME_DRIFT:
        return
    # Set Default Mesh Origin stands the frame between the character's own
    # thighs: where they are Tommy's, that puts it right; where they are not,
    # the character itself is what sits off.
    own = default_mesh_origin(armature)
    if own is not None and (own - tommy).length > MESH_FRAME_DRIFT:
        hips = tommy - Vector(convert.to_blender_vector(ORIGIN_FROM_THIGHS))
        fix = (f"Its hips sit {(own - tommy).length:.2f} m from where Tommy's "
               f"do. Move or scale the character so they match - {hips.z:.2f} "
               f"m up - then press Set Default Mesh Origin in the 4DS Model "
               f"sidebar tab; or keep it, and it is carried off by that much "
               f"in the game's own animations.")
    else:
        fix = ("Select the character and press Set Default Mesh Origin in "
               "the 4DS Model sidebar tab.")
    warn(f"The mesh frame of '{skin.name}' sits {drift:.2f} m from where "
         f"Tommy's does, at hip height. The game's character animations put "
         f"the frame there outright, so they would carry the whole body "
         f"{drift:.2f} m off.", fix=fix)


def painted_on(armature, skinned):
    """Every joint of *armature* carrying weight on any of *skinned*."""
    joints = {bone.name for bone in armature.data.bones}
    painted = set()
    for obj in skinned:
        if obj.data is None:
            continue
        found, _taken = influence.painted_joints(obj, obj.data, joints)
        painted |= found
    return painted


def validate_influence_boxes(armature, skinned, fail, warn):
    """What each joint needs to be weighted and written.

    A joint's weights come from its own painted ones or from its influence
    box, so a joint with neither cannot be weighted at all and stops the
    export. A joint painted by hand may have no box: nothing in the game reads
    one, so it is written the box a new joint is made with, and said so. A
    joint holds one box, which is all there is room for on it.
    """
    painted = painted_on(armature, skinned)
    for bone in armature.data.bones:
        box = influence.box_of(armature, bone.name)
        if box is None:
            if bone.name in painted:
                warn(f"Joint '{bone.name}' has no influence box, so it is "
                     f"written the box a new joint is made with. Its weights "
                     f"are the painted ones either way.",
                     fix="Press Add Influence Box in the joint panel to give "
                         "it one of its own.")
            else:
                fail(f"Joint '{bone.name}' has no influence box and no weights "
                     f"painted, so there is nothing to weight it from.",
                     fix="Paint its weights, or press Add Influence Box in the "
                         "joint panel and fit the box around what it moves.")
            continue
        if influence.is_flat(box) and bone.name not in painted:
            fail(f"Joint '{bone.name}' has no weights painted, and its "
                 f"influence box has an axis of no length, so no weights can "
                 f"be made from it.",
                 fix="Give the box a size along that axis in the joint panel, "
                     "or paint the joint's weights.")



def validate_vertex_groups(obj, armature, warn):
    """Every vertex group carrying weight has to name a joint.

    Vertices bind to joints by vertex-group name. Blender does not rename a
    vertex group when the joint it matches is renamed, so a rename silently
    unbinds the geometry: those vertices fall through to the mesh frame instead
    of following the joint, and nothing says so until you see it in game.
    """
    if armature is None or obj.data is None:
        return
    # Only the joints hanging from the mesh take part in its skin; a joint
    # the mesh itself hangs from, or one beside it, does not.
    space = JointSpace(armature, obj)
    joints = {bone.name for bone in space.joints() if space.below_mesh(bone)}
    # The mesh's own share is how a vertex says "follow the mesh, not a
    # joint" - not a mistake.
    held = share_group(obj, armature)
    blend = {held[0].name} if held is not None else set()
    # A morph region's vertex group says which vertices a morph moves, not
    # which joint they follow.
    blend |= {group.vertex_group for group in obj.ls3d_morph_groups
              if group.vertex_group}

    weighted = set()
    groups = obj.vertex_groups
    for vertex in obj.data.vertices:
        for entry in vertex.groups:
            if entry.weight <= EPSILON or entry.group >= len(groups):
                continue
            weighted.add(groups[entry.group].name)

    stray = sorted(weighted - joints - blend)
    if not stray:
        return
    listed = ", ".join(f"'{name}'" for name in stray[:4])
    if len(stray) > 4:
        listed += f" and {len(stray) - 4} more"
    warn(f"'{obj.name}' has weight in {len(stray)} vertex group(s) that match "
         f"no joint hanging from it: {listed}. Those vertices follow the mesh "
         f"instead.",
         fix="Rename the group to match its joint, or the joint to match the "
             "group - Blender does not keep the two in step for you.")


def validate_skinned_mesh(obj, wants_shape_keys, type_name, fail):
    """Structural requirements a Single Mesh / Single Morph has to meet."""
    mesh = obj.data
    if mesh is None or not mesh.vertices:
        fail(f"{type_name} '{obj.name}' has no geometry.",
             fix="Give it vertices, or change its visual type.")
        return

    shape_keys = mesh.shape_keys
    has_keys = bool(shape_keys and len(shape_keys.key_blocks) > 1)
    # A Single Morph with nothing to morph is written with an empty morph
    # block, which the game ships thirteen of - so that is said by the morph
    # group check rather than refused here.
    if not wants_shape_keys and has_keys:
        fail(f"{type_name} '{obj.name}' has shape keys; a Single Mesh cannot "
             f"carry both weights and morph targets.",
             fix="Remove the shape keys, or change the visual type to Single "
                 "Morph.")

    if find_armature(obj) is None:
        fail(f"{type_name} '{obj.name}' is not bound to an armature.",
             fix="Parent it to an armature, or add an Armature modifier.")
        return
    # No painted weights is not a fault: a joint with none has its weights made
    # from its influence box.


def validate_character_name(obj, type_name, warn):
    """A skinned mesh the game's character animations can move.

    Those animations move the body - a crouch, a side roll - through the
    frame called ``base``, matched by its exact name, and only turn the joints.
    A skinned mesh called anything else keeps still through them while its
    limbs bend. The game's own animals are the exception, with animations of
    their own keyed to their own names, so this warns rather than refuses.
    """
    if obj.name == C.CHARACTER_MESH_NAME:
        return
    warn(f"{type_name} '{obj.name}' is not called "
         f"'{C.CHARACTER_MESH_NAME}'. The game's character animations move "
         f"the body through the frame of that name and only turn the joints, "
         f"so they will not move this mesh: the limbs bend, but the body stays "
         f"where it stood through a crouch or a roll.",
         fix=f"Rename the mesh to '{C.CHARACTER_MESH_NAME}'. A model played "
             f"only by animations made for it, keyed to its own name - as the "
             f"game's birds and dogs are - can keep its name.")


# ── Morph groups ──────────────────────────────────────────────────────────────
def validate_morph_groups(obj, lods, type_name, fail, warn):
    """Every group needs a basis key that actually exists on the mesh."""
    groups = obj.ls3d_morph_groups
    if not groups:
        warn(f"'{obj.name}' is set to {type_name} but has no morph groups, so "
             f"its morph block is written with no targets.",
             fix="Add a morph group in the 4DS panel if it should morph.")
        return

    if max((len(g.targets) for g in groups), default=0) == 0:
        warn(f"'{obj.name}' is set to {type_name} but no morph group has any "
             f"targets; no morph data will be written.",
             fix="Add targets to a morph group, or change the visual type.")
        return

    referenced = False
    for lod_obj in lods:
        shape_keys = lod_obj.data.shape_keys
        if not shape_keys:
            continue
        for group in groups:
            if not group.targets:
                continue
            basis = group.targets[0].shape_key_name
            if shape_keys.key_blocks.get(basis) is None:
                fail(f"Morph group '{group.name}': basis shape key '{basis}' "
                     f"is missing from '{lod_obj.name}'.",
                     fix="Point the group's first target at a shape key that "
                         "exists.")
            if any(shape_keys.key_blocks.get(t.shape_key_name)
                   for t in group.targets):
                referenced = True

    if not referenced:
        warn(f"'{obj.name}': none of the shape keys named in its morph groups "
             f"exist on the mesh; no morph data will be written.",
             fix="Point the morph targets at shape keys the mesh actually has.")



# ── Textures ──────────────────────────────────────────────────────────────────
# The game's image loader is narrow in ways nothing in Blender hints at, and a
# texture it cannot read is not an export failure: the model file is perfectly
# valid, the surface just comes out wrong in game. So everything here is said
# out loud rather than refused.
#
# Every rule below was measured against the 4651 textures the game ships, which
# are plain 40-byte-header BMPs, uncompressed, at 8 or 24 bits.

#: The only two extensions the loader recognizes; see the constant itself.
TEXTURE_EXTENSIONS = C.TEXTURE_EXTENSIONS

#: What the fix line says for anything the material panel's button repairs.
FIX_WITH_BUTTON = ("Press Fix Texture File under the texture in the material "
                   "panel - or, for an animated one, Fix Frame Files in its "
                   "Texture Animation box.")

#: The loader's BMP and TGA limits, shared with the repair that fixes them.
BMP_INFO_HEADER_BYTES = C.BMP_INFO_HEADER_BYTES
BMP_BIT_DEPTHS = C.BMP_BIT_DEPTHS
TGA_COMPRESSED_TYPES = C.TGA_COMPRESSED_TYPES
TGA_ORIGIN_RIGHT = C.TGA_ORIGIN_RIGHT

#: Where the animated-texture frame counter writes its two digits: this far
#: back from the end of the name, followed by a dot and the extension.
ANIMATED_NAME_TAIL = 6


def _power_of_two(value):
    return value > 0 and (value & (value - 1)) == 0


def next_power_of_two(value):
    """The side the game loads a texture at: *value* rounded up to a power of two."""
    side = 1
    while side < value:
        side *= 2
    return side


def _size_problems(width, height):
    """Sides that are not a power of two, each checked on its own.

    The game rounds each side up to the next power of two independently and
    enlarges the image to fit with no smoothing, so the message names which side
    is off and the size it will actually be drawn at.
    """
    wide_ok = _power_of_two(width)
    high_ok = _power_of_two(height)
    if wide_ok and high_ok:
        return []
    wide_up = next_power_of_two(width)
    high_up = next_power_of_two(height)
    if not wide_ok and not high_ok:
        which = (f"width {width} and height {height} are not a power of two, "
                 f"the game rounds them up to {wide_up} and {high_up}")
    elif not wide_ok:
        which = (f"width {width} is not a power of two, the game rounds it up "
                 f"to {wide_up}")
    else:
        which = (f"height {height} is not a power of two, the game rounds it "
                 f"up to {high_up}")
    return [(f"is {width}x{height}: {which}",
             f"Resize it to {wide_up}x{high_up} in an image editor.")]


def texture_problems(path):
    """What the game's loader would make of the image at *path*.

    Returns ``[(problem, fix)]``. Only the header is read, so this is cheap
    enough to run over every material on every export.
    """
    problems = []
    extension = os.path.splitext(path)[1].lower()
    if extension not in TEXTURE_EXTENSIONS:
        return [(f"is {extension or 'a file with no extension'}, and the game "
                 f"reads only .bmp and .tga",
                 "Convert it to a 24-bit .bmp.")]

    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError as exc:
        return [(f"could not be read ({exc.strerror or exc})",
                 "Check the file is where the material says it is.")]

    if extension == ".tga":
        if len(head) < 18:
            return [("is too short to be a Targa", "Re-save it.")]
        image_type = head[2]
        descriptor = head[17]
        width, height = struct.unpack("<HH", head[12:16])
        if image_type in TGA_COMPRESSED_TYPES:
            problems.append((
                "is a run-length encoded Targa, and the loader reads Targa "
                "pixels straight through without unpacking them",
                FIX_WITH_BUTTON))
        if descriptor & TGA_ORIGIN_RIGHT:
            problems.append((
                "is a right-to-left Targa; the loader notices and then reads "
                "it left-to-right anyway, so it comes out mirrored",
                FIX_WITH_BUTTON))
        return problems + _size_problems(width, height)

    if head[:2] != b"BM":
        return [("does not start with a BMP signature",
                 "Re-save it as a Windows .bmp.")]
    if len(head) < 34:
        return [("is too short to hold a BMP header", "Re-save it.")]

    pixel_offset = struct.unpack("<I", head[10:14])[0]
    header_size = struct.unpack("<I", head[14:18])[0]
    width, height, _planes, depth, compression = struct.unpack("<iihhI",
                                                               head[18:34])

    if header_size != BMP_INFO_HEADER_BYTES:
        slide = pixel_offset - (14 + BMP_INFO_HEADER_BYTES)
        named = {108: "BITMAPV4HEADER", 124: "BITMAPV5HEADER"}.get(header_size)
        pixels_out = abs(slide) * 8 // max(depth, 1)
        problems.append((
            f"has a {header_size}-byte info header"
            + (f" ({named})" if named else "")
            + f" and the loader reads exactly {BMP_INFO_HEADER_BYTES}, so it "
              f"starts taking pixels {abs(slide)} byte(s) "
            + ("early" if slide > 0 else "late")
            + f". The image arrives about {pixels_out} pixel(s) out of place "
              f"and every UV on it lands somewhere else",
            FIX_WITH_BUTTON))
    elif not compression:
        entries = struct.unpack("<I", head[46:50])[0] if len(head) >= 50 else 0
        palette = 4 * (entries or ((1 << depth) if 0 < depth <= 8 else 0))
        expected = 14 + BMP_INFO_HEADER_BYTES + palette
        if pixel_offset != expected:
            problems.append((
                f"keeps its pixels at byte {pixel_offset}, but the loader reads "
                f"them straight after the header and palette, at byte "
                f"{expected}, so the image arrives out of place",
                FIX_WITH_BUTTON))

    if height < 0:
        problems.append((
            "stores its rows top to bottom, which the loader cannot read, so "
            "nothing is drawn with it",
            FIX_WITH_BUTTON))

    if compression:
        problems.append((
            f"uses BMP compression {compression}, which the loader refuses "
            f"outright, so nothing is drawn with it",
            FIX_WITH_BUTTON))

    if depth not in BMP_BIT_DEPTHS:
        problems.append((
            f"is {depth} bits per pixel, and the loader can expand only "
            f"{', '.join(str(d) for d in BMP_BIT_DEPTHS)}",
            FIX_WITH_BUTTON))

    return problems + _size_problems(width, abs(height))


def animated_name_problem(name, frames):
    """Whether the frame counter can spell *name*'s frames, as ``(problem, fix)``.

    The game builds each frame's file name by writing two digits a fixed
    distance back from the end of the one it was given, then a dot. A name not
    shaped ``<base><two digits>.<extension>`` gets those digits written over
    whatever happens to sit there.
    """
    if frames < 2:
        return None
    if len(name) < ANIMATED_NAME_TAIL:
        return (f"is animated, and '{name}' is too short for the frame "
                f"counter, which writes two digits {ANIMATED_NAME_TAIL} "
                f"characters back from the end of the name",
                "Name the frames like 'WATER00.BMP', numbered from 00.")
    tail = name[-ANIMATED_NAME_TAIL:]
    if not (tail[:2].isdigit() and tail[2] == "."):
        return (f"is animated, but '{name}' does not end in two digits, a dot "
                f"and a three-letter extension, so the frame counter writes "
                f"its digits over the middle of the name",
                "Name the frames like 'WATER00.BMP', numbered from 00.")
    return None
