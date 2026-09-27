"""End-to-end tests that run the addon inside Blender.

These need Blender, unlike ``verify_corpus`` which exercises the format layer on
its own. Run them with::

    blender --background --factory-startup \
        --python io_mafia_toolkit/tools/blender_tests.py -- \
        --models "D:/Hry/Mafia Editovani/models" \
        --maps   "D:/Hry/Mafia Editovani/maps"

Two suites run:

* **round-trip** - import each model, export it, and compare the two documents
  structurally (frame, vertex, face, material, joint, morph and portal counts).
* **regression** - one focused check per data-loss bug that has been fixed, so a
  future change that reintroduces one fails here rather than in someone's model.
"""

import argparse
import dataclasses as _dataclasses
import hashlib
import math
import os
import re
import shutil
import struct
import sys

import bmesh
import bpy

_ADDON_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _ADDON_PARENT not in sys.path:
    sys.path.insert(0, _ADDON_PARENT)

import io_mafia_toolkit                                          # noqa: E402
from io_mafia_toolkit.packages import module as ls3d_module      # noqa: E402
read_file = ls3d_module("4ds.codec").read_file
from io_mafia_toolkit.common import constants as C   # noqa: E402

PASSED = []
FAILED = []
SKIPPED = []

#: Counts a model is known to come back with, and why. One LOD in the whole
#: game - 'Object01' of '4old071' - ends on a real vertex that cannot be told
#: apart from the copy the import makes for a face naming a vertex twice (see
#: `repeated_vertices` in 4ds/mesh.py): both are a last vertex used once,
#: sitting on an earlier corner of its own face with the same normal and UV.
#: The export merges it back, so the model comes out one vertex shorter with
#: one already-flat face written as a repeated corner instead. Nothing the
#: game draws changes. No other model of the 3,119 is in this position, so a
#: second one showing up here is a real failure.
KNOWN_DEVIATIONS = {
    "4old071.4ds": ({"vertices": -1, "degenerate_faces": 1},
                    "its last vertex cannot be told from a copy"),
}

#: Bone-parented objects must reproduce the engine's frame chain to float
#: precision, so this only absorbs accumulation noise.
BONE_PARENT_TOLERANCE = 1e-4


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line, flush=True)
    return condition


def summarize(doc):
    """Counts that must survive an import/export cycle unchanged."""
    totals = {
        "frames": len(doc.frames), "materials": len(doc.materials),
        "vertices": 0, "faces": 0, "lods": 0, "joints": 0, "dummies": 0,
        "targets": 0, "target_links": 0, "sectors": 0, "portals": 0,
        "morph_regions": 0, "morph_vertices": 0, "instances": 0,
        "glows": 0, "mirrors": 0, "bone_groups": 0,
    }
    # Counted separately: a face naming a vertex twice is kept, not dropped -
    # the import gives the repeat a copy so Blender can hold the face, and the
    # export writes the face back as it was. A model comes back with as many
    # as it came with.
    degenerate = 0
    textures = set()
    for frame in doc.frames:
        totals["joints"] += bool(frame.joint)
        totals["dummies"] += bool(frame.dummy)
        totals["mirrors"] += bool(frame.mirror)
        if frame.target:
            totals["targets"] += 1
            totals["target_links"] += len(frame.target.links)
        if frame.sector:
            totals["sectors"] += 1
            totals["portals"] += len(frame.sector.portals)
        if frame.lens_flare:
            totals["glows"] += len(frame.lens_flare.glows)
        if frame.geometry:
            totals["instances"] += bool(frame.geometry.instance_id)
            totals["lods"] += len(frame.geometry.lods)
            for lod in frame.geometry.lods:
                totals["vertices"] += len(lod.vertices)
                for group in lod.face_groups:
                    totals["faces"] += len(group.faces)
                    degenerate += sum(1 for f in group.faces if len(set(f)) < 3)
        if frame.skin:
            totals["bone_groups"] += sum(len(l.groups) for l in frame.skin.lods)
        if frame.morph and frame.morph.target_count:
            for lod in frame.morph.lods:
                totals["morph_regions"] += len(lod.regions)
                totals["morph_vertices"] += sum(len(r.indices) for r in lod.regions)
    for material in doc.materials:
        for name in (material.diffuse_texture, material.alpha_texture,
                     material.env_texture):
            if name:
                textures.add(name.upper())
    totals["degenerate_faces"] = degenerate
    return totals, textures


def import_op(path):
    return getattr(bpy.ops.import_scene, "4ds")(filepath=path)


def export_op(path):
    return getattr(bpy.ops.export_scene, "4ds")(filepath=path)


def fresh_scene():
    """Empty the scene between tests.

    Datablocks are purged directly rather than by calling
    ``wm.read_factory_settings``: looping that call in background mode destabilizes
    Blender after a handful of iterations and eventually crashes the process,
    which would look like an addon fault.
    """
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    # Removing one block can drop another's user count to zero, so sweep
    # repeatedly until nothing more can go. Leftovers accumulate across models
    # and eventually destabilize a background session.
    collections = (bpy.data.meshes, bpy.data.armatures, bpy.data.materials,
                   bpy.data.images, bpy.data.actions, bpy.data.shape_keys,
                   bpy.data.node_groups, bpy.data.textures)
    for _ in range(4):
        removed = 0
        for collection in collections:
            for block in list(collection):
                if block.users == 0:
                    collection.remove(block)
                    removed += 1
        if not removed:
            break


def _non_unit_normals(doc, tolerance=1e-3):
    """``(worst length, frame name, count)`` for normals that are not unit."""
    worst = 1.0
    where = None
    count = 0
    for frame in doc.frames:
        if not frame.geometry:
            continue
        for lod in frame.geometry.lods:
            for vertex in lod.vertices:
                length = math.sqrt(sum(c * c for c in vertex.normal))
                if abs(length - 1.0) > tolerance:
                    count += 1
                    if abs(length - 1.0) > abs(worst - 1.0):
                        worst, where = length, frame.name
    return (worst, where, count) if count else None


def run_round_trip(models, out_dir):
    """Import, export and compare. Vertex counts may legitimately grow when a
    model makes surfaces double-sided with reversed duplicate faces, so that one
    difference is reported but not treated as a failure."""
    print("\n=== round-trip ===")
    for path in models:
        name = os.path.basename(path)
        # Always clear first: --factory-startup opens with the default cube and
        # its material, which would otherwise be exported alongside the model.
        fresh_scene()
        try:
            source_doc = read_file(path)
            before, before_textures = summarize(source_doc)
        except Exception as exc:
            # A file the format layer cannot read at all (an older version, or a
            # damaged one) is not a round-trip failure - the addon is expected
            # to refuse it with a clear message.
            SKIPPED.append(name)
            print(f"[SKIP] round-trip {name}  ({exc})", flush=True)
            continue
        if import_op(path) != {"FINISHED"}:
            check(f"round-trip {name}", False, "import failed")
            continue
        out = os.path.join(out_dir, name)
        if export_op(out) != {"FINISHED"}:
            check(f"round-trip {name}", False, "export failed")
            continue
        written = read_file(out)
        after, after_textures = summarize(written)

        # The export must not *introduce* a normal that is not unit length,
        # but it must not tidy one away either: 76 of the game's own normals
        # are off unit and are the model's data, so they come back out as they
        # went in. What would be wrong is more of them coming out than went in.
        bad = _non_unit_normals(written)
        was_bad = _non_unit_normals(source_doc)
        if bad and bad[2] > (was_bad[2] if was_bad else 0):
            worst, where, count = bad
            check(f"round-trip {name}: no normal loses its length", False,
                  f"{count} not unit, was {was_bad[2] if was_bad else 0}; "
                  f"worst length {worst:.6f} in '{where}'")
            continue

        lost = before_textures - after_textures
        # A vertex count may legitimately grow (see the double-sided note above),
        # so it is reported but does not fail the check. Everything else must
        # match, faces naming a vertex twice included: they are the file's own
        # and come back as they were.
        expected = dict(before)
        known, why = KNOWN_DEVIATIONS.get(os.path.basename(name).lower(),
                                          ({}, ""))
        for key, delta in known.items():
            expected[key] += delta

        # Counts that may legitimately come out higher without anything being
        # lost: a vertex splits when its corner normals genuinely disagree, and
        # geometry gets duplicated when an object's parent forces it later in
        # the file than an object instancing it. Fewer than expected always
        # means something went missing, so that still fails.
        MAY_EXCEED = ("vertices", "faces", "lods")
        hard = []
        for key in expected:
            if key == "vertices":
                continue
            if key == "instances":
                # An instance may decay into a full copy, never the reverse.
                if after[key] > expected[key]:
                    hard.append(f"instances {before[key]}->{after[key]} "
                                f"(expected at most {expected[key]})")
                continue
            if key in MAY_EXCEED:
                if after[key] < expected[key]:
                    hard.append(f"{key} {before[key]}->{after[key]} "
                                f"(expected at least {expected[key]})")
                continue
            if after[key] != expected[key]:
                hard.append(f"{key} {before[key]}->{after[key]} "
                            f"(expected {expected[key]})")
        if lost:
            hard.append(f"textures lost {sorted(lost)[:3]}")
        soft = (f"vertices {before['vertices']}->{after['vertices']}"
                if before["vertices"] != after["vertices"] else "")

        parts = hard + ([soft] if soft else [])
        if why:
            parts.append(f"known: {why}")
        check(f"round-trip {name}", not hard,
              "; ".join(parts) if parts else "identical")


def _to_signed(value):
    """A 32-bit flag word as Blender's signed IntProperty stores it."""
    return value - (1 << 32) if value >= (1 << 31) else value


def _bits(document):
    """Every value in a document by its field path, floats by their bits."""
    out = {}

    def walk(value, path):
        if _dataclasses.is_dataclass(value) and not isinstance(value, type):
            for field in _dataclasses.fields(value):
                walk(getattr(value, field.name), f"{path}.{field.name}")
        elif isinstance(value, (list, tuple)):
            out[path + ".len"] = len(value)
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, float):
            out[path] = struct.pack("<d", value)
        else:
            out[path] = value
    walk(document, "")
    return out


def run_regressions(models_dir, out_dir):
    """One check per fixed data-loss bug."""
    print("\n=== regressions ===")

    def model(name):
        return os.path.join(models_dir, name)

    # A failed export must leave any existing file exactly as it was.
    fresh_scene()
    target = os.path.join(out_dir, "atomic.4ds")
    if os.path.isfile(model("Tommy.4ds")):
        import_op(model("Tommy.4ds"))
        export_op(target)
        original = hashlib.sha256(open(target, "rb").read()).digest()

        skin = [o for o in bpy.context.scene.objects
                if o.type == "MESH" and int(o.visual_type) in (2, 3)][0]
        clone = skin.copy()
        clone.data = skin.data.copy()
        clone.name = "second_skin"
        bpy.context.collection.objects.link(clone)

        result = export_op(target)
        unchanged = hashlib.sha256(open(target, "rb").read()).digest() == original
        leftovers = [f for f in os.listdir(out_dir) if f.endswith(".tmp")]
        check("failed export preserves the existing file",
              result == {"CANCELLED"} and unchanged and not leftovers,
              f"result={result} leftovers={leftovers}")

        # Applying a new basis must not disturb any other morph group.
        fresh_scene()
        import_op(model("Tommy.4ds"))
        obj = next((o for o in bpy.context.scene.objects
                    if o.type == "MESH" and len(o.ls3d_morph_groups) > 1), None)
        if obj is not None:
            bpy.context.view_layer.objects.active = obj
            others = obj.ls3d_morph_groups[1]
            keys = obj.data.shape_keys.key_blocks
            snapshot = {t.shape_key_name: [p.co.copy() for p in keys[t.shape_key_name].data]
                        for t in others.targets if t.shape_key_name in keys}
            obj.ls3d_active_morph_group = 0
            obj.ls3d_morph_groups[0].active_target_index = 1
            outcome = bpy.ops.ls3d.morph_make_basis()
            disturbed = sum(
                1 for key, before in snapshot.items()
                if any((a.co - b).length > 1e-6
                       for a, b in zip(keys[key].data, before)))
            check("apply-as-basis leaves other groups untouched",
                  outcome == {"FINISHED"} and disturbed == 0,
                  f"disturbed={disturbed}")

        # A skinned mesh bound only by an Armature modifier must still export.
        fresh_scene()
        import_op(model("Tommy.4ds"))
        skin = [o for o in bpy.context.scene.objects
                if o.type == "MESH" and int(o.visual_type) in (2, 3)][0]
        world = skin.matrix_world.copy()
        skin.parent = None
        skin.matrix_world = world
        path = os.path.join(out_dir, "modifier_only.4ds")
        result = export_op(path)
        present = (result == {"FINISHED"}
                   and any(f.name == skin.name for f in read_file(path).frames))
        check("modifier-only skinned mesh is exported", present, f"result={result}")

    # Objects parented to a bone must land where the frame chain says. The
    # engine composes T@R@S down the whole chain including each joint's scale,
    # while bones are placed at their scale-free rest position - so the
    # bone-parent matrix has to compensate. Dropping that term put hands, eyes
    # and weapon dummies on the high-detail characters in the wrong place.
    if os.path.isfile(model("!TommyHIGH.4ds")):
        from mathutils import Matrix, Quaternion, Vector
        fresh_scene()
        source = read_file(model("!TommyHIGH.4ds"))
        import_op(model("!TommyHIGH.4ds"))

        # Target frames are mirrored into Blender as Track To constraints, which
        # deliberately pose the bones they act on - TommyHIGH's neck tracks
        # 'targetN', which swings the eyes and head mesh by a few millimeters.
        # That is the feature working, so mute it to compare rest positions.
        for armature in bpy.context.scene.objects:
            if armature.type != "ARMATURE":
                continue
            for pose_bone in armature.pose.bones:
                for constraint in pose_bone.constraints:
                    constraint.mute = True
        bpy.context.view_layer.update()

        def frame_world(index, cache={}):
            if index in cache:
                return cache[index]
            frame = source.frames[index - 1]
            local = Matrix.LocRotScale(
                Vector((frame.position[0], frame.position[2], frame.position[1])),
                Quaternion((frame.rotation[0], frame.rotation[1],
                            frame.rotation[3], frame.rotation[2])),
                Vector((frame.scale[0], frame.scale[2], frame.scale[1])))
            cache[index] = local
            if frame.parent_id:
                local = frame_world(frame.parent_id) @ local
            cache[index] = local
            return local

        misplaced = []
        checked = 0
        for index, frame in enumerate(source.frames, start=1):
            obj = bpy.data.objects.get(frame.name)
            if obj is None or obj.parent_type != "BONE":
                continue
            checked += 1
            want = frame_world(index).translation
            got = obj.matrix_world.translation
            # This is the format's own placement rule, so it must match
            # exactly. Without the compensation term these objects land
            # 3.4-4.4 cm out.
            if (want - got).length > BONE_PARENT_TOLERANCE:
                misplaced.append((frame.name,
                                  [round(v, 3) for v in want],
                                  [round(v, 3) for v in got]))
        check("bone-parented objects sit where the frame chain puts them",
              checked > 0 and not misplaced,
              f"checked {checked}, misplaced {misplaced[:3]}")

        # Joint scale is an editable 4DS value with no Blender equivalent, and
    # it must stay purely informational: the bone's real scale stays at 1 and
    # editing the value must not move the skinned mesh. The engine agrees -
    # the stored inverse-bind matrices cancel the authored scale exactly, so
    # the game draws the mesh undeformed at rest.
    if os.path.isfile(model("!TommyHIGH.4ds")):
        fresh_scene()
        import_op(model("!TommyHIGH.4ds"))
        bpy.context.view_layer.update()
        skin = next((o for o in bpy.context.scene.objects
                     if o.type == "MESH" and int(o.visual_type) in (2, 3)), None)
        armature = next((o for o in bpy.context.scene.objects
                         if o.type == "ARMATURE"), None)

        def deformed_points():
            """World-space vertex positions after the armature modifier."""
            graph = bpy.context.evaluated_depsgraph_get()
            evaluated = skin.evaluated_get(graph)
            mesh = evaluated.to_mesh()
            points = [skin.matrix_world @ v.co.copy() for v in mesh.vertices]
            evaluated.to_mesh_clear()
            return points

        if skin is not None and armature is not None:
            root = next((b for b in armature.pose.bones
                         if b.get("ls3d_joint_scale")
                         and abs(b["ls3d_joint_scale"][0] - 1.0) > 1e-4), None)
            if root is not None:
                at_import = deformed_points()
                pose_scale = tuple(round(v, 4) for v in root.scale)
                locked = tuple(root.lock_scale)
                authored = tuple(root["ls3d_joint_scale"])

                root["ls3d_joint_scale"] = tuple(v * 2.0 for v in authored)
                armature.update_tag()
                skin.update_tag()
                bpy.context.view_layer.update()
                moved = max(((a - b).length for a, b in
                             zip(at_import, deformed_points())), default=0.0)
                readback = tuple(round(v, 4) for v in root["ls3d_joint_scale"])
                root["ls3d_joint_scale"] = authored

                check("joint scale is editable but never moves the mesh",
                      pose_scale == (1.0, 1.0, 1.0) and locked == (True, True, True)
                      and moved < 1e-6
                      and readback == tuple(round(v * 2.0, 4) for v in authored),
                      f"pose scale {pose_scale} locked={locked}; doubling "
                      f"'{root.name}' moved a vertex {moved:.6f} and read back "
                      f"{readback}")

        # Joint scale must survive into both inverse-bind matrices the format
    # stores: the one in each FRAME_JOINT payload and the one per bone group in
    # the skin block. The engine cancels them against the *scaled* joint world
    # at skin time, so a bind built from the unscaled rest matrix leaves the
    # scale uncanceled and visibly deforms the mesh. Scale propagates down the
    # chain, so this bites bones that are themselves at 1.0.
    for model_name in ("!TommyHIGH.4ds", "pes03.4ds"):
        if not os.path.isfile(model(model_name)):
            continue
        fresh_scene()
        source = read_file(model(model_name))
        import_op(model(model_name))
        path = os.path.join(out_dir, f"bind_{model_name}")
        if export_op(path) != {"FINISHED"}:
            check(f"bind matrices survive joint scale ({model_name})", False,
                  "export failed")
            continue

        def binds_by_name(doc):
            skin = next((f for f in doc.frames if f.skin and f.skin.lods), None)
            if skin is None:
                return {}
            groups = skin.skin.lods[0].groups
            return {f.name: (groups[f.joint.joint_id].inverse_bind,
                             f.joint.matrix, f.scale)
                    for f in doc.frames
                    if f.joint is not None and f.joint.joint_id < len(groups)}

        before, after = binds_by_name(source), binds_by_name(read_file(path))
        worst_skin = worst_joint = 0.0
        worst_bone = None
        for bone_name, (skin_bind, joint_bind, _scale) in before.items():
            if bone_name not in after:
                continue
            new_skin, new_joint, _ = after[bone_name]
            ds = max(abs(a - b) for a, b in zip(skin_bind, new_skin))
            dj = max(abs(a - b) for a, b in zip(joint_bind, new_joint))
            if max(ds, dj) > max(worst_skin, worst_joint):
                worst_bone = bone_name
            worst_skin = max(worst_skin, ds)
            worst_joint = max(worst_joint, dj)
        check(f"bind matrices survive joint scale ({model_name})",
              worst_skin < 1e-4 and worst_joint < 1e-4,
              f"skin-block bind {worst_skin:.6f}, FRAME_JOINT payload "
              f"{worst_joint:.6f}"
              + (f" worst at '{worst_bone}'" if worst_bone else ""))

        # A billboard's axis is stored independently of its lock flag and must
    # survive whichever mode is set. 127 shipping billboards use axis 2 with the
    # lock clear, and an earlier rule rewrote every one of them to axis 0.
    for axis_prop, mode_prop, expect in (("3", "1", (2, False)),
                                         ("3", "2", (2, True)),
                                         ("2", "1", (1, False)),
                                         ("1", "1", (0, False))):
        fresh_scene()
        bpy.ops.mesh.primitive_plane_add()
        board = bpy.context.object
        board.ls3d_frame_type = str(1)
        board.visual_type = str(4)
        board.rot_axis = axis_prop
        board.rot_mode = mode_prop
        path = os.path.join(out_dir, "billboard.4ds")
        result = export_op(path)
        got = None
        if result == {"FINISHED"}:
            for frame in read_file(path).frames:
                if frame.billboard is not None:
                    got = (frame.billboard.axis, frame.billboard.axis_locked)
                    break
        check(f"billboard axis={axis_prop} mode={mode_prop} round-trips",
              got == expect, f"wrote {got}, expected {expect}")

    # The group's basis is pinned to the first slot. The arrows previously let
    # it be moved - or let slot 1 move up into it - which silently rebased the
    # group while the list still labeled row 0 "Basis".
    if os.path.isfile(model("!TommyHIGH.4ds")):
        fresh_scene()
        import_op(model("!TommyHIGH.4ds"))
        obj = next((o for o in bpy.context.scene.objects
                    if o.type == "MESH" and len(o.ls3d_morph_groups) > 0), None)
        if obj is not None and len(obj.ls3d_morph_groups[0].targets) > 3:
            bpy.context.view_layer.objects.active = obj
            group = obj.ls3d_morph_groups[0]
            names = lambda: [t.shape_key_name for t in group.targets]
            basis = names()[0]

            def attempt(index, action):
                group.active_target_index = index
                return bpy.ops.ls3d.morph_target(action=action)

            refused = [attempt(0, "DOWN"), attempt(0, "UP"), attempt(1, "UP")]
            pinned_after_refusals = names()[0] == basis

            before = names()
            moved_ok = attempt(2, "UP") == {"FINISHED"} and names() != before
            still_pinned = names()[0] == basis

            group.active_target_index = 2
            promoted = names()[2]
            rebased = (bpy.ops.ls3d.morph_make_basis() == {"FINISHED"}
                       and names()[0] == promoted)

            check("the morph basis cannot be moved out of the first slot",
                  all(r == {"CANCELLED"} for r in refused)
                  and pinned_after_refusals and moved_ok and still_pinned
                  and rebased,
                  f"refusals={refused}, other targets still reorder={moved_ok}, "
                  f"basis pinned={still_pinned}, Apply as Basis works={rebased}")

    # The object panel names each instanced frame COPY or MASTER, from the
    # export's own plan rather than anything the importer left behind - so what
    # it says is what the file holds, for an imported model and for linked
    # duplicates made in Blender alike. (The viewport labels and the faded
    # object color that once did this too are gone.)
    class _PanelLines:
        def __init__(self):
            self.lines = []

        def _child(self, *a, **k):
            return self

        box = row = column = split = grid_flow = _child

        def label(self, text="", **k):
            self.lines.append(text)

        def prop(self, *a, **k):
            pass

        def separator(self, *a, **k):
            pass

        def operator(self, *a, **k):
            pass

        def panel(self, *a, **k):
            # An open body, so what a folded section says is read as well.
            return self, self

    _vp = ls3d_module("4ds.viewport")
    _panel = ls3d_module("4ds.ui").The4DSObjectPanel

    def _panel_says(obj):
        drawn = _PanelLines()
        _panel._draw_instance(_panel, drawn, obj)
        return drawn.lines

    def _file_instances(path):
        doc = read_file(path)
        names = {i + 1: f.name for i, f in enumerate(doc.frames)}
        return {f.name: names[f.geometry.instance_id] for f in doc.frames
                if f.geometry is not None and f.geometry.instance_id}

    instanced = os.path.join(models_dir, "vjezdtov1.4ds")
    if os.path.isfile(instanced):
        fresh_scene()
        import_op(instanced)
        _vp.forget_instance_plan()
        imported_plan = {c.name: m.name for c, m in _vp.instance_plan().items()}
        in_source = _file_instances(instanced)
        copy_name, owner_name = sorted(in_source.items())[0]
        copy_lines = _panel_says(bpy.data.objects[copy_name])
        owner_lines = _panel_says(bpy.data.objects[owner_name])
        # Nothing about the file's instancing is kept on the objects: the
        # panel and the export both work it out from the shared mesh.
        check("the panel names an imported model's copies and master as the file does",
              bool(in_source) and imported_plan == in_source
              and not hasattr(bpy.types.Object, "ls3d_instance_source")
              and not any("ls3d_instance_source" in o.keys()
                          for o in bpy.data.objects)
              and "Instancing: COPY" in copy_lines
              and f"Master: {owner_name}" in copy_lines
              and "Instancing: MASTER" in owner_lines,
              f"file {len(in_source)} copies, plan agrees "
              f"{imported_plan == in_source}; panel on '{copy_name}' "
              f"{copy_lines[:2]}, on '{owner_name}' {owner_lines[:1]}")

    # Linked duplicates made in Blender carry nothing from an import, and the
    # panel has to name them all the same - exactly as the export writes them.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    hand_master = bpy.context.object
    hand_master.name = "hand_post"
    for step in (1, 2):
        duplicate = bpy.data.objects.new(f"hand_post_copy{step}",
                                         hand_master.data)
        duplicate.location = (3.0 * step, 0.0, 0.0)
        bpy.context.collection.objects.link(duplicate)
    bpy.ops.mesh.primitive_uv_sphere_add(location=(0.0, 5.0, 0.0))
    bpy.context.object.name = "hand_lone"
    bpy.context.view_layer.update()

    nothing_recorded = not any("ls3d_instance_source" in o.keys()
                               for o in bpy.data.objects)
    hand_plan = {c.name: m.name for c, m in _vp.instance_plan().items()}
    hand_path = os.path.join(out_dir, "hand_instances.4ds")
    export_op(hand_path)
    hand_written = _file_instances(hand_path)
    copy_lines = _panel_says(bpy.data.objects["hand_post_copy1"])
    master_lines = _panel_says(hand_master)
    lone_lines = _panel_says(bpy.data.objects["hand_lone"])

    check("the panel names linked duplicates made in Blender as the export writes them",
          nothing_recorded
          and hand_plan == {"hand_post_copy1": "hand_post",
                            "hand_post_copy2": "hand_post"}
          and hand_written == hand_plan
          and "Instancing: COPY" in copy_lines
          and "Master: hand_post" in copy_lines
          and "Instancing: MASTER" in master_lines
          and not lone_lines,
          f"nothing recorded {nothing_recorded}; plan {hand_plan}; file "
          f"{hand_written}; panel on a copy {copy_lines[:2]}, on the master "
          f"{master_lines[:1]}, on an unshared object {lone_lines}")

    # The plan is kept between panel redraws and let go when the scene changes,
    # so an edit that changes who owns the mesh has to show at once.
    bpy.data.objects["hand_post_copy2"].visual_type = "2"      # never instanced
    bpy.context.view_layer.update()
    after_type = {c.name: m.name for c, m in _vp.instance_plan().items()}
    written_in_full = _panel_says(bpy.data.objects["hand_post_copy2"])
    bpy.data.objects.remove(hand_master, do_unlink=True)
    bpy.context.view_layer.update()
    after_delete = {c.name: m.name for c, m in _vp.instance_plan().items()}
    check("the panel follows an edit that changes who owns the mesh",
          after_type == {"hand_post_copy1": "hand_post"}
          and "Instancing: WRITTEN IN FULL" in written_in_full
          and after_delete == {},
          f"after making a copy a Single Mesh {after_type} and its panel says "
          f"{written_in_full[:1]}; after deleting the master {after_delete}")

    # Flag panels list only what the engine reads, but every bit must still
    # survive a round trip through the raw byte, and the two-bit shadow and
    # projector fields must decode to what the shipping files actually store.
    flagged = os.path.join(models_dir, "chevroletm6H00.4ds")
    if os.path.isfile(flagged):
        fresh_scene()
        import_op(flagged)
        obj = next((o for o in bpy.data.objects
                    if o.type == "MESH"
                    and int(o.ls3d_frame_type) == C.FRAME_VISUAL), None)
        if obj is not None:
            decoded = (obj.rf2_shadow_diffuse, obj.rf2_projection_diffuse,
                       obj.rf2_is_mesh_object, obj.cf_enabled)
            original = obj.render_flags2 & 0xFF
            original_cull = obj.cull_flags
            obj.rf2_shadow_alpha = True
            widened = obj.render_flags2 & 0xFF
            obj.rf2_shadow_alpha = False
            restored = obj.render_flags2 & 0xFF

            obj.cull_flags = 0xFF
            obj.cf_enabled = False
            reserved_kept = (obj.cull_flags & 0xFF) == 0xFE

            # Every bit of every flag byte must be reachable from a checkbox,
            # and the raw field and the checkboxes must move together.
            cull_bits = {m for m, _a, _l, _d in C.RESERVED_CULL_FLAGS}
            cull_bits.update((C.CF_ENABLED, C.CF_POS_LOCKED))
            render_bits = {m for m, _a, _l, _d in C.RESERVED_RENDER_FLAGS}
            render_bits.update((C.RF_MANAGED_LOD, C.RF_NO_MIRROR,
                                C.RF_FLAT_LIGHT))
            logic_bits = (C.LF_ZBIAS | C.LF_SHADOW_DIFFUSE | C.LF_SHADOW_ALPHA
                          | C.LF_IS_MESH_OBJECT | C.LF_NO_TWOSIDED_COLLISION
                          | C.LF_PROJECTION_DIFFUSE | C.LF_PROJECTION_ALPHA
                          | C.LF_NO_FOG)

            obj.cull_flags = 0x44
            raw_drives_toggles = (obj.cf_res_scale_baked
                                  and obj.cf_res_bound_valid
                                  and not obj.cf_res_matrix_built)
            obj.cf_res_matrix_built = True
            toggles_drive_raw = (obj.cull_flags & 0xFF) == 0x54

            check("every flag bit has a checkbox and both directions agree",
                  sum(cull_bits) == 0xFF and sum(render_bits) == 0xFF
                  and logic_bits == 0xFF
                  and raw_drives_toggles and toggles_drive_raw,
                  f"cull covers 0x{sum(cull_bits):02X}, render 0x{sum(render_bits):02X}, "
                  f"logic 0x{logic_bits:02X}; raw->toggles={raw_drives_toggles}, "
                  f"toggles->raw={toggles_drive_raw}")

            # A bit a model actually uses must never be hidden, even with the
            # "show reserved" preference off.
            ls3d_ui = ls3d_module("4ds.ui")
            obj.cull_flags = 0x44
            hidden = [label for mask, _a, label, _d in C.RESERVED_CULL_FLAGS
                      if (obj.cull_flags & 0xFF) & mask
                      and not (ls3d_ui._show_reserved_flags()
                               or (obj.cull_flags & 0xFF) & mask)]
            visible_off = [label for mask, _a, label, _d in C.RESERVED_CULL_FLAGS
                           if ls3d_ui._show_reserved_flags()
                           or (obj.cull_flags & 0xFF) & mask]
            check("engine bits a model uses are never hidden",
                  not hidden and len(visible_off) >= 2,
                  f"raw 0x44 shows {visible_off}, hidden={hidden or 'none'}")

            obj.cull_flags = original_cull
            check("flag fields decode and preserve every bit",
                  decoded == (True, True, True, True)
                  and widened == original + 4 and restored == original
                  and reserved_kept,
                  f"decoded shadow/projector/mesh/enabled={decoded}, "
                  f"0x{original:02X}->0x{widened:02X}->0x{restored:02X}, "
                  f"raw byte keeps engine bits={reserved_kept}")

    # A texture that cannot be loaded must still keep its name, or re-exporting
    # a model imported without a texture folder writes empty texture names.
    textured = os.path.join(models_dir, "chevroletm6H00.4ds")
    if os.path.isfile(textured):
        import io_mafia_toolkit as _addon
        saved_prefs = _addon.get_preferences
        try:
            class _NoTextures:
                textures_path = ""
                show_raw_flags = True
                show_reserved_flags = False
            _addon.get_preferences = lambda: _NoTextures()

            fresh_scene()
            import_op(textured)
            shown = sum(1 for m in bpy.data.materials
                        if m.ls3d_diffuse_tex is not None)
            target = os.path.join(out_dir, "notextures.4ds")
            export_op(target)
            before = [m.diffuse_texture for m in read_file(textured).materials]
            after = [m.diffuse_texture for m in read_file(target).materials]
            expected = sum(1 for name in before if name)
            kept = sum(1 for a, b in zip(before, after) if a and a == b)
        finally:
            _addon.get_preferences = saved_prefs

        check("texture names survive with no texture folder set",
              kept == expected and shown >= expected,
              f"{kept}/{expected} names written back, {shown} shown in the "
              f"material fields")

    # Every structural check must actually reject the thing it guards against.
    # A check that silently passes bad geometry is worse than no check at all,
    # so each one gets a fixture built to violate it.
    from io_mafia_toolkit.common import report as report_module
    caught = []
    original_error = report_module.Report.error

    def record(self, message, fix=None):
        caught.append(message)
        return original_error(self, message, fix=fix)

    def cube(name, size=2.0):
        bpy.ops.mesh.primitive_cube_add(size=size)
        obj = bpy.context.object
        obj.name = name
        return obj

    def punch_hole(obj):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.data.polygons[0].select = True
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.delete(type="FACE")
        bpy.ops.object.mode_set(mode="OBJECT")

    def open_sector():
        obj = cube("sector_open")
        obj.ls3d_frame_type = str(C.FRAME_SECTOR)
        punch_hole(obj)

    def flat_sector():
        bpy.ops.mesh.primitive_plane_add(size=2.0)
        obj = bpy.context.object
        obj.name = "sector_flat"
        obj.ls3d_frame_type = str(C.FRAME_SECTOR)

    def dented_sector():
        obj = cube("sector_dented")
        obj.ls3d_frame_type = str(C.FRAME_SECTOR)
        obj.data.vertices[0].co = (6.0, 6.0, 6.0)

    def open_occluder():
        obj = cube("occ_open")
        obj.ls3d_frame_type = str(C.FRAME_OCCLUDER)
        punch_hole(obj)

    def wide_portal():
        parent = cube("sector_ok")
        parent.ls3d_frame_type = str(C.FRAME_SECTOR)
        bpy.ops.mesh.primitive_circle_add(vertices=12, fill_type="NGON")
        portal = bpy.context.object
        portal.name = "sector_ok_portal01"
        portal.ls3d_frame_type = str(C.FRAME_SECTOR)
        portal.parent = parent

    def flat_view_box():
        mirror = cube("mirror01")
        mirror.ls3d_frame_type = str(C.FRAME_VISUAL)
        mirror.visual_type = str(C.VISUAL_MIRROR)
        mirror.ls3d_mirror_box_center = (0.0, 1.0, 0.0)
        mirror.ls3d_mirror_box_x = (1.0, 0.0, 0.0)
        mirror.ls3d_mirror_box_y = (0.0, 1.0, 0.0)
        # and no Z axis: nothing the game can look into

    def unset_view_box():
        mirror = cube("mirror02")
        mirror.ls3d_frame_type = str(C.FRAME_VISUAL)
        mirror.visual_type = str(C.VISUAL_MIRROR)

    def crowded_skeleton():
        # One joint more than the game gathers below a skinned mesh.
        bpy.ops.object.armature_add(enter_editmode=True, location=(0.0, 0.0, 0.0))
        rig = bpy.context.object
        bones = rig.data.edit_bones
        parent = bones[0]
        parent.name = "joint0"
        parent.head, parent.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.05)
        for index in range(1, C.MAX_SKIN_JOINTS + 1):
            bone = bones.new(f"joint{index}")
            bone.head = (0.0, 0.0, index * 0.05)
            bone.tail = (0.0, 0.0, index * 0.05 + 0.05)
            bone.parent = parent
            parent = bone
        bpy.ops.object.mode_set(mode="OBJECT")
        body = cube("crowded")
        body.ls3d_frame_type = str(C.FRAME_VISUAL)
        body.visual_type = str(C.VISUAL_SINGLEMESH)
        body.parent = rig
        body.modifiers.new("Armature", "ARMATURE").object = rig

    def groupless_morph():
        obj = cube("morphless")
        obj.ls3d_frame_type = str(C.FRAME_VISUAL)
        obj.visual_type = str(C.VISUAL_MORPH)

    fixtures = (
        ("sector with a hole", open_sector, "not a closed mesh"),
        ("sector that is a flat plane", flat_sector, "does not enclose"),
        ("occluder with a hole", open_occluder, "not a closed mesh"),
        ("portal over the corner limit", wide_portal, "over the limit"),
        ("mirror view box with an axis of no length", flat_view_box, "no z axis"),
        ("mirror with no view box set", unset_view_box, "no x, y, z axis"),
        ("skinned mesh with more joints than the game gathers", crowded_skeleton,
         f"only the first {C.MAX_SKIN_JOINTS}"),
    )

    report_module.Report.error = record
    missed = []
    try:
        for label, build, fragment in fixtures:
            fresh_scene()
            caught.clear()
            build()
            result = export_op(os.path.join(out_dir, "invalid.4ds"))
            if (result != {"CANCELLED"}
                    or not any(fragment in m.lower() for m in caught)):
                missed.append(label)
    finally:
        report_module.Report.error = original_error

    # Convexity only warns: the game ships sectors that fail it, so blocking
    # the export would make the addon unable to round-trip its own missions.
    warned = []
    original_warn = report_module.Report.warn

    def record_warn(self, message, fix=None):
        warned.append(message)
        return original_warn(self, message, fix=fix)

    report_module.Report.warn = record_warn
    try:
        fresh_scene()
        dented_sector()
        convex_result = export_op(os.path.join(out_dir, "dented.4ds"))
    finally:
        report_module.Report.warn = original_warn

    check("a non-convex sector warns but still exports",
          convex_result == {"FINISHED"}
          and any("not convex" in m.lower() for m in warned),
          f"result={convex_result}, warned="
          f"{any('not convex' in m.lower() for m in warned)}")

    # A morph visual with nothing to morph is what thirteen of the game's
    # models are, so it is written - with a morph block holding no targets -
    # and said, rather than refused.
    warned.clear()
    report_module.Report.warn = record_warn
    try:
        fresh_scene()
        groupless_morph()
        morphless_path = os.path.join(out_dir, "morphless.4ds")
        morphless_result = export_op(morphless_path)
    finally:
        report_module.Report.warn = original_warn
    morphless = (read_file(morphless_path).frames
                 if morphless_result == {"FINISHED"} else [])
    check("a morph with no groups exports with an empty morph block",
          morphless_result == {"FINISHED"}
          and any("no morph groups" in m.lower() for m in warned)
          and any(f.morph is not None and f.morph.target_count == 0
                  for f in morphless),
          f"result={morphless_result}, warned="
          f"{any('no morph groups' in m.lower() for m in warned)}")

    check("structural checks reject invalid geometry",
          not missed,
          f"{len(fixtures) - len(missed)}/{len(fixtures)} fixtures rejected"
          + (f"; missed {missed}" if missed else ""))

    # Two builds come out of one tree. The workshop one holds everything, so
    # the Blender it is installed into can run these tests against exactly
    # what is installed; the one that ships holds the add-on and nothing else
    # - a test suite inside somebody else's Blender is neither wanted nor
    # safe to run there. Both are read here from the build script itself, so
    # a folder added later is not quietly shipped.
    import importlib.util as _loading
    _build_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "build_addon.py")
    _spec = _loading.spec_from_file_location("ls3d_build", _build_path)
    _build = _loading.module_from_spec(_spec)
    _spec.loader.exec_module(_build)
    _workshop = [short for _full, short in _build.source_files()]
    _shipped = [short for _full, short in _build.source_files(release=True)]
    _left_out = sorted(set(_workshop) - set(_shipped))
    check("the build that ships holds the add-on and not the workshop",
          _shipped and _workshop
          and not any("/tools/" in short for short in _shipped)
          and any("/tools/" in short for short in _workshop)
          and all("/tools/" in short for short in _left_out)
          and any(short.endswith("/cli.py") for short in _shipped)
          and any(short.endswith("/__init__.py") for short in _shipped)
          and not any(short.endswith(".pyc") or "__pycache__" in short
                      for short in _workshop),
          f"{len(_shipped)} file(s) ship against {len(_workshop)} in the "
          f"workshop build; left out {len(_left_out)}, all of them the "
          f"workshop: {all('/tools/' in short for short in _left_out)}")

    # The command line is the same add-on, driven without a window: the file
    # kinds it knows, the settings it hands the dialogs, and what it makes of
    # a value typed as a word.
    _cli_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "cli.py")
    _spec = _loading.spec_from_file_location("ls3d_cli_test", _cli_path)
    _cli = _loading.module_from_spec(_spec)
    _spec.loader.exec_module(_cli)
    _kinds = {kind: _cli.FORMATS[kind][0] for kind in _cli.FORMATS}
    _values = _cli.options_from(["selection_only=True", "kept=off",
                                 "count=3", "share=0.5", "name=base"])
    _refused = []
    for _bad in ("nonsense", "=1"):
        try:
            _cli.options_from([_bad])
        except ValueError:
            _refused.append(_bad)
    try:
        _cli.format_for("thing.fbx", "open")
        _unknown_kind = False
    except ValueError:
        _unknown_kind = True
    _args = _cli.parser().parse_args(
        ["--open", "a.4ds", "--open", "b.5ds", "--export", "c.4ds",
         "--textures", "somewhere", "--check"])

    # A script handed to a Blender that has a window runs outside that
    # window's own context - bpy.context.window is empty though a window is
    # open - and an operator that polls for what is being looked at refuses.
    # Switching an armature into Edit Mode is one, so importing a character
    # through the command line came out broken with a window open and perfect
    # without one, which is the only reason every test here passed. The
    # command borrows the window when there is one to borrow.
    _borrowed = _cli.working_context()
    _windows = list(getattr(bpy.context.window_manager, "windows", ()) or ())
    _is_plain = type(_borrowed).__name__ == "nullcontext"
    _borrows = bool(_windows) != _is_plain

    # An operator whose poll says no raises rather than returning, and a run
    # that dies on that says nothing useful and leaves an exit code claiming
    # everything went fine.
    def _poll_says_no():
        raise RuntimeError("poll() failed, context is incorrect")

    _went = _cli.attempt("a step", lambda: {"FINISHED"})
    _said_no = _cli.attempt("a step", lambda: {"CANCELLED"})
    _refused_loudly = _cli.attempt("a step", _poll_says_no)

    # Blender is started as a program of its own, so a name typed against the
    # folder somebody is standing in has to be settled before it is handed
    # over - and the add-on can be installed the same way, which is what lets
    # a machine with nothing but Blender on it get set up.
    _relative = _cli.parser().parse_args(
        ["--open", "tommy.4ds", "--export", "out/tommy.4ds",
         "--save", "here.blend", "--textures", "maps", "--install",
         "--option", "selection_only=True", "--gui"])
    _handed = _cli.as_arguments(_relative)
    _paths = [_handed[_handed.index(flag) + 1]
              for flag in ("--open", "--export", "--save", "--textures")]
    _install_alone = _cli.parser().parse_args(["--install"]).install == ""
    _install_named = _cli.parser().parse_args(
        ["--install", "a.zip"]).install == "a.zip"

    # The folder it packs to install is the add-on and nothing Python leaves
    # lying about.
    import tempfile as _tempfile
    import zipfile as _zipfile
    _nest = _tempfile.mkdtemp()
    _package = os.path.join(_nest, "io_mafia_toolkit")
    os.makedirs(os.path.join(_package, "__pycache__"))
    os.makedirs(os.path.join(_package, "4ds"))
    for _leaf in ("__init__.py", "4ds/codec.py", "__pycache__/stale.pyc",
                  "4ds/old.pyc"):
        with open(os.path.join(_package, _leaf), "w", encoding="utf-8") as _f:
            _f.write("# a file\n")
    _packed = _zipfile.ZipFile(_cli.packed(_package)).namelist()

    # The command's front door is one file in two places: beside the add-on's
    # code, where it ships and goes wherever the add-on goes, and at the top
    # of the project, where it is handed out next to the zip. Two copies that
    # drift apart are two commands, so they are held the same.
    _here_bat = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "mbt.bat")
    _top_bat = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "mbt.bat")
    _bats_alike = (os.path.isfile(_here_bat) and os.path.isfile(_top_bat)
                   and open(_here_bat, "rb").read()
                   == open(_top_bat, "rb").read())
    check("the command line knows the four kinds and reads its arguments",
          _kinds == {".4ds": "import_scene.4ds", ".5ds": "import_scene.5ds",
                     ".tck": "import_scene.tck", ".6ds": "import_scene.6ds"}
          and _values == {"selection_only": True, "kept": False, "count": 3,
                          "share": 0.5, "name": "base"}
          and _refused == ["nonsense", "=1"] and _unknown_kind
          and _args.open == ["a.4ds", "b.5ds"] and _args.export == ["c.4ds"]
          and _args.textures == "somewhere" and _args.check
          and not _args.gui
          and _cli.format_for("x.6ds", "write")[1] == "export_scene.6ds"
          and _went and not _said_no and not _refused_loudly
          and all(os.path.isabs(place) for place in _paths)
          and "--install" in _handed and "--gui" in _handed
          and "--option" in _handed and "selection_only=True" in _handed
          and _install_alone and _install_named and _borrows
          and sorted(_packed) == ["io_mafia_toolkit/4ds/codec.py",
                                  "io_mafia_toolkit/__init__.py"]
          and _bats_alike,
          f"kinds {sorted(_kinds)}; values {_values}; refused {_refused}; "
          f"an unknown kind is refused {_unknown_kind}; opens {_args.open}; "
          f"a step that went through {_went}, one refused {not _said_no}, "
          f"one whose poll said no {not _refused_loudly}; paths handed over "
          f"{[os.path.isabs(place) for place in _paths]}; packed {_packed}; "
          f"with {len(_windows)} window(s) it borrows one "
          f"{not _is_plain}; "
          f"the two mbt.bat are the same file {_bats_alike}")

    # Blender reads bl_info by parsing the file and running literal_eval over
    # the dict - it never imports the module for this. Anything that is not a
    # plain literal (an f-string, a name, a call) makes the whole dict
    # unreadable, and the add-on then installs but never appears in the add-on
    # list. Check it the same way Blender does.
    import ast as _ast
    _src = open(io_mafia_toolkit.__file__, encoding="utf-8").read()
    _info, _err = None, ""
    for _node in _ast.walk(_ast.parse(_src)):
        if (isinstance(_node, _ast.Assign)
                and getattr(_node.targets[0], "id", "") == "bl_info"):
            try:
                _info = _ast.literal_eval(_node.value)
            except Exception as exc:            # exactly Blender's failure
                _err = f"{type(exc).__name__}: {exc}"
            break

    check("bl_info survives the scan Blender does before importing",
          isinstance(_info, dict) and "name" in _info and "version" in _info,
          f"parsed={_info.get('name') if _info else None} "
          f"version={_info.get('version') if _info else None}"
          + (f"; literal_eval said {_err}" if _err else ""))

    # Import and export must drive Blender's progress bar, and hand it back.
    from io_mafia_toolkit.common import report as report_module
    seen, wired = [], []
    original_progress = report_module.Report.progress

    def spy(self, percent):
        seen.append(float(percent))
        wired.append(self.progress_fn is not None)
        return original_progress(self, percent)

    report_module.Report.progress = spy
    progress_model = model("!TommyHIGH.4ds")
    try:
        if not os.path.isfile(progress_model):
            raise FileNotFoundError(progress_model)
        fresh_scene()
        import_op(progress_model)
        import_seen, import_wired = list(seen), list(wired)
        seen.clear(); wired.clear()
        export_op(os.path.join(out_dir, "progress.4ds"))
        export_seen, export_wired = list(seen), list(wired)
    except FileNotFoundError:
        import_seen = export_seen = None
    finally:
        report_module.Report.progress = original_progress

    def climbs(values):
        return (len(values) > 5 and abs(min(values)) < 0.01
                and max(values) >= 100.0
                and all(b >= a - 0.01 for a, b in zip(values, values[1:])))

    if import_seen is None:
        print("[SKIP] import and export drive the progress bar  "
              "(!TommyHIGH.4ds not in the regression folder)")
    else:
        check("import and export drive the progress bar",
              climbs(import_seen) and climbs(export_seen)
              and all(import_wired) and all(export_wired),
              f"import {len(import_seen)} updates "
              f"{min(import_seen):.0f}-{max(import_seen):.0f}, "
              f"export {len(export_seen)} updates "
              f"{min(export_seen):.0f}-{max(export_seen):.0f}, "
              f"all connected to the bar="
              f"{all(import_wired) and all(export_wired)}")

    # The env-mapping labels are Blender axes translated from the game's own.
    # Derive them from the stored U/V pairs and compare, so a mistranslation
    # cannot slip through unnoticed.
    STORED_UV = {           # mode -> (U axis, V axis) in the game's axes
        0: ("X", "Y"), 1: ("X", "Z"), 2: ("X", "Z"),
        3: ("Y", "Z"), 4: ("X", "Y"), 5: ("Z", "Y"),
    }
    SWAP = {"X": "X", "Y": "Z", "Z": "Y"}       # Mafia -> Blender
    wrong = []
    for mode, (u_axis, v_axis) in STORED_UV.items():
        expected = SWAP[u_axis] + SWAP[v_axis]
        label = C.ENV_UV_ITEMS[mode][1]
        if mode == 5:
            ok = "Dummy" in label and f"local {SWAP[u_axis]}" in C.ENV_UV_ITEMS[mode][2]
        else:
            ok = label.split()[-1] == expected
        if not ok:
            wrong.append(f"mode {mode}: {label!r} != {expected}")
    check("env mapping labels use Blender axes",
          not wrong,
          f"checked {len(STORED_UV)} modes"
          + (f", wrong: {wrong}" if wrong else ", all match the engine"))

    # The material word packs three small numbers alongside its flags. Every
    # bit needs a control, and the packed fields must write back exactly. This
    # used to run only when an earlier check had happened to leave a material
    # behind, which none did - so it makes its own.
    if not bpy.data.materials:
        bpy.data.materials.new("coverage")
    if bpy.data.materials:
        mat = bpy.data.materials[0]
        before = mat.ls3d_material_flags & 0xFFFFFFFF
        mat.ls3d_env_blend = "5"
        mat.ls3d_env_uv_mode = "3"
        mat.ls3d_env_tiling = 4
        moved = mat.ls3d_material_flags & 0xFFFFFFFF
        packed_ok = ((moved >> 8) & 7 == 5 and (moved >> 12) & 7 == 3
                     and moved & 0xFF == 4)
        mat.ls3d_env_blend = str((before >> 8) & 7)
        mat.ls3d_env_uv_mode = str((before >> 12) & 7)
        mat.ls3d_env_tiling = before & 0xFF
        restored = (mat.ls3d_material_flags & 0xFFFFFFFF) == before

        covered = (C.MTL_ENV_TILE_MASK | C.MTL_ENV_BLEND_MASK | C.MTL_ENV_UV_MASK
                   | C.MTL_TEXTURE_MANAGER | C.MTL_ALPHA_ENABLE
                   | C.MTL_DISABLE_U_TILING | C.MTL_DISABLE_V_TILING
                   | C.MTL_DIFFUSE_ENABLE | C.MTL_ENV_ENABLE
                   | C.MTL_FORCE_TRUECOLOR | C.MTL_NO_COMPRESSION
                   | C.MTL_NO_CACHED_TEXTURE | C.MTL_DIFFUSE_MIPMAP
                   | C.MTL_ALPHA_IN_TEX | C.MTL_ALPHA_ANIMATED
                   | C.MTL_DIFFUSE_ANIMATED | C.MTL_DIFFUSE_COLORED
                   | C.MTL_DIFFUSE_DOUBLESIDED | C.MTL_ALPHA_COLORKEY
                   | C.MTL_ALPHATEX | C.MTL_ALPHA_ADDITIVE)
        drawn = all(hasattr(mat, name) for name in
                    ("ls3d_env_tiling", "ls3d_env_blend", "ls3d_env_uv_mode",
                     "ls3d_flag_texture_manager", "ls3d_flag_force_truecolor",
                     "ls3d_flag_no_compression", "ls3d_flag_no_cached_texture"))

        check("every material bit has a control and packed fields round-trip",
              covered == 0xFFFFFFFF and packed_ok and restored and drawn,
              f"covered 0x{covered:08X} ({bin(covered).count('1')}/32 bits), "
              f"packed writes={packed_ok}, restored={restored}, "
              f"all controls present={drawn}")

    # A name merely containing "_lod" must not be treated as a LOD.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.object.name = "car_lodge"
    path = os.path.join(out_dir, "lodge.4ds")
    result = export_op(path)
    check("object named 'car_lodge' still exports",
          result == {"FINISHED"}
          and any(f.name == "car_lodge" for f in read_file(path).frames))

    # Every string in a 4DS has a single length byte in front of it, so 255
    # bytes is the ceiling. Past it the text would be cut - and cutting it
    # silently is exactly what the export must not do.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    edge = bpy.context.object
    edge.name = "n" * 255
    edge.ls3d_user_props = "x" * 255
    path = os.path.join(out_dir, "longprops.4ds")
    at_limit = export_op(path)
    stored = read_file(path).frames[0] if at_limit == {"FINISHED"} else None
    edge.ls3d_user_props = "x" * 256
    over = export_op(path)
    scene_check = bpy.ops.ls3d.check_scene()
    check("text is refused once it will not fit the format's length byte",
          at_limit == {"FINISHED"} and stored is not None
          and len(stored.name) == 255 and len(stored.user_props) == 255
          and over == {"CANCELLED"} and scene_check == {"CANCELLED"},
          f"at 255: {at_limit}, at 256: {over}, Check Scene: {scene_check}")

    # Both switch distances are stored squared. The panel shows meters, so the
    # meter view and the raw value must stay in step in both directions, and a
    # file must round-trip through the meter field untouched.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.ls3d_portal_near = 400.0             # the value every shipped portal uses
    shown = round(obj.ls3d_portal_view_dist_m, 6)
    obj.ls3d_portal_view_dist_m = 12.5
    back = round(obj.ls3d_portal_near, 6)
    obj.ls3d_lod_dist = 789823.0
    lod_shown = round(obj.ls3d_lod_distance_m, 3)
    obj.ls3d_lod_distance_m = 30.0
    lod_back = round(obj.ls3d_lod_dist, 3)
    obj.ls3d_portal_near = 0.0
    zero_shown = obj.ls3d_portal_view_dist_m
    check("squared switch distances read back as meters both ways",
          shown == 20.0 and back == 156.25 and lod_shown == 888.72
          and lod_back == 900.0 and zero_shown == 0.0,
          f"portal 400 -> {shown} m, 12.5 m -> {back}; "
          f"lod 789823 -> {lod_shown} m, 30 m -> {lod_back}; zero -> {zero_shown}")

    # The 8-corner cap is a hard limit in the format, so the boundary matters:
    # 8 has to go through and 9 has to stop the whole export.
    def portal_scene(corners, as_edge=False):
        fresh_scene()
        bpy.ops.mesh.primitive_cube_add()
        room = bpy.context.object
        room.name = "sector_room"
        room.ls3d_frame_type = str(C.FRAME_SECTOR)
        data = bpy.data.meshes.new("portal")
        obj = bpy.data.objects.new("sector_room_portal01", data)
        bpy.context.collection.objects.link(obj)
        obj.parent = room
        obj.ls3d_frame_type = str(C.FRAME_SECTOR)
        bm = bmesh.new()
        ring = [bm.verts.new((math.cos(2 * math.pi * i / corners),
                              math.sin(2 * math.pi * i / corners), 0.0))
                for i in range(corners)]
        if as_edge:
            bm.edges.new((ring[0], ring[1]))
        elif corners >= 3:
            bm.faces.new(ring)
        bm.to_mesh(data)
        bm.free()
        return obj

    outcomes = {}
    for corners, edge in ((2, True), (3, False), (8, False), (9, False)):
        portal_scene(corners, edge)
        path = os.path.join(out_dir, f"portal_{corners}.4ds")
        if os.path.exists(path):
            os.remove(path)
        outcomes[corners] = (export_op(path), os.path.exists(path))
    check("the portal corner limits stop the export at the boundary",
          outcomes[3][0] == {"FINISHED"} and outcomes[8][0] == {"FINISHED"}
          and outcomes[9][0] == {"CANCELLED"} and not outcomes[9][1]
          and outcomes[2][0] == {"CANCELLED"} and not outcomes[2][1],
          ", ".join(f"{n}: {r} written={w}" for n, (r, w) in outcomes.items()))

    # Everything the exporter derives has to come back the same as the file it
    # was derived from. This is the check that found the bone-parent scale, the
    # empty bone-group bounds, the flipped portal planes, the lexicographic
    # portal order and the skin frame's scale leaking into every joint.
    for model_name in ("Paulie.4ds", "!TommyHIGH.4ds"):
        if not os.path.isfile(model(model_name)):
            continue
        fresh_scene()
        before = read_file(model(model_name))
        import_op(model(model_name))
        path = os.path.join(out_dir, f"derived_{model_name}")
        if export_op(path) != {"FINISHED"}:
            check(f"derived values match the original ({model_name})", False,
                  "export failed")
            continue
        after = read_file(path)

        worst, where = 0.0, ""

        def measure(label, a, b):
            nonlocal worst, where
            delta = max((abs(float(x) - float(y)) for x, y in zip(a, b)),
                        default=0.0)
            if delta > worst:
                worst, where = delta, label

        fa = {f.name: f for f in before.frames}
        fb = {f.name: f for f in after.frames}
        for name, x in fa.items():
            y = fb.get(name)
            if y is None:
                continue
            measure(f"{name} position", x.position, y.position)
            measure(f"{name} scale", x.scale, y.scale)
            if x.dummy and y.dummy:
                measure(f"{name} dummy box",
                        x.dummy.bbox_min + x.dummy.bbox_max,
                        y.dummy.bbox_min + y.dummy.bbox_max)

        def groups_by_bone(doc):
            """Bone groups keyed by the joint that owns them, as the file links
            them - group order alone is not a safe pairing."""
            skin = next((f for f in doc.frames if f.skin and f.skin.lods), None)
            if skin is None:
                return {}
            groups = skin.skin.lods[0].groups
            return {f.name: groups[f.joint.joint_id] for f in doc.frames
                    if f.joint is not None and f.joint.joint_id < len(groups)}

        after_groups = groups_by_bone(after)
        for bone_name, ga in groups_by_bone(before).items():
            gb = after_groups.get(bone_name)
            if gb is None:
                continue
            measure(f"{bone_name} bind", ga.inverse_bind, gb.inverse_bind)
            measure(f"{bone_name} group bounds", ga.bbox_min + ga.bbox_max,
                    gb.bbox_min + gb.bbox_max)

        check(f"derived values match the original ({model_name})",
              worst < 1e-4, f"worst {worst:.8f} at {where or 'nothing'}")

    # An empty draws a cube centered on its origin, so it cannot stand in for a
    # box that is off-center or not cubic - 146 of the game's 19323 dummies.
    # Those get the real box outlined instead, and the panel edits the numbers.
    fresh_scene()
    _v = ls3d_module("4ds.viewport")
    (dummy_box, dummy_box_is_drawable, has_mirror_box, _dummy_box_corners,
     _outlined_boxes) = (_v.dummy_box, _v.dummy_box_is_drawable,
                         _v.has_mirror_box, _v._dummy_box_corners,
                         _v._outlined_boxes)

    def dummy(name, low, high):
        obj = bpy.data.objects.new(name, None)
        bpy.context.collection.objects.link(obj)
        obj.empty_display_type = "CUBE"
        obj.ls3d_frame_type = str(C.FRAME_DUMMY)
        obj.bbox_min = low
        obj.bbox_max = high
        obj.empty_display_size = max(abs(v) for v in low + high) or 0.1
        return obj

    cubic = dummy("cubic", (-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
    slab = dummy("slab", (-0.127, -0.016, -0.08), (0.127, 0.016, 0.08))
    offset = dummy("offset", (-6.888, -13.131, -6.888), (19.668, 13.131, 20.664))
    scratch = bpy.data.objects.new("scratch", None)
    bpy.context.collection.objects.link(scratch)
    scratch.empty_display_type = "CUBE"
    scratch.ls3d_frame_type = str(C.FRAME_DUMMY)
    scratch.empty_display_size = 0.25

    outlined = {o.name for o, _color in _outlined_boxes(bpy.context)}
    corners = _dummy_box_corners(offset)
    spans_x = (round(min(c.x for c in corners), 3),
               round(max(c.x for c in corners), 3))
    check("the dummy box overlay covers exactly the boxes an empty cannot draw",
          dummy_box_is_drawable(cubic) and not dummy_box_is_drawable(slab)
          and not dummy_box_is_drawable(offset)
          and dummy_box_is_drawable(scratch)
          and outlined == {"slab", "offset"}
          and spans_x == (-6.888, 19.668)
          and dummy_box(scratch) == ((-0.25, -0.25, -0.25), (0.25, 0.25, 0.25)),
          f"outlined {sorted(outlined)}, offset spans x {spans_x}")

    # An outlined box that is not centered on its empty gets a dashed line from
    # its middle to the empty, so the two can be told apart from a neighbor's.
    # The dashes are sized in pixels: zooming in doubles how many there are.
    from types import SimpleNamespace as _View
    from mathutils import Matrix as _Matrix, Vector as _Vector
    _links = _v.dummy_link_lines
    _region = _View(width=1000)

    def _zoomed(scale):
        return _View(window_matrix=_Matrix.Diagonal((scale, scale, 1.0, 1.0)),
                     perspective_matrix=_Matrix.Identity(4))

    offset.location = (10.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    far_view = _links(offset, _region, _zoomed(0.1))
    near_view = _links(offset, _region, _zoomed(0.2))
    very_near = _links(offset, _region, _zoomed(1000.0))
    box_middle = _Vector((10.0 + 6.39, 0.0, 6.888))
    dash_lengths = [(far_view[i + 1] - far_view[i]).length
                    for i in range(0, len(far_view), 2)]
    on_the_line = all(
        ((p - box_middle).cross(_Vector((10.0, 0.0, 0.0)) - box_middle)).length
        < 1e-4 for p in far_view)
    check("an off-center dummy box is linked to its empty by a dashed line",
          len(far_view) % 2 == 0 and len(far_view) // 2 > 10
          and (far_view[0] - box_middle).length < 1e-4
          and (far_view[-1] - _Vector((10.0, 0.0, 0.0))).length < 1e-4
          and on_the_line
          and max(dash_lengths) - min(dash_lengths) < 1e-4
          and abs(len(near_view) / len(far_view) - 2.0) < 0.1
          and len(very_near) // 2 == _v.LINK_DASH_LIMIT
          and _links(cubic, _region, _zoomed(0.1)) == []
          and _links(slab, _region, _zoomed(0.1)) == [],
          f"{len(far_view) // 2} dash(es) far, {len(near_view) // 2} near, "
          f"{len(very_near) // 2} very near; starts at the box middle "
          f"{(far_view[0] - box_middle).length < 1e-4 if far_view else None}, "
          f"ends at the empty "
          f"{(far_view[-1].to_tuple(3) if far_view else None)}; centered boxes "
          f"{len(_links(cubic, _region, _zoomed(0.1)))}, "
          f"{len(_links(slab, _region, _zoomed(0.1)))}")
    # The dot the overlay puts at a box's middle marks that middle - the point
    # the panel's Center numbers move, and where the dashed line starts.
    dot = _v.box_center(_v._dummy_box_corners(offset))
    centered_dot = _v.box_center(_v._dummy_box_corners(cubic))
    check("a dummy box carries a dot at its middle",
          (dot - box_middle).length < 1e-4
          and (centered_dot - cubic.matrix_world.translation).length < 1e-4,
          f"the off-center box's dot {dot.to_tuple(3)} against its middle "
          f"{box_middle.to_tuple(3)}; a centered box's dot "
          f"{centered_dot.to_tuple(3)} against its empty "
          f"{cubic.matrix_world.translation.to_tuple(3)}")

    # A dot is only a dot if the shader drawing it knows what a point size is.
    # The line shader the boxes are drawn with puts down a single pixel however
    # wide a point is asked to be - on a 4K screen, nothing at all - so the
    # dots get a shader of their own, and here it is drawn and counted.
    dot_pixels = None
    try:
        import gpu as _gpu
        import numpy as _np
        from gpu_extras.batch import batch_for_shader as _dot_batch
        _gpu.init()                        # the suite runs without a window
        _dot_shader = _gpu.shader.from_builtin(_v.DOT_SHADER)
        _canvas = _gpu.types.GPUOffScreen(32, 32)
        with _canvas.bind():
            _sheet = _gpu.state.active_framebuffer_get()
            _sheet.clear(color=(0.0, 0.0, 0.0, 1.0))
            _gpu.state.point_size_set(_v.BOX_CENTER_DOT_PIXELS)
            _batch = _dot_batch(_dot_shader, "POINTS", {"pos": [(0.0, 0.0, 0.0)]})
            _dot_shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
            _batch.draw(_dot_shader)
            _shot = _np.array(_sheet.read_color(0, 0, 32, 32, 4, 0,
                                               "FLOAT").to_list())
            _gpu.state.point_size_set(1.0)
        _canvas.free()
        dot_pixels = int((_shot[:, :, 0] > 0.3).sum())
    except (ImportError, RuntimeError, SystemError, ValueError) as _dot_error:
        dot_detail = f"no GPU here to draw with ({type(_dot_error).__name__})"
    else:
        dot_detail = (f"{dot_pixels} pixel(s) lit for a "
                      f"{_v.BOX_CENTER_DOT_PIXELS:.0f} px dot drawn through "
                      f"{_v.DOT_SHADER}")
    check("the dot is drawn as wide as it is asked to be",
          dot_pixels is None or dot_pixels >= 9, dot_detail)

    offset.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()

    # And the box the overlay draws is the box that gets written.
    path = os.path.join(out_dir, "dummy_boxes.4ds")
    written = {}
    if export_op(path) == {"FINISHED"}:
        written = {f.name: (tuple(round(v, 3) for v in f.dummy.bbox_min),
                            tuple(round(v, 3) for v in f.dummy.bbox_max))
                   for f in read_file(path).frames if f.dummy is not None}
    def to_file_axes(vector):            # Blender (x, y, z) -> Mafia (x, z, y)
        return (vector[0], vector[2], vector[1])

    expected = (to_file_axes((-6.888, -13.131, -6.888)),
                to_file_axes((19.668, 13.131, 20.664)))
    check("what the overlay draws is what the file gets",
          written.get("offset") == expected
          and written.get("scratch") == ((-0.25, -0.25, -0.25),
                                         (0.25, 0.25, 0.25)),
          f"offset={written.get('offset')} expected {expected}, "
          f"scratch={written.get('scratch')}")

    # A mirror keeps an authored bound the game trusts instead of measuring the
    # mesh, and nothing in Blender draws it, so it gets outlined too.
    mirror_model = model("zrcadlo.4ds")
    if os.path.isfile(mirror_model):
        fresh_scene()
        import_op(mirror_model)
        mirror = next((o for o in bpy.context.scene.objects
                       if has_mirror_box(o)), None)
        names = {o.name for o, _color in _outlined_boxes(bpy.context)}
        views = {o.name for o, _color
                 in ls3d_module("4ds.viewport")._mirror_view_boxes(bpy.context)}
        span = None
        if mirror is not None:
            span = tuple(round(mirror.bbox_max[a] - mirror.bbox_min[a], 3)
                         for a in range(3))
        check("a mirror's own bound is outlined too, and its view box",
              mirror is not None and mirror.name in names
              and span == (13.622, 0.0, 13.622)
              and mirror.name in views,
              f"outlined {sorted(names)}, view boxes {sorted(views)}, "
              f"mirror span {span}")

    # The box shares its axes with the object's own geometry, so it can carry
    # plain XYZ labels. The pipe in 'xv trubka2.4ds' proves it: its dummy box
    # and its mesh span exactly the same figures on all three axes.
    pipe_model = model("xv trubka2.4ds")
    if os.path.isfile(pipe_model):
        fresh_scene()
        import_op(pipe_model)
        mesh_obj = next((o for o in bpy.context.scene.objects
                         if o.type == "MESH"), None)
        box_obj = next((o for o in bpy.context.scene.objects
                        if o.type == "EMPTY"
                        and o.empty_display_type == "CUBE"
                        and max(o.bbox_max) > 1.0), None)
        mesh_span = box_span = None
        if mesh_obj is not None and box_obj is not None:
            low = [min(v.co[axis] for v in mesh_obj.data.vertices)
                   for axis in range(3)]
            high = [max(v.co[axis] for v in mesh_obj.data.vertices)
                    for axis in range(3)]
            mesh_span = tuple(round(high[a] - low[a], 3) for a in range(3))
            box_span = tuple(round(box_obj.bbox_max[a] - box_obj.bbox_min[a], 3)
                             for a in range(3))
        labeled = all(
            bpy.types.Object.bl_rna.properties[name].subtype.startswith("XYZ")
            for name in ("bbox_min", "bbox_max"))
        check("the dummy box uses the object's own axes and is labeled XYZ",
              mesh_span is not None and mesh_span == box_span
              and box_span == (0.211, 0.211, 3.697) and labeled,
              f"mesh {mesh_span} vs box {box_span}, XYZ labels={labeled}")


    # Sibling order among frames carries no meaning to the engine, but it does
    # decide which of several objects sharing a mesh is written first, and an
    # instance can only point backwards. Sorting siblings by name put
    # 'SCTR_0psch' ahead of 'SCTR_hlhala' and cost MISE08-HOTEL a whole
    # duplicated LOD chain. The order is read from the scene, which Blender
    # keeps in the order objects were added - nothing is recorded at import.
    hotel = os.path.join(os.path.dirname(models_dir), "missions",
                         "MISE08-HOTEL", "scene.4ds")
    if os.path.isfile(hotel):
        fresh_scene()
        import_op(hotel)
        # Nothing may be carried over from the import to make this work.
        stored = [o.name for o in bpy.context.scene.objects
                  if "ls3d_frame_order" in o.keys()]
        check("frame order needs nothing stored on the objects",
              not stored, f"objects carrying a recorded order: {stored[:4]}")
        path = os.path.join(out_dir, "frame_order.4ds")
        before = [f.name for f in read_file(hotel).frames]
        after = []
        if export_op(path) == {"FINISHED"}:
            after = [f.name for f in read_file(path).frames]
        in_place = sum(1 for a, b in zip(before, after) if a == b)
        check("frames keep the order the file had them in",
              before == after,
              f"{in_place} of {len(before)} in place")

    # A joint made from the Add menu borrows the shared display empty the
    # importer uses, and that empty must stay out of every collection - it is a
    # sphere empty, which is what a lens flare looks like to the exporter, so a
    # linked one would be written out as a stray flare.
    joint_display = ls3d_module("4ds.joint_display")
    fresh_scene()
    bpy.ops.ls3d.add_joint()
    armature = next((o for o in bpy.context.scene.objects
                     if o.type == "ARMATURE"), None)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.ls3d.add_joint()
    pose_bones = list(armature.pose.bones) if armature else []
    shapes = {b.custom_shape.name for b in pose_bones if b.custom_shape}
    shape = bpy.data.objects.get(joint_display.JOINT_SHAPE_NAME)
    check("a joint from the Add menu reuses the shared shape",
          len(pose_bones) == 2
          and shapes == {joint_display.JOINT_SHAPE_NAME}
          and all("ls3d_joint_scale" in b for b in pose_bones)
          and shape is not None and not shape.users_collection,
          f"{len(pose_bones)} joint(s), shapes {sorted(shapes)}, "
          f"shape linked={bool(shape and shape.users_collection)}")

    # Vertices bind to joints by vertex-group name. Blender renames the group
    # when you rename the joint, but not the other way round, so a renamed
    # group silently unbinds its vertices to the mesh frame.
    skinned = model("pes03.4ds")
    if os.path.isfile(skinned):
        fresh_scene()
        import_op(skinned)
        mesh = next((o for o in bpy.context.scene.objects
                     if o.type == "MESH" and o.vertex_groups), None)
        said = []
        original_warn = report_module.Report.warn

        def _listen(self, message, fix=None):
            said.append(message)
            return original_warn(self, message, fix)

        report_module.Report.warn = _listen
        try:
            clean = export_op(os.path.join(out_dir, "groups_ok.4ds"))
            quiet = [m for m in said if "match no joint" in m]
            said.clear()
            mesh.vertex_groups[2].name += "_STRAY"
            strayed = export_op(os.path.join(out_dir, "groups_stray.4ds"))
            noisy = [m for m in said if "match no joint" in m]
        finally:
            report_module.Report.warn = original_warn
        # The export may also stop: a vertex blended between the renamed group
        # and a real joint is left with weights that no longer add up, which
        # Blender would show as fully on the real joint and the game would not.
        # Either way, the warning names the group that caused it.
        check("a vertex group matching no joint is reported",
              clean == {"FINISHED"} and not quiet
              and strayed in ({"FINISHED"}, {"CANCELLED"}) and len(noisy) == 1,
              f"before: {quiet or 'nothing'}; after: {noisy or 'nothing'}, "
              f"export {strayed}")

    # Check Scene runs the same build the export does and stops before writing.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    clean = bpy.ops.ls3d.check_scene()
    fresh_scene()
    bpy.ops.ls3d.add_sector()
    broken = bpy.context.object
    bm = bmesh.new()
    bm.from_mesh(broken.data)
    bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
    bm.to_mesh(broken.data)
    bm.free()
    broken.data.update()
    refused = bpy.ops.ls3d.check_scene()
    check("Check Scene reports what the export would, without writing",
          clean == {"FINISHED"} and refused == {"CANCELLED"},
          f"clean={clean}, outward-facing sector={refused}")

    # A model claiming animated objects makes the game open a .5ds of its name
    # beside it, and a missing one fails the whole model load - after the model
    # has appeared - so the mission never records its frame names and every
    # scene2.bin entry for them is ignored. Export and Check Scene both say so,
    # and neither says anything once the animation is there or the count is 0.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    lonely_dir = os.path.join(out_dir, "animation_beside")
    shutil.rmtree(lonely_dir, ignore_errors=True)
    os.makedirs(lonely_dir)
    lonely = os.path.join(lonely_dir, "gate.4ds")
    heard = []
    original_warn = report_module.Report.warn

    def _heard(self, message, fix=None):
        heard.append(message)
        return original_warn(self, message, fix)

    def _missing_said():
        found = [m for m in heard if "'gate.5ds'" in m and "scene2.bin" in m]
        heard.clear()
        return bool(found)

    last_export = bpy.context.window_manager.operator_properties_last(
        "export_scene.4ds")
    scene = bpy.context.scene
    report_module.Report.warn = _heard
    try:
        scene.ls3d_animated_object_count = 0
        export_op(lonely)
        quiet_at_zero = not _missing_said()
        scene.ls3d_animated_object_count = 2
        exported_missing = export_op(lonely) == {"FINISHED"} and _missing_said()
        last_export.filepath = lonely
        checked_missing = (bpy.ops.ls3d.check_scene() == {"FINISHED"}
                           and _missing_said())
        open(os.path.join(lonely_dir, "gate.5ds"), "wb").close()
        export_op(lonely)
        quiet_with_animation = not _missing_said()
        bpy.ops.ls3d.check_scene()
        checked_quiet = not _missing_said()
    finally:
        report_module.Report.warn = original_warn
        scene.ls3d_animated_object_count = 0
    check("a model counting animated objects with no .5ds beside it is warned about",
          quiet_at_zero and exported_missing and checked_missing
          and quiet_with_animation and checked_quiet,
          f"zero quiet {quiet_at_zero}, export said {exported_missing}, check "
          f"said {checked_missing}, quiet with the animation "
          f"{quiet_with_animation}/{checked_quiet}")

    # A geometry LOD counts its vertices in a uint16, but 0xFFFF is not a
    # count there - it says the LOD borrows LOD 0's vertices and carries only
    # UVs. Writing 65535 real vertices produced a file the game read as that
    # shorthand and mangled, and reading one would have derailed the parser.
    _visual_codec = ls3d_module("4ds.codec.visual")
    _types = ls3d_module("4ds.codec.types")
    _binary = ls3d_module("common.binary")

    def lod_of(count):
        return _types.LOD(vertices=[_types.Vertex()] * count)

    ceiling = {}
    for count in (C.MAX_VERTICES_PER_LOD, C.MAX_VERTICES_PER_LOD + 1):
        geom = _types.Geometry(lods=[lod_of(count)])
        try:
            _visual_codec.write_geometry(_binary.BinaryWriter(), geom, "test")
            ceiling[count] = "written"
        except _binary.FormatError:
            ceiling[count] = "refused"

    # And the shorthand itself round-trips through the format layer.
    base = _types.LOD(vertices=[_types.Vertex(position=(1.0, 2.0, 3.0),
                                              normal=(0.0, 1.0, 0.0),
                                              uv=(0.25, 0.5))] * 3)
    borrowed = _types.LOD(distance=9.0, shared_uv=[(0.1, 0.2), (0.3, 0.4),
                                                   (0.5, 0.6)])
    writer = _binary.BinaryWriter()
    _visual_codec.write_geometry(writer, _types.Geometry(lods=[base, borrowed]),
                                 "test")
    blob = writer.getvalue()
    back = _visual_codec.read_geometry(_binary.BinaryReader(blob))
    # 2 instance + 1 count, then LOD0 (4 + 2 + 3*32 + 1) and LOD1 (4 + 2 + 3*8 + 1)
    expected_size = 3 + (4 + 2 + 96 + 1) + (4 + 2 + 24 + 1)
    check("a LOD that borrows LOD 0's vertices is read and written as one",
          ceiling == {C.MAX_VERTICES_PER_LOD: "written",
                      C.MAX_VERTICES_PER_LOD + 1: "refused"}
          and C.MAX_VERTICES_PER_LOD == 0xFFFE
          and len(blob) == expected_size
          # UVs go out as float32 and come back widened, so they are compared
          # to the precision the format actually carries.
          and [(round(u, 6), round(v, 6)) for u, v in back.lods[1].shared_uv]
              == borrowed.shared_uv
          and not back.lods[1].vertices
          and back.lods[0].vertices[0].uv == (0.25, 0.5),
          f"ceiling {ceiling}; {len(blob)} bytes vs {expected_size}; "
          f"read back {[(round(u, 3), round(v, 3)) for u, v in back.lods[1].shared_uv]}")

    # An occluder's face count has a ceiling the format never states. The
    # silhouette walk indexes faces through a uint16 adjacency table, so past
    # 65535 it follows the wrong neighbors and nothing complains.
    _scene_codec = ls3d_module("4ds.codec.scene")
    _types = ls3d_module("4ds.codec.types")
    _binary = ls3d_module("common.binary")
    verdicts = {}
    for faces in (C.MAX_OCCLUDER_FACES, C.MAX_OCCLUDER_FACES + 1):
        block = _types.Occluder(vertices=[(0.0, 0.0, 0.0)],
                                faces=[(0, 0, 0)] * faces)
        try:
            _scene_codec.write_occluder(_binary.BinaryWriter(), block, "test")
            verdicts[faces] = "written"
        except _binary.FormatError:
            verdicts[faces] = "refused"
    check("an occluder over the face ceiling is refused",
          verdicts == {C.MAX_OCCLUDER_FACES: "written",
                       C.MAX_OCCLUDER_FACES + 1: "refused"},
          f"at the limit and one over: {verdicts}")

    # A dummy is a box, and the only way to change it used to be typing six
    # numbers. It has six drag handles now, one per face. They are placed from
    # the transform and the box every redraw and nothing about them is stored,
    # so what has to hold is the arithmetic: a handle sits exactly on its face
    # whatever the object is doing, and dragging it moves that face and no other.
    from mathutils import Vector as _Vector
    _gizmo = ls3d_module("4ds.gizmo_dummy")
    fresh_scene()
    boxed = bpy.data.objects.new("boxed", None)
    bpy.context.collection.objects.link(boxed)
    boxed.empty_display_type = "CUBE"
    boxed.ls3d_frame_type = str(C.FRAME_DUMMY)
    bpy.context.view_layer.objects.active = boxed
    boxed.select_set(True)      # the handles only show for a selected dummy

    _dummy_box = ls3d_module("4ds.viewport").dummy_box
    worst = 0.0
    for low, high, scale, rotation, location in (
            # a plain empty nobody has given a box: the handles have to fall
            # back to the size it is drawn at, not collapse onto the origin
            ((0.0,) * 3, (0.0,) * 3, (1.0,) * 3, (0.0,) * 3, (0.0,) * 3),
            ((-1.0,) * 3, (1.0,) * 3, (1.0,) * 3, (0.0,) * 3, (0.0,) * 3),
            # the off-center one from the game, which an empty cannot draw
            ((-6.888, -6.888, -13.131), (19.668, 20.664, 13.131),
             (1.0,) * 3, (0.0,) * 3, (0.0,) * 3),
            ((-1.0, -2.0, -3.0), (4.0, 5.0, 6.0),
             (2.0, 3.0, 0.5), (0.4, -1.1, 0.9), (5.0, -2.0, 7.0)),
            ((-1.0, -2.0, -3.0), (4.0, 5.0, 6.0),
             (-1.0, 2.0, 1.0), (0.0, 0.7, 0.0), (0.0,) * 3),
    ):
        boxed.bbox_min, boxed.bbox_max = low, high
        boxed.scale, boxed.rotation_euler, boxed.location = (
            scale, rotation, location)
        boxed.empty_display_size = 0.75
        bpy.context.view_layer.update()
        drawn_low, drawn_high = _dummy_box(boxed)
        for index, (axis, sign) in enumerate(_gizmo.FACES):
            # the handle is drawn at its matrix's own origin now, so that is
            # the position to check - no offset displaces it
            at = _gizmo.handle_matrix(boxed, index).translation
            center = [(drawn_low[a] + drawn_high[a]) * 0.5 for a in range(3)]
            center[axis] = (drawn_high if sign > 0 else drawn_low)[axis]
            worst = max(worst,
                        (at - boxed.matrix_world @ _Vector(center)).length)

    # Dragging: every handle pulls outward, each moves one face only, and
    # letting go where it started leaves the box exactly as it was.
    boxed.bbox_min, boxed.bbox_max = (-1.0, -2.0, -3.0), (4.0, 5.0, 6.0)
    boxed.scale = (2.0, 3.0, 0.5)
    bpy.context.view_layer.update()
    grew, restored, spilled = [], [], []
    for index, (axis, _sign) in enumerate(_gizmo.FACES):
        before = (tuple(boxed.bbox_min), tuple(boxed.bbox_max))
        start = _gizmo.face_offset(boxed, index)
        _gizmo.set_face_offset(boxed, index, start + 2.0)
        after = (tuple(boxed.bbox_min), tuple(boxed.bbox_max))
        grew.append(after[1][axis] - after[0][axis]
                    > before[1][axis] - before[0][axis])
        spilled.append(sum(1 for a in range(3) if a != axis
                           and (before[0][a], before[1][a])
                           != (after[0][a], after[1][a])))
        _gizmo.set_face_offset(boxed, index, start)
        restored.append(abs(_gizmo.face_offset(boxed, index) - start) < 1e-5)

    # A face shoved past its opposite would invert the box, which the format
    # cannot express, so the drag stops just short instead.
    _gizmo.set_face_offset(boxed, 0, -1000.0)
    clamped = boxed.bbox_min[0] < boxed.bbox_max[0]

    # And dragging a plain empty that never had a box gives it one.
    plain = bpy.data.objects.new("plain_dummy", None)
    bpy.context.collection.objects.link(plain)
    plain.empty_display_type = "CUBE"
    plain.ls3d_frame_type = str(C.FRAME_DUMMY)
    plain.empty_display_size = 0.5
    had_none = not any(plain.bbox_min) and not any(plain.bbox_max)
    # An offset is the distance from the origin, so dragging the +Z face out to
    # 2.0 puts that face at 2.0 and leaves the other five where the empty drew
    # them.
    _gizmo.set_face_offset(plain, 5, 2.0)
    materialized = (tuple(round(v, 3) for v in plain.bbox_min) == (-0.5, -0.5, -0.5)
                    and tuple(round(v, 3) for v in plain.bbox_max) == (0.5, 0.5, 2.0))

    check("a dummy's box has drag handles that sit exactly on its faces",
          worst < 1e-4 and all(grew) and all(restored) and not any(spilled)
          and clamped and had_none and materialized
          and _gizmo.LS3D_GGT_DummyBox.poll(bpy.context),
          f"worst handle error {worst:.2e}; grow {all(grew)}, "
          f"restore {all(restored)}, other axes touched {sum(spilled)}, "
          f"clamped {clamped}, box made from a bare empty {materialized}")

    # The outline has a color of its own at rest and another once the dummy is
    # picked, so it is obvious which box you are about to edit.
    fresh_scene()
    _v2 = ls3d_module("4ds.viewport")
    odd = bpy.data.objects.new("odd", None)
    bpy.context.collection.objects.link(odd)
    odd.empty_display_type = "CUBE"
    odd.ls3d_frame_type = str(C.FRAME_DUMMY)
    odd.bbox_min = (-6.888, -13.131, -6.888)
    odd.bbox_max = (19.668, 13.131, 20.664)
    for other in bpy.context.scene.objects:
        other.select_set(False)
    at_rest = dict(_v2._outlined_boxes(bpy.context))
    odd.select_set(True)
    picked = dict(_v2._outlined_boxes(bpy.context))
    check("a dummy box outline turns red when its dummy is picked",
          at_rest.get(odd) == _v2.DUMMY_BOX_COLOR
          and picked.get(odd) == _v2.DUMMY_BOX_COLOR_SELECTED
          and _v2.DUMMY_BOX_COLOR != _v2.DUMMY_BOX_COLOR_SELECTED,
          f"at rest {tuple(round(c, 2) for c in at_rest.get(odd, ()))}, "
          f"picked {tuple(round(c, 2) for c in picked.get(odd, ()))}")

    # A handle is centered on its face, so dragging any face does move where the
    # other five are drawn - their faces have new middles. What must not move is
    # the *value* each one reports, because that is what Blender's drag arithmetic
    # is built on: shift the number under a handle that is not being dragged and
    # it jumps the moment you grab it.
    fresh_scene()
    dragged = bpy.data.objects.new("dragged", None)
    bpy.context.collection.objects.link(dragged)
    dragged.empty_display_type = "CUBE"
    dragged.ls3d_frame_type = str(C.FRAME_DUMMY)
    dragged.bbox_min, dragged.bbox_max = (-1.0, -2.0, -3.0), (4.0, 5.0, 6.0)
    bpy.context.view_layer.objects.active = dragged
    bpy.context.view_layer.update()

    disturbed, off_face = [], []
    for index in range(6):
        before = [round(_gizmo.face_offset(dragged, i), 4) for i in range(6)]
        _gizmo.set_face_offset(dragged, index,
                               _gizmo.face_offset(dragged, index) + 2.0)
        bpy.context.view_layer.update()
        after = [round(_gizmo.face_offset(dragged, i), 4) for i in range(6)]
        moved = [i for i in range(6) if before[i] != after[i]]
        if moved != [index]:
            disturbed.append((index, moved))
        # and every handle is still on the middle of its own face
        low, high = _dummy_box(dragged)
        for i, (axis, sign) in enumerate(_gizmo.FACES):
            at = _gizmo.handle_matrix(dragged, i).translation
            center = [(low[a] + high[a]) * 0.5 for a in range(3)]
            center[axis] = (high if sign > 0 else low)[axis]
            if (at - dragged.matrix_world @ _Vector(center)).length > 1e-4:
                off_face.append((index, i))
    check("dragging one face leaves every other handle's value alone",
          not disturbed and not off_face,
          "each drag moved only its own value, and all six stayed centered"
          if not disturbed and not off_face
          else f"values disturbed {disturbed}, handles off their faces {off_face}")

    # The maths above is reachable from a test; the gizmo group's own hooks are
    # not, because a real one needs a 3D region. So they are run against a stand
    # in - which is the point: a name that went missing inside setup or
    # draw_prepare used to surface only as a traceback in someone's console.
    fresh_scene()
    hooked = bpy.data.objects.new("hooked", None)
    bpy.context.collection.objects.link(hooked)
    hooked.empty_display_type = "CUBE"
    hooked.ls3d_frame_type = str(C.FRAME_DUMMY)
    hooked.bbox_min, hooked.bbox_max = (-1.0, -2.0, -3.0), (4.0, 5.0, 6.0)
    bpy.context.view_layer.objects.active = hooked
    hooked.select_set(True)
    bpy.context.view_layer.update()

    class _StandInGizmo:
        is_modal = False        # the real one reports whether it is being dragged

        def target_set_prop(self, _name, target, prop):
            self.target = (target, prop)

    class _StandInGizmos:
        def __init__(self):
            self.made = []

        def new(self, name):
            gizmo = _StandInGizmo()
            self.made.append((name, gizmo))
            return gizmo

        def clear(self):
            self.made.clear()

    group_body = {k: v for k, v in
                  _gizmo.LS3D_GGT_DummyBox.__dict__.items()
                  if not k.startswith("bl_")}
    group = type("G", (), group_body)()
    group.gizmos = _StandInGizmos()

    hooks_ran, complaint = True, ""
    try:
        group.setup(bpy.context)
        group.draw_prepare(bpy.context)
    except Exception as exc:                                  # noqa: BLE001
        hooks_ran, complaint = False, f"{type(exc).__name__}: {exc}"

    built = [name for name, _g in group.gizmos.made]
    placed = all(hasattr(g, "matrix_basis") for _n, g in group.gizmos.made)
    # Each handle drags a real property rather than a pair of handlers, the way
    # Blender's own gizmo template does: a handler-bound target is read
    # differently once a drag is running.
    wired = all(getattr(g, "target", (None, None))[0] is hooked
                for _n, g in group.gizmos.made)
    # ... and each one reaches its own face, not the last index of the loop
    reads = sorted(round(getattr(hooked, g.target[1]), 3)
                   for _n, g in group.gizmos.made) if wired else []

    # And the group is configured the way Blender's own arrow template is:
    # anything extra there is what drew the dragged handle twice.
    options = _gizmo.LS3D_GGT_DummyBox.bl_options
    check("the gizmo group's own hooks run without tripping over a name",
          hooks_ran and len(built) == 6
          and set(built) == {_gizmo.LS3D_GT_DummyHandle.bl_idname}
          and placed and wired
          and reads == sorted([1.0, 4.0, 2.0, 5.0, 3.0, 6.0])
          and options == {"3D", "PERSISTENT"}
          # ours is the only drawing there is, so it keeps drawing through a
          # drag rather than leaving the face bare
          and all(getattr(g, "use_draw_modal", False)
                  for _n, g in group.gizmos.made),
          complaint or f"{len(built)} handle(s) built, matrices set {placed}, "
                       f"handlers wired {wired}, offsets {reads}, "
                       f"options {sorted(options)}")

    # The drag is ours now, so its guard is worth pinning down: with nothing
    # to project through there is no honest answer for how far the pointer
    # moved along the axis, and the handle must hold still rather than leap.
    class _NoView:
        region = None
        region_data = None

    from mathutils import Matrix as _Mat
    held = _gizmo.drag_distance(_NoView(), _Mat.Identity(4), (0, 0), (40, 40))
    check("a drag with nothing to project through moves nothing",
          held is None,
          f"got {held!r} instead of None")

    # A face stays under the pointer. Seen in perspective with its axis half
    # toward the view, two opposite faces used to follow the pointer at
    # different speeds - the drag was measured along the axis drawn a whole
    # metre out, which perspective shrinks one way and swells the other.
    # Pointed at the place 10 cm out along each face's axis, each face now
    # goes those 10 cm.
    import math as _math
    from bpy_extras.view3d_utils import location_3d_to_region_2d as _to_screen

    class _Region:
        width, height = 1000, 800

    class _Perspective:
        is_perspective = True
        view_distance = 2.0

        def __init__(self):
            eye = _Mat.Translation((1.2, -1.6, 1.1)) @ _Mat.Rotation(
                _math.radians(70.0), 4, "X") @ _Mat.Rotation(
                _math.radians(35.0), 4, "Y").inverted()
            self.view_matrix = eye.inverted()
            near, far, lens = 0.05, 100.0, 1.0 / _math.tan(_math.radians(25.0))
            aspect = _Region.width / _Region.height
            window = _Mat(((lens / aspect, 0.0, 0.0, 0.0), (0.0, lens, 0.0, 0.0),
                           (0.0, 0.0, (far + near) / (near - far),
                            2.0 * far * near / (near - far)),
                           (0.0, 0.0, -1.0, 0.0)))
            self.window_matrix = window
            self.perspective_matrix = window @ self.view_matrix

    class _InView:
        region = _Region()
        region_data = _Perspective()

    from mathutils import Vector as _DVec
    followed = []
    for sign in (1.0, -1.0):
        face = _DVec((0.15 * sign, 0.0, 1.0))
        axis = _DVec((sign, 0.0, 0.0))
        handle = _Mat.Translation(face) @ axis.to_track_quat(
            "Z", "Y").to_matrix().to_4x4()
        start = _to_screen(_InView.region, _InView.region_data, face)
        aim = _to_screen(_InView.region, _InView.region_data, face + axis * 0.1)
        followed.append(_gizmo.drag_distance(_InView(), handle, tuple(start),
                                             tuple(aim)))
    check("a face follows the pointer, whichever side of the box it is on",
          all(d is not None and abs(d - 0.1) < 1e-4 for d in followed),
          f"pointed 10 cm out, the two faces went {followed}")

    # -- Light ---------------------------------------------------------------
    # Forty bytes, read as one blob in the engine's own member order, and not
    # one model the game ships has a light frame in it - so the corpus cannot
    # vouch for this block the way it does for every other one. The checks below
    # stand in for that: the block is exactly forty bytes, every field comes
    # back where it went, and a light with nothing set stops the export.
    _scene_codec = ls3d_module("4ds.codec.scene")
    _types = ls3d_module("4ds.codec.types")
    _binary = ls3d_module("common.binary")

    source_light = _types.Light(
        mode=0xFF6B, light_type=C.LIGHT_SPOT, power=1.25,
        color=(0.25, 0.5, 0.75), range_near=3.5, range_far=42.0,
        cone_inner=0.34906587, cone_outer=0.69813174)
    light_writer = _binary.BinaryWriter()
    _scene_codec.write_light(light_writer, source_light)
    raw_light = light_writer.getvalue()
    reread = _scene_codec.read_light(_binary.BinaryReader(raw_light, "light"))
    # Order matters as much as size: mode and type are both unsigned words, so
    # swapping the two would round-trip perfectly here and still be wrong in the
    # game. Unpacking the bytes by hand is what catches that.
    import struct as _struct
    by_hand = _struct.unpack("<2I8f", raw_light)
    check("a light's forty bytes round-trip field for field",
          len(raw_light) == 40
          and reread.mode == source_light.mode
          and reread.light_type == source_light.light_type
          and abs(reread.power - source_light.power) < 1e-6
          and tuple(round(c, 6) for c in reread.color) == (0.25, 0.5, 0.75)
          and abs(reread.range_near - 3.5) < 1e-6
          and abs(reread.range_far - 42.0) < 1e-6
          and by_hand[0] == 0xFF6B and by_hand[1] == C.LIGHT_SPOT
          and abs(by_hand[2] - 1.25) < 1e-6
          and abs(by_hand[9] - source_light.cone_outer) < 1e-6,
          f"{len(raw_light)} byte(s); as words {by_hand[0]:#x}, {by_hand[1]}")

    # A light frame with no settings is a bug upstream. Writing forty zeros for
    # it would make a file that loads and is silently unlit.
    _doc_codec = ls3d_module("4ds.codec.document")
    bare_light = _types.Frame()
    bare_light.frame_type = C.FRAME_LIGHT
    bare_light.name = "light"
    try:
        _doc_codec.write_frame(_binary.BinaryWriter(), bare_light, 1)
        refused_bare = False
    except _binary.FormatError:
        refused_bare = True
    check("a light frame with no settings is refused, not zero-filled",
          refused_bare,
          "" if refused_bare else "the writer stood in forty zero bytes")

    # Through Blender: the eight values are the whole light, and the export
    # reads them off the object rather than deriving any of them.
    fresh_scene()
    lamp = bpy.data.objects.new("street_lamp", None)
    bpy.context.collection.objects.link(lamp)
    lamp.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    lamp.ls3d_frame_type = str(C.FRAME_LIGHT)
    lamp.ls3d_light_type = str(C.LIGHT_SPOT)
    lamp.ls3d_light_mode = 0x1234
    lamp.ls3d_light_power = 2.5
    lamp.ls3d_light_color = (0.125, 0.25, 0.5)
    lamp.ls3d_light_range_near = 1.5
    lamp.ls3d_light_range_far = 37.25
    lamp.ls3d_light_cone_inner = 0.5
    lamp.ls3d_light_cone_outer = 1.0
    lamp.location = (3.0, -4.0, 5.0)

    light_path = os.path.join(out_dir, "light.4ds")
    written_light = None
    if export_op(light_path) == {"FINISHED"}:
        written_light = read_file(light_path)
    light_frame = written_light.frames[0] if written_light else None
    back = light_frame.light if light_frame is not None else None
    check("a light's eight values survive the export exactly",
          light_frame is not None and light_frame.frame_type == C.FRAME_LIGHT
          and back is not None
          and back.mode == 0x1234 and back.light_type == C.LIGHT_SPOT
          and abs(back.power - 2.5) < 1e-6
          and tuple(round(c, 6) for c in back.color) == (0.125, 0.25, 0.5)
          and abs(back.range_near - 1.5) < 1e-6
          and abs(back.range_far - 37.25) < 1e-6
          and abs(back.cone_inner - 0.5) < 1e-6
          and abs(back.cone_outer - 1.0) < 1e-6,
          f"wrote {back}")

    # And back in: re-importing what was just written has to land on the same
    # eight numbers, because nothing about them is recomputed.
    if written_light is not None:
        fresh_scene()
        import_op(light_path)
        reimported = next((o for o in bpy.context.scene.objects
                           if int(getattr(o, "ls3d_frame_type", -1))
                           == C.FRAME_LIGHT), None)
        check("a light re-imports onto the same eight values",
              reimported is not None
              and reimported.type == "EMPTY"
              and reimported.empty_display_type == C.LIGHT_EMPTY_DISPLAY
              and int(reimported.ls3d_light_type) == C.LIGHT_SPOT
              and reimported.ls3d_light_mode == 0x1234
              and abs(reimported.ls3d_light_power - 2.5) < 1e-6
              and abs(reimported.ls3d_light_range_far - 37.25) < 1e-6
              and abs(reimported.ls3d_light_cone_outer - 1.0) < 1e-6,
              f"kind {reimported and int(reimported.ls3d_light_type)}, "
              f"mode {reimported and reimported.ls3d_light_mode:#x}")

    # Both whole-word fields have to survive their top bit. Blender's integer
    # property is signed, so a word above 0x7FFFFFFF does not fit as a positive
    # number - given a floor of zero it would be clamped on the way in and the
    # file would come back out different. They are kept as the signed pattern
    # and masked on the way out, the way every other flag word here is.
    fresh_scene()
    wide_light = bpy.data.objects.new("wide_light", None)
    bpy.context.collection.objects.link(wide_light)
    wide_light.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    wide_light.ls3d_frame_type = str(C.FRAME_LIGHT)
    from_file = _types.Light(mode=0xF0000001, light_type=0x80000003,
                             power=1.0, color=(1.0, 1.0, 1.0),
                             range_near=1.0, range_far=10.0,
                             cone_inner=0.1, cone_outer=0.2)
    importer = ls3d_module("4ds.importer")
    stub = type("S", (), {"report": type("R", (), {
        "warn": staticmethod(lambda *a, **k: None)})()})()
    importer.Importer._apply_light(stub, wide_light, from_file)
    # Unknown kinds read as None, which is what the engine makes of them, while
    # the word itself is untouched.
    shows_none = int(wide_light.ls3d_light_type) == 0
    wide_path = os.path.join(out_dir, "light_wide.4ds")
    wide_written = None
    if export_op(wide_path) == {"FINISHED"}:
        wide_written = read_file(wide_path)
    wide_back = wide_written.frames[0].light if wide_written else None
    check("a light's mode and type survive their top bit",
          wide_back is not None
          and wide_back.mode == 0xF0000001
          and wide_back.light_type == 0x80000003
          and shows_none,
          f"mode {wide_back and wide_back.mode:#x}, type "
          f"{wide_back and wide_back.light_type:#x}, dropdown reads None "
          f"{shows_none}")

    # The aim handle. A light has no direction field - it shines the way its
    # frame faces, along the frame's own forward axis, which the Y/Z swap puts
    # on Blender's local +Y. So the handle has to sit on +Y, and dragging it has
    # to turn the object and change nothing else.
    from mathutils import Vector as _Vec
    _aim = ls3d_module("4ds.gizmo_light")
    fresh_scene()
    beam = bpy.data.objects.new("beam", None)
    bpy.context.collection.objects.link(beam)
    beam.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    beam.ls3d_frame_type = str(C.FRAME_LIGHT)
    beam.ls3d_light_type = str(C.LIGHT_SPOT)
    beam.ls3d_light_range_far = 12.0
    bpy.context.view_layer.objects.active = beam
    beam.select_set(True)

    on_axis = 0.0
    for rotation, location, scale in (
            ((0.0,) * 3, (0.0,) * 3, (1.0,) * 3),
            ((0.4, -1.1, 0.9), (5.0, -2.0, 7.0), (1.0,) * 3),
            ((0.0, 0.7, 0.0), (-3.0, 1.0, 0.0), (2.0, 2.0, 2.0)),
    ):
        beam.rotation_euler = rotation
        beam.location = location
        beam.scale = scale
        bpy.context.view_layer.update()
        at = _aim.aim_position(beam)
        wanted_at = (beam.matrix_world.normalized()
                     @ _Vec((0.0, _aim.aim_reach(beam), 0.0)))
        on_axis = max(on_axis, (at - wanted_at).length)

    # The handle rides the end of what the viewport draws, so a spot's reach is
    # its far range times the frame's scale - the same figure the cone uses.
    beam.scale = (2.0, 2.0, 2.0)
    bpy.context.view_layer.update()
    rides_beam = abs(_aim.aim_reach(beam) - 24.0) < 1e-4
    # A spot left at range zero would put the handle inside the light's own
    # marker, where it could never be grabbed.
    beam.ls3d_light_range_far = 0.0
    grabbable = _aim.aim_reach(beam) == _aim.AIM_REACH_FALLBACK
    beam.ls3d_light_range_far = 12.0

    # Aiming: local +Y ends up on the target, and nothing else moves.
    beam.location = (1.0, 2.0, 3.0)
    beam.scale = (1.5, 1.5, 1.5)
    bpy.context.view_layer.update()
    before_aim = (tuple(round(v, 6) for v in beam.location),
                  tuple(round(v, 6) for v in beam.scale),
                  int(beam.ls3d_light_type), beam.ls3d_light_mode,
                  round(beam.ls3d_light_power, 6),
                  round(beam.ls3d_light_range_far, 6),
                  round(beam.ls3d_light_cone_outer, 6))
    aimed = 0.0
    for target in ((10.0, 2.0, 3.0), (1.0, 2.0, 30.0), (-5.0, -6.0, -7.0)):
        _aim.aim_at(beam, _Vec(target))
        bpy.context.view_layer.update()
        heading = ((beam.matrix_world.normalized() @ _Vec((0.0, 1.0, 0.0)))
                   - beam.matrix_world.translation).normalized()
        wanted = (_Vec(target) - beam.matrix_world.translation).normalized()
        aimed = max(aimed, (heading - wanted).length)
    after_aim = (tuple(round(v, 6) for v in beam.location),
                 tuple(round(v, 6) for v in beam.scale),
                 int(beam.ls3d_light_type), beam.ls3d_light_mode,
                 round(beam.ls3d_light_power, 6),
                 round(beam.ls3d_light_range_far, 6),
                 round(beam.ls3d_light_cone_outer, 6))
    # Aiming a light that is already sitting on its target has no answer, so it
    # says so rather than snapping to some arbitrary heading.
    degenerate = _aim.aim_at(beam, beam.matrix_world.translation)

    check("a light's aim handle sits on its beam and turns it to face a target",
          on_axis < 1e-5 and rides_beam and grabbable
          and aimed < 1e-5 and before_aim == after_aim and not degenerate,
          f"handle off the beam by {on_axis:.2e}, reach follows scale "
          f"{rides_beam}, zero range still grabbable {grabbable}, worst aim "
          f"error {aimed:.2e}, everything else untouched "
          f"{before_aim == after_aim}")

    # Aiming a parented light writes the local rotation, because that is what
    # the file stores - the parent's own turn has to come back out of it.
    fresh_scene()
    mast = bpy.data.objects.new("mast", None)
    bpy.context.collection.objects.link(mast)
    mast.rotation_euler = (0.0, 0.0, 1.2)
    mast.location = (4.0, 0.0, 0.0)
    child_beam = bpy.data.objects.new("child_beam", None)
    bpy.context.collection.objects.link(child_beam)
    child_beam.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    child_beam.ls3d_frame_type = str(C.FRAME_LIGHT)
    child_beam.ls3d_light_type = str(C.LIGHT_SPOT)
    child_beam.parent = mast
    child_beam.location = (0.0, 1.0, 0.0)
    bpy.context.view_layer.update()
    _aim.aim_at(child_beam, _Vec((0.0, 0.0, 20.0)))
    bpy.context.view_layer.update()
    child_heading = ((child_beam.matrix_world.normalized()
                      @ _Vec((0.0, 1.0, 0.0)))
                     - child_beam.matrix_world.translation).normalized()
    child_wanted = (_Vec((0.0, 0.0, 20.0))
                    - child_beam.matrix_world.translation).normalized()
    check("aiming a parented light still points it where you asked",
          (child_heading - child_wanted).length < 1e-5,
          f"off by {(child_heading - child_wanted).length:.2e}")

    # Only the two kinds with a heading get a handle, and only while the light
    # is the one you have picked.
    fresh_scene()
    picked = bpy.data.objects.new("picked", None)
    bpy.context.collection.objects.link(picked)
    picked.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    picked.ls3d_frame_type = str(C.FRAME_LIGHT)
    bpy.context.view_layer.objects.active = picked
    picked.select_set(True)
    offered = {}
    for kind in (C.LIGHT_POINT, C.LIGHT_SPOT, C.LIGHT_DIRECTIONAL,
                 C.LIGHT_AMBIENT, C.LIGHT_FOG):
        picked.ls3d_light_type = str(kind)
        offered[kind] = _aim.LS3D_GGT_LightAim.poll(bpy.context)
    picked.ls3d_light_type = str(C.LIGHT_SPOT)
    picked.select_set(False)
    deselected = _aim.LS3D_GGT_LightAim.poll(bpy.context)
    # A dummy is not a light and must not collect an aim handle either.
    picked.empty_display_type = "CUBE"
    picked.ls3d_frame_type = str(C.FRAME_DUMMY)
    picked.select_set(True)
    not_a_light = _aim.LS3D_GGT_LightAim.poll(bpy.context)
    check("only a spot or directional light is offered an aim handle",
          offered == {C.LIGHT_POINT: False, C.LIGHT_SPOT: True,
                      C.LIGHT_DIRECTIONAL: True, C.LIGHT_AMBIENT: False,
                      C.LIGHT_FOG: False}
          and not deselected and not not_a_light,
          f"by kind {offered}, when deselected {deselected}, "
          f"on a dummy {not_a_light}")

    # The group's hooks need a real 3D region, so they run against a stand-in -
    # the same reason the dummy's do. A name that went missing inside setup used
    # to surface only as a traceback in somebody's console.
    fresh_scene()
    hooked_light = bpy.data.objects.new("hooked_light", None)
    bpy.context.collection.objects.link(hooked_light)
    hooked_light.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    hooked_light.ls3d_frame_type = str(C.FRAME_LIGHT)
    hooked_light.ls3d_light_type = str(C.LIGHT_SPOT)
    bpy.context.view_layer.objects.active = hooked_light
    hooked_light.select_set(True)
    bpy.context.view_layer.update()

    class _StandInBall:
        is_modal = False

    class _StandInBalls:
        def __init__(self):
            self.made = []

        def new(self, name):
            ball = _StandInBall()
            self.made.append((name, ball))
            return ball

        def clear(self):
            self.made.clear()

    aim_body = {k: v for k, v in _aim.LS3D_GGT_LightAim.__dict__.items()
                if not k.startswith("bl_")}
    aim_group = type("A", (), aim_body)()
    aim_group.gizmos = _StandInBalls()
    aim_hooks, aim_complaint = True, ""
    try:
        aim_group.setup(bpy.context)
        aim_group.draw_prepare(bpy.context)
    except Exception as exc:                                  # noqa: BLE001
        aim_hooks, aim_complaint = False, f"{type(exc).__name__}: {exc}"
    aim_built = [name for name, _b in aim_group.gizmos.made]
    check("the aim handle's own hooks run without tripping over a name",
          aim_hooks and aim_built == [_aim.LS3D_GT_LightAim.bl_idname]
          and all(hasattr(b, "matrix_basis") for _n, b in aim_group.gizmos.made)
          and all(getattr(b, "use_draw_modal", False)
                  for _n, b in aim_group.gizmos.made)
          and _aim.LS3D_GGT_LightAim.bl_options == {"3D", "PERSISTENT"},
          aim_complaint or f"built {aim_built}")

    # Typing a rotation is the other way to aim, and the operator is the middle
    # ground: point the light at whatever else is selected, or at the 3D cursor.
    fresh_scene()
    aimer = bpy.data.objects.new("aimer", None)
    bpy.context.collection.objects.link(aimer)
    aimer.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    aimer.ls3d_frame_type = str(C.FRAME_LIGHT)
    aimer.ls3d_light_type = str(C.LIGHT_SPOT)
    bpy.context.view_layer.objects.active = aimer
    aimer.select_set(True)
    bpy.context.scene.cursor.location = (0.0, 0.0, 9.0)
    cursor_result = bpy.ops.ls3d.light_aim()
    bpy.context.view_layer.update()
    at_cursor = ((aimer.matrix_world.normalized() @ _Vec((0.0, 1.0, 0.0)))
                 - aimer.matrix_world.translation).normalized()

    post = bpy.data.objects.new("post", None)
    bpy.context.collection.objects.link(post)
    post.location = (6.0, 0.0, 0.0)
    post.select_set(True)
    object_result = bpy.ops.ls3d.light_aim()
    bpy.context.view_layer.update()
    at_object = ((aimer.matrix_world.normalized() @ _Vec((0.0, 1.0, 0.0)))
                 - aimer.matrix_world.translation).normalized()

    # Two candidates is ambiguous, so it says so rather than picking one.
    # An operator that reports an error raises out of bpy.ops, so that is what
    # "it refused" looks like from here.
    spare = bpy.data.objects.new("spare", None)
    bpy.context.collection.objects.link(spare)
    spare.select_set(True)
    facing_before = tuple(round(v, 6) for v in aimer.rotation_quaternion)
    try:
        crowded = bpy.ops.ls3d.light_aim()
    except RuntimeError as exc:
        crowded = {"CANCELLED"} if "one thing to aim at" in str(exc) else str(exc)
    # and refusing leaves the light pointing where it already was
    crowded_kept = tuple(round(v, 6) for v in aimer.rotation_quaternion)         == facing_before
    check("the aim operator points a light at the cursor or at one object",
          cursor_result == {"FINISHED"} and object_result == {"FINISHED"}
          and (at_cursor - _Vec((0.0, 0.0, 1.0))).length < 1e-5
          and (at_object - _Vec((1.0, 0.0, 0.0))).length < 1e-5
          and crowded == {"CANCELLED"} and crowded_kept,
          f"cursor {cursor_result} gave "
          f"{tuple(round(v, 3) for v in at_cursor)}, object {object_result} "
          f"gave {tuple(round(v, 3) for v in at_object)}, two selected "
          f"{crowded}, heading kept {crowded_kept}")

    # The Add menu builds one, and what it builds has to be a light that
    # actually lights: a spot pointing down, with the engine's own starting
    # figures and the real-time bit on - without that bit it does nothing at all
    # while the game runs, which is a light that looks right and is not one.
    fresh_scene()
    bpy.context.scene.cursor.location = (2.0, 3.0, 4.0)
    added = bpy.ops.ls3d.add_light()
    made = bpy.context.object
    pointing = ((made.matrix_world.normalized() @ _Vec((0.0, 1.0, 0.0)))
                - made.matrix_world.translation).normalized()
    check("the Add menu builds a light that already lights something",
          added == {"FINISHED"} and made is not None
          and made.type == "EMPTY"
          and made.empty_display_type == C.LIGHT_EMPTY_DISPLAY
          and int(made.ls3d_frame_type) == C.FRAME_LIGHT
          and int(made.ls3d_light_type_value) == C.LIGHT_SPOT
          and made.ls3d_light_mode == C.DEFAULT_LIGHT_MODE
          and made.ls3d_light_mode & C.LM_REALTIME
          and abs(made.ls3d_light_range_far - C.DEFAULT_LIGHT_RANGE_FAR) < 1e-6
          and abs(made.ls3d_light_cone_outer
                  - C.DEFAULT_LIGHT_CONE_OUTER) < 1e-6
          and tuple(round(v, 3) for v in made.location) == (2.0, 3.0, 4.0)
          # pointing down, which is what a lamp does - an unrotated frame
          # shines sideways
          and (pointing - _Vec((0.0, 0.0, -1.0))).length < 1e-5
          # and it gets the aim handle straight away
          and _aim.LS3D_GGT_LightAim.poll(bpy.context),
          f"result {added}, kind "
          f"{made and int(made.ls3d_light_type_value)}, mode "
          f"{made and made.ls3d_light_mode:#x}, pointing "
          f"{tuple(round(v, 3) for v in pointing)}")

    # The panel draws from the eight values and the viewport draws its own
    # outline from them. Neither may raise on any of the nine kinds, and a
    # draw handler that raises leaves the whole 3D view blank.
    class _LightLayout:
        scale_y = 1.0

        def _child(self, *a, **k):
            return _LightLayout()

        box = row = column = split = grid_flow = _child

        def label(self, text="", **k):
            pass

        def prop(self, data, name, **k):
            getattr(data, name)          # the panel must name real properties

        def separator(self):
            pass

        def operator(self, idname, **k):
            return type("Op", (), {"direction": ""})()

        def panel(self, idname, default_closed=False):
            return _LightLayout(), _LightLayout()

    fresh_scene()
    shown = bpy.data.objects.new("shown", None)
    bpy.context.collection.objects.link(shown)
    shown.empty_display_type = C.LIGHT_EMPTY_DISPLAY
    shown.ls3d_frame_type = str(C.FRAME_LIGHT)
    bpy.context.view_layer.objects.active = shown
    _ui_4ds = ls3d_module("4ds.ui")
    _vp_light = ls3d_module("4ds.viewport")
    drew, drew_complaint = True, ""
    for kind in list(range(C.LIGHT_TYPE_COUNT)) + [99]:
        shown.ls3d_light_type_value = kind
        try:
            _ui_4ds.The4DSObjectPanel._draw_light(
                _ui_4ds.The4DSObjectPanel, _LightLayout(), shown)
            _vp_light.light_lines(shown)
        except Exception as exc:                              # noqa: BLE001
            drew = False
            drew_complaint += f" kind {kind}: {type(exc).__name__}: {exc};"
    check("a light's panel and outline draw for every kind the engine knows",
          drew, drew_complaint.strip())

    # Six bits of the mode word are read, each by a different part of the game.
    # Bit 4 lights lit objects while the game runs and is off in the engine's
    # default; everything else - bit 2 and bits 7 up - is never read.
    shown.ls3d_light_mode = 0
    shown.lm_lit_objects = True
    lit_bit = shown.ls3d_light_mode
    check("every light mode bit the game reads has a switch",
          C.LM_LIT_OBJECTS == 0x10 and lit_bit == 0x10
          and sorted(bit for bit, _a, _l, _d in C.LIGHT_MODE_FLAGS)
          == [0x01, 0x02, 0x08, 0x10, 0x20, 0x40]
          and C.LIGHT_MODE_UNREAD_BITS == 0xFFFFFF84
          and not C.DEFAULT_LIGHT_MODE & C.LM_LIT_OBJECTS,
          f"lit objects bit set the word to {lit_bit:#x}; unread "
          f"{C.LIGHT_MODE_UNREAD_BITS:#x}")

    # The panel labels the two stored ranges for what each kind makes of them,
    # says which mode bits are kept unread, warns when a light lights nothing,
    # and says where a light takes effect at all - with the scene2.bin how-to in
    # a section of its own that starts folded.
    class _LightWords:
        def __init__(self):
            self.labels = []
            self.props = []
            self.panels = []

        def label(self, text="", **_k):
            self.labels.append(text)

        def prop(self, _data, name, text="", **_k):
            self.props.append((name, text))

        def operator(self, idname, **_k):
            return type("Op", (), {})()

        def panel(self, idname, default_closed=False):
            # Hand back an open body so the folded text is read as well.
            self.panels.append((idname, default_closed))
            return self, self

        def __getattr__(self, _name):
            return lambda *a, **k: self

    def _light_panel(kind, mode=C.DEFAULT_LIGHT_MODE):
        shown.ls3d_light_type_value = kind
        shown.ls3d_light_mode = mode
        shown.ls3d_light_range_near = 50.0
        shown.ls3d_light_range_far = 80.0
        words = _LightWords()
        _ui_4ds.The4DSObjectPanel._draw_light(_ui_4ds.The4DSObjectPanel, words,
                                               shown)
        return words

    fog_words = _light_panel(C.LIGHT_FOG)
    density_words = _light_panel(C.LIGHT_POINTFOG)
    spot_words = _light_panel(C.LIGHT_SPOT)
    dark_words = _light_panel(C.LIGHT_SPOT, mode=C.LM_SHADOW)
    fog_labels = " | ".join(fog_words.labels)
    # A note is one sentence, which the panel breaks to the width it is drawn
    # at, so what it says is read off the lines put back together.
    fog_said = " ".join(fog_words.labels)
    spot_said = " ".join(spot_words.labels)
    dark_said = " ".join(dark_words.labels)
    check("the light panel says what each kind makes of its values",
          ("ls3d_light_range_near", "Start %") in fog_words.props
          and ("ls3d_light_range_far", "Thickest %") in fog_words.props
          and "Thickest at 80% of the view distance" in fog_said
          and "starting at 50% of that - 40% of the view." in fog_said
          and not any(name == "ls3d_light_power" for name, _t in fog_words.props)
          and ("ls3d_light_range_near", "Density") in density_words.props
          and not any(name == "ls3d_light_range_far"
                      for name, _t in density_words.props)
          and ("ls3d_light_range_near", "Near Range") in spot_words.props
          # The bits nothing reads get no switches, only a count in words.
          and "8 more switches are on that the game never" in spot_said
          and not any(name.startswith("lm_bit_")
                      for name, _t in spot_words.props)
          and any("sectors" in t for t in spot_words.labels)
          and "With neither Lights Objects nor Lights" in dark_said
          and "With neither Lights Objects nor Lights" not in spot_said,
          f"fog said {fog_labels[:200]}; density props {density_words.props}")
    # A note is written as one sentence and broken where the panel it is
    # drawn in runs out of room. Blender cuts a label short with no way to
    # read the rest, and a note broken by hand is cut short the moment
    # somebody drags the sidebar narrower than the person who wrote it.
    _panels = ls3d_module("common.panels")
    _sentence = ("The armature stands where the skinned mesh's frame is: the "
                 "point the joints hang from and the game's animations move "
                 "and turn the body about.")
    narrow = _panels.lines_for(_sentence, 140)
    wide = _panels.lines_for(_sentence, 400)
    whole = " ".join(narrow) == _sentence and " ".join(wide) == _sentence

    class _Note:
        def __init__(self):
            self.lines = []

        def label(self, text="", icon="NONE"):
            self.lines.append((text, icon))

    _note = _Note()
    _panels.say(_note, _sentence, icon="INFO")
    _icons = [icon for _text, icon in _note.lines]
    check("a panel note is written whole and broken to the panel's width",
          whole and len(narrow) > len(wide) > 1
          and max(len(line) for line in narrow)
          < max(len(line) for line in wide)
          and len(_note.lines) > 1 and _icons[0] == "INFO"
          and set(_icons[1:]) == {"BLANK1"},
          f"{len(narrow)} line(s) in a narrow panel, longest "
          f"{max(len(line) for line in narrow)} letters; {len(wide)} in a "
          f"wide one, longest {max(len(line) for line in wide)}; every word "
          f"kept {whole}; icons {_icons}")

    check("the light panel folds away how to switch a light on in scene2.bin",
          all(("ls3d_light_scene2", True) in words.panels
              for words in (fog_words, density_words, spot_words, dark_words))
          and "Switching It On in scene2.bin" in spot_words.labels
          # Which file each requirement goes in, and which kinds it is for.
          and "In the model's .4ds:" in spot_words.labels
          and "In the mission's scene2.bin:" in spot_words.labels
          and f"Frame <model object>.{shown.name}" in spot_words.labels
          and all(line in fog_words.labels for line in
                  ("Animated Objects 0, or its .5ds beside it", "Enable on",
                   "Power not 0", "Sector", "Color or Power",
                   "Range or Cone", "Range", "All kinds", "Spot",
                   "Layered Fog", "Lighting kinds, Layered Fog"))
          and C.LIGHT_SPOT in dict((t, s) for t, _k, s in
                                   _ui_4ds._LIGHT_DATABLOCK_NEEDS)["Range or Cone"]
          and C.LIGHT_FOG not in dict((t, s) for t, _k, s in
                                      _ui_4ds._LIGHT_DATABLOCK_NEEDS)["Color or Power"]
          and C.LIGHT_FOG in dict((t, s) for t, _k, s in
                                  _ui_4ds._LIGHT_MODEL_NEEDS)["Enable on"]
          # Said in words: no raw values anywhere in the light's descriptions.
          and not any("0x" in text for words in
                      (fog_words, density_words, spot_words, dark_words)
                      for text in words.labels),
          f"panels {spot_words.panels}")

    # Each switch moves exactly its own bit, the bits nothing reads have no
    # switches, and the raw field takes hex with or without 0x - and refuses
    # what is not a value that fits the word instead of cutting it.
    words_obj = bpy.data.objects.new("every_bit", None)
    bpy.context.collection.objects.link(words_obj)
    flag_words = (
        ("ls3d_light_mode", 32, C.LIGHT_MODE_FLAGS),
        ("ls3d_sector_flags1", 32,
         ((C.SF_OCCLUDER, "sf_occluder", "", ""),
          (C.SF_SMALL_PORTAL_CULL, "sf_small_portal_cull", "", ""),
          (C.SF_SOUND_REVERB, "sf_sound_reverb", "", ""))
         + C.RESERVED_SECTOR_FLAGS),
        ("ls3d_portal_flags", 32,
         ((C.PF_ENABLED, "pf_enabled", "", ""),
          (C.PF_FAR_CULL, "pf_far_cull", "", ""))
         + C.RESERVED_PORTAL_FLAGS),
        ("ls3d_target_flags", 16, C.TARGET_FLAGS),
    )
    bit_problems = []
    for prop, width, table in flag_words:
        full = (1 << width) - 1
        for mask, attr, _label, _desc in table:
            setattr(words_obj, prop, 0)
            setattr(words_obj, attr, True)
            moved = getattr(words_obj, prop) & full
            setattr(words_obj, attr, False)
            if moved != mask or getattr(words_obj, prop) & full:
                bit_problems.append(f"{attr} moved {moved:#x}, not {mask:#x}")

    typed = []
    for text in ("ff68", "0x80000001", "  FF_6B ", "zz", "0x100000000", ""):
        words_obj.ls3d_light_mode_str = text
        typed.append(words_obj.ls3d_light_mode & 0xFFFFFFFF)
    words_obj.ls3d_target_flags_str = "0x10000"
    too_wide_kept = words_obj.ls3d_target_flags == 0
    words_obj.ls3d_target_flags_str = "8"
    target_typed = (words_obj.ls3d_target_flags, words_obj.tf_follow_scale,
                    words_obj.ls3d_target_flags_str)
    words_obj.ls3d_portal_flags_str = "80000000"
    portal_top = (words_obj.ls3d_portal_flags & 0xFFFFFFFF == 0x80000000
                  and words_obj.ls3d_portal_flags_str == "0x80000000")
    leftovers = [name for name in ("lm_bit_8", "sf_bit_3", "pf_bit_31",
                                   "tf_bit_4", "sg_group_0")
                 if hasattr(words_obj, name)]
    words_obj.ls3d_sector_flags2_str = "0x80000001"
    groups_typed = words_obj.ls3d_sector_flags2 & 0xFFFFFFFF == 0x80000001
    if not groups_typed:
        bit_problems.append("the Group Mask field did not take hex")
    if leftovers:
        bit_problems.append(f"unread-bit switches still there: {leftovers}")
    # The frame flag bytes take hex the same way, and refuse a second byte.
    words_obj.cull_flags_str = "0x44"
    words_obj.cull_flags_str = "144"
    byte_typed = (words_obj.cull_flags, words_obj.cull_flags_str,
                  words_obj.cf_res_scale_baked)
    raw_on_by_default = io_mafia_toolkit.LS3D_AddonPreferences.bl_rna \
        .properties["show_raw_flags"].default
    bpy.data.objects.remove(words_obj)
    check("flag switches move their own bit and the raw fields take hex",
          not bit_problems
          and typed == [0xFF68, 0x80000001, 0xFF6B, 0xFF6B, 0xFF6B, 0xFF6B]
          and too_wide_kept and target_typed == (8, True, "0x0008")
          and portal_top and raw_on_by_default
          and byte_typed == (0x44, "0x44", True),
          f"{bit_problems[:4]}; typed {[hex(v) for v in typed]}; target "
          f"{target_typed}, too wide kept {too_wide_kept}; portal top "
          f"{portal_top}; raw on by default {raw_on_by_default}; frame byte "
          f"{byte_typed}")

    # The outlines and handles are drawn from settings Blender knows nothing
    # about, and it repaints no viewport when one of them changes - a light
    # turned into a point light showed no range handles until something else
    # repainted the view. So every setting the drawing reads asks for it.
    _props_module = ls3d_module("properties")
    fresh_scene()
    bpy.ops.ls3d.add_light()
    lamp = bpy.context.object
    bpy.ops.ls3d.add_dummy()
    crate = bpy.context.object
    bpy.ops.ls3d.add_projector()
    beamer = bpy.context.object
    repaints = []
    real_redraw = _props_module.redraw_3d_views
    _props_module.redraw_3d_views = lambda: repaints.append(True)
    try:
        silent = []
        for target, name, value in (
                (lamp, "ls3d_light_type", str(C.LIGHT_POINT)),
                (lamp, "ls3d_light_type_value", C.LIGHT_SPOT),
                (lamp, "ls3d_light_range_near", 2.5),
                (lamp, "ls3d_light_range_far", 40.0),
                (lamp, "ls3d_light_cone_inner", 0.5),
                (lamp, "ls3d_light_cone_outer", 1.0),
                (lamp, "ls3d_light_color", (0.2, 0.4, 0.6)),
                (crate, "bbox_min", (-2.0, -2.0, -2.0)),
                (crate, "bbox_max", (2.0, 2.0, 2.0)),
                (beamer, "ls3d_projector_orthogonal",
                 not beamer.ls3d_projector_orthogonal)):
            before = len(repaints)
            setattr(target, name, value)
            if len(repaints) == before:
                silent.append(name)
    finally:
        _props_module.redraw_3d_views = real_redraw
    real_redraw()                # runs with no window without complaint
    check("changing what the viewport draws from repaints the viewport",
          not silent and lamp.ls3d_light_type_value == C.LIGHT_SPOT,
          f"no repaint from {silent}")

    # The four handles each drag one stored value, measured the way it is
    # drawn: the ranges as distances along the beam, the cones as how wide
    # they are where the beam ends - scaled by the frame, as the drawing is -
    # and none of them drags a value past its partner.
    import math as _lmath
    _light_gizmo = ls3d_module("4ds.gizmo_light")
    fresh_scene()
    bpy.ops.ls3d.add_light()
    spot = bpy.context.object
    spot.rotation_mode = "QUATERNION"
    spot.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    spot.location = (0.0, 0.0, 0.0)
    spot.scale = (2.0, 2.0, 2.0)
    spot.ls3d_light_range_near = 1.0
    spot.ls3d_light_range_far = 10.0
    spot.ls3d_light_cone_inner = _lmath.radians(20.0)
    spot.ls3d_light_cone_outer = _lmath.radians(60.0)
    bpy.context.view_layer.update()
    reads = (spot.ls3d_light_near_reach, spot.ls3d_light_far_reach,
             round(spot.ls3d_light_outer_radius, 5))
    spot.ls3d_light_far_reach = 30.0             # world units, frame scaled 2
    far_after = spot.ls3d_light_range_far
    spot.ls3d_light_near_reach = 100.0           # past the far one: stops there
    near_stopped = spot.ls3d_light_range_near
    spot.ls3d_light_near_reach = 4.0
    spot.ls3d_light_outer_radius = 30.0          # 30 across at 30 along: 90 deg
    outer_after = _lmath.degrees(spot.ls3d_light_cone_outer)
    spot.ls3d_light_inner_radius = 1000.0        # past the outer: stops there
    inner_stopped = _lmath.degrees(spot.ls3d_light_cone_inner)
    spot.ls3d_light_inner_radius = 0.0
    spot.ls3d_light_outer_radius = 1.0e9         # a half turn has no rim
    outer_capped = _lmath.degrees(spot.ls3d_light_cone_outer)
    spot.ls3d_light_outer_radius = 30.0
    bpy.context.view_layer.update()
    placed = {prop: tuple(round(v, 4) for v in matrix.translation)
              for prop, matrix in _light_gizmo.range_handle_matrices(spot).items()}
    check("the light's range and cone handles drag exactly what they show",
          reads == (2.0, 20.0, round(_lmath.tan(_lmath.radians(30.0)) * 20.0, 5))
          and abs(far_after - 15.0) < 1e-6
          and abs(near_stopped - 15.0) < 1e-6
          and abs(outer_after - 90.0) < 1e-3
          and abs(inner_stopped - 90.0) < 1e-3
          and abs(outer_capped - 179.0) < 1e-3
          and placed["ls3d_light_near_reach"] == (0.0, 4.0, 0.0)
          and placed["ls3d_light_far_reach"] == (0.0, 30.0, 0.0)
          and placed["ls3d_light_outer_radius"] == (30.0, 30.0, 0.0)
          and placed["ls3d_light_inner_radius"] == (0.0, 30.0, 0.0),
          f"read {reads}; far {far_after}, near stopped {near_stopped}, "
          f"outer {outer_after:.2f}, inner stopped {inner_stopped:.2f}, capped "
          f"{outer_capped:.2f}; placed {placed}")

    # Point and spot lights get the handles; the rest have no distances to
    # drag. On a spot the aim ball steps past the far handle by a fixed number
    # of pixels, so the two never sit on top of each other.
    spot.select_set(True)
    bpy.context.view_layer.objects.active = spot
    polls = {}
    for kind in (C.LIGHT_POINT, C.LIGHT_SPOT, C.LIGHT_DIRECTIONAL, C.LIGHT_FOG):
        spot.ls3d_light_type_value = kind
        polls[kind] = _light_gizmo.LS3D_GGT_LightRange.poll(bpy.context)
    spot.ls3d_light_type_value = C.LIGHT_SPOT
    from types import SimpleNamespace as _Ctx
    from mathutils import Matrix as _LMatrix
    zoomed = _Ctx(region=_Ctx(width=1000),
                  region_data=_Ctx(window_matrix=_LMatrix.Diagonal((0.1, 0.1, 1.0, 1.0)),
                                   perspective_matrix=_LMatrix.Identity(4)))
    bare_aim = _light_gizmo.aim_position(spot).y
    stepped_aim = _light_gizmo.aim_position(spot, zoomed).y
    spot_lines = len(_vp_light.light_lines(spot))
    spot.ls3d_light_range_near = 0.0
    no_near_lines = len(_vp_light.light_lines(spot))
    spot.ls3d_light_type_value = C.LIGHT_FOG
    fog_lines = _vp_light.light_lines(spot)
    check("handles on point and spot lights only, the aim ball clear of them",
          polls == {C.LIGHT_POINT: True, C.LIGHT_SPOT: True,
                    C.LIGHT_DIRECTIONAL: False, C.LIGHT_FOG: False}
          and abs(bare_aim - 30.0) < 1e-4
          # 36 pixels at 0.02 world units a pixel
          and abs(stepped_aim - (30.0 + 36.0 * 0.02)) < 1e-3
          and spot_lines - no_near_lines == 2 * 32
          and fog_lines == [],
          f"polls {polls}; aim {bare_aim:.3f} -> {stepped_aim:.3f}; spot lines "
          f"{spot_lines} vs {no_near_lines} without near; fog draws "
          f"{len(fog_lines)}")

    # The export warns about a light that lights nothing: a lighting kind with
    # neither of the two lighting bits. Either bit is enough, and a fog - which
    # asks for neither - is never warned about.
    _light_checks = ls3d_module("4ds.validation")
    from types import SimpleNamespace as _LightData

    def _lights_nothing(kind, mode):
        said = []
        _light_checks.validate_light(
            _LightData(name="lamp"),
            _LightData(mode=mode, light_type=kind, power=1.0, range_near=1.0,
                       range_far=10.0, cone_inner=0.3, cone_outer=0.6),
            lambda message, fix=None: said.append(message))
        return any("lights nothing" in message for message in said)

    check("a light with neither lighting bit is warned about, a fog never",
          _lights_nothing(C.LIGHT_SPOT, C.LM_SHADOW)
          and not _lights_nothing(C.LIGHT_SPOT, C.LM_LIT_OBJECTS)
          and not _lights_nothing(C.LIGHT_POINT, C.LM_REALTIME)
          and _lights_nothing(C.LIGHT_AMBIENT, 0)
          and not _lights_nothing(C.LIGHT_FOG, 0),
          "spot shadow-only, spot lit-objects, point objects, ambient 0, fog 0")

    # The export writes what is set and nothing else. A fallback would be
    # unreachable anyway - every one of these is a registered property, so it
    # always has a value - which makes it dead code that only fires when
    # something is genuinely wrong, and then hides it behind a number nobody
    # chose. So there are none, and a value that cannot be read stops the whole
    # export rather than being stood in for.
    import inspect as _inspect
    _export_util = ls3d_module("4ds.export_util")
    leftovers = []
    for module_name in ("4ds.export_util", "4ds.export_frames",
                        "4ds.export_payloads", "4ds.export_materials",
                        "4ds.exporter"):
        source = _inspect.getsource(ls3d_module(module_name))
        for line_no, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "def _prop(" in stripped:
                continue
            # a reader called with a second argument, or a getattr on one of
            # our own properties with something to fall back on
            if re.search(r"_(?:int|float)_prop\([^)]+,[^)]+,", stripped) or                re.search(r'getattr\([^,]+,\s*"(?:ls3d_|cull_|render_|rot_|bbox_)'
                         r'[^"]*",\s*[^)]', stripped):
                leftovers.append(f"{module_name}:{line_no} {stripped[:60]}")

    # and the readers really do stop rather than substitute
    class _Bare:
        name = "bare"

    stopped = []
    for reader in (_export_util._int_prop, _export_util._float_prop):
        try:
            reader(_Bare(), "ls3d_frame_type")
            stopped.append(f"{reader.__name__} returned a value")
        except _export_util.ExportError:
            pass
        except Exception as exc:                                  # noqa: BLE001
            stopped.append(f"{reader.__name__} raised {type(exc).__name__}")

    check("the export has no fallback values anywhere",
          not leftovers and not stopped,
          f"fallbacks left: {leftovers or 'none'}; "
          f"readers that did not stop: {stopped or 'none'}")

    # A frame missing the payload its own type calls for is a bug upstream, and
    # a blank stand-in would make a file that loads and is wrong - worse than
    # one never written. Every such case raises instead.
    _doc = ls3d_module("4ds.codec.document")
    _types2 = ls3d_module("4ds.codec.types")
    _bin = ls3d_module("common.binary")
    refused = {}
    for label, frame in (
            ("visual with no visual type",
             _types2.Frame(frame_type=C.FRAME_VISUAL, name="v")),
            ("projector with no payload",
             _types2.Frame(frame_type=C.FRAME_VISUAL, name="p",
                           visual_type=C.VISUAL_PROJECTOR)),
            ("emitor with no mesh",
             _types2.Frame(frame_type=C.FRAME_EMITOR, name="e")),
    ):
        try:
            _doc.write_frame(_bin.BinaryWriter(), frame, 1)
            refused[label] = "written"
        except _bin.FormatError:
            refused[label] = "refused"
    check("a frame with no payload is refused rather than written blank",
          all(v == "refused" for v in refused.values()),
          f"{refused}")

    # A mirror measures the camera against its reflection range and gives up
    # past it, so a range of zero is not "no limit" - it is a mirror that never
    # reflects and only ever draws its flat tint. One built from the menu used
    # to be exactly that, silently.
    fresh_scene()
    bpy.ops.ls3d.add_mirror()
    fresh_range = bpy.context.object.ls3d_mirror_range
    path = os.path.join(out_dir, "mirror_range.4ds")
    written = None
    if export_op(path) == {"FINISHED"}:
        written = next((f.mirror.draw_distance for f in read_file(path).frames
                        if f.mirror is not None), None)

    # ... and a mirror somebody has zeroed by hand is called out rather than
    # written quietly.
    bpy.context.object.ls3d_mirror_range = 0.0
    said = []
    original_warn = report_module.Report.warn

    def _hear(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _hear
    try:
        zeroed = export_op(os.path.join(out_dir, "mirror_zero.4ds"))
    finally:
        report_module.Report.warn = original_warn

    check("a mirror from the Add menu can actually reflect",
          fresh_range == C.DEFAULT_MIRROR_RANGE
          and written == C.DEFAULT_MIRROR_RANGE
          and C.DEFAULT_MIRROR_RANGE == 100.0
          and zeroed == {"FINISHED"}
          and any("never reflect" in message for message in said),
          f"new mirror carries {fresh_range:g}, wrote {written}; "
          f"zeroed said {[m[:48] for m in said] or 'nothing'}")

    # What a mirror carries has to come back through Blender unhurt. The view
    # box is four fields on the mirror - the file's own matrix, column by
    # column - so it comes back to the bit, rotated or not. Geometry is
    # compared as a set, which says the same thing whatever the order.
    def vertex_key(vertex, places=4):
        return (tuple(round(c, places) for c in vertex.position),
                tuple(round(c, places) for c in vertex.normal),
                tuple(round(c, places) for c in vertex.uv))

    def triangles(lod):
        out = []
        for group in lod.face_groups:
            for a, b, c in group.faces:
                out.append((group.material_id,
                            tuple(sorted((vertex_key(lod.vertices[a]),
                                          vertex_key(lod.vertices[b]),
                                          vertex_key(lod.vertices[c]))))))
        return sorted(out)

    # one cubic and rotated, one axis-aligned, one enormous
    mirrored = [name for name in ("kaluz.4ds", "kaluz8.4ds", "zrcadlo.4ds")
                if os.path.isfile(model(name))]
    exact, derived, geometry_ok = {}, {}, {}
    for name in mirrored:
        fresh_scene()
        import_op(model(name))
        out_path = os.path.join(out_dir, f"rt_{name}")
        if export_op(out_path) != {"FINISHED"}:
            exact[name] = "export failed"
            continue
        before, after = read_file(model(name)), read_file(out_path)
        a = next(f.mirror for f in before.frames if f.mirror is not None)
        b = next(f.mirror for f in after.frames if f.mirror is not None)
        # The bound's min and max are the extremes of real vertex values, so
        # they come back bit for bit. Its sphere is an average of those, and an
        # average rounds - on kaluz8 a center that should be the origin is
        # stored as 5.96e-08 and recomputed as 2.98e-08. Both are zero.
        exact[name] = all(getattr(a, field) == getattr(b, field) for field in
                          ("bbox_min", "bbox_max", "tint", "draw_distance",
                           "faces", "view_matrix"))
        derived[name] = (max(abs(x - y) for x, y in zip(a.center, b.center))
                         <= 1e-6 and abs(a.radius - b.radius) <= 1e-6)
        # every mesh in the file, as sets
        same = True
        for fa, fb in zip(before.frames, after.frames):
            if not (fa.geometry and fb.geometry):
                continue
            for la, lb in zip(fa.geometry.lods, fb.geometry.lods):
                if (sorted(map(vertex_key, la.vertices))
                        != sorted(map(vertex_key, lb.vertices))
                        or triangles(la) != triangles(lb)):
                    same = False
        geometry_ok[name] = same

    check("a mirror survives a trip through Blender with nothing lost",
          bool(mirrored)
          and all(exact.values())
          and all(derived.values())
          and all(geometry_ok.values()),
          f"exact fields {exact}; derived within tolerance {derived}; "
          f"geometry as a set {geometry_ok}")

    # ... and a view box typed in goes into the file as typed, and comes back
    # into the same fields. Nothing about it is squared up or rescaled: a box
    # the game can use need not have square corners.
    fresh_scene()
    bpy.ops.ls3d.add_mirror()
    typed = bpy.context.object
    typed.name = "typed"
    fields = {"ls3d_mirror_box_center": (0.25, 1.5, -0.125),
              "ls3d_mirror_box_x": (1.1, 0.2, 0.0),
              "ls3d_mirror_box_y": (0.0, 2.0, 0.3),
              "ls3d_mirror_box_z": (0.05, 0.0, 0.7)}
    for field, value in fields.items():
        setattr(typed, field, value)
    typed_path = os.path.join(out_dir, "mirror_typed.4ds")
    stored, returned = None, {}
    if export_op(typed_path) == {"FINISHED"}:
        stored = next(f.mirror.view_matrix for f in read_file(typed_path).frames
                      if f.mirror is not None)
        fresh_scene()
        import_op(typed_path)
        back = bpy.data.objects.get("typed")
        if back is not None:
            returned = {field: tuple(getattr(back, field)) for field in fields}

    def _f32(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]

    center, x_axis, y_axis, z_axis = (tuple(map(_f32, fields[f])) for f in fields)
    expected = (x_axis[0], x_axis[2], x_axis[1], 0.0,
                z_axis[0], z_axis[2], z_axis[1], 0.0,
                y_axis[0], y_axis[2], y_axis[1], 0.0,
                center[0], center[2], center[1], 1.0)
    check("a typed view box is written as typed and comes back the same",
          stored is not None and tuple(stored) == expected
          and returned == {field: tuple(map(_f32, value))
                           for field, value in fields.items()},
          f"wrote {stored}, read back {returned}")

    # An empty stands for a frame by its shape, and a dummy is a box - so the
    # cube is the only shape it can be. Every other shape used to fall through
    # to Dummy, which handed box handles to helper empties and wrote them into
    # the model as boxes nobody asked for.
    fresh_scene()
    _v3 = ls3d_module("4ds.viewport")
    verdicts = {}
    for display in ("CUBE", "SPHERE", "ARROWS", "PLAIN_AXES",
                    "CIRCLE", "CONE", "SINGLE_ARROW", "IMAGE"):
        probe = bpy.data.objects.new(f"empty_{display}", None)
        bpy.context.collection.objects.link(probe)
        probe.empty_display_type = display
        # Only where the shape allows it - a sphere or arrows empty refuses
        # Dummy outright now, which is the point.
        try:
            probe.ls3d_frame_type = str(C.FRAME_DUMMY)
            takes_dummy = True
        except TypeError:
            takes_dummy = False
        bpy.context.view_layer.objects.active = probe
        probe.select_set(True)
        verdicts[display] = (_v3.carries_frame(probe),
                             _gizmo.LS3D_GGT_DummyBox.poll(bpy.context),
                             takes_dummy)
        probe.select_set(False)

    check("only a cube empty is a dummy",
          verdicts["CUBE"] == (True, True, True)
          # the other shapes are frames, but never dummies: a sphere is a
          # lens flare, arrows a projector, a cone a light, and plain axes a
          # target or one of the payload-free frames.
          and all(verdicts[d] == (True, False, False)
                  for d in ("SPHERE", "ARROWS", "CONE", "PLAIN_AXES"))
          # and these are not frames at all. They still accept the value -
          # an enum has to offer something - but nothing reads it: no handles,
          # no panel, and the export passes them by.
          and all(verdicts[d] == (False, False, True) for d in
                  ("CIRCLE", "IMAGE", "SINGLE_ARROW")),
          f"{ {d: v for d, v in verdicts.items()} }")

    # A mirror's view box is no object of its own: it is four fields on the
    # mirror, outlined in the viewport, with a handle on each face the way a
    # dummy has. Dragging one face moves that face and leaves the one opposite
    # where it was.
    fresh_scene()
    bpy.ops.ls3d.add_mirror()
    mirror_obj = bpy.context.object
    mirror_obj.select_set(True)
    handles = (_gizmo.LS3D_GGT_MirrorViewBox.poll(bpy.context),
               _gizmo.LS3D_GGT_DummyBox.poll(bpy.context))
    outlined = [o.name for o, _c in _v3._mirror_view_boxes(bpy.context)]
    near, far = mirror_obj.ls3d_mirror_face_0, mirror_obj.ls3d_mirror_face_1
    mirror_obj.ls3d_mirror_face_1 = far + 1.0
    moved = (round(mirror_obj.ls3d_mirror_face_0 - near, 6),
             round(mirror_obj.ls3d_mirror_face_1 - far, 6))
    # Placed on a handle's face, facing out of it.
    handle = _gizmo.mirror_handle_matrix(mirror_obj, 1)
    face_middle = mirror_obj.matrix_world @ (
        _v3.mirror_view_box(mirror_obj)[0]
        + _v3.mirror_view_box(mirror_obj)[1][0])
    check("a mirror's view box is outlined and has a handle on each face",
          handles == (True, False) and outlined == [mirror_obj.name]
          and not mirror_obj.children
          and moved == (0.0, 1.0)
          and (handle.translation - face_middle).length < 1e-6,
          f"view box handles, dummy handles {handles}; outlined {outlined}; "
          f"children {[c.name for c in mirror_obj.children]}; dragging +X by "
          f"one moved the faces by {moved}")

    # ... and one that names no frame is left out of the file rather than
    # written as a dummy, with a word about why.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()            # something to export
    helper = bpy.data.objects.new("helper", None)
    bpy.context.collection.objects.link(helper)
    helper.empty_display_type = "CIRCLE"
    said = []
    original_warn = report_module.Report.warn

    def _listen(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _listen
    try:
        path = os.path.join(out_dir, "helper_empty.4ds")
        result = export_op(path)
    finally:
        report_module.Report.warn = original_warn
    names = [f.name for f in read_file(path).frames] if result == {"FINISHED"} else []
    check("an empty that names no frame stays out of the model",
          result == {"FINISHED"} and "helper" not in names
          and any("left out" in message for message in said),
          f"frames {names}; said {[m[:60] for m in said] or 'nothing'}")

    # The outline has a color of its own at rest and another once the dummy is
    # picked, so it is obvious which box you are about to edit.
    fresh_scene()
    _v2 = ls3d_module("4ds.viewport")
    odd = bpy.data.objects.new("odd", None)
    bpy.context.collection.objects.link(odd)
    odd.empty_display_type = "CUBE"
    odd.ls3d_frame_type = str(C.FRAME_DUMMY)
    odd.bbox_min = (-6.888, -13.131, -6.888)
    odd.bbox_max = (19.668, 13.131, 20.664)
    for other in bpy.context.scene.objects:
        other.select_set(False)
    at_rest = dict(_v2._outlined_boxes(bpy.context))
    odd.select_set(True)
    picked = dict(_v2._outlined_boxes(bpy.context))
    check("a dummy box outline turns red when its dummy is picked",
          at_rest.get(odd) == _v2.DUMMY_BOX_COLOR
          and picked.get(odd) == _v2.DUMMY_BOX_COLOR_SELECTED
          and _v2.DUMMY_BOX_COLOR != _v2.DUMMY_BOX_COLOR_SELECTED,
          f"at rest {tuple(round(c, 2) for c in at_rest.get(odd, ()))}, "
          f"picked {tuple(round(c, 2) for c in picked.get(odd, ()))}")

    # A handle is centered on its face, so dragging any face does move where the
    # other five are drawn - their faces have new middles. What must not move is
    # the *value* each one reports, because that is what Blender's drag arithmetic
    # is built on: shift the number under a handle that is not being dragged and
    # it jumps the moment you grab it.
    fresh_scene()
    dragged = bpy.data.objects.new("dragged", None)
    bpy.context.collection.objects.link(dragged)
    dragged.empty_display_type = "CUBE"
    dragged.ls3d_frame_type = str(C.FRAME_DUMMY)
    dragged.bbox_min, dragged.bbox_max = (-1.0, -2.0, -3.0), (4.0, 5.0, 6.0)
    bpy.context.view_layer.objects.active = dragged
    bpy.context.view_layer.update()

    disturbed, off_face = [], []
    for index in range(6):
        before = [round(_gizmo.face_offset(dragged, i), 4) for i in range(6)]
        _gizmo.set_face_offset(dragged, index,
                               _gizmo.face_offset(dragged, index) + 2.0)
        bpy.context.view_layer.update()
        after = [round(_gizmo.face_offset(dragged, i), 4) for i in range(6)]
        moved = [i for i in range(6) if before[i] != after[i]]
        if moved != [index]:
            disturbed.append((index, moved))
        # and every handle is still on the middle of its own face
        low, high = _dummy_box(dragged)
        for i, (axis, sign) in enumerate(_gizmo.FACES):
            at = _gizmo.handle_matrix(dragged, i).translation
            center = [(low[a] + high[a]) * 0.5 for a in range(3)]
            center[axis] = (high if sign > 0 else low)[axis]
            if (at - dragged.matrix_world @ _Vector(center)).length > 1e-4:
                off_face.append((index, i))
    check("dragging one face leaves every other handle's value alone",
          not disturbed and not off_face,
          "each drag moved only its own value, and all six stayed centered"
          if not disturbed and not off_face
          else f"values disturbed {disturbed}, handles off their faces {off_face}")

    # The maths above is reachable from a test; the gizmo group's own hooks are
    # not, because a real one needs a 3D region. So they are run against a stand
    # in - which is the point: a name that went missing inside setup or
    # draw_prepare used to surface only as a traceback in someone's console.
    fresh_scene()
    hooked = bpy.data.objects.new("hooked", None)
    bpy.context.collection.objects.link(hooked)
    hooked.empty_display_type = "CUBE"
    hooked.ls3d_frame_type = str(C.FRAME_DUMMY)
    hooked.bbox_min, hooked.bbox_max = (-1.0, -2.0, -3.0), (4.0, 5.0, 6.0)
    bpy.context.view_layer.objects.active = hooked
    hooked.select_set(True)
    bpy.context.view_layer.update()

    class _StandInGizmo:
        is_modal = False        # the real one reports whether it is being dragged

        def target_set_prop(self, _name, target, prop):
            self.target = (target, prop)

    class _StandInGizmos:
        def __init__(self):
            self.made = []

        def new(self, name):
            gizmo = _StandInGizmo()
            self.made.append((name, gizmo))
            return gizmo

        def clear(self):
            self.made.clear()

    group_body = {k: v for k, v in
                  _gizmo.LS3D_GGT_DummyBox.__dict__.items()
                  if not k.startswith("bl_")}
    group = type("G", (), group_body)()
    group.gizmos = _StandInGizmos()

    hooks_ran, complaint = True, ""
    try:
        group.setup(bpy.context)
        group.draw_prepare(bpy.context)
    except Exception as exc:                                  # noqa: BLE001
        hooks_ran, complaint = False, f"{type(exc).__name__}: {exc}"

    built = [name for name, _g in group.gizmos.made]
    placed = all(hasattr(g, "matrix_basis") for _n, g in group.gizmos.made)
    # Each handle drags a real property rather than a pair of handlers, the way
    # Blender's own gizmo template does: a handler-bound target is read
    # differently once a drag is running.
    wired = all(getattr(g, "target", (None, None))[0] is hooked
                for _n, g in group.gizmos.made)
    # ... and each one reaches its own face, not the last index of the loop
    reads = sorted(round(getattr(hooked, g.target[1]), 3)
                   for _n, g in group.gizmos.made) if wired else []

    # And the group is configured the way Blender's own arrow template is:
    # anything extra there is what drew the dragged handle twice.
    options = _gizmo.LS3D_GGT_DummyBox.bl_options
    check("the gizmo group's own hooks run without tripping over a name",
          hooks_ran and len(built) == 6
          and set(built) == {_gizmo.LS3D_GT_DummyHandle.bl_idname}
          and placed and wired
          and reads == sorted([1.0, 4.0, 2.0, 5.0, 3.0, 6.0])
          and options == {"3D", "PERSISTENT"}
          # ours is the only drawing there is, so it keeps drawing through a
          # drag rather than leaving the face bare
          and all(getattr(g, "use_draw_modal", False)
                  for _n, g in group.gizmos.made),
          complaint or f"{len(built)} handle(s) built, matrices set {placed}, "
                       f"handlers wired {wired}, offsets {reads}, "
                       f"options {sorted(options)}")

    # The drag is ours now, so its guard is worth pinning down: with nothing
    # to project through there is no honest answer for how far the pointer
    # moved along the axis, and the handle must hold still rather than leap.
    class _NoView:
        region = None
        region_data = None

    from mathutils import Matrix as _Mat
    held = _gizmo.drag_distance(_NoView(), _Mat.Identity(4), (0, 0), (40, 40))
    check("a drag with nothing to project through moves nothing",
          held is None,
          f"got {held!r} instead of None")

    # It is a dummy's gizmo, not every empty's.
    fresh_scene()
    boxed = bpy.data.objects.new("boxed", None)
    bpy.context.collection.objects.link(boxed)
    boxed.empty_display_type = "CUBE"
    boxed.ls3d_frame_type = str(C.FRAME_DUMMY)
    bpy.context.view_layer.objects.active = boxed
    boxed.select_set(True)
    on_dummy = _gizmo.LS3D_GGT_DummyBox.poll(bpy.context)
    boxed.select_set(False)
    on_deselected = _gizmo.LS3D_GGT_DummyBox.poll(bpy.context)
    boxed.select_set(True)
    bpy.ops.mesh.primitive_cube_add()
    on_mesh = _gizmo.LS3D_GGT_DummyBox.poll(bpy.context)
    bpy.context.view_layer.objects.active = None
    on_nothing = _gizmo.LS3D_GGT_DummyBox.poll(bpy.context)
    check("the box handles appear for a selected dummy and nothing else",
          on_dummy and not on_deselected and not on_mesh and not on_nothing,
          f"selected dummy={on_dummy}, deselected={on_deselected}, "
          f"mesh={on_mesh}, nothing active={on_nothing}")

    # Seven frame types store nothing beyond the header every frame has: their
    # engine classes take the base load, which stops after the transform, the
    # culling byte, the name and the user properties. So each is a plain empty,
    # and reading one is a matter of not looking for a payload nobody wrote.
    fresh_scene()
    made = {}
    for frame_type in sorted(C.PAYLOADLESS_FRAME_TYPES):
        name = C.FRAME_TYPE_NAMES[frame_type].lower().replace(" ", "_")
        obj = bpy.data.objects.new(name, None)
        bpy.context.collection.objects.link(obj)
        obj.empty_display_type = "PLAIN_AXES"
        obj.ls3d_frame_type = str(frame_type)
        obj.ls3d_user_props = f"props for {name}"
        obj.location = (float(frame_type), 0.0, 0.0)
        made[name] = frame_type

    path = os.path.join(out_dir, "bare_frames.4ds")
    wrote = {}
    size_before = None
    if export_op(path) == {"FINISHED"}:
        doc = read_file(path)
        size_before = os.path.getsize(path)
        for frame in doc.frames:
            wrote[frame.name] = (frame.frame_type, frame.user_props,
                                 round(frame.position[0], 3),
                                 # nothing else may have been written for them
                                 frame.geometry is None and frame.dummy is None
                                 and frame.sector is None and frame.target is None
                                 and frame.occluder is None and frame.joint is None)
    fresh_scene()
    import_op(path)
    back = {o.name: (int(getattr(o, "ls3d_frame_type", -1)),
                     o.type, o.empty_display_type)
            for o in bpy.context.scene.objects}
    # And writing it a second time from what came back reproduces the file.
    again = os.path.join(out_dir, "bare_frames_again.4ds")
    identical = (export_op(again) == {"FINISHED"}
                 and os.path.getsize(again) == size_before)

    expected = {name: (frame_type, f"props for {name}", float(frame_type), True)
                for name, frame_type in made.items()}
    check("frames that store only a transform round-trip as plain empties",
          len(made) == 7 and wrote == expected
          and all(back.get(name) == (frame_type, "EMPTY", "PLAIN_AXES")
                  for name, frame_type in made.items())
          and identical,
          f"{len(wrote)} frame(s) written; re-export identical={identical}; "
          f"mismatches "
          f"{ {k: v for k, v in wrote.items() if expected.get(k) != v} }")

    # An emitor is a particle emitter, and the mesh it carries is the particle
    # rather than the emitter. It arrives in the same block a visual's geometry
    # does, so it round-trips as a mesh - but it must never be an instance
    # source: the game resolves one by casting the source frame to a mesh object
    # and reading the pointer at that class's mesh offset, which on an emitor is
    # a particle setting.
    fresh_scene()
    bpy.ops.mesh.primitive_plane_add()
    emitor = bpy.context.object
    emitor.name = "smoke"
    emitor.ls3d_frame_type = str(C.FRAME_EMITOR)
    emitor.data.materials.append(bpy.data.materials.new("SMOKE.BMP"))
    twin = emitor.copy()                  # shares the mesh
    twin.name = "smoke_twin"
    bpy.context.collection.objects.link(twin)
    # ... and a plain visual on the very same mesh, written after both.
    visual = emitor.copy()
    visual.name = "visual_on_same_mesh"
    visual.ls3d_frame_type = str(C.FRAME_VISUAL)
    bpy.context.collection.objects.link(visual)

    path = os.path.join(out_dir, "emitor.4ds")
    wrote = {}
    materials = []
    if export_op(path) == {"FINISHED"}:
        doc = read_file(path)
        materials = list(doc.materials)
        for frame in doc.frames:
            wrote[frame.name] = (
                frame.frame_type,
                frame.geometry.instance_id if frame.geometry else None,
                len(frame.geometry.lods) if frame.geometry else 0,
                frame.skin is None and frame.morph is None,
            )
    fresh_scene()
    import_op(path)
    back = {o.name: int(getattr(o, "ls3d_frame_type", -1))
            for o in bpy.context.scene.objects if o.type == "MESH"}
    check("an emitor carries its particle mesh and is never an instance source",
          # both emitors write their geometry in full, neither borrows
          wrote.get("smoke") == (C.FRAME_EMITOR, 0, 1, True)
          and wrote.get("smoke_twin") == (C.FRAME_EMITOR, 0, 1, True)
          # the visual sharing the mesh owns it rather than pointing at an emitor
          and wrote.get("visual_on_same_mesh") == (C.FRAME_VISUAL, 0, 1, True)
          and back.get("smoke") == C.FRAME_EMITOR
          and back.get("visual_on_same_mesh") == C.FRAME_VISUAL
          # its material reaches the table rather than being stripped the way
          # a sector's or an occluder's is
          and len(materials) == 1,
          f"wrote {wrote}; materials {materials}; read back {back}")

    # A lit object is a mesh with baked lighting, and the model stores exactly
    # what a plain object does - the bake is handed to the frame by the mission
    # that places it. So it has to read, write and instance like one.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    lit = bpy.context.object
    lit.name = "lit"
    lit.ls3d_frame_type = str(C.FRAME_VISUAL)
    lit.visual_type = str(C.VISUAL_LITOBJECT)
    lit.data.materials.append(bpy.data.materials.new("LIT.BMP"))
    copy = lit.copy()                       # shares the mesh: an instance
    copy.name = "lit_copy"
    bpy.context.collection.objects.link(copy)

    path = os.path.join(out_dir, "litobject.4ds")
    written = {}
    if export_op(path) == {"FINISHED"}:
        for frame in read_file(path).frames:
            written[frame.name] = (frame.visual_type,
                                   bool(frame.render_flags2 & C.LF_IS_MESH_OBJECT),
                                   frame.geometry.instance_id if frame.geometry
                                   else None,
                                   len(frame.geometry.lods) if frame.geometry
                                   else 0)
    # ... and it survives coming back in.
    fresh_scene()
    import_op(path)
    reimported = {o.name: int(getattr(o, "visual_type", -1))
                  for o in bpy.context.scene.objects if o.type == "MESH"}
    check("a lit object round-trips as a plain mesh with its own type",
          written.get("lit") == (C.VISUAL_LITOBJECT, True, 0, 1)
          # the copy shares the mesh, so it is written as an instance of it
          and written.get("lit_copy") == (C.VISUAL_LITOBJECT, True, 1, 0)
          and reimported.get("lit") == C.VISUAL_LITOBJECT
          and C.VISUAL_LITOBJECT in C.GEOMETRY_VISUAL_TYPES
          and C.VISUAL_LITOBJECT not in C.SKINNED_VISUAL_TYPES
          and C.VISUAL_LITOBJECT not in C.MORPH_VISUAL_TYPES,
          f"wrote {written}; read back {reimported}")

    # The frame-type table had 15 as Landscape. The engine's own enum runs
    # ... Area 14, Shadow 15, Landscape 16, Emitor 17, and visual sub-types
    # stop at 8 - a ninth was listed that no file can name.
    check("the frame and visual type tables match the engine's own",
          C.FRAME_SHADOW == 15 and C.FRAME_LANDSCAPE == 16
          and C.FRAME_EMITOR == 17 and C.FRAME_TYPE_COUNT == 18
          and C.FRAME_TYPE_NAMES[15] == "Shadow"
          and C.FRAME_TYPE_NAMES[16] == "Landscape"
          and C.FRAME_TYPE_NAMES[17] == "Emitor"
          and C.VISUAL_TYPE_COUNT == 9
          and sorted(C.VISUAL_TYPE_NAMES) == list(range(9))
          and max(C.FRAME_TYPE_NAMES) == C.FRAME_TYPE_COUNT - 1,
          f"frames 15-17 {[C.FRAME_TYPE_NAMES[n] for n in (15, 16, 17)]}, "
          f"{len(C.VISUAL_TYPE_NAMES)} visual sub-type(s)")

    # A type past the end of either table - or frame type 0 - is one the game
    # refuses to load, and the reader says so in those words rather than
    # calling it merely unsupported.
    _count_doc = ls3d_module("4ds.codec.document")
    _count_bin = ls3d_module("common.binary")

    def _named_type(frame_type, visual_type=None):
        writer = _count_bin.BinaryWriter()
        writer.raw(b"4DS\0")
        writer.u16(C.VERSION_MAFIA)
        writer.raw(bytes(8))
        writer.u16(0)
        writer.u16(1)
        writer.u8(frame_type)
        if visual_type is not None:
            writer.u8(visual_type)
        writer.raw(bytes(64))
        try:
            _count_doc.read_document(writer.getvalue())
        except _count_doc.UnsupportedFeatureError as exc:
            return str(exc)
        return ""

    refusals = {"frame 18": _named_type(C.FRAME_TYPE_COUNT),
                "frame 0": _named_type(0),
                "visual 9": _named_type(C.FRAME_VISUAL, C.VISUAL_TYPE_COUNT)}
    check("a frame or visual type that does not exist is named as such",
          "frame type 18 does not exist" in refusals["frame 18"]
          and "frame type 0 does not exist" in refusals["frame 0"]
          and "visual type 9 does not exist" in refusals["visual 9"]
          and all("refuses to load" in said for said in refusals.values()),
          f"{refusals}")

    # The environment bits are packed numbers, so no switch is named after one
    # of their bits; the diffuse mipmap bit says what it really does.
    material_rna = bpy.types.Material.bl_rna.properties
    check("no material switch stands for one bit of a packed environment number",
          not any(name in material_rna for name in (
              "ls3d_flag_misc_unlit", "ls3d_flag_env_overlay",
              "ls3d_flag_env_multiply", "ls3d_flag_env_additive",
              "ls3d_flag_env_projy", "ls3d_flag_env_detaily",
              "ls3d_flag_env_detailz"))
          and not any(hasattr(C, name) for name in (
              "MTL_MISC_UNLIT", "MTL_ENV_OVERLAY", "MTL_ENV_MULTIPLY",
              "MTL_ENV_ADDITIVE", "MTL_ENV_PROJY", "MTL_ENV_DETAILY",
              "MTL_ENV_DETAILZ"))
          and material_rna["ls3d_flag_diffuse_mipmap"].name == "Diffuse MipMap",
          f"mipmap switch is called {material_rna['ls3d_flag_diffuse_mipmap'].name}")

    # Sound (4) and Area (14) frames are three bytes in a file - a type and a
    # parent - because the game reads nothing more of one. The import skips
    # them and says so, what hung from one hangs from the frame above it with
    # nothing moved, a target link to one is dropped with a word, and no export
    # ever writes one.
    import contextlib as _skip_context
    import io as _skip_io
    _codec_doc = ls3d_module("4ds.codec.document")
    _codec_types = ls3d_module("4ds.codec.types")
    _binary = ls3d_module("common.binary")

    def _skip_file(extra_after_sound=b""):
        writer = _binary.BinaryWriter()
        writer.raw(b"4DS\0")
        writer.u16(C.VERSION_MAFIA)
        writer.raw(bytes(8))
        writer.u16(0)                                        # no materials
        writer.u16(5)
        _codec_doc.write_frame(writer, _codec_types.Frame(
            frame_type=C.FRAME_DUMMY, position=(1.0, 3.0, 2.0), name="root",
            cull_flags=9, dummy=_codec_types.Dummy((-1.0,) * 3, (1.0,) * 3)), 1)
        writer.raw(struct.pack("<BH", C.FRAME_SOUND, 1))     # frame 2, under 1
        writer.raw(extra_after_sound)
        writer.raw(struct.pack("<BH", C.FRAME_AREA, 2))      # frame 3, under 2
        _codec_doc.write_frame(writer, _codec_types.Frame(
            frame_type=C.FRAME_DUMMY, parent_id=3, position=(0.0, 5.0, 0.0),
            name="under_area", cull_flags=9,
            dummy=_codec_types.Dummy((-1.0,) * 3, (1.0,) * 3)), 4)
        _codec_doc.write_frame(writer, _codec_types.Frame(
            frame_type=C.FRAME_TARGET, name="aimer", cull_flags=9,
            target=_codec_types.Target(flags=0, links=[4, 2])), 5)
        writer.u8(0)
        return writer.getvalue()

    skip_path = os.path.join(out_dir, "sound_area.4ds")
    with open(skip_path, "wb") as handle:
        handle.write(_skip_file())
    parsed = read_file(skip_path)
    fresh_scene()
    heard = _skip_io.StringIO()
    with _skip_context.redirect_stdout(heard):
        skip_result = import_op(skip_path)
    said = heard.getvalue()
    bpy.context.view_layer.update()
    names = sorted(o.name for o in bpy.context.scene.objects)
    under = bpy.data.objects.get("under_area")
    aimer = bpy.data.objects.get("aimer")
    linked = [e.name for e in aimer.ls3d_target_objects] if aimer else None
    world = tuple(round(v, 4) for v in under.matrix_world.translation) \
        if under else None

    again_path = os.path.join(out_dir, "sound_area_again.4ds")
    export_op(again_path)
    again_types = [f.frame_type for f in read_file(again_path).frames]

    refused = None
    try:
        _codec_doc.write_frame(_binary.BinaryWriter(),
                               _codec_types.Frame(frame_type=C.FRAME_SOUND), 1)
    except _codec_doc.UnsupportedFeatureError as exc:
        refused = str(exc)

    # A file that stored the usual header after one is out of step from there
    # on, in game as here - and the error says which frame is the likely cause.
    padded_path = os.path.join(out_dir, "sound_padded.4ds")
    with open(padded_path, "wb") as handle:
        handle.write(_skip_file(struct.pack("<3f3f4fB", 0, 0, 0, 1, 1, 1,
                                            1, 0, 0, 0, 9) + b"\0\0"))
    padded_error = None
    try:
        read_file(padded_path)
    except Exception as exc:                            # noqa: BLE001
        padded_error = str(exc)

    check("Sound and Area frames are skipped on import, and never written",
          [f.frame_type for f in parsed.frames] == [6, 4, 14, 6, 7]
          and parsed.frames[1].parent_id == 1 and parsed.frames[2].parent_id == 2
          and parsed.frames[3].name == "under_area"
          and skip_result == {"FINISHED"}
          and names == ["aimer", "root", "under_area"]
          and under.parent is bpy.data.objects["root"]
          and world == (1.0, 2.0, 8.0)
          and linked == ["under_area"]
          and "Skipped 2 Sound/Area frame(s): frame 2 (Sound), 3 (Area)" in said
          and "Sound - skipped" in said and "Area - skipped" in said
          and "1 frame(s) that hung from one now hang from the frame above" in said
          and "links to frame 2, a Sound or Area frame the import skipped" in said
          and sorted(again_types) == [6, 6, 7]
          and refused is not None and "never written" in refused
          and padded_error is not None
          and "Frame 2 is a Sound or Area frame" in padded_error,
          f"read {[f.frame_type for f in parsed.frames]}; import {skip_result} "
          f"made {names}; under_area parent "
          f"{under.parent.name if under and under.parent else None} at {world}; "
          f"aimer links {linked}; said skipped "
          f"{'Skipped 2 Sound/Area' in said}; exported {again_types}; writer "
          f"refused {refused is not None}; padded file error "
          f"{(padded_error or '')[-160:]!r}")

    # A projector has no geometry at all: four bytes and a transform. What it
    # covers is a fixed unit shape the frame carries, so nothing about the
    # volume is stored and everything about it has to be rebuilt.
    projector_file = os.path.join(_ADDON_PARENT, "test_projector.4ds")
    if os.path.isfile(projector_file):
        fresh_scene()
        import_op(projector_file)
        source = read_file(projector_file).frames[0].projector
        obj = next((o for o in bpy.context.scene.objects
                    if int(getattr(o, "visual_type", -1)) == C.VISUAL_PROJECTOR),
                   None)
        named = obj.ls3d_projector_material.name if obj is not None and             obj.ls3d_projector_material else None
        check("a projector imports as an empty carrying its material",
              obj is not None and obj.type == "EMPTY"
              and obj.empty_display_type == "ARROWS"
              and int(obj.ls3d_frame_type) == C.FRAME_VISUAL
              and obj.ls3d_projector_orthogonal == source.orthogonal
              and obj.ls3d_projector_mode == source.mode
              and named is not None,
              f"file said {source}, object has "
              f"orthogonal={obj and obj.ls3d_projector_orthogonal}, "
              f"mode={obj and obj.ls3d_projector_mode}, material={named}")

        path = os.path.join(out_dir, "projector.4ds")
        written = None
        if export_op(path) == {"FINISHED"}:
            written = read_file(path)
        frame = written.frames[0] if written else None
        check("the projector's four bytes survive the trip",
              frame is not None and frame.projector == source
              and frame.geometry is None
              # its material has to reach the table even though no mesh uses it
              and 0 < source.material_id <= len(written.materials),
              f"wrote {frame and frame.projector} with "
              f"{written and len(written.materials)} material(s)")

        # A projector is not one of the engine's mesh objects. That bit tells
        # the game to read the frame's own fields as a mesh pointer and follow
        # it, so it has to be off however the authoring tool left it - the file
        # here arrives with it set.
        check("the renderable-object bit is cleared on a projector",
              frame is not None
              and not frame.render_flags2 & C.LF_IS_MESH_OBJECT
              and read_file(projector_file).frames[0].render_flags2
                  & C.LF_IS_MESH_OBJECT,
              f"file 0x{read_file(projector_file).frames[0].render_flags2:02X} "
              f"-> written 0x{frame.render_flags2:02X}" if frame else "no frame")

    # The mode byte carries the fade and the blend together, and the game reads
    # it by comparing whole values rather than by masking bits. Every one of the
    # six it knows has to survive the two dropdowns.
    fresh_scene()
    bpy.ops.ls3d.add_projector()
    projector = bpy.context.object
    projector.ls3d_projector_material = bpy.data.materials.new("PROJ.BMP")
    projector.ls3d_projector_material.ls3d_diffuse_tex = bpy.data.images.new(
        "PROJ.BMP", 4, 4)
    projector.ls3d_projector_material.ls3d_flag_diffuse_enable = True
    round_tripped = {}
    for byte, (falloff, blended) in sorted(C.PROJECTOR_MODES.items()):
        projector.ls3d_projector_falloff = str(falloff)
        projector.ls3d_projector_blend = str(int(blended))
        round_tripped[byte] = projector.ls3d_projector_mode

    # A byte the game does not know behaves as mode 1 there - constant falloff,
    # alpha blended - so that is what the panel has to show, whatever the spare
    # bits look like.
    projector.ls3d_projector_mode = 12
    shown = (projector.ls3d_projector_falloff, projector.ls3d_projector_blend)
    kept = None
    if export_op(os.path.join(out_dir, "projector_mode.4ds")) == {"FINISHED"}:
        kept = read_file(os.path.join(out_dir,
                                      "projector_mode.4ds")).frames[0].projector
    check("every mode the game knows round-trips, and one it does not is kept",
          round_tripped == {b: b for b in C.PROJECTOR_MODES}
          and shown == (str(C.PROJECTOR_FALLOFF_CONSTANT), "1")
          and kept is not None and kept.mode == 12,
          f"wrote {round_tripped}; mode 12 shows as {shown}, written "
          f"{kept and kept.mode}")

    # Nothing but the diffuse texture reaches the surface, and without one the
    # game skips the projector - so the export says so rather than staying quiet.
    fresh_scene()
    bpy.ops.ls3d.add_projector()
    bpy.context.object.ls3d_projector_material = bpy.data.materials.new(
        "BARE.BMP")
    said = []
    original_warn = report_module.Report.warn

    def _hear(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _hear
    try:
        textureless = export_op(os.path.join(out_dir, "projector_notex.4ds"))
    finally:
        report_module.Report.warn = original_warn
    # The game's image loader is narrower than anything in Blender suggests,
    # and a texture it cannot read spoils the surface without spoiling the
    # model file - so the check reports rather than refuses. Each case below is
    # a real header written to disk, because the point of the check is that it
    # reads the file the game would read.
    _validation = ls3d_module("4ds.validation")

    def _bmp(name, width, height, depth=24, header=40, compression=0):
        """A BMP with exactly the header fields this case is about."""
        import struct as _s
        row = ((width * depth // 8) + 3) // 4 * 4
        pixels = b"\x00" * (row * height)
        palette = b"\x00" * (4 * (1 << depth)) if depth <= 8 else b""
        offset = 14 + header + len(palette)
        info = _s.pack("<IiihhIIiiII", header, width, height, 1, depth,
                       compression, len(pixels), 0, 0, 0, 0)
        info += b"\x00" * (header - 40)          # V4/V5 tails are just padding
        blob = (b"BM" + _s.pack("<IHHI", offset + len(pixels), 0, 0, offset)
                + info + palette + pixels)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as handle:
            handle.write(blob)
        return path

    def _kinds(path):
        return [problem for problem, _fix
                in _validation.texture_problems(path)]

    plain = _kinds(_bmp("tex_plain.bmp", 64, 64))
    v5 = _kinds(_bmp("tex_v5.bmp", 64, 64, header=124))
    v4 = _kinds(_bmp("tex_v4.bmp", 64, 64, header=108))
    squashed = _kinds(_bmp("tex_rle.bmp", 64, 64, depth=8, compression=1))
    shallow = _kinds(_bmp("tex_4bit.bmp", 64, 64, depth=4))
    near_miss = _kinds(_bmp("tex_126.bmp", 126, 128))
    stretched = _kinds(_bmp("tex_640.bmp", 640, 640))
    squat = _kinds(_bmp("tex_256x90.bmp", 256, 90))
    paletted = _kinds(_bmp("tex_8bit.bmp", 32, 32, depth=8))

    wrong_kind = os.path.join(out_dir, "tex_not_an_image.png")
    with open(wrong_kind, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
    unreadable = _kinds(wrong_kind)

    def _says(problems, needle):
        return any(needle in problem for problem in problems)

    check("a texture the game's loader cannot read is reported",
          not plain and not paletted
          # the exact bug that cost a day: a V5 header slides every pixel
          and len(v5) == 1 and _says(v5, "124-byte info header")
          and _says(v5, "84 byte(s) early")
          and len(v4) == 1 and _says(v4, "108-byte info header")
          and _says(squashed, "compression 1")
          and _says(shallow, "4 bits per pixel")
          and _says(unreadable, ".png")
          # each side is checked on its own, and the message says which
          # one is off and what size the game rounds it up to
          and _says(near_miss, "is 126x128: width 126 is not a power of two, "
                               "the game rounds it up to 128")
          and _says(squat, "is 256x90: height 90 is not a power of two, the "
                           "game rounds it up to 128")
          and _says(stretched, "is 640x640: width 640 and height 640 are not a "
                               "power of two, the game rounds them up to 1024 "
                               "and 1024")
          and len(near_miss) == len(squat) == len(stretched) == 1,
          f"plain {plain}; v5 {v5}; v4 {v4}; compressed {squashed}; "
          f"4-bit {shallow}; png {unreadable}; 126x128 {near_miss}; "
          f"256x90 {squat}; 640x640 {stretched}")

    # The same texture checks run on import and from the Check 4DS Scene
    # button, not only when a file is written.
    import contextlib as _size_context
    import io as _size_io
    size_dir = os.path.join(out_dir, "size_textures")
    os.makedirs(size_dir, exist_ok=True)
    wide_texture = os.path.join(size_dir, "WIDE640.BMP")
    shutil.copyfile(os.path.join(out_dir, "tex_640.bmp"), wide_texture)
    real_preferences = io_mafia_toolkit.get_preferences

    class _SizePrefs:
        textures_path = size_dir

        def __getattr__(self, name):
            return getattr(real_preferences(), name)

    def _heard(call):
        buffer = _size_io.StringIO()
        with _size_context.redirect_stdout(buffer):
            outcome = call()
        return outcome, buffer.getvalue()

    size_model = os.path.join(out_dir, "size_textures.4ds")
    io_mafia_toolkit.get_preferences = lambda: _SizePrefs()
    try:
        fresh_scene()
        bpy.ops.mesh.primitive_plane_add()
        sized = bpy.data.materials.new("sized")
        sized.ls3d_material_flags = _to_signed(C.MTL_DIFFUSE_ENABLE)
        sized.ls3d_diffuse_tex = bpy.data.images.load(wide_texture)
        bpy.context.object.data.materials.append(sized)
        checked, check_said = _heard(lambda: bpy.ops.ls3d.check_scene())
        exported, export_said = _heard(lambda: export_op(size_model))
        fresh_scene()
        imported, import_said = _heard(lambda: import_op(size_model))
    finally:
        io_mafia_toolkit.get_preferences = real_preferences
    rounded = ("640x640: width 640 and height 640 are not a power of two, the "
               "game rounds them up to 1024 and 1024")
    check("texture sizes are checked on export, on import and by Check Scene",
          checked == {"FINISHED"} and rounded in check_said
          and exported == {"FINISHED"} and rounded in export_said
          and imported == {"FINISHED"}
          and f"Texture 'WIDE640.BMP' is {rounded}" in import_said
          and import_said.count(rounded) == 1,
          f"check {checked} said it {rounded in check_said}; export "
          f"{exported} said it {rounded in export_said}; import {imported} "
          f"said it {import_said.count(rounded)} time(s)")

    # ── Repairing texture files ───────────────────────────────────────────────
    # Every kind of file the game misreads is rewritten into the one form its
    # loader reads, and what it then reads is the picture that was authored:
    # same colors, same alpha, and for a paletted image the same palette and
    # the same index in every pixel - an 8-bit texture's color key is its
    # palette entry 0, so it has to stay 8-bit.
    import struct as _px
    _repair = ls3d_module("4ds.texture_repair")
    _texfix = ls3d_module("4ds.ops_texfix")
    fix_dir = os.path.join(out_dir, "texture_fix")
    shutil.rmtree(fix_dir, ignore_errors=True)
    os.makedirs(fix_dir)
    W, H = 8, 4
    palette = b"".join(bytes(((i * 7) % 256, 255 - i * 16, i * 16, 0))
                       for i in range(16))
    index_of = [[(x + 2 * y) % 16 for x in range(W)] for y in range(H)]
    color_of = [[(x * 30, y * 60, (x * y * 10) % 256, 255 - x * 20)
                 for x in range(W)] for y in range(H)]

    def _bmp_blob(depth, rows, header=40, compression=0, height=H,
                  masks=b"", pal=b"", gap=b"", colors=0):
        """A BMP laid out however a case wants; *rows* already packed."""
        body = b"".join(rows)
        if header == 12:
            info = _px.pack("<IHHHH", 12, W, height, 1, depth)
        else:
            info = _px.pack("<IiiHHIIiiII", 40, W, height, 1, depth,
                            compression, len(body), 2835, 2835, colors, 0)
            if header > 40:
                info = (_px.pack("<I", header) + info[4:] + masks
                        + b"\x00" * (header - 40 - len(masks)))
            else:
                info += masks
        start = 14 + len(info) + len(pal) + len(gap)
        return (b"BM" + _px.pack("<IHHI", start + len(body), 0, 0, start)
                + info + pal + gap + body)

    def _pad(row):
        return row + b"\x00" * ((4 - len(row) % 4) % 4)

    def _game_reads(blob):
        """What the game's loader makes of a BMP: 14 + 40 bytes, the palette
        straight after, rows bottom first straight after that."""
        (size, width, height, _p, depth, compression, _i, _x, _y, used,
         _c) = _px.unpack_from("<IiiHHIIiiII", blob, 14)
        if size != 40 or compression or height <= 0:
            return None
        entries = used or ((1 << depth) if depth <= 8 else 0)
        at = 54 + 4 * entries
        pal = blob[54:at]
        stride = ((width * depth + 31) // 32) * 4
        grid = []
        for y in range(height):
            row = blob[at + y * stride:at + (y + 1) * stride]
            line = []
            for x in range(width):
                if depth == 8:
                    i = row[x]
                    line.append((pal[i * 4 + 2], pal[i * 4 + 1], pal[i * 4], i))
                elif depth == 24:
                    b, g, r = row[3 * x:3 * x + 3]
                    line.append((r, g, b, 255))
                elif depth == 32:
                    b, g, r, a = row[4 * x:4 * x + 4]
                    line.append((r, g, b, a))
            grid.append(line)
        return depth, grid

    paletted_expect = [[(palette[i * 4 + 2], palette[i * 4 + 1], palette[i * 4],
                         i) for i in line] for line in index_of]
    rgb_rows = [_pad(b"".join(bytes((c[2], c[1], c[0])) for c in line))
                for line in color_of]
    rgba_rows = [b"".join(bytes((c[2], c[1], c[0], c[3])) for c in line)
                 for line in color_of]
    rgb_expect = [[(c[0], c[1], c[2], 255) for c in line] for line in color_of]
    rgba_expect = [[tuple(c) for c in line] for line in color_of]

    def _rle8(rows):
        out = bytearray()
        for y, line in enumerate(rows):
            rest = line
            if y == 0:                          # one literal run, then runs
                out += bytes((0, 3)) + bytes(line[:3]) + b"\x00"
                rest = line[3:]
            for value in rest:
                out += bytes((1, value))
            out += b"\x00\x00"
        return [bytes(out + b"\x00\x01")]

    def _rle4(rows):
        out = bytearray()
        for line in rows:
            for value in line:
                out += bytes((1, value << 4))
            out += b"\x00\x00"
        return [bytes(out + b"\x00\x01")]

    def _nibbles(line):
        return _pad(bytes((line[i] << 4) | line[i + 1]
                          for i in range(0, len(line), 2)))

    # 5-6-5 values; each channel is expanded back to a byte by its own scale.
    wide = [[((x * 4) % 32, (y * 12) % 64, (x + y) % 32) for x in range(W)]
            for y in range(H)]
    rows_565 = [_pad(b"".join(_px.pack("<H", (r << 11) | (g << 5) | b)
                              for r, g, b in line)) for line in wide]
    expect_565 = [[(r * 255 // 31, g * 255 // 63, b * 255 // 31, 255)
                   for r, g, b in line] for line in wide]

    cases = {
        "v5_24bit.bmp": (_bmp_blob(24, rgb_rows, header=124), 24, rgb_expect),
        "v4_32bit_fields.bmp": (_bmp_blob(
            32, rgba_rows, header=108, compression=3,
            masks=_px.pack("<IIII", 0xFF0000, 0xFF00, 0xFF, 0xFF000000)),
            32, rgba_expect),
        "fields_565.bmp": (_bmp_blob(
            16, rows_565, compression=3,
            masks=_px.pack("<III", 0xF800, 0x07E0, 0x001F)), 24, expect_565),
        "rle8.bmp": (_bmp_blob(8, _rle8(index_of), compression=1,
                               pal=palette, colors=16), 8, paletted_expect),
        "4bit.bmp": (_bmp_blob(4, [_nibbles(l) for l in index_of], pal=palette,
                               colors=16), 8, paletted_expect),
        "rle4.bmp": (_bmp_blob(4, _rle4(index_of), compression=2, pal=palette,
                               colors=16), 8, paletted_expect),
        "top_down.bmp": (_bmp_blob(24, list(reversed(rgb_rows)), height=-H),
                         24, rgb_expect),
        "gap.bmp": (_bmp_blob(24, rgb_rows, gap=b"\x00\x00"), 24, rgb_expect),
        "os2.bmp": (_bmp_blob(4, [_nibbles(l) for l in index_of], header=12,
                              pal=b"".join(palette[i * 4:i * 4 + 3]
                                           for i in range(16))),
                    8, paletted_expect),
    }
    repaired_ok = {}
    for name, (blob, want_depth, want) in cases.items():
        path = os.path.join(fix_dir, name)
        with open(path, "wb") as handle:
            handle.write(blob)
        planned = _repair.plan_repair(path) is not None
        try:
            fixed_blob = _repair.repair(blob, ".bmp")
        except _repair.RepairError as exc:
            repaired_ok[name] = f"refused: {exc}"
            continue
        out_path = os.path.join(fix_dir, "out_" + name)
        with open(out_path, "wb") as handle:
            handle.write(fixed_blob)
        clean = (_repair.plan_repair(out_path) is None
                 and not _validation.texture_problems(out_path))
        read = _game_reads(fixed_blob)
        good = (planned and clean and read is not None
                and read[0] == want_depth and read[1] == want)
        repaired_ok[name] = good or (planned, clean, read and read[0])

    tga_rows = [b"".join(bytes((c[2], c[1], c[0])) for c in line[:4])
                + bytes((255, 0, 0)) * 4 for line in color_of]
    raw_tga = (bytes((0, 0, 2)) + b"\x00" * 9 + _px.pack("<HHBB", W, H, 24, 0)
               + b"".join(tga_rows))
    packed = bytearray(raw_tga[:18])
    packed[2] = 10
    for line in tga_rows:
        packed += bytes((3,)) + line[:12] + bytes((0x80 | 3,)) + line[12:15]
    turned = bytearray(raw_tga[:18])
    turned[17] = 0x10
    for line in tga_rows:
        turned += b"".join(line[i:i + 3] for i in range(21, -1, -3))
    tga_results = {}
    for name, blob in (("packed.tga", bytes(packed)),
                       ("turned.tga", bytes(turned))):
        path = os.path.join(fix_dir, name)
        with open(path, "wb") as handle:
            handle.write(blob)
        tga_results[name] = (_repair.plan_repair(path) is not None
                             and _repair.repair(blob, ".tga") == raw_tga)
    check("each kind of misread texture file is rewritten into what the game reads",
          all(value is True for value in repaired_ok.values())
          and all(tga_results.values()),
          f"BMP {repaired_ok}; TGA {tga_results}")

    # The header rewrite - the case that cost a day - changes no pixel: the
    # same image Blender shows before and after, and the same bytes.
    v5_blob = cases["v5_24bit.bmp"][0]
    shown = bpy.data.images.load(os.path.join(fix_dir, "v5_24bit.bmp"))
    before_pixels = list(shown.pixels)
    bpy.data.images.remove(shown)
    shown = bpy.data.images.load(os.path.join(fix_dir, "out_v5_24bit.bmp"))
    after_pixels = list(shown.pixels)
    bpy.data.images.remove(shown)
    with open(os.path.join(fix_dir, "out_v5_24bit.bmp"), "rb") as handle:
        out_blob = handle.read()
    check("a BMP header rewrite keeps every pixel",
          before_pixels == after_pixels
          and out_blob[54:] == v5_blob[14 + 124:]
          and _px.unpack_from("<I", out_blob, 14)[0] == 40,
          f"Blender shows the same {before_pixels == after_pixels}; pixel "
          f"bytes identical {out_blob[54:] == v5_blob[14 + 124:]}")

    # The buttons: one under a texture slot fixes that file, one under the
    # animation fixes every frame in the range the Frames field sets - and not
    # the frame past it. Each original goes into texture_backups first, once.
    class _PanelRecorder:
        def __init__(self):
            self.labels = []
            self.ops = []

        def label(self, text="", **_k):
            self.labels.append(text)

        def operator(self, idname, text="", **_k):
            made = type("Op", (), {"idname": idname, "text": text})()
            self.ops.append(made)
            return made

        def __getattr__(self, _name):
            return lambda *a, **k: self

    fresh_scene()
    anim_dir = os.path.join(fix_dir, "anim")
    os.makedirs(anim_dir)
    for number in range(4):
        with open(os.path.join(anim_dir, f"FIRE{number:02d}.BMP"), "wb") as handle:
            handle.write(v5_blob)
    single = os.path.join(anim_dir, "WALL.BMP")
    with open(single, "wb") as handle:
        handle.write(v5_blob)

    _ui = ls3d_module("4ds.ui")
    walled = bpy.data.materials.new("walled")
    walled.ls3d_material_flags = _to_signed(C.MTL_DIFFUSE_ENABLE)
    walled.ls3d_diffuse_tex = bpy.data.images.load(single)
    wall_panel = _PanelRecorder()
    _ui._draw_material_diffuse(wall_panel, walled)
    offered = [op.idname for op in wall_panel.ops]
    wall_fixed = bpy.ops.ls3d.texture_fix(material="walled", slot="DIFFUSE")
    backups = os.path.join(anim_dir, _texfix.BACKUP_FOLDER)
    backup_path = os.path.join(backups, "WALL.BMP")
    backed_up = False
    if os.path.isfile(backup_path):
        with open(backup_path, "rb") as handle:
            backed_up = handle.read() == v5_blob
    after_panel = _PanelRecorder()
    _ui._draw_material_diffuse(after_panel, walled)
    again = bpy.ops.ls3d.texture_fix(material="walled", slot="DIFFUSE")
    check("Fix Texture File rewrites the slot's file, after backing it up",
          "ls3d.texture_fix" in offered
          and any("124-byte BMP header" in t for t in wall_panel.labels)
          and wall_fixed == {"FINISHED"}
          and _texfix.plan_for_image(walled.ls3d_diffuse_tex) is None
          and backed_up
          and "ls3d.texture_fix" not in [op.idname for op in after_panel.ops]
          and again == {"CANCELLED"}
          and sorted(os.listdir(backups)) == ["WALL.BMP"],
          f"offered {offered}, said {wall_panel.labels[-2:]}; fixed "
          f"{wall_fixed}, backup {backed_up}; again {again}")

    fiery = bpy.data.materials.new("fiery")
    fiery.ls3d_material_flags = _to_signed(C.MTL_DIFFUSE_ENABLE
                                           | C.MTL_DIFFUSE_ANIMATED)
    fiery.ls3d_diffuse_tex = bpy.data.images.load(
        os.path.join(anim_dir, "FIRE00.BMP"))
    fiery.ls3d_anim_frames = 3
    fiery.ls3d_anim_period = 50
    anim_panel = _PanelRecorder()
    _ui._draw_material_colors(anim_panel, fiery)
    anim_ops = [(op.idname, op.text) for op in anim_panel.ops]
    in_range = len(_texfix.animation_plans(fiery, fresh=True))
    frames_fixed = bpy.ops.ls3d.texture_anim_fix(material="fiery")
    states = [_repair.plan_repair(os.path.join(anim_dir, f"FIRE{n:02d}.BMP"))
              is None for n in range(4)]
    check("Fix Frame Files rewrites every frame in the Frames range, no more",
          ("ls3d.texture_anim_fix", "Fix Frame Files (3)") in anim_ops
          and in_range == 3 and frames_fixed == {"FINISHED"}
          and states == [True, True, True, False]
          and len(os.listdir(backups)) == 4,
          f"panel offered {anim_ops}; {in_range} in range; {frames_fixed}; "
          f"fixed per frame {states}")

    # Only BMP and TGA go in a slot: the picker lists no others, anything else
    # assigned is taken straight back out, and an imported model naming one
    # gets no texture there and says so.
    fresh_scene()
    refusing = bpy.data.materials.new("refusing")
    png_image = bpy.data.images.new("WOOD.PNG", 4, 4)
    bmp_image = bpy.data.images.new("WOOD.BMP", 4, 4)
    bare_image = bpy.data.images.new("Untitled", 4, 4)
    offered_png = _texfix.slot_poll(refusing, png_image)
    offered_bmp = _texfix.slot_poll(refusing, bmp_image)
    refusing.ls3d_diffuse_tex = png_image
    kept_png = refusing.ls3d_diffuse_tex
    refusing.ls3d_env_tex = bare_image
    kept_bare = refusing.ls3d_env_tex
    refusing.ls3d_alpha_tex = bmp_image
    kept_bmp = refusing.ls3d_alpha_tex

    bpy.ops.mesh.primitive_plane_add()
    named = bpy.data.materials.new("named")
    named.ls3d_material_flags = _to_signed(C.MTL_DIFFUSE_ENABLE)
    named.ls3d_diffuse_tex = bpy.data.images.new("PLANK.BMP", 4, 4)
    bpy.context.object.data.materials.append(named)
    plank_path = os.path.join(fix_dir, "plank.4ds")
    export_op(plank_path)
    with open(plank_path, "rb") as handle:
        plank = handle.read()
    with open(plank_path, "wb") as handle:
        handle.write(plank.replace(b"PLANK.BMP", b"PLANK.PNG"))
    fresh_scene()
    heard = _size_io.StringIO()
    with _size_context.redirect_stdout(heard):
        png_import = import_op(plank_path)
    imported_mat = next((m for m in bpy.data.materials
                         if m.name.startswith("4ds_material")), None)
    colorkey_text = bpy.types.Material.bl_rna.properties[
        "ls3d_flag_alpha_colorkey"].description
    check("only BMP and TGA textures are taken, in a slot or from a model",
          not offered_png and offered_bmp
          and kept_png is None and kept_bare is None and kept_bmp == bmp_image
          and png_import == {"FINISHED"} and imported_mat is not None
          and imported_mat.ls3d_diffuse_tex is None
          and "Texture 'PLANK.PNG' is a PNG file. The game reads only BMP and "
              "TGA textures, so it was not assigned." in heard.getvalue()
          and "first entry in an 8-bit texture's indexed color table"
          in colorkey_text,
          f"picker offers png {offered_png}, bmp {offered_bmp}; kept png "
          f"{kept_png}, no-extension {kept_bare}, bmp {kept_bmp}; import "
          f"{png_import} left {imported_mat and imported_mat.ls3d_diffuse_tex}")

    # The frame counter writes two digits a fixed distance back from the end of
    # the name, so an animated texture has to be named for it.
    named = {name: _validation.animated_name_problem(name, frames)
             for name, frames in (("2VLNY00.BMP", 20), ("WATER.BMP", 20),
                                  ("A.BMP", 20), ("2VLNY00.BMP", 1))}
    check("an animated texture whose name cannot be numbered is reported",
          named["2VLNY00.BMP"] is None
          and named["WATER.BMP"] is not None
          and named["A.BMP"] is not None,
          f"{ {k: (v[0][:40] if v else None) for k, v in named.items()} }")

    # And the whole thing is measured against the game's own textures: if it
    # complains about those, it is wrong about what the loader accepts.
    shipped_dir = os.path.join(os.path.dirname(models_dir), "maps")
    if os.path.isdir(shipped_dir):
        import glob as _glob
        serious = []
        looked = 0
        for texture in _glob.glob(os.path.join(shipped_dir, "*.bmp"))[:1500]:
            looked += 1
            for problem, _fix in _validation.texture_problems(texture):
                # Size is a quality note; the rest would mean it does not load.
                if "power of two" not in problem:
                    serious.append((os.path.basename(texture), problem[:60]))
        check("the texture check clears the textures the game itself ships",
              looked > 0 and not serious,
              f"{looked} shipped texture(s) checked, {len(serious)} flagged"
              + (f": {serious[:3]}" if serious else ""))

    check("a projector whose material has no texture is reported",
          textureless == {"FINISHED"}
          and any("no diffuse texture" in message for message in said),
          f"result={textureless}, said {said or 'nothing'}")

    # The volume is rebuilt from the transform, so the outline has to follow the
    # object rather than anything remembered from the file.
    fresh_scene()
    _viewport = ls3d_module("4ds.viewport")
    bpy.ops.ls3d.add_projector()
    projector = bpy.context.object
    projector.ls3d_projector_orthogonal = False
    projector.location = (0.0, 0.0, 0.0)
    projector.scale = (2.0, 3.0, 2.0)
    bpy.context.view_layer.update()
    pyramid = _viewport.projector_lines(projector)
    apexes = sum(1 for point in pyramid if point.length < 1e-6)
    reach = max(point.y for point in pyramid)
    width = max(abs(point.x) for point in pyramid)
    projector.ls3d_projector_orthogonal = True
    box = _viewport.projector_lines(projector)
    near_face = sum(1 for point in box if abs(point.y) < 1e-6)
    check("the projected volume is drawn from the transform alone",
          len(pyramid) == 16 and apexes == 4
          and abs(reach - 3.0) < 1e-6 and abs(width - 2.0) < 1e-6
          and len(box) == 24 and near_face == 12,
          f"pyramid {len(pyramid)} points, {apexes} at the origin, reach "
          f"{reach}, "
          f"half-width {width}; box {len(box)} points, {near_face} at the near "
          f"face")

    # The viewport sync runs on every frame and visual type edit, so anything it
    # writes is written again and again. The display type has to be in it - it
    # is what says the empty is a projector - but the size is the user's, and
    # putting it there shrank an empty the moment it was made a projector and
    # undid every resize afterwards.
    fresh_scene()
    plain = bpy.data.objects.new("plain", None)
    bpy.context.collection.objects.link(plain)
    plain.empty_display_type = "ARROWS"
    plain.empty_display_size = 3.0
    plain.ls3d_frame_type = str(C.FRAME_VISUAL)
    plain.visual_type = str(C.VISUAL_PROJECTOR)
    converted = plain.empty_display_size
    plain.empty_display_size = 7.0
    _viewport = ls3d_module("4ds.viewport")
    _viewport.update_viewport_display(plain)
    plain.cull_flags = plain.cull_flags        # any edit re-runs the sync
    plain.visual_type = str(C.VISUAL_PROJECTOR)
    resized = plain.empty_display_size
    bpy.ops.ls3d.add_projector()
    made = bpy.context.object.empty_display_size
    check("making an empty a projector does not resize it",
          converted == 3.0 and resized == 7.0
          and made == _viewport.PROJECTOR_MARKER_SIZE
          and plain.empty_display_type == "ARROWS",
          f"3.0 stayed {converted}, 7.0 stayed {resized}, a new one is {made}")

    # The lens flare had the same clobbering: every sync put the sphere back to
    # 0.05, so a flare could not be made bigger to find it in a scene.
    fresh_scene()
    sphere = bpy.data.objects.new("sphere", None)
    bpy.context.collection.objects.link(sphere)
    sphere.empty_display_type = "SPHERE"
    sphere.empty_display_size = 2.0
    sphere.ls3d_frame_type = str(C.FRAME_VISUAL)
    sphere.visual_type = str(C.VISUAL_LENSFLARE)
    flare_converted = sphere.empty_display_size
    sphere.empty_display_size = 4.0
    _viewport.update_viewport_display(sphere)
    sphere.visual_type = str(C.VISUAL_LENSFLARE)
    flare_resized = sphere.empty_display_size
    bpy.ops.ls3d.add_lens_flare()
    flare_made = bpy.context.object.empty_display_size
    check("making an empty a lens flare does not resize it",
          flare_converted == 2.0 and flare_resized == 4.0
          and abs(flare_made - _viewport.LENS_FLARE_MARKER_SIZE) < 1e-6
          and sphere.empty_display_type == "SPHERE",
          f"2.0 stayed {flare_converted}, 4.0 stayed {flare_resized}, a new "
          f"one is {flare_made}")

    # The outline is the object - there is no mesh under it - so it has to read
    # as one, following the selection the way any other object's wire does.
    fresh_scene()
    active, selected = _viewport._selection_colors(bpy.context)
    bpy.ops.ls3d.add_projector()
    first = bpy.context.object
    bpy.ops.ls3d.add_projector()
    second = bpy.context.object
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = None
    at_rest = dict(_viewport._visible_projectors(bpy.context))
    first.select_set(True)
    bpy.context.view_layer.objects.active = first
    second.select_set(True)
    picked = dict(_viewport._visible_projectors(bpy.context))
    check("a projector's outline follows the selection",
          len(at_rest) == 2
          and set(at_rest.values()) == {_viewport.PROJECTOR_COLOR}
          and picked.get(first) == active
          and picked.get(second) == selected
          and _viewport.PROJECTOR_COLOR == (0.0, 0.0, 0.0, 1.0),
          f"at rest {sorted(set(at_rest.values()))}, active "
          f"{picked.get(first)}, selected {picked.get(second)}")

    # A model built entirely from the Add > 4DS menu has to export clean on the
    # first try - that is the whole point of the operators. Every one of these
    # types has a convention the exporter checks and nothing else enforced.
    fresh_scene()
    bpy.ops.ls3d.add_sector()
    sector = bpy.context.object
    bpy.ops.ls3d.add_portal()
    first_portal = bpy.context.object.name
    bpy.context.view_layer.objects.active = sector
    bpy.ops.ls3d.add_portal()
    second_portal = bpy.context.object.name
    bpy.ops.ls3d.add_dummy()
    bpy.ops.ls3d.add_target()
    bpy.ops.ls3d.add_occluder()
    bpy.ops.ls3d.add_mirror()
    mirror = bpy.context.object
    view_box = ([c.name for c in mirror.children],
                all(any(getattr(mirror, f"ls3d_mirror_box_{axis}"))
                    for axis in "xyz"))
    bpy.ops.ls3d.add_lens_flare()
    flare = bpy.context.object
    flare.ls3d_glows[0].material = bpy.data.materials.new("FLARE.BMP")
    bpy.ops.ls3d.add_projector()
    projected = bpy.data.materials.new("PROJECTED.BMP")
    # A projector paints the material's diffuse texture and nothing else, so a
    # material without one is a warning rather than a working setup.
    projected.ls3d_diffuse_tex = bpy.data.images.new("PROJECTED.BMP", 4, 4)
    projected.ls3d_flag_diffuse_enable = True
    bpy.context.object.ls3d_projector_material = projected
    bpy.ops.ls3d.add_light()

    said = []
    original_warn = report_module.Report.warn

    def _watch(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _watch
    try:
        result = export_op(os.path.join(out_dir, "authored.4ds"))
    finally:
        report_module.Report.warn = original_warn
    check("a model built from the Add menu exports clean",
          result == {"FINISHED"} and not said
          and first_portal == "sector_portal1"
          and second_portal == "sector_portal2"
          and view_box == ([], True),
          f"result={result}, portals {first_portal}/{second_portal}, "
          f"view box {view_box}, warnings {said or 'none'}")

    # ... and with the flag bytes the game's own models of each type carry.
    # A frame written with a zero culling byte has its Enable bit clear, which
    # loads fine and is then never drawn, so the menu has to seed them.
    seeded = {}
    if result == {"FINISHED"}:
        document = read_file(os.path.join(out_dir, "authored.4ds"))
        for frame in document.frames:
            seeded[frame.name] = (frame.cull_flags, frame.render_flags,
                                  frame.render_flags2)
            if frame.sector is not None:
                seeded["#sector"] = (frame.sector.flags1, frame.sector.flags2,
                                     tuple(p.flags for p in frame.sector.portals))
    check("the Add menu seeds the flags the game's own models use",
          seeded.get("sector", (0,))[0] == C.DEFAULT_CULL_FLAGS_SECTOR
          and seeded.get("#sector") == (C.DEFAULT_SECTOR_FLAGS1,
                                        C.DEFAULT_SECTOR_FLAGS2,
                                        (C.DEFAULT_PORTAL_FLAGS,) * 2)
          and seeded.get("dummy", (0,))[0] == C.DEFAULT_CULL_FLAGS
          and seeded.get("target", (0,))[0] == C.DEFAULT_CULL_FLAGS
          and seeded.get("occluder", (0,))[0] == C.DEFAULT_CULL_FLAGS
          and seeded.get("light", (0,))[0] == C.DEFAULT_CULL_FLAGS
          and seeded.get("mirror") == (C.DEFAULT_CULL_FLAGS,
                                       C.DEFAULT_RENDER_FLAGS,
                                       C.DEFAULT_RENDER_FLAGS2_MIRROR)
          and seeded.get("lensflare") == (C.DEFAULT_CULL_FLAGS,
                                          C.DEFAULT_RENDER_FLAGS,
                                          C.DEFAULT_RENDER_FLAGS2)
          and seeded.get("projector") == (C.DEFAULT_CULL_FLAGS,
                                          C.DEFAULT_RENDER_FLAGS,
                                          C.DEFAULT_RENDER_FLAGS2_PROJECTOR),
          f"wrote {seeded}")

    # Mark Sharp has to reach the file. The format has no edge flags: a hard
    # edge exists only as two vertices in the same place with different
    # normals, so a sharp edge must show up as a split. Blender folds the
    # sharp_edge attribute into the corner normals it reports, and the exporter
    # splits on those, so this works without the format knowing what an edge is.
    def cube_vertices(smooth, sharp_edges):
        fresh_scene()
        bpy.ops.mesh.primitive_cube_add()
        mesh = bpy.context.object.data
        for polygon in mesh.polygons:
            polygon.use_smooth = smooth
        for index, edge in enumerate(mesh.edges):
            edge.use_edge_sharp = index in sharp_edges
        mesh.update()
        path = os.path.join(out_dir, "sharp.4ds")
        if export_op(path) != {"FINISHED"}:
            return None
        return sum(len(lod.vertices) for f in read_file(path).frames
                   if f.geometry for lod in f.geometry.lods)

    flat = cube_vertices(False, set())
    smooth = cube_vertices(True, set())
    some_sharp = cube_vertices(True, {0, 1, 2, 3})
    all_sharp = cube_vertices(True, set(range(12)))
    check("Mark Sharp splits vertices so the hard edge reaches the file",
          smooth == 14 and some_sharp > smooth and all_sharp == flat == 24,
          f"flat {flat}, smooth {smooth}, 4 sharp {some_sharp}, "
          f"all sharp {all_sharp}")

    # An instance borrows its source's whole LOD chain, and the source has to
    # come earlier in the file. When a parent forces the LOD-carrying object
    # later than another user of the same mesh, that other one owns the
    # geometry - and must own the LOD levels too, or every frame referencing it
    # silently drops to one.
    hotel = os.path.join(os.path.dirname(models_dir), "missions",
                         "MISE08-HOTEL", "scene.4ds")
    if os.path.isfile(hotel):
        fresh_scene()
        import_op(hotel)
        path = os.path.join(out_dir, "instance_lods.4ds")
        levels = {}
        if export_op(path) == {"FINISHED"}:
            doc = read_file(path)
            by_id = {i: f for i, f in enumerate(doc.frames, 1)}
            for frame in doc.frames:
                name = frame.name.lower()
                if not name.startswith("sloupk") or "_lod" in name:
                    continue
                geometry = frame.geometry
                if geometry and geometry.instance_id:
                    source = by_id[geometry.instance_id]
                    levels[frame.name] = len(source.geometry.lods)
                elif geometry:
                    levels[frame.name] = len(geometry.lods)
        original = read_file(hotel)
        expected = max((len(f.geometry.lods) for f in original.frames
                        if f.geometry and f.name.lower().startswith("sloupk")),
                       default=0)
        check("an instance keeps its LOD levels when its source is displaced",
              len(levels) == 5 and expected == 6
              and all(v == expected for v in levels.values()),
              f"expected {expected} levels each, got {levels}")

    # Every normal goes out as it is set, a short one and one of no length at
    # all included: the game ships both ('glass' in mise17-vezeni sits its
    # normals square to their face, and FREERIDE has fifteen of no length).
    import struct as _struct
    fresh_scene()
    bpy.ops.mesh.primitive_plane_add()
    flat = bpy.context.object
    flat.name = "odd_normals"
    flat.ls3d_frame_type = str(C.FRAME_VISUAL)
    flat.visual_type = str(C.VISUAL_OBJECT)
    flat.data.materials.append(bpy.data.materials.new("ODD.BMP"))
    wanted = [(0.0, 0.0, 1.0), (0.0, -0.3609, 0.8461), (0.0, 0.0, 0.0),
              (0.25, 0.0, 0.0)]
    corner_normals = flat.data.attributes.new("custom_normal", "FLOAT_VECTOR",
                                              "CORNER")
    by_vertex = {}
    for loop in flat.data.loops:
        corner_normals.data[loop.index].vector = wanted[loop.vertex_index]
        by_vertex[loop.vertex_index] = wanted[loop.vertex_index]
    odd_path = os.path.join(out_dir, "odd_normals.4ds")
    odd_result = export_op(odd_path)
    written = []
    if odd_result == {"FINISHED"}:
        frame = next(f for f in read_file(odd_path).frames if f.name == "odd_normals")
        written = [v.normal for v in frame.geometry.lods[0].vertices]
    f32 = lambda value: _struct.unpack("<f", _struct.pack("<f", value))[0]
    expected = [tuple(f32(c) for c in (n[0], n[2], n[1])) for n in wanted]
    check("normals are written as they are set, short and zero-length ones too",
          odd_result == {"FINISHED"} and [tuple(n) for n in written] == expected,
          f"result={odd_result}, written={written}")

    # Normals have to survive a save exactly. Blender's own custom-normal
    # storage is a pair of 16-bit numbers, and it neither holds the value it
    # was given nor settles on one: a minority of corners shift a step further
    # every time a model is saved, always the same way. The importer sidesteps
    # it by writing the corner attribute as float vectors instead.
    fresh_scene()
    sample = model("frank.4ds")
    if not os.path.exists(sample):
        SKIPPED.append("normals are written back unchanged")
        print("[SKIP] a normal written back is the same normal "
              "(frank.4ds not in the models folder)", flush=True)
    else:
        getattr(bpy.ops.import_scene, "4ds")(filepath=sample)
    kept = {}
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and obj.data.polygons:
            attribute = obj.data.attributes.get("custom_normal")
            kept[obj.name] = attribute.data_type if attribute else "none"
    from mathutils import Vector as _Vec
    exact = True
    worst_step = 0.0
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or not obj.data.polygons:
            continue
        before = [_Vec(n.vector) for n in obj.data.corner_normals]
        obj.data.attributes["custom_normal"].data.foreach_set(
            "vector", [axis for normal in before for axis in normal])
        after = [_Vec(n.vector) for n in obj.data.corner_normals]
        step = max(((a - b).length for a, b in zip(before, after)), default=0.0)
        worst_step = max(worst_step, step)
        exact = exact and step == 0.0
    check("a normal written back is the same normal, to the last bit",
          not kept or (exact and set(kept.values()) == {"FLOAT_VECTOR"}),
          f"worst change {worst_step:.3e}, attribute types "
          f"{sorted(set(kept.values()))}")

    # Containment is a plane test that assumes the normals point inward, so a
    # sector facing out is an inert room. Blender's primitives all face out.
    fresh_scene()
    _validation = ls3d_module("4ds.validation")
    _volume_report = _validation._volume_report
    CONVEX_TOLERANCE = _validation.CONVEX_TOLERANCE

    def facing(obj):
        _edges, _faces, inward, outward = _volume_report(obj, CONVEX_TOLERANCE)
        return ("inward" if inward else "") + ("outward" if outward else "")

    bpy.ops.mesh.primitive_cube_add()
    room = bpy.context.object
    room.name = "sector_room"
    before_pick = facing(room)
    room.ls3d_frame_type = str(C.FRAME_SECTOR)
    after_pick = facing(room)
    flipped_ok = export_op(os.path.join(out_dir, "sector_in.4ds"))
    bm = bmesh.new()
    bm.from_mesh(room.data)
    bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
    bm.to_mesh(room.data)
    bm.free()
    room.data.update()
    outward_result = export_op(os.path.join(out_dir, "sector_out.4ds"))
    check("picking Sector faces the mesh inward, and an outward one is refused",
          before_pick == "outward" and after_pick == "inward"
          and flipped_ok == {"FINISHED"} and outward_result == {"CANCELLED"},
          f"{before_pick} -> {after_pick}; inward={flipped_ok}, "
          f"outward={outward_result}")

    # A portal is the opening in a wall, so it belongs on the wall.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    room = bpy.context.object
    room.name = "sector_room"
    room.ls3d_frame_type = str(C.FRAME_SECTOR)
    bpy.ops.mesh.primitive_plane_add(location=(0.0, 0.0, 1.0))
    hole = bpy.context.object
    hole.name = "sector_room_portal01"
    hole.parent = room
    hole.ls3d_frame_type = str(C.FRAME_SECTOR)
    from io_mafia_toolkit.common import report as report_module
    said = []
    original_warn = report_module.Report.warn

    def _catch(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _catch
    try:
        on_wall = export_op(os.path.join(out_dir, "portal_on.4ds"))
        on_wall_said = [m for m in said if "off the sector" in m]
        said.clear()
        hole.location.z = 4.0            # float it well clear of the cube
        bpy.context.view_layer.update()
        off_wall = export_op(os.path.join(out_dir, "portal_off.4ds"))
        off_wall_said = [m for m in said if "off the sector" in m]
    finally:
        report_module.Report.warn = original_warn
    check("a portal floating away from its sector is reported",
          on_wall == {"FINISHED"} and not on_wall_said
          and off_wall == {"FINISHED"} and off_wall_said,
          f"on the wall said {on_wall_said or 'nothing'}; "
          f"floating said {off_wall_said or 'nothing'}")

    # A portal is projected onto one plane at run time, so a folded one opens
    # onto a different shape than the one in the viewport.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    sector = bpy.context.object
    sector.name = "sector_room"
    sector.ls3d_frame_type = str(C.FRAME_SECTOR)
    bpy.ops.mesh.primitive_plane_add()
    portal = bpy.context.object
    portal.name = "sector_room_portal01"
    portal.parent = sector
    portal.ls3d_frame_type = str(C.FRAME_SECTOR)
    from io_mafia_toolkit.common import report as report_module
    flat = export_op(os.path.join(out_dir, "portal_flat.4ds"))
    portal.data.vertices[0].co.z += 1.0          # fold one corner right out
    folded_warned = []
    original_warn = report_module.Report.warn

    def _capture(self, message, fix=None):
        folded_warned.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _capture
    try:
        folded = export_op(os.path.join(out_dir, "portal_folded.4ds"))
    finally:
        report_module.Report.warn = original_warn
    # The game ships three sectors with no faces and one portal with no area.
    # They do nothing in game, and the engine loads them without complaint, so
    # the export says so and writes them rather than refusing a whole mission
    # scene. A sector part-way built is a different matter and still an error.
    def _sector_mesh(name, keep_faces):
        fresh_scene()
        bpy.ops.ls3d.add_sector()
        sector = bpy.context.object
        sector.name = name
        bm = bmesh.new()
        bm.from_mesh(sector.data)
        bm.faces.ensure_lookup_table()
        drop = bm.faces[keep_faces:]
        bmesh.ops.delete(bm, geom=drop, context="FACES_ONLY")
        bm.to_mesh(sector.data)
        bm.free()
        sector.data.update()
        return sector

    said = []
    original_warn = report_module.Report.warn

    def _watch(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    faceless = _sector_mesh("hollow", 0)
    faces_left = len(faceless.data.polygons)
    report_module.Report.warn = _watch
    try:
        path = os.path.join(out_dir, "sector_faceless.4ds")
        empty_result = export_op(path)
    finally:
        report_module.Report.warn = original_warn
    written = 0
    if empty_result == {"FINISHED"}:
        for frame in read_file(path).frames:
            if frame.sector is not None:
                written = len(frame.sector.vertices)

    _sector_mesh("halfbuilt", 2)
    partial = export_op(os.path.join(out_dir, "sector_partial.4ds"))
    check("a sector with no faces is allowed through, a half-built one is not",
          faces_left == 0 and empty_result == {"FINISHED"} and written == 8
          and any("nothing is ever inside" in m for m in said)
          and partial == {"CANCELLED"},
          f"faces {faces_left}, no-face export {empty_result} keeping {written} "
          f"vertices, two-face export {partial}, warnings {said or 'none'}")

    # A portal with its corners in a line has no opening, but the game ships
    # one and settles its plane on an axis rather than refusing to load.
    fresh_scene()
    bpy.ops.ls3d.add_sector()
    hull = bpy.context.object
    bpy.ops.ls3d.add_portal()
    inline = bpy.context.object
    # Three corners on one line, the way the game's own one is stored.
    inline.data.clear_geometry()
    inline.data.from_pydata([(1.0, 0.0, -0.5), (1.0, 0.0, 0.0),
                             (1.0, 0.0, 0.5)], [], [(0, 1, 2)])
    inline.data.update()
    said = []
    report_module.Report.warn = _watch
    try:
        path = os.path.join(out_dir, "portal_line.4ds")
        line_result = export_op(path)
    finally:
        report_module.Report.warn = original_warn
    corners = 0
    plane = None
    if line_result == {"FINISHED"}:
        for frame in read_file(path).frames:
            if frame.sector is not None and frame.sector.portals:
                corners = len(frame.sector.portals[0].vertices)
                plane = frame.sector.portals[0].plane_normal
    check("a portal with no area is allowed through with its corners intact",
          line_result == {"FINISHED"} and corners == 3
          and plane is not None and abs(sum(c * c for c in plane) - 1.0) < 1e-6
          and any("no area" in m for m in said),
          f"result={line_result}, {corners} corner(s), plane {plane}, "
          f"warnings {said or 'none'}")

    # Which way a portal points is not cosmetic: the game sorts a sector's
    # portals by the sign of the camera against their plane. Every portal the
    # game ships has its plane facing into its own sector with the stored ring
    # wound around that plane, so both have to come out that way.
    def _sector_with_portal(turn_portal_around):
        fresh_scene()
        bpy.ops.ls3d.add_sector()
        sector = bpy.context.object
        bpy.ops.ls3d.add_portal()
        portal = bpy.context.object
        if turn_portal_around:
            bm = bmesh.new()
            bm.from_mesh(portal.data)
            bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
            bm.to_mesh(portal.data)
            bm.free()
            portal.data.update()
        return sector, portal

    def _portal_facing(obj, sector):
        from mathutils import Vector
        center = sum((sector.matrix_world @ v.co for v in sector.data.vertices),
                     Vector()) / len(sector.data.vertices)
        polygon = obj.data.polygons[0]
        normal = (obj.matrix_world.to_3x3() @ polygon.normal).normalized()
        return normal.dot((center - obj.matrix_world @ polygon.center)
                          .normalized())

    def _written_portal(path):
        for frame in read_file(path).frames:
            if frame.sector is not None and frame.sector.portals:
                sector, portal = frame.sector, frame.sector.portals[0]
                center = [sum(v[a] for v in sector.vertices)
                          / len(sector.vertices) for a in range(3)]
                ring = [0.0, 0.0, 0.0]
                for index, first in enumerate(portal.vertices):
                    second = portal.vertices[(index + 1) % len(portal.vertices)]
                    ring[0] += (first[1] - second[1]) * (first[2] + second[2])
                    ring[1] += (first[2] - second[2]) * (first[0] + second[0])
                    ring[2] += (first[0] - second[0]) * (first[1] + second[1])
                length = math.sqrt(sum(c * c for c in ring)) or 1.0
                ring = [c / length for c in ring]
                return (sum(a * b for a, b in zip(portal.plane_normal, center))
                        + portal.plane_offset,
                        sum(a * b for a, b in zip(ring, portal.plane_normal)))
        return None, None

    sector, portal = _sector_with_portal(False)
    straight = _portal_facing(portal, sector)
    said = []
    original_warn = report_module.Report.warn

    def _watch(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _watch
    try:
        path = os.path.join(out_dir, "portal_facing.4ds")
        good = export_op(path)
    finally:
        report_module.Report.warn = original_warn
    side, wound = _written_portal(path) if good == {"FINISHED"} else (None, None)
    check("a new portal faces into its sector, and is written that way",
          good == {"FINISHED"} and straight > 0.9 and not said
          and side is not None and side > 0.0 and wound > 0.99,
          f"blender facing {straight:.3f}, sector center side {side}, "
          f"winding against plane {wound}, warnings {said or 'none'}")

    sector, portal = _sector_with_portal(True)
    turned = _portal_facing(portal, sector)
    said = []
    report_module.Report.warn = _watch
    try:
        path = os.path.join(out_dir, "portal_backwards.4ds")
        fixed = export_op(path)
    finally:
        report_module.Report.warn = original_warn
    side, wound = _written_portal(path) if fixed == {"FINISHED"} else (None, None)
    # Written as it is set, the way the game's own three such portals are: the
    # plane and the ring still agree, and both face out.
    check("a portal facing out of its sector is reported and written as set",
          fixed == {"FINISHED"} and turned < -0.9
          and any("faces out of its sector" in m for m in said)
          and side is not None and side < 0.0 and wound > 0.99,
          f"blender facing {turned:.3f}, sector center side {side}, "
          f"winding against plane {wound}, warnings {said or 'none'}")

    # The format holds one ring of corners. Faces that touch are merged into
    # that ring first, so what has to be refused is a portal made of pieces
    # that do not touch - there is no single outline to write.
    fresh_scene()
    bpy.ops.ls3d.add_sector()
    bpy.ops.ls3d.add_portal()
    split = bpy.context.object
    bm = bmesh.new()
    bm.from_mesh(split.data)
    bm.faces.ensure_lookup_table()
    copy = bmesh.ops.duplicate(bm, geom=bm.faces[:])
    bmesh.ops.translate(
        bm, verts=[e for e in copy["geom"] if isinstance(e, bmesh.types.BMVert)],
        vec=(0.0, 0.0, 3.0))
    bm.to_mesh(split.data)
    bm.free()
    split.data.update()
    islands = len(split.data.polygons)
    refused = export_op(os.path.join(out_dir, "portal_split.4ds"))

    # A portal that bends but holds together is a different matter: it becomes
    # one ring, and the flatness check is what has something to say about it.
    fresh_scene()
    bpy.ops.ls3d.add_sector()
    bpy.ops.ls3d.add_portal()
    lumpy = bpy.context.object
    bm = bmesh.new()
    bm.from_mesh(lumpy.data)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1,
                              use_grid_fill=True)
    bm.verts.ensure_lookup_table()
    inside = [v for v in bm.verts if len(v.link_faces) == 4]
    for vertex in inside:
        vertex.co += lumpy.data.polygons[0].normal * 0.4
    bm.to_mesh(lumpy.data)
    bm.free()
    lumpy.data.update()
    said = []
    report_module.Report.warn = _watch
    try:
        merged = export_op(os.path.join(out_dir, "portal_lumpy.4ds"))
    finally:
        report_module.Report.warn = original_warn
    corners = 0
    if merged == {"FINISHED"}:
        for frame in read_file(os.path.join(out_dir, "portal_lumpy.4ds")).frames:
            if frame.sector is not None and frame.sector.portals:
                corners = len(frame.sector.portals[0].vertices)
    check("a portal in separate pieces is refused, a bent one becomes one ring",
          islands > 1 and refused == {"CANCELLED"}
          and merged == {"FINISHED"} and corners == 8
          and any("off its outline" in m for m in said),
          f"{islands} island(s) -> {refused}; bent -> {merged} with {corners} "
          f"corners, warnings {said or 'none'}")

    check("a folded portal is reported, a flat one is not",
          flat == {"FINISHED"} and folded == {"FINISHED"}
          and any("not flat" in m for m in folded_warned),
          f"flat={flat}, folded={folded}, said {folded_warned or 'nothing'}")

    # Two engine sector bits stop the sector working entirely if a file has
    # them; the export refuses rather than writing a room that never draws.
    for bit, label in ((C.SF_PARSING, "Parsing"), (C.SF_PREPARED, "Prepared")):
        fresh_scene()
        bpy.ops.mesh.primitive_cube_add()
        hazard = bpy.context.object
        hazard.name = "sector_room"
        hazard.ls3d_frame_type = str(C.FRAME_SECTOR)
        hazard.ls3d_sector_flags1 = bit
        result = export_op(os.path.join(out_dir, "sector_hazard.4ds"))
        check(f"a sector saved with the engine's {label} bit is refused",
              result == {"CANCELLED"}, f"result={result}")

    # The bits the game redoes every frame are harmless, and the panel says so
    # when a sector has any on - they otherwise read as alarming as the two
    # above. The named bits are the ones the table and the checks use.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    wiped_room = bpy.context.object
    wiped_room.ls3d_frame_type = str(C.FRAME_SECTOR)
    wiped_room.ls3d_sector_flags1 = C.SF_VISIBLE | C.SF_VIS_CYCLE
    wiped_lines = _PanelLines()
    _panel._draw_sector(_panel, wiped_lines, wiped_room)
    wiped_room.ls3d_sector_flags1 = C.SF_OCCLUDER
    clean_lines = _PanelLines()
    _panel._draw_sector(_panel, clean_lines, wiped_room)
    table_bits = {attr: mask for mask, attr, _l, _d in C.RESERVED_SECTOR_FLAGS}
    check("the sector panel says which stored bits the game redoes every frame",
          "Visible, Visibility Cycle: redone every frame, harmless" in wiped_lines.lines
          and not any("redone every frame" in t for t in clean_lines.lines)
          and C.SECTOR_PER_FRAME_BITS == (table_bits["sf_res_visible"]
                                          | table_bits["sf_res_collected"]
                                          | table_bits["sf_res_vis_cycle"])
          and table_bits["sf_res_parsing"] == C.SF_PARSING == 1 << 1
          and table_bits["sf_res_prepared"] == C.SF_PREPARED == 1 << 2,
          f"with Visible and Visibility Cycle on it said {wiped_lines.lines}; "
          f"with only Occluder {clean_lines.lines}")

    # The light-group mask has no controls: Mafia turns every group on for
    # every sector once a mission has loaded, so nothing set here could
    # survive. It still has to reach the file untouched.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    grouped = bpy.context.object
    grouped.name = "sector_room"
    grouped.ls3d_frame_type = str(C.FRAME_SECTOR)
    grouped.ls3d_sector_flags2 = _to_signed(0x80000008)
    path = os.path.join(out_dir, "group_mask.4ds")
    written = None
    if export_op(path) == {"FINISHED"}:
        written = next((f.sector.flags2 & 0xFFFFFFFF for f in read_file(path).frames
                        if f.sector is not None), None)
    check("the light-group mask round-trips with no controls for it",
          written == 0x80000008
          and not hasattr(bpy.types.Object, "ls3d_sector_groups"),
          f"wrote 0x{written:08X}" if written is not None else "nothing written")

    # Sectors, portals and occluders are hulls: no face groups, so nothing in
    # the file can name a material. One left in a slot used to be written into
    # the material table anyway.
    fresh_scene()
    stray = bpy.data.materials.new("HULL.BMP")
    bpy.ops.mesh.primitive_cube_add()
    sector = bpy.context.object
    sector.name = "sector_room"
    sector.ls3d_frame_type = str(C.FRAME_SECTOR)
    sector.data.materials.append(stray)
    bpy.ops.mesh.primitive_cube_add()
    occluder = bpy.context.object
    occluder.name = "occluder_wall"
    occluder.ls3d_frame_type = str(C.FRAME_OCCLUDER)
    occluder.data.materials.append(stray)
    bpy.ops.mesh.primitive_plane_add()
    portal = bpy.context.object
    portal.name = "sector_room_portal01"
    portal.parent = sector
    portal.ls3d_frame_type = str(C.FRAME_SECTOR)
    portal.data.materials.append(stray)
    bpy.ops.mesh.primitive_cube_add()
    mesh = bpy.context.object
    mesh.name = "prop"
    mesh.ls3d_frame_type = str(C.FRAME_VISUAL)
    mesh.data.materials.append(bpy.data.materials.new("REAL.BMP"))
    path = os.path.join(out_dir, "hullmats.4ds")
    result = export_op(path)
    table = []
    if result == {"FINISHED"}:
        table = [m.diffuse_texture or "(unnamed)" for m in read_file(path).materials]
    check("a hull's material never reaches the material table",
          result == {"FINISHED"} and len(table) == 1,
          f"table={table}")

    # And the panel offers no material controls on one, only the way out.
    bpy.context.view_layer.objects.active = sector
    slots_before = len(sector.material_slots)
    cleared = bpy.ops.ls3d.clear_frame_materials()
    check("material slots can be cleared off a hull from the panel",
          slots_before == 1 and cleared == {"FINISHED"}
          and len(sector.material_slots) == 0,
          f"{slots_before} slot(s) -> {len(sector.material_slots)}")

    # Panels were not covered at all, and the bugs they hide are of the
    # "field behind the wrong toggle" kind. A recording layout is enough to
    # catch those: it answers like a UILayout and remembers what was drawn.
    class _Recorder:
        def __init__(self, calls):
            self.calls = calls
            self.enabled = True
            self.alignment = "EXPAND"

        def _child(self, *a, **k):
            return _Recorder(self.calls)

        box = row = column = split = grid_flow = _child

        def label(self, text="", **k):
            self.calls.append(("label", text))

        def prop(self, data, name, **k):
            self.calls.append(("prop", name))

        def template_ID(self, data, name, **k):
            self.calls.append(("prop", name))

        def template_list(self, *a, **k):
            self.calls.append(("list", a[0] if a else ""))

        def separator(self):
            pass

        def operator(self, idname, **k):
            self.calls.append(("op", idname))
            return type("Op", (), {"direction": ""})()

        def panel(self, idname, default_closed=False):
            return _Recorder(self.calls), _Recorder(self.calls)

    ls3d_ui = ls3d_module("4ds.ui")

    def drawn(method, target, raw_flags):
        calls = []
        original = io_mafia_toolkit.get_preferences
        io_mafia_toolkit.get_preferences = lambda: type("P", (), {
            "show_raw_flags": raw_flags, "textures_path": "",
            })()
        try:
            method(ls3d_ui.The4DSObjectPanel, _Recorder(calls), target)
        finally:
            io_mafia_toolkit.get_preferences = original
        return [name for kind, name in calls if kind == "prop"]

    fresh_scene()
    bpy.ops.mesh.primitive_plane_add()
    portal = bpy.context.object
    portal.name = "sector_portal01"
    portal.ls3d_frame_type = str(C.FRAME_SECTOR)
    plain = drawn(ls3d_ui.The4DSObjectPanel._draw_portal, portal, False)
    raw = drawn(ls3d_ui.The4DSObjectPanel._draw_portal, portal, True)
    check("stored portal values are not hidden by the raw flag preference",
          "ls3d_portal_far" in plain and "ls3d_portal_view_dist_m" in plain
          and "ls3d_portal_flags_str" not in plain
          and "ls3d_portal_flags_str" in raw,
          f"without raw flags {plain}")

    # A flare's materials are on no mesh, so the Material tab never shows them.
    fresh_scene()
    flare = bpy.data.objects.new("lensflare", None)
    bpy.context.collection.objects.link(flare)
    flare.empty_display_type = "SPHERE"
    flare.ls3d_frame_type = str(C.FRAME_VISUAL)
    flare.visual_type = str(C.VISUAL_LENSFLARE)
    element = flare.ls3d_glows.add()
    element.material = bpy.data.materials.new("flare.bmp")
    flare.ls3d_show_glow_material = False
    folded = drawn(ls3d_ui.The4DSObjectPanel._draw_lensflare, flare, False)
    flare.ls3d_show_glow_material = True
    opened = drawn(ls3d_ui.The4DSObjectPanel._draw_lensflare, flare, False)
    check("a flare element's material is editable from the flare panel",
          "ls3d_diffuse_tex" in opened and "ls3d_flag_env_enable" in opened
          and "ls3d_opacity" in opened and "ls3d_diffuse_tex" not in folded,
          f"folded {len(folded)} controls, opened {len(opened)}")

    # A mirror's view box is edited from the mirror's own panel, the way a
    # dummy's box is from the dummy's.
    class _PanelShim:
        """Stands in for the panel instance: a layout plus its own methods."""

        def __init__(self, calls):
            self.layout = _Recorder(calls)

        def __getattr__(self, name):
            attribute = getattr(ls3d_ui.The4DSObjectPanel, name)
            return attribute.__get__(self) if callable(attribute) else attribute

    def whole_panel(target):
        calls = []
        panel = _PanelShim(calls)
        context = type("Ctx", (), {"object": target,
                                   "scene": bpy.context.scene})()
        original = io_mafia_toolkit.get_preferences
        io_mafia_toolkit.get_preferences = lambda: type("P", (), {
            "show_raw_flags": True, "textures_path": "",
            })()
        try:
            ls3d_ui.The4DSObjectPanel.draw(panel, context)
        finally:
            io_mafia_toolkit.get_preferences = original
        return [name for kind, name in calls if kind == "prop"]

    fresh_scene()
    bpy.ops.ls3d.add_mirror()
    mirror_props = whole_panel(bpy.context.object)
    bpy.ops.ls3d.add_dummy()
    dummy_props = whole_panel(bpy.context.object)
    view_fields = {"ls3d_mirror_box_center", "ls3d_mirror_box_x",
                   "ls3d_mirror_box_y", "ls3d_mirror_box_z"}
    check("a mirror's panel offers its view box, as a dummy's offers its box",
          view_fields <= set(mirror_props)
          and {"bbox_min", "bbox_max"} <= set(dummy_props)
          and not view_fields & set(dummy_props),
          f"mirror offers {sorted(view_fields & set(mirror_props))}")

    # The model as a whole has a sidebar tab of its own: the scene check, the
    # animated object count and the switch that shows the joints' boxes. None
    # of the three belongs to a selected object, and none is in the object
    # panel any more.
    fresh_scene()
    bpy.ops.ls3d.add_joint()
    model_calls = []

    class _ScenePanelShim:
        def __init__(self, calls):
            self.layout = _Recorder(calls)

    ls3d_ui.The4DSModelPanel.draw(
        _ScenePanelShim(model_calls),
        type("Ctx", (), {"scene": bpy.context.scene,
                         "object": bpy.context.object})())
    model_props = {name for kind, name in model_calls if kind == "prop"}
    model_ops = {name for kind, name in model_calls if kind == "op"}
    object_calls = whole_panel(bpy.context.object)
    check("the model's own controls are in the sidebar, not on an object",
          {"ls3d_animated_object_count", C.SHOW_INFLUENCE_BOXES_PROP,
           C.INFLUENCE_BOXES_IN_FRONT_PROP, C.JOINT_DISPLAY_SCALE_PROP}
          <= model_props
          and "ls3d.check_scene" in model_ops
          and "ls3d_animated_object_count" not in set(object_calls),
          f"sidebar offers {sorted(model_props)} and {sorted(model_ops)}, the "
          f"object panel {sorted(set(object_calls))[:6]}")

    # How big the joint markers are drawn is the scene's own setting, and it
    # reaches what is drawn: the marker's size is driven by the joint's own
    # 4DS scale times the setting.
    def marker_size(rig, bone_name):
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        return evaluated.pose.bones[bone_name].custom_shape_scale_xyz[0]

    fresh_scene()
    bpy.ops.ls3d.add_joint()
    marked_rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    marked_joint = marked_rig.data.bones[0].name
    setattr(bpy.context.scene, C.JOINT_DISPLAY_SCALE_PROP, 1.0)
    bpy.context.view_layer.update()
    at_one = marker_size(marked_rig, marked_joint)
    setattr(bpy.context.scene, C.JOINT_DISPLAY_SCALE_PROP, 4.0)
    bpy.context.view_layer.update()
    at_four = marker_size(marked_rig, marked_joint)
    # A joint's own scale still counts: the two multiply.
    marked_rig.pose.bones[marked_joint]["ls3d_joint_scale"] = (2.0, 2.0, 2.0)
    # A property written straight onto a bone does not move the scene by
    # itself, so the drivers reading it are told to look again.
    marked_rig.update_tag()
    bpy.context.view_layer.update()
    with_joint_scale = marker_size(marked_rig, marked_joint)
    marked_rig.pose.bones[marked_joint]["ls3d_joint_scale"] = (1.0, 1.0, 1.0)
    marked_rig.update_tag()
    # Nothing about the model changes: a marker is drawn, never written.
    bpy.ops.mesh.primitive_cube_add()
    marked_path = os.path.join(out_dir, "marker_size.4ds")
    marked_out = export_op(marked_path)
    marked_frames = (len(read_file(marked_path).frames)
                     if marked_out == {"FINISHED"} else 0)
    setattr(bpy.context.scene, C.JOINT_DISPLAY_SCALE_PROP, 1.0)
    check("the joint marker slider reaches what is drawn, and nothing else",
          abs(at_one - 0.03) < 1e-6 and abs(at_four - 0.12) < 1e-6
          and abs(with_joint_scale - 0.24) < 1e-6
          and marked_out == {"FINISHED"} and marked_frames == 2,
          f"marker at 1x {at_one}, at 4x {at_four}, with a joint scale of 2 "
          f"{with_joint_scale}; export {marked_out} wrote "
          f"{marked_frames} frame(s)")

    # The engine's own bits: the ones this file uses are always listed, and
    # with the preference off the rest are not listed at all - the raw field
    # above still reaches every one of them.
    def reserved_shown(raw_value, show):
        calls = []
        target = type("T", (), {"cull_flags": raw_value})()
        original = io_mafia_toolkit.get_preferences
        io_mafia_toolkit.get_preferences = lambda: type("P", (), {
            "show_raw_flags": True, "show_reserved_flags": show,
            "textures_path": "",
            })()
        try:
            ls3d_ui._reserved_grid(_Recorder(calls), target,
                                   C.RESERVED_CULL_FLAGS, "cull_flags")
        finally:
            io_mafia_toolkit.get_preferences = original
        return ([name for kind, name in calls if kind == "prop"],
                [text for kind, text in calls if kind == "label"])

    used = C.RESERVED_CULL_FLAGS[0][0]
    folded_props, folded_labels = reserved_shown(used, False)
    all_props, _all_labels = reserved_shown(used, True)
    none_props, none_labels = reserved_shown(0, False)
    check("engine bits a file uses are listed, and the rest are not listed at all",
          folded_props == [C.RESERVED_CULL_FLAGS[0][1]]
          and len(all_props) == len(C.RESERVED_CULL_FLAGS)
          and not none_props and not none_labels
          and all("off" not in text for text in folded_labels),
          f"with one bit on: {folded_props} under {folded_labels}; all "
          f"{len(all_props)}; with none on: {none_props}")

    # The renderable-object bit follows the visual type, not a preference: the
    # game carries it on every visual it ships except its mirrors, and a
    # projector is the other kind that must never have it.
    fresh_scene()
    bpy.ops.mesh.primitive_plane_add()
    plain = bpy.context.object
    plain.name = "plain"
    plain.ls3d_frame_type = str(C.FRAME_VISUAL)
    plain.rf2_is_mesh_object = False          # cleared by hand, not by default
    bpy.ops.mesh.primitive_cube_add()
    mirror = bpy.context.object
    mirror.name = "mirror"
    mirror.ls3d_frame_type = str(C.FRAME_VISUAL)
    mirror.visual_type = str(C.VISUAL_MIRROR)
    mirror.rf2_is_mesh_object = True          # and a mirror must never keep it
    bpy.ops.ls3d.fit_view_box()
    bpy.ops.ls3d.add_projector()
    beam = bpy.context.object
    beam.name = "beam"
    beam.rf2_is_mesh_object = True            # nor a projector
    beam.ls3d_projector_material = bpy.data.materials.new("BEAM.BMP")
    path = os.path.join(out_dir, "renderable.4ds")
    result = export_op(path)
    written = {}
    if result == {"FINISHED"}:
        for frame in read_file(path).frames:
            if frame.render_flags2 is not None:
                written[frame.name] = bool(frame.render_flags2 & C.LF_IS_MESH_OBJECT)
    check("the renderable-object bit follows the visual type",
          result == {"FINISHED"} and written.get("plain") is True
          and written.get("mirror") is False
          and written.get("beam") is False,
          f"wrote {written}")

    # ── animations ────────────────────────────────────────────────────────
    # An animation carries no skeleton, so it is matched onto whatever model is
    # already loaded, by track name. What has to survive the trip is every key
    # of every channel, and the length of the animation.
    _anim = ls3d_module("5ds.codec")
    read_animation_file, write_animation_file, Animation, Track = (
        _anim.read_animation_file, _anim.write_animation_file,
        _anim.Animation, _anim.Track)
    fresh_scene()
    made = Animation(end_frame=12)
    turning = Track(name="spinner")
    turning.rotations = [(0, (1.0, 0.0, 0.0, 0.0)), (6, (0.0, 1.0, 0.0, 0.0)),
                         (12, (1.0, 0.0, 0.0, 0.0))]
    turning.positions = [(0, (0.0, 0.0, 0.0)), (12, (1.5, 2.5, -3.5))]
    turning.scales = [(0, (1.0, 1.0, 1.0)), (12, (2.0, 0.5, 1.0))]
    cues = Track(name="notify")
    cues.notes = [(3, 7), (9, 42)]
    made.tracks = [turning, cues]
    path = os.path.join(out_dir, "made.5ds")
    write_animation_file(made, path)
    read_back = read_animation_file(path)
    same = (len(read_back.tracks) == 2
            and read_back.end_frame == 12
            and read_back.tracks[0].rotations == turning.rotations
            and read_back.tracks[0].positions == turning.positions
            and read_back.tracks[0].scales == turning.scales
            and read_back.tracks[1].notes == cues.notes)
    check("an animation written from nothing reads back the same",
          same,
          f"{len(read_back.tracks)} track(s), end frame {read_back.end_frame}")

    # ... and the same through Blender, which is where the keys have to be
    # turned into pose channels and back.
    fresh_scene()
    for name in ("spinner", "notify"):
        bpy.ops.object.empty_add()
        bpy.context.object.name = name
    result = getattr(bpy.ops.import_scene, "5ds")(filepath=path)
    fps = bpy.context.scene.render.fps
    end = bpy.context.scene.frame_end
    out = os.path.join(out_dir, "made_again.5ds")
    exported = getattr(bpy.ops.export_scene, "5ds")(filepath=out)
    again = read_animation_file(out) if exported == {"FINISHED"} else None
    worst = 0.0
    by_name_notes = None
    if again is not None:
        by_name = {t.name: t for t in again.tracks}
        if "notify" in by_name:
            by_name_notes = by_name["notify"].notes
        for original in (turning,):
            copy = by_name.get(original.name)
            if copy is None:
                worst = float("inf")
                continue
            for channel in ("rotations", "positions", "scales"):
                for (_, one), (_, two) in zip(getattr(original, channel),
                                              getattr(copy, channel)):
                    worst = max(worst, max(abs(a - b) for a, b in zip(one, two)))
    check("an animation survives a trip through Blender",
          result == {"FINISHED"} and exported == {"FINISHED"}
          and again is not None and len(again.tracks) == 2
          and again.end_frame == 12 and fps == 25 and end == 12
          and by_name_notes == cues.notes and worst < 1e-5,
          f"import={result}, export={exported}, fps={fps}, end={end}, "
          f"worst key change {worst:.2e}")

    # A track's flag word says which key arrays it carries. Worked out from
    # the keys it always agrees with them; set by hand it can say something
    # else, which the game accepts. What it must never say is a bit the game
    # does not know, or both kinds of event at once.
    anim_format = ls3d_module("5ds.codec")
    fresh_scene()
    bpy.ops.object.empty_add()
    thing = bpy.context.object
    thing.name = "thing"
    thing.rotation_mode = "QUATERNION"
    thing.keyframe_insert("rotation_quaternion", frame=0)
    thing.keyframe_insert("rotation_quaternion", frame=10)
    bpy.context.scene.frame_end = 10
    flag_path = os.path.join(out_dir, "flags.5ds")

    automatic = getattr(bpy.ops.export_scene, "5ds")(filepath=flag_path)
    auto_flags = anim_format.read_animation_file(flag_path).tracks[0].flags

    thing.ls3d_anim_auto_flags = False
    thing.ls3d_anim_flags = anim_format.KEY_ROTATION | anim_format.KEY_SCALE
    claimed = getattr(bpy.ops.export_scene, "5ds")(filepath=flag_path)
    written = anim_format.read_animation_file(flag_path).tracks[0]

    thing.ls3d_anim_flags = anim_format.KEY_ROTATION | 0x01
    unknown = getattr(bpy.ops.export_scene, "5ds")(filepath=flag_path)
    thing.ls3d_anim_flags = anim_format.KEY_NOTE | anim_format.KEY_NOTE_STRING
    both = getattr(bpy.ops.export_scene, "5ds")(filepath=flag_path)

    check("key flags follow the keys, or the user, and never the impossible",
          automatic == {"FINISHED"} and auto_flags == anim_format.KEY_ROTATION
          and claimed == {"FINISHED"}
          and written.flags == (anim_format.KEY_ROTATION
                                | anim_format.KEY_SCALE)
          and len(written.scales) == 0
          and unknown == {"CANCELLED"} and both == {"CANCELLED"},
          f"auto=0x{auto_flags:X}, claimed=0x{written.flags:X} with "
          f"{len(written.scales)} scale key(s), unknown bit={unknown}, "
          f"both event kinds={both}")

    # Blender keys rotation on the channel its rotation mode selects, and a new
    # object is in Euler mode. Reading only the quaternion channel found no
    # keys there - so a spinning object exported with no rotation at all, and
    # if it had nothing else keyed it got no track at all, silently.
    fresh_scene()
    bpy.context.scene.frame_end = 30
    modes = {}
    for mode in ("QUATERNION", "XYZ", "ZYX", "AXIS_ANGLE"):
        bpy.ops.object.empty_add()
        spinner = bpy.context.object
        spinner.name = f"spin_{mode}"
        spinner.rotation_mode = mode
        if mode == "QUATERNION":
            spinner.keyframe_insert("rotation_quaternion", frame=1)
            spinner.rotation_quaternion = (0.7071, 0.0, 0.7071, 0.0)
            spinner.keyframe_insert("rotation_quaternion", frame=30)
        elif mode == "AXIS_ANGLE":
            spinner.keyframe_insert("rotation_axis_angle", frame=1)
            spinner.rotation_axis_angle = (1.5707963, 0.0, 1.0, 0.0)
            spinner.keyframe_insert("rotation_axis_angle", frame=30)
        else:
            spinner.keyframe_insert("rotation_euler", frame=1)
            spinner.rotation_euler = (0.0, 1.5707963, 0.0)
            spinner.keyframe_insert("rotation_euler", frame=30)
        modes[mode] = spinner

    # One object keyed on rotation and position together: the rotation used to
    # vanish while the position went through, which is the quiet version.
    bpy.ops.object.empty_add()
    both_ways = bpy.context.object
    both_ways.name = "euler_and_move"
    both_ways.rotation_mode = "XYZ"
    both_ways.keyframe_insert("rotation_euler", frame=1)
    both_ways.keyframe_insert("location", frame=1)
    both_ways.rotation_euler = (0.0, 1.0, 0.0)
    both_ways.location = (0.0, 0.0, 3.0)
    both_ways.keyframe_insert("rotation_euler", frame=30)
    both_ways.keyframe_insert("location", frame=30)

    modes_path = os.path.join(out_dir, "rotation_modes.5ds")
    wrote_modes = getattr(bpy.ops.export_scene, "5ds")(filepath=modes_path)
    by_name = {t.name: t for t
               in anim_format.read_animation_file(modes_path).tracks}

    # Every mode reaches the file, and each one says the same turn: a quarter
    # circle about Y, which the axis swap puts on the file's Z.
    turned = {}
    for mode, spinner in modes.items():
        track = by_name.get(spinner.name)
        turned[mode] = None
        if track is not None and len(track.rotations) == 2:
            from mathutils import Quaternion as _Q
            first = _Q(track.rotations[0][1])
            last = _Q(track.rotations[1][1])
            turned[mode] = round(math.degrees(first.rotation_difference(last).angle), 1)

    mixed = by_name.get("euler_and_move")
    # And the panel reads the same channel the export does, so it cannot show
    # "no rotation" for a frame that plainly spins.
    _anim_io = ls3d_module("5ds.io")
    panel_word = _anim_io.derived_key_flags(modes["XYZ"])

    check("rotation exports from whichever channel the rotation mode keys",
          wrote_modes == {"FINISHED"}
          and set(turned) == {"QUATERNION", "XYZ", "ZYX", "AXIS_ANGLE"}
          and all(angle is not None and abs(angle - 90.0) < 0.5
                  for angle in turned.values())
          and mixed is not None
          and len(mixed.rotations) == 2 and len(mixed.positions) == 2
          and mixed.flags == (anim_format.KEY_ROTATION
                              | anim_format.KEY_POSITION)
          and panel_word & anim_format.KEY_ROTATION,
          f"turn per mode {turned}; mixed track rotations "
          f"{mixed and len(mixed.rotations)}, positions "
          f"{mixed and len(mixed.positions)}, flags "
          f"0x{mixed.flags if mixed else 0:X}; panel 0x{panel_word:X}")

    # Two quaternions give the ends of a turn, never the way round it went, so
    # past half a circle the game takes the short arc - and an exact full turn
    # is the same rotation twice, which does not move at all. Blender plays the
    # Euler curve right through, so nothing on screen says this is coming.
    fresh_scene()
    bpy.context.scene.frame_end = 30
    bpy.ops.object.empty_add()
    full_spin = bpy.context.object
    full_spin.name = "full_spin"
    full_spin.rotation_mode = "XYZ"
    full_spin.keyframe_insert("rotation_euler", frame=1)
    full_spin.rotation_euler = (0.0, 2.0 * math.pi, 0.0)
    full_spin.keyframe_insert("rotation_euler", frame=30)
    spotted = _anim_io.long_turns(_anim_io.channelbag(full_spin), full_spin)

    # Quartered, the same spin is carried exactly.
    fresh_scene()
    bpy.context.scene.frame_end = 40
    bpy.ops.object.empty_add()
    quartered = bpy.context.object
    quartered.name = "quartered"
    quartered.rotation_mode = "XYZ"
    for step in range(5):
        quartered.rotation_euler = (0.0, step * math.pi / 2.0, 0.0)
        quartered.keyframe_insert("rotation_euler", frame=1 + step * 10)
    quiet = _anim_io.long_turns(_anim_io.channelbag(quartered), quartered)
    quarter_path = os.path.join(out_dir, "quartered.5ds")
    wrote_quarters = getattr(bpy.ops.export_scene, "5ds")(filepath=quarter_path)
    quarter_track = anim_format.read_animation_file(quarter_path).tracks[0]

    check("a turn too big for two keys is reported, and a quartered one is not",
          len(spotted) == 1
          and math.degrees(spotted[0][2]) > 179.0
          and not quiet
          and wrote_quarters == {"FINISHED"}
          and len(quarter_track.rotations) == 5,
          f"full spin flagged {len(spotted)} gap(s) "
          f"{[round(math.degrees(g[2])) for g in spotted]}; quartered flagged "
          f"{len(quiet)}, wrote {len(quarter_track.rotations)} key(s)")

    # The engine blends two rotation keys by always taking the shorter arc - it
    # flips the second whenever the pair points into opposite halves - so a turn
    # of half a circle or more between two keys plays the other way round. An
    # Euler curve can author exactly that, so the export splits such a stretch
    # by evaluating the curve in between. Checked by playing the file back with
    # the engine's own blend and comparing it with Blender, frame by frame: a
    # ring dialing back and forth, three of whose moves are past half a turn.
    def _engine_slerp(a, b, t):
        cos_a = sum(x * y for x, y in zip(a, b))
        if cos_a < 0.0:
            cos_a = -cos_a
            b = tuple(-v for v in b)
        if 1.0 - cos_a <= 1e-6:
            c0, c1 = 1.0 - t, t
        else:
            angle = math.acos(min(1.0, cos_a))
            inverse = 1.0 / math.sin(angle)
            c0 = math.sin((1.0 - t) * angle) * inverse
            c1 = math.sin(angle * t) * inverse
        return tuple(c0 * x + c1 * y for x, y in zip(a, b))

    def _played(keys, frame):
        if frame <= keys[0][0]:
            return keys[0][1]
        if frame >= keys[-1][0]:
            return keys[-1][1]
        for (f0, q0), (f1, q1) in zip(keys, keys[1:]):
            if f0 <= frame < f1:
                return _engine_slerp(q0, q1, (frame - f0) / (f1 - f0))
        return keys[-1][1]

    fresh_scene()
    bpy.ops.object.empty_add()
    ring = bpy.context.object
    ring.name = "dialing_ring"
    ring.rotation_mode = "XYZ"
    dial = [(1, 0.0), (125, 120.0), (200, 120.0), (375, -55.38),
            (450, -55.38), (715, 230.77), (790, 230.77), (945, 73.85),
            (1025, 73.85), (1200, 258.46), (1280, 258.46), (1440, 92.31),
            (1520, 92.31), (1765, 360.0)]
    for frame, degrees in dial:
        ring.rotation_euler = (0.0, math.radians(degrees), 0.0)
        ring.keyframe_insert("rotation_euler", frame=frame)
    for fcurve in _anim_io.channelbag(ring).fcurves:
        for point in fcurve.keyframe_points:
            point.interpolation = "LINEAR"
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 1765

    dial_path = os.path.join(out_dir, "dialing.5ds")
    wrote_dial = getattr(bpy.ops.export_scene, "5ds")(filepath=dial_path,
                                                      fix_long_turns=True)
    dial_keys = next(t for t in anim_format.read_animation_file(dial_path).tracks
                     if t.name == "dialing_ring").rotations

    def _turn_between(a, b):
        """The angle between two rotations, precise near zero.

        Not acos of their dot: Blender's quaternions are 32-bit, so the dot is
        only good to about 6e-8, and acos turns that into a floor of about 0.04
        degree - a difference no rotation actually has. atan2 of the chord
        lengths keeps full precision down to nothing.
        """
        na = math.sqrt(sum(v * v for v in a))
        nb = math.sqrt(sum(v * v for v in b))
        a = [v / na for v in a]
        b = [v / nb for v in b]
        if sum(x * y for x, y in zip(a, b)) < 0.0:
            b = [-v for v in b]
        apart = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
        together = math.sqrt(sum((x + y) ** 2 for x, y in zip(a, b)))
        return math.degrees(2.0 * math.atan2(apart, together))

    worst_dial = 0.0
    for frame in range(1, 1766, 3):
        bpy.context.scene.frame_set(frame)
        # Blender's own rotation, built in 64 bits from the angle it plays.
        half = ring.rotation_euler[1] * 0.5
        want = (math.cos(half), 0.0, math.sin(half), 0.0)
        w, x, y, z = _played(dial_keys, frame)
        got = (w, x, z, y)                        # back from the file's axes
        worst_dial = max(worst_dial, _turn_between(want, got))

    check("a turn past half a circle is split so the game plays it the same way",
          wrote_dial == {"FINISHED"}
          and len(dial_keys) > len(dial)
          and worst_dial < 0.01,
          f"{len(dial)} key(s) in Blender, {len(dial_keys)} written; played "
          f"back with the engine's own blend the worst frame is "
          f"{worst_dial:.4f} degree(s) off Blender")

    # A quaternion curve is never split. It is already in the file's own terms,
    # and the game's own animations store thousands of neighboring keys in
    # opposite halves - a turn of nearly nothing with its sign flipped. Blender
    # blends those the long way round, so splitting along that path would make
    # a re-exported game animation spin where the original barely moved.
    fresh_scene()
    bpy.ops.object.empty_add()
    flipper = bpy.context.object
    flipper.name = "flipped_signs"
    flipper.rotation_mode = "QUATERNION"
    flip_keys = [(0, (1.0, 0.0, 0.0, 0.0)),
                 (10, (-0.9962, 0.0, 0.0, -0.0872)),    # ten degrees, sign flipped
                 (20, (0.9848, 0.0, 0.0, 0.1736)),      # twenty, flipped back
                 (30, (-0.9659, 0.0, 0.0, -0.2588))]
    for frame, quat in flip_keys:
        flipper.rotation_quaternion = quat
        flipper.keyframe_insert("rotation_quaternion", frame=frame)
    bpy.context.scene.frame_start = 0
    bpy.context.scene.frame_end = 30
    _turns, flip_added, _fast = _anim_io.export_rotation_keys(
        _anim_io.channelbag(flipper), flipper)
    flip_path = os.path.join(out_dir, "flipped_signs.5ds")
    getattr(bpy.ops.export_scene, "5ds")(filepath=flip_path,
                                         fix_long_turns=True)
    flipped_out = next(t for t in anim_format.read_animation_file(flip_path).tracks
                       if t.name == "flipped_signs").rotations
    check("a quaternion curve with flipped signs is never split",
          flip_added == 0 and len(flipped_out) == len(flip_keys),
          f"{flip_added} key(s) the split would add; {len(flipped_out)} "
          f"written for {len(flip_keys)} keyed")

    # Splitting long turns is an export autofix, the same shape as the weight
    # fixes: an option in both export dialogs, off until asked for. Off writes
    # exactly what was keyed and says which turns will play the other way; on
    # adds keys read off the curve.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    swinger = bpy.context.object
    swinger.name = "swinger"
    swinger.rotation_mode = "XYZ"
    for frame, degrees in ((0, 0.0), (30, 270.0)):
        swinger.rotation_euler = (0.0, math.radians(degrees), 0.0)
        swinger.keyframe_insert("rotation_euler", frame=frame)
    bpy.context.scene.frame_start = 0
    bpy.context.scene.frame_end = 30

    def _rotation_keys(path, name="swinger"):
        return len(next(t for t in anim_format.read_animation_file(path).tracks
                        if t.name == name).rotations)

    fixed_path = os.path.join(out_dir, "swing_fixed.5ds")
    literal_path = os.path.join(out_dir, "swing_literal.5ds")
    getattr(bpy.ops.export_scene, "5ds")(filepath=fixed_path,
                                         fix_long_turns=True)
    # Left at its default, which has to be the literal export.
    getattr(bpy.ops.export_scene, "5ds")(filepath=literal_path)
    fixed_count = _rotation_keys(fixed_path)
    literal_count = _rotation_keys(literal_path)

    # Off, the export still has to say which turn will play backwards.
    class _Heard:
        def __init__(self):
            self.warnings, self.infos = [], []

        def warn(self, message, fix=None):
            self.warnings.append(message)

        def info(self, message):
            self.infos.append(message)

    ops_5ds = ls3d_module("5ds.ops")
    heard_off, heard_on = _Heard(), _Heard()
    ops_5ds._warn_about_long_turns(bpy.context.scene, heard_off,
                                   fix_long_turns=False)
    ops_5ds._warn_about_long_turns(bpy.context.scene, heard_on,
                                   fix_long_turns=True)
    warned_off = any("swinger" in w and "270" in w for w in heard_off.warnings)
    quiet_on = not heard_on.warnings and any("swinger" in i
                                             for i in heard_on.infos)

    # The model export writes the .5ds too, and has to honor the same choice.
    model_fixed = os.path.join(out_dir, "swing_model_fixed.4ds")
    model_literal = os.path.join(out_dir, "swing_model_literal.4ds")
    getattr(bpy.ops.export_scene, "4ds")(filepath=model_fixed,
                                         write_animation=True,
                                         fix_long_turns=True)
    getattr(bpy.ops.export_scene, "4ds")(filepath=model_literal,
                                         write_animation=True)
    beside_fixed = _rotation_keys(model_fixed[:-4] + ".5ds")
    beside_literal = _rotation_keys(model_literal[:-4] + ".5ds")

    defaults = (
        bpy.ops.export_scene.__getattribute__("5ds").get_rna_type()
        .properties["fix_long_turns"].default,
        bpy.ops.export_scene.__getattribute__("4ds").get_rna_type()
        .properties["fix_long_turns"].default)

    check("Fix Long Rotation Turns is an export option in both dialogs",
          defaults == (False, False)
          and fixed_count > 2 and literal_count == 2
          and warned_off and quiet_on
          and beside_fixed == fixed_count and beside_literal == 2,
          f"defaults {defaults}; animation export on {fixed_count} key(s), off "
          f"{literal_count}; off warned {warned_off}, on stayed quiet "
          f"{quiet_on}; model export on {beside_fixed}, off {beside_literal}")

    # Named event cues: the game can read them, nothing it ships uses them,
    # and they are stored as offsets into the same table the names live in.
    named = anim_format.Animation(end_frame=20)
    cue_track = anim_format.Track(name="cues")
    cue_track.named_notes = [(0, "step"), (7, "shout"), (19, "land")]
    named.tracks = [cue_track]
    raw = anim_format.write_animation(named)
    reread = anim_format.read_animation(raw)
    check("named event cues survive a write and a read",
          reread.tracks[0].named_notes == cue_track.named_notes
          and reread.tracks[0].flags == anim_format.KEY_NOTE_STRING
          and anim_format.write_animation(reread) == raw,
          f"read back {reread.tracks[0].named_notes}, "
          f"flags 0x{reread.tracks[0].flags:X}")

    anim_io = ls3d_module("5ds.io")
    motion_io = ls3d_module("tck.io")

    # 81 of the game's animations put two keys on one frame. An fcurve cannot
    # hold both and neither can the game - its search steps over the earlier of
    # a pair - so the later one is what survives.
    kept, dropped = anim_io.without_duplicate_frames(
        [(0, "a"), (0, "b"), (5, "c"), (9, "d"), (9, "e")])
    check("two keys on one frame collapse to the one the game plays",
          kept == [(0, "b"), (5, "c"), (9, "e")] and dropped == 2,
          f"kept {kept}, dropped {dropped}")

    # A quaternion that has been through the file is a hair off unit, because
    # its four components are 32-bit floats. Scaling it back to exactly one
    # moves the rotation, and on a frame carrying an offset that shift lands
    # in the position a thousand times larger than the rounding it tidied up.
    # So the export writes what the scene holds, here as everywhere else.
    fresh_scene()
    off_unit = anim_format.Animation(end_frame=4)
    leaning = anim_format.Track(name="leaner")
    leaning.rotations = [(0, (0.9999994, 0.0, 0.0, 0.0)),
                         (4, (0.7071063, 0.7071063, 0.0, 0.0))]
    off_unit.tracks = [leaning]
    source = os.path.join(out_dir, "offunit.5ds")
    anim_format.write_animation_file(off_unit, source)

    bpy.ops.object.empty_add()
    bpy.context.object.name = "leaner"
    loaded = getattr(bpy.ops.import_scene, "5ds")(filepath=source)
    written = os.path.join(out_dir, "offunit_again.5ds")
    saved = getattr(bpy.ops.export_scene, "5ds")(filepath=written)
    back = (anim_format.read_animation_file(written).tracks[0]
            if saved == {"FINISHED"} else None)
    # Compared against the file as written, not the doubles typed above: the
    # format stores 32-bit floats, so those are what went in.
    stored = anim_format.read_animation_file(source).tracks[0]
    worst = 0.0
    if back is not None:
        for (_, one), (_, two) in zip(stored.rotations, back.rotations):
            worst = max(worst, max(abs(a - b) for a, b in zip(one, two)))
    check("a rotation is written back exactly, not scaled to unit length",
          loaded == {"FINISHED"} and saved == {"FINISHED"}
          and back is not None and worst == 0.0,
          f"import={loaded}, export={saved}, worst change {worst:.3e}")

    # Each animation gets an action of its own, named after the file, so a
    # character's whole move set can be loaded at once and picked between in
    # the Action Editor rather than each import wiping the last.
    fresh_scene()
    for name in ("walkish", "jumpish"):
        clip = anim_format.Animation(end_frame=6)
        moving = anim_format.Track(name="mover")
        moving.rotations = [(0, (1.0, 0.0, 0.0, 0.0)),
                            (6, (0.0, 0.0, 1.0, 0.0))]
        clip.tracks = [moving]
        anim_format.write_animation_file(clip,
                                         os.path.join(out_dir, name + ".5ds"))
    bpy.ops.object.empty_add()
    bpy.context.object.name = "mover"
    first = getattr(bpy.ops.import_scene, "5ds")(
        filepath=os.path.join(out_dir, "walkish.5ds"))
    second = getattr(bpy.ops.import_scene, "5ds")(
        filepath=os.path.join(out_dir, "jumpish.5ds"))
    mover = bpy.data.objects["mover"]
    assigned = mover.animation_data.action.name if mover.animation_data else ""
    names = sorted(a.name for a in bpy.data.actions)
    check("each animation lands in its own action, named after the file",
          first == {"FINISHED"} and second == {"FINISHED"}
          and assigned == "jumpish" and "walkish" in names
          and bpy.data.actions["walkish"].use_fake_user,
          f"assigned {assigned!r}, actions {names}")

    # The panel's summary has to say what the scene actually holds, cheaply
    # enough to run from a draw handler.
    summary = anim_io.scene_animation_summary(bpy.context.scene)
    checked = bpy.ops.ls3d.check_animation()
    check("the animation panel counts the scene, and Check agrees it exports",
          summary["tracks"] == [("mover", ["Rotation"])]
          and summary["first"] == 0 and summary["last"] == 6
          and checked == {"FINISHED"},
          f"summary {summary}, check {checked}")

    # A 4DS ends with a count of animated objects, and the game treats
    # anything above zero as "my movement is in the .5ds beside me" - it swaps
    # the extension and loads it. 31 of the game's models say so, and all 31
    # have that file. The import can bring it in when asked, and says something
    # when a model claims to move but arrives without it either way.
    fresh_scene()
    animated = model("9promitac.4ds")
    if os.path.exists(animated):
        loaded = getattr(bpy.ops.import_scene, "4ds")(
            filepath=animated, load_animation=True)
        declared = bpy.context.scene.ls3d_animated_object_count
        # Counted by name: earlier tests leave their own actions behind,
        # since an action with a fake user outlives the objects it drove.
        actions = len([a for a in bpy.data.actions
                       if a.name.startswith("9promitac")])

        # The same model on its own, with no animation next to it.
        alone = os.path.join(out_dir, "9promitac.4ds")
        shutil.copyfile(animated, alone)
        fresh_scene()
        said = []
        original_warn = report_module.Report.warn

        def _watch(self, message, fix=None):
            said.append(message)
            return original_warn(self, message, fix)

        report_module.Report.warn = _watch
        try:
            orphan = getattr(bpy.ops.import_scene, "4ds")(filepath=alone)
        finally:
            report_module.Report.warn = original_warn
        # Off by default: the model alone, with the animation only mentioned.
        # The actions from the run above outlive the scene - a fake user keeps
        # them - so they go before the count means anything.
        fresh_scene()
        for leftover in [a for a in bpy.data.actions
                         if a.name.startswith("9promitac")]:
            bpy.data.actions.remove(leftover)
        quiet = getattr(bpy.ops.import_scene, "4ds")(filepath=animated)
        untouched = len([a for a in bpy.data.actions
                         if a.name.startswith("9promitac")])

        check("an animated model brings its animation when asked, not before",
              loaded == {"FINISHED"} and declared == 2 and actions == 2
              and orphan == {"FINISHED"}
              and quiet == {"FINISHED"} and untouched == 0
              and any("no animation beside it" in m for m in said),
              f"declared {declared}, {actions} action(s) when asked, "
              f"{untouched} when not; alone -> {orphan}, "
              f"warnings {said or 'none'}")
    else:
        SKIPPED.append("an animated model brings its own animation")
        print("[SKIP] an animated model brings its own animation "
              "(9promitac.4ds not in the models folder)", flush=True)

    # A 4DS's animated-object count is not a flag with a spare byte round it:
    # in all 31 of the game's animated models it is exactly the track count of
    # the animation beside them. So it follows the scene as things are keyed -
    # but only upward from nothing, because a model that declares two animated
    # objects still declares them when its movement has not been loaded.
    fresh_scene()
    scene = bpy.context.scene
    scene.ls3d_animated_object_count = 0
    bpy.ops.object.empty_add()
    first_object = bpy.context.object
    first_object.name = "counted"
    first_object.rotation_mode = "QUATERNION"
    first_object.keyframe_insert("rotation_quaternion", frame=0)
    bpy.context.view_layer.update()
    one = scene.ls3d_animated_object_count

    bpy.ops.object.empty_add()
    bpy.context.object.name = "counted2"
    bpy.context.object.keyframe_insert("location", frame=0)
    bpy.context.view_layer.update()
    two = scene.ls3d_animated_object_count

    # Nothing animated: the number stays as the model declared it.
    fresh_scene()
    scene.ls3d_animated_object_count = 5
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.view_layer.update()
    kept = scene.ls3d_animated_object_count

    check("the animated-object count follows the scene, without erasing it",
          one == 1 and two == 2 and kept == 5,
          f"one animated -> {one}, two -> {two}, "
          f"declared 5 with none animated -> {kept}")

    # A .tck holds where the actor travels while an animation plays. It has no
    # key times: a flat array sampled every so many milliseconds, which the
    # game indexes by the clock. The period has to survive, because it is not
    # the animation's own 40 ms - most of the game's tracks sample twice that
    # often, and halving one would change the motion it describes.
    track_format = ls3d_module("tck.codec")
    made = track_format.MotionTrack(duration=200, key_period=20)
    for index in range(made.expected_key_count):
        made.positions.append((index * 0.25, 0.0, index * 0.5))
        made.directions.append((0.0, 0.0, 1.0))
    made.base_position = made.positions[0]
    made.base_direction = made.directions[0]
    raw = track_format.write_track(made)
    reread = track_format.read_track(raw, "made.tck")
    check("a motion track written from nothing reads back the same",
          reread.positions == made.positions
          and reread.directions == made.directions
          and reread.key_period == 20 and reread.duration == 200
          and len(reread.positions) == 11
          and track_format.write_track(reread) == raw,
          f"{len(reread.positions)} sample(s) every {reread.key_period} ms")

    # ... and through Blender: onto a parent empty above the model, off again
    # at whatever period is asked for.
    fresh_scene()
    clip = anim_format.Animation(end_frame=5)
    mover = anim_format.Track(name="traveler")
    mover.rotations = [(0, (1.0, 0.0, 0.0, 0.0)), (5, (1.0, 0.0, 0.0, 0.0))]
    clip.tracks = [mover]
    clip_path = os.path.join(out_dir, "travel.5ds")
    anim_format.write_animation_file(clip, clip_path)
    track_format.write_track_file(made, os.path.join(out_dir, "travel.tck"))

    bpy.ops.object.empty_add()
    bpy.context.object.name = "traveler"
    brought = getattr(bpy.ops.import_scene, "5ds")(filepath=clip_path)
    holders = [o for o in bpy.context.scene.objects
               if motion_io.is_motion_track(o)]
    parented = (holders and bpy.data.objects["traveler"].parent is holders[0])
    period = holders[0].ls3d_motion_period if holders else 0

    # The live toggle mutes the travel rather than throwing it away.
    bag = anim_io.channelbag(holders[0]) if holders else None
    holders[0].ls3d_motion_enabled = False
    muted = all(c.mute for c in bag.fcurves) if bag else False
    holders[0].ls3d_motion_enabled = True
    unmuted = not any(c.mute for c in bag.fcurves) if bag else False

    # Written back out at a period of our choosing.
    bpy.context.scene.ls3d_motion_period = 40
    holders[0].ls3d_motion_period = 40
    again = os.path.join(out_dir, "travel_again.5ds")
    saved = getattr(bpy.ops.export_scene, "5ds")(filepath=again)
    beside = os.path.splitext(again)[0] + ".tck"
    rewritten = (track_format.read_track_file(beside)
                 if os.path.exists(beside) else None)
    check("a motion track rides above the model and can be resampled",
          brought == {"FINISHED"} and parented and period == 20
          and muted and unmuted and saved == {"FINISHED"}
          and rewritten is not None and rewritten.key_period == 40
          and len(rewritten.positions) == rewritten.expected_key_count,
          f"parented={parented}, period in={period}, muted={muted}, "
          f"out={rewritten.key_period if rewritten else None} ms with "
          f"{len(rewritten.positions) if rewritten else 0} sample(s)")

    # The file keeps two reference points the game blends against. Neither
    # can be worked back out of the samples, so a round trip has to carry
    # them rather than guess at the first sample.
    fresh_scene()
    referenced = track_format.MotionTrack(duration=200, key_period=20)
    for index in range(referenced.expected_key_count):
        referenced.positions.append((index * 0.25, 0.0, index * 0.5))
    referenced.base_position = (-0.278607, 0.106, -0.014178)
    referenced.base_direction = (0.225665, 0.104, 0.104784)
    kept_path = os.path.join(out_dir, "reference.tck")
    track_format.write_track_file(referenced, kept_path)

    bpy.ops.object.empty_add()
    bpy.context.object.name = "walker"
    loaded = getattr(bpy.ops.import_scene, "tck")(filepath=kept_path)
    riders = [o for o in bpy.context.scene.objects
              if motion_io.is_motion_track(o)]
    out_path = os.path.join(out_dir, "reference_again.tck")
    resaved = (getattr(bpy.ops.export_scene, "tck")(filepath=out_path)
               if riders else {"CANCELLED"})
    returned = (track_format.read_track_file(out_path)
                if os.path.exists(out_path) else None)
    same = lambda a, b: (a is not None and b is not None
                         and all(abs(x - y) < 1e-6 for x, y in zip(a, b)))
    check("a motion track's reference points come back as they went in",
          loaded == {"FINISHED"} and resaved == {"FINISHED"}
          and returned is not None
          and same(returned.base_position, referenced.base_position)
          and same(returned.base_direction, referenced.base_direction)
          and len(returned.positions) == len(referenced.positions),
          f"base in {referenced.base_position} out "
          f"{returned.base_position if returned else None}; aim in "
          f"{referenced.base_direction} out "
          f"{returned.base_direction if returned else None}; "
          f"{len(returned.positions) if returned else 0} sample(s)")

    # A target frame aims the things linked to it, and a Track To beats
    # whatever an animation says - so a linked object stays pinned on the
    # target instead of turning the way the animation turns it. The aim can be
    # switched off without touching the links, which still export.
    fresh_scene()
    bpy.ops.ls3d.add_target()
    aim = bpy.context.object
    aim.name = "look"
    bpy.ops.mesh.primitive_cube_add()
    watcher = bpy.context.object
    watcher.name = "watcher"
    bpy.context.view_layer.objects.active = aim
    aim.ls3d_target_add_name = "watcher"
    bpy.ops.ls3d.add_target_object()

    def aiming():
        return [not getattr(c, "mute", False) for c in watcher.constraints
                if c.type == "TRACK_TO"]

    made = len(aiming())
    aim.ls3d_target_enabled = False
    released = aiming()
    links_kept = len(aim.ls3d_target_objects)
    aim.ls3d_target_enabled = True
    restored = aiming()
    check("a target's aim can be switched off without losing the link",
          made == 1 and released == [False] and restored == [True]
          and links_kept == 1,
          f"{made} constraint(s); off -> {released}, on -> {restored}, "
          f"{links_kept} link(s) kept")

    # The file dialogs are drawn by hand now, so a typo in one of them would
    # only show up when someone opened it.
    anim_ops = ls3d_module("5ds.ops")
    track_ops = ls3d_module("tck.ops")

    class _Dialog:
        """Stands in for the operator while its draw() is exercised.

        An option it has not heard of reads as off, so a dialog gaining one
        does not need this updated - what is being checked is that draw runs.
        """

        def __init__(self, calls):
            self.layout = _Recorder(calls)
            self.filepath = ""

        def __getattr__(self, _name):
            return False

    def dialog_for(operator, calls):
        """A stub carrying the operator's own helper methods."""
        panel = _Dialog(calls)
        for name in ("chosen",):
            method = getattr(operator, name, None)
            if callable(method):
                setattr(panel, name, method.__get__(panel, _Dialog))
        return panel

    drawn_ok = {}
    for operator, label in ((io_mafia_toolkit.Import4DS, "import 4ds"),
                            (io_mafia_toolkit.Export4DS, "export 4ds"),
                            (anim_ops.Import5DS, "import 5ds"),
                            (anim_ops.Export5DS, "export 5ds"),
                            (track_ops.ImportTCK, "import tck"),
                            (track_ops.ExportTCK, "export tck")):
        calls = []
        try:
            operator.draw(dialog_for(operator, calls), bpy.context)
            drawn_ok[label] = len(calls)
        except Exception as problem:
            drawn_ok[label] = f"failed: {problem}"

    # And again with Selected Objects Only on, since that branch draws more.
    selected_ok = {}
    for operator, label in ((io_mafia_toolkit.Export4DS, "export 4ds"),
                            (anim_ops.Export5DS, "export 5ds"),
                            (track_ops.ExportTCK, "export tck")):
        calls = []
        panel = dialog_for(operator, calls)
        panel.selection_only = True
        try:
            operator.draw(panel, bpy.context)
            selected_ok[label] = len(calls)
        except Exception as problem:
            selected_ok[label] = f"failed: {problem}"

    check("every file dialog draws its own options",
          all(isinstance(v, int) and v >= 2 for v in drawn_ok.values())
          and all(isinstance(v, int) and v >= 2
                  for v in selected_ok.values()),
          f"{drawn_ok}; with Selected Objects Only: {selected_ok}")

    # What makes an empty the movement track is the switch on it, never its
    # name - a name is something a person changes without meaning anything by
    # it. There is one of them, and it is a helper rather than a frame, so it
    # must never turn up in the model.
    fresh_scene()
    bpy.ops.object.empty_add()
    helper = bpy.context.object
    helper.name = "Helper"
    bpy.context.view_layer.objects.active = helper
    bpy.ops.ls3d.mark_motion_track()
    marked = motion_io.is_motion_track(helper)
    # Renaming it changes nothing, which is the point.
    helper.name = "Something Else Entirely"
    still_marked = motion_io.is_motion_track(helper)

    # A .tck holds one track, so marking another hands the job over rather
    # than leaving two for the export to choose between.
    bpy.ops.object.empty_add()
    second = bpy.context.object
    second.name = "The Next One"
    bpy.context.view_layer.objects.active = second
    bpy.ops.ls3d.mark_motion_track()
    handed_over = (motion_io.is_motion_track(second)
                   and not motion_io.is_motion_track(helper))
    # Setting the switch directly has to keep the scene down to one as well.
    bpy.ops.object.empty_add()
    third = bpy.context.object
    third.ls3d_is_motion_track = True
    only_one = [o.name for o in bpy.context.scene.objects
                if motion_io.is_motion_track(o)]

    bpy.context.view_layer.objects.active = third
    bpy.ops.ls3d.mark_motion_track()
    released = motion_io.is_motion_track(third)
    check("the movement track is marked by a switch, and there is one of it",
          marked and still_marked and handed_over
          and only_one == [third.name] and not released,
          f"marked={marked}, still marked after renaming={still_marked}, "
          f"handed over={handed_over}, marked in the scene={only_one}, "
          f"released={not released}")

    # It is an authoring aid: the model must not gain a frame for it.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.object.name = "body"
    bpy.ops.object.empty_add()
    rider = bpy.context.object
    rider.name = "carries the movement"
    rider.ls3d_is_motion_track = True
    model_path = os.path.join(out_dir, "with_track.4ds")
    written = export_op(model_path)
    frame_names = ([f.name for f in read_file(model_path).frames]
                   if written == {"FINISHED"} else [])
    check("the movement track never becomes a frame of the model",
          written == {"FINISHED"} and rider.name not in frame_names
          and "body" in frame_names,
          f"result={written}, frames {frame_names}")

    # Event cues are what the game acts on as an animation plays: a footstep
    # lands, a shot goes off, a car door swings. They are numbers in the file
    # and names in the panel.
    cue_ops = ls3d_module("5ds.ops")
    fresh_scene()
    bpy.ops.ls3d.add_dummy()
    notifier = bpy.context.object
    notifier.name = "notify"
    bpy.context.scene.frame_end = 30
    for at, kind in ((5, "1"), (12, "2"), (20, "40")):
        bpy.context.scene.frame_current = at
        bpy.context.scene.ls3d_event_kind = kind
        bpy.ops.ls3d.add_event_cue()
    listed = [(frame, anim_format.event_label(value))
              for frame, value in cue_ops.event_cues(notifier)]

    bpy.context.scene.frame_current = 12
    bpy.ops.ls3d.remove_event_cue()
    left = cue_ops.event_cues(notifier)

    cue_path = os.path.join(out_dir, "cues.5ds")
    saved = getattr(bpy.ops.export_scene, "5ds")(filepath=cue_path)
    written = None
    if saved == {"FINISHED"}:
        for candidate in anim_format.read_animation_file(cue_path).tracks:
            if candidate.notes:
                written = candidate
    # Cues live on one frame of the model, not on whatever is selected, so
    # the panel has to find them wherever they are - looking at the armature
    # and seeing an empty list reads as "events do not work".
    bpy.ops.object.empty_add()
    elsewhere = bpy.context.object
    elsewhere.name = "somewhere else"
    bpy.context.view_layer.objects.active = elsewhere
    found = cue_ops.event_owner(bpy.context)
    from_elsewhere = cue_ops.event_cues(found) if found else []
    bpy.context.view_layer.objects.active = notifier

    check("event cues are named in the panel and numbered in the file",
          listed == [(5, "Right Footstep"), (12, "Left Footstep"),
                     (20, "Shoot")]
          and left == [(5, 1), (20, 40)]
          and written is not None and written.notes == [(5, 1), (20, 40)]
          and written.flags == anim_format.KEY_NOTE
          and anim_format.event_label(16) == "Event 16"
          and anim_format.event_label(42) == "Fueling Starts"
          and found is notifier and from_elsewhere == left,
          f"listed {listed}; after removing one {left}; "
          f"written {written.notes if written else None}; "
          f"seen from another object on {found.name if found else None}")

    # The game looks its cues up on one frame by name. Anywhere else they are
    # written into the file and read straight back out again, so a round trip
    # looks clean while the animation stays silent in game.
    fresh_scene()
    bpy.ops.ls3d.add_dummy()
    stray = bpy.context.object
    stray.name = anim_format.NOTIFY_TRACK
    bpy.context.scene.frame_end = 20
    bpy.context.scene.frame_current = 5
    bpy.context.scene.ls3d_event_kind = "1"
    bpy.ops.ls3d.add_event_cue()
    # Renaming afterwards is how cues end up somewhere the game never looks.
    stray.name = "left hand"
    stray_path = os.path.join(out_dir, "stray_cues.5ds")
    refused_stray = getattr(bpy.ops.export_scene, "5ds")(filepath=stray_path)
    stray.name = anim_format.NOTIFY_TRACK
    allowed = getattr(bpy.ops.export_scene, "5ds")(filepath=stray_path)
    check("cues on any frame but the notify one refuse to export",
          refused_stray == {"CANCELLED"} and allowed == {"FINISHED"},
          f"on 'left hand' {refused_stray}, "
          f"on '{anim_format.NOTIFY_TRACK}' {allowed}")

    # A few of the game's animations put two cues on the same frame. One
    # curve cannot hold two keys at one frame, so the cues get a slot each.
    fresh_scene()
    bpy.ops.ls3d.add_dummy()
    stacker = bpy.context.object
    stacker.name = anim_format.NOTIFY_TRACK
    bpy.context.scene.frame_end = 20
    for at, kind in ((6, "1"), (6, "40"), (9, "2")):
        bpy.context.scene.frame_current = at
        bpy.context.scene.ls3d_event_kind = kind
        bpy.ops.ls3d.add_event_cue()
    stacked = cue_ops.event_cues(stacker)
    # Asking twice for the same cue on the same frame changes nothing.
    bpy.context.scene.frame_current = 6
    bpy.context.scene.ls3d_event_kind = "1"
    again = bpy.ops.ls3d.add_event_cue()
    stack_path = os.path.join(out_dir, "stacked.5ds")
    stack_written = getattr(bpy.ops.export_scene, "5ds")(filepath=stack_path)
    stack_back = None
    if stack_written == {"FINISHED"}:
        for candidate in anim_format.read_animation_file(stack_path).tracks:
            if candidate.notes:
                stack_back = candidate.notes
    # Removing peels one off at a time rather than emptying the frame.
    bpy.ops.ls3d.remove_event_cue()
    peeled = cue_ops.event_cues(stacker)
    check("two event cues can share a frame and survive the round trip",
          stacked == [(6, 1), (6, 40), (9, 2)] and again == {"CANCELLED"}
          and stack_back == [(6, 1), (6, 40), (9, 2)]
          and len(peeled) == 2 and (6, 40) not in peeled,
          f"in Blender {stacked}, re-added {again}, in the file {stack_back}, "
          f"after removing one {peeled}")

    # The travel belongs in the .tck beside the animation. Writing it into the
    # .5ds as well would animate a frame no model has.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.object.name = "body"
    bpy.context.scene.frame_end = 10
    bpy.ops.object.empty_add()
    travel = bpy.context.object
    travel.name = motion_io.MOTION_NAME
    travel.ls3d_is_motion_track = True
    travel.location = (0.0, 0.0, 0.0)
    travel.keyframe_insert("location", frame=0)
    travel.location = (3.0, 0.0, 0.0)
    travel.keyframe_insert("location", frame=10)
    body = bpy.data.objects["body"]
    body.rotation_mode = "QUATERNION"
    body.keyframe_insert("rotation_quaternion", frame=0)
    travel_path = os.path.join(out_dir, "travel.5ds")
    travel_written = getattr(bpy.ops.export_scene, "5ds")(filepath=travel_path)
    in_file = ([t.name for t in
                anim_format.read_animation_file(travel_path).tracks]
               if travel_written == {"FINISHED"} else [])
    check("the movement track stays out of the animation file",
          travel_written == {"FINISHED"} and motion_io.MOTION_NAME not in in_file
          and "body" in in_file,
          f"result={travel_written}, tracks {in_file}")

    # The flag word the panel shows is read off the curves, not remembered
    # from the import: keying a channel has to change what it says.
    fresh_scene()
    bpy.ops.object.empty_add()
    flagged = bpy.context.object
    flagged.name = "flagged"
    flagged.rotation_mode = "QUATERNION"
    empty_word = anim_io.derived_key_flags(flagged)
    flagged.keyframe_insert("rotation_quaternion", frame=0)
    after_rotation = anim_io.derived_key_flags(flagged)
    flagged.keyframe_insert("location", frame=0)
    after_position = anim_io.derived_key_flags(flagged)
    # A stale number left over from an import must not color the answer.
    flagged.ls3d_anim_flags = 0xFF
    unmoved = anim_io.derived_key_flags(flagged)
    bag = anim_io.channelbag(flagged)
    for curve in [c for c in bag.fcurves
                  if c.data_path == "rotation_quaternion"]:
        bag.fcurves.remove(curve)
    after_clearing = anim_io.derived_key_flags(flagged)
    check("the key flags shown are worked out from the curves each time",
          empty_word == 0
          and after_rotation == anim_format.KEY_ROTATION
          and after_position == (anim_format.KEY_ROTATION
                                 | anim_format.KEY_POSITION)
          and unmoved == after_position
          and after_clearing == anim_format.KEY_POSITION,
          f"none={empty_word}, rotation={after_rotation}, "
          f"both={after_position}, with a stale 0xFF stored={unmoved}, "
          f"after clearing rotation={after_clearing}")

    # Animations are picked from a list in the sidebar. An animation driving
    # several things keeps an action for each, so picking one has to bring the
    # rest of it along.
    fresh_scene()
    for who in ("listwalker", "listhat"):
        bpy.ops.object.empty_add()
        bpy.context.object.name = who
    for clip_name in ("listwalk", "listrun"):
        listed_clip = anim_format.Animation(end_frame=6)
        for who in ("listwalker", "listhat"):
            piece = anim_format.Track(name=who)
            piece.rotations = [(0, (1.0, 0.0, 0.0, 0.0)),
                               (6, (0.0, 1.0, 0.0, 0.0))]
            listed_clip.tracks.append(piece)
        listed_path = os.path.join(out_dir, clip_name + ".5ds")
        anim_format.write_animation_file(listed_clip, listed_path)
        getattr(bpy.ops.import_scene, "5ds")(filepath=listed_path)

    walker = bpy.data.objects["listwalker"]
    hat = bpy.data.objects["listhat"]
    grouped = sorted(a.name for a in
                     anim_io.actions_in_animation(
                         bpy.data.actions["listwalk (listwalker)"]))
    owned = anim_io.object_for_action(bpy.data.actions["listwalk (listhat)"],
                                      bpy.context.scene)
    bpy.context.scene.ls3d_action_index = bpy.data.actions.find(
        "listwalk (listwalker)")
    switched = bpy.ops.ls3d.activate_action()
    both = (walker.animation_data.action.name,
            hat.animation_data.action.name)

    # Renaming is the point of the list, and it must not strand the animation.
    bpy.data.actions["listrun (listwalker)"].name = "Sprint (listwalker)"
    bpy.data.actions["listrun (listhat)"].name = "Sprint (listhat)"
    renamed = bpy.ops.ls3d.activate_action(name="Sprint (listwalker)")
    after = (walker.animation_data.action.name, hat.animation_data.action.name)

    # Switching animations leaves the scene range alone - it is the user's
    # setting, several animations can be on at once, and the export reads it.
    # Fitting the range is a button of its own.
    stretch = anim_format.Animation(end_frame=40)
    stretched = anim_format.Track(name="listwalker")
    stretched.rotations = [(0, (1.0, 0.0, 0.0, 0.0)),
                           (40, (0.0, 1.0, 0.0, 0.0))]
    stretch.tracks = [stretched]
    stretch_path = os.path.join(out_dir, "listlong.5ds")
    anim_format.write_animation_file(stretch, stretch_path)
    getattr(bpy.ops.import_scene, "5ds")(filepath=stretch_path)
    bpy.context.scene.frame_start = 0
    bpy.context.scene.frame_end = 6
    bpy.context.scene.frame_current = 5
    bpy.ops.ls3d.activate_action(name="Sprint (listwalker)")
    short_range = (bpy.context.scene.frame_start,
                   bpy.context.scene.frame_end,
                   bpy.context.scene.frame_current)
    bpy.ops.ls3d.activate_action(name="listlong")
    # Still 0 to 6: picking the longer animation moved nothing.
    held_range = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    fitted = bpy.ops.ls3d.fit_scene_range()
    long_range = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    # Four actions, two animations: the list shows one row each.
    rows = sorted(anim_io.animation_base_name(a)
                  for a in anim_io.leading_actions(
                      [a for a in bpy.data.actions
                       if a.name.startswith(("listwalk", "listrun",
                                             "Sprint"))]))
    check("animations can be picked from the list and renamed",
          rows == ["Sprint", "listwalk"]
          and grouped == ["listwalk (listhat)", "listwalk (listwalker)"]
          and owned is hat and switched == {"FINISHED"}
          and both == ("listwalk (listwalker)", "listwalk (listhat)")
          and renamed == {"FINISHED"}
          and after == ("Sprint (listwalker)", "Sprint (listhat)")
          and short_range == (0, 6, 5) and held_range == (0, 6)
          and fitted == {"FINISHED"} and long_range == (0, 40),
          f"rows {rows}; group {grouped}; '(listhat)' belongs to "
          f"{owned.name if owned else None}; after picking {both}; "
          f"after renaming {after}; short {short_range}, long {long_range}")

    # The animated object count is what sends the engine looking for a .5DS, so
    # zero has to be sayable even in a scene full of keys - that is the state a
    # model is in while its animation is still being built. It used to follow
    # the scene unconditionally, and the handler put the number straight back
    # on the next depsgraph update, so typing zero did nothing at all.
    fresh_scene()
    bpy.ops.object.empty_add()
    keyed = bpy.context.object
    keyed.name = "counted"
    keyed.rotation_mode = "QUATERNION"
    keyed.keyframe_insert("rotation_quaternion", frame=1)
    keyed.rotation_quaternion = (0.0, 0.0, 1.0, 0.0)
    keyed.keyframe_insert("rotation_quaternion", frame=10)
    bpy.context.scene.frame_end = 10
    bpy.context.view_layer.update()

    # fresh_scene leaves the scene properties alone, so start from nothing
    # declared rather than from whatever an earlier check left behind.
    bpy.context.scene.ls3d_animated_count_pinned = False
    bpy.context.scene.ls3d_animated_object_count = 0
    bpy.context.scene.ls3d_animated_count_pinned = False
    anim_io.refresh_animated_object_count(bpy.context.scene)
    followed = bpy.context.scene.ls3d_animated_object_count
    pinned_yet = bpy.context.scene.ls3d_animated_count_pinned

    # Typing a number pins it, and nothing may raise it back.
    bpy.context.scene.ls3d_animated_object_count = 0
    now_pinned = bpy.context.scene.ls3d_animated_count_pinned
    for _ in range(3):
        bpy.context.view_layer.update()
        anim_io.refresh_animated_object_count(bpy.context.scene)
    stayed = bpy.context.scene.ls3d_animated_object_count

    # And the number that reaches the file is the one on screen.
    count_path = os.path.join(out_dir, "pinned_count.4ds")
    wrote_zero = export_op(count_path) == {"FINISHED"}
    in_file = read_file(count_path).animated_object_count if wrote_zero else -1

    # Handing it back makes it follow again.
    released = bpy.ops.ls3d.follow_animated_count()
    following_again = (not bpy.context.scene.ls3d_animated_count_pinned
                       and bpy.context.scene.ls3d_animated_object_count
                       == followed)

    check("a hand-typed animated object count stays where it is put",
          followed >= 1 and not pinned_yet
          and now_pinned and stayed == 0
          and wrote_zero and in_file == 0
          and released == {"FINISHED"} and following_again,
          f"counted {followed}, pinned before typing {pinned_yet}; after "
          f"typing 0 pinned={now_pinned} and three refreshes left {stayed}; "
          f"file says {in_file}; released {released}, following again "
          f"{following_again}")

    # Cues are only offered on the frame the game reads them from, so they
    # cannot be put somewhere the export will then refuse.
    fresh_scene()
    bpy.ops.object.empty_add()
    elsewhere_obj = bpy.context.object
    elsewhere_obj.name = "l_hand"
    bpy.ops.ls3d.add_dummy()
    notify_obj = bpy.context.object
    notify_obj.name = anim_format.NOTIFY_TRACK
    bpy.context.scene.frame_end = 20
    bpy.context.scene.frame_current = 5
    bpy.context.view_layer.objects.active = elsewhere_obj
    offered_elsewhere = bpy.ops.ls3d.add_event_cue.poll()
    # A plain Blender empty is not a Dummy - it comes in as a Target frame -
    # so the name alone must not be enough.
    bpy.ops.object.empty_add()
    impostor = bpy.context.object
    notify_obj.name = "the real one"
    impostor.name = anim_format.NOTIFY_TRACK
    bpy.context.view_layer.objects.active = impostor
    offered_on_impostor = bpy.ops.ls3d.add_event_cue.poll()
    impostor.name = "not it"
    notify_obj.name = anim_format.NOTIFY_TRACK
    bpy.context.view_layer.objects.active = notify_obj
    offered_on_notify = bpy.ops.ls3d.add_event_cue.poll()
    bpy.context.scene.ls3d_event_kind = "1"
    bpy.ops.ls3d.add_event_cue()
    landed = cue_ops.event_cues(notify_obj)
    strays_none = cue_ops.stray_cue_owners(bpy.context.scene)
    notify_obj.name = "moved away"
    strays_now = [o.name for o in cue_ops.stray_cue_owners(bpy.context.scene)]
    check("cues can only be added to a Dummy frame called notify",
          offered_elsewhere is False and offered_on_notify is True
          and offered_on_impostor is False
          and landed == [(5, 1)] and strays_none == []
          and strays_now == ["moved away"],
          f"offered on 'l_hand'={offered_elsewhere}, on a plain empty "
          f"named notify={offered_on_impostor}, on the Dummy="
          f"{offered_on_notify}, landed {landed}, strays {strays_now}")

    # An imported animation keeps a fake user so a whole move set survives
    # saving, which means nothing drops it without being asked.
    fresh_scene()
    for who in ("delwalker", "delhat"):
        bpy.ops.object.empty_add()
        bpy.context.object.name = who
    for clip_name in ("delkeep", "deldrop"):
        del_clip = anim_format.Animation(end_frame=6)
        for who in ("delwalker", "delhat"):
            piece = anim_format.Track(name=who)
            piece.rotations = [(0, (1.0, 0.0, 0.0, 0.0)),
                               (6, (0.0, 1.0, 0.0, 0.0))]
            del_clip.tracks.append(piece)
        del_path = os.path.join(out_dir, clip_name + ".5ds")
        anim_format.write_animation_file(del_clip, del_path)
        getattr(bpy.ops.import_scene, "5ds")(filepath=del_path)
    mine = lambda: sorted(a.name for a in bpy.data.actions
                          if a.name.startswith(("delkeep", "deldrop")))
    before_delete = mine()
    dropped = bpy.ops.ls3d.delete_action(name="deldrop (delwalker)")
    after_delete = mine()
    walker_now = bpy.data.objects["delwalker"].animation_data
    check("an animation can be deleted, every part of it at once",
          before_delete == ["delcrop"] * 0 + ["deldrop (delhat)",
                                              "deldrop (delwalker)",
                                              "delkeep (delhat)",
                                              "delkeep (delwalker)"]
          and dropped == {"FINISHED"}
          and after_delete == ["delkeep (delhat)", "delkeep (delwalker)"]
          and (walker_now is None or walker_now.action is None),
          f"before {before_delete}; after {after_delete}; "
          f"walker holds "
          f"{walker_now.action.name if walker_now and walker_now.action else None}")

    # Cues carrying a word rather than a number. Text cannot sit on a curve,
    # so these are a list on the frame - but the export must read that list as
    # it stands, never anything remembered from the import.
    fresh_scene()
    worded = anim_format.Animation(end_frame=20)
    worded_track = anim_format.Track(name=anim_format.NOTIFY_TRACK)
    worded_track.named_notes = [(3, "left step"), (9, "reload"), (14, "shout")]
    worded.tracks = [worded_track]
    worded_path = os.path.join(out_dir, "worded.5ds")
    anim_format.write_animation_file(worded, worded_path)

    bpy.ops.ls3d.add_dummy()
    worded_frame = bpy.context.object
    worded_frame.name = anim_format.NOTIFY_TRACK
    getattr(bpy.ops.import_scene, "5ds")(filepath=worded_path)
    read_in = cue_ops.named_events(worded_frame)

    # Edit every way there is: change one, drop one, add one.
    worded_frame.ls3d_named_events[1].text = "CHANGED"
    worded_frame.ls3d_named_events[1].frame = 11
    worded_frame.ls3d_named_events.remove(2)
    bpy.context.view_layer.objects.active = worded_frame
    bpy.context.scene.frame_current = 18
    bpy.ops.ls3d.add_named_event()
    worded_frame.ls3d_named_events[-1].text = "brand new"

    worded_out = os.path.join(out_dir, "worded_out.5ds")
    worded_saved = getattr(bpy.ops.export_scene, "5ds")(filepath=worded_out)
    written_back = (anim_format.read_animation_file(worded_out).tracks[0]
                    .named_notes if worded_saved == {"FINISHED"} else None)

    # A track holds one kind of cue or the other, so both together is refused.
    bpy.context.scene.frame_current = 5
    bpy.context.scene.ls3d_event_kind = "1"
    bpy.ops.ls3d.add_event_cue()
    both = getattr(bpy.ops.export_scene, "5ds")(filepath=worded_out)

    check("named events are read, edited and written back as they stand",
          read_in == [(3, "left step"), (9, "reload"), (14, "shout")]
          and worded_saved == {"FINISHED"}
          and written_back == [(3, "left step"), (11, "CHANGED"),
                               (18, "brand new")]
          and both == {"CANCELLED"},
          f"read {read_in}; written back {written_back}; "
          f"with both kinds of cue {both}")

    # A model, its animation and its travel are three files the game finds by
    # sharing a name, so each is written beside the one the user named.
    fresh_scene()
    beside_dir = os.path.join(out_dir, "beside")
    os.makedirs(beside_dir, exist_ok=True)
    for stale in os.listdir(beside_dir):
        os.remove(os.path.join(beside_dir, stale))
    bpy.ops.mesh.primitive_cube_add()
    beside_body = bpy.context.object
    beside_body.name = "body"
    beside_body.rotation_mode = "QUATERNION"
    bpy.context.scene.frame_start = 0
    bpy.context.scene.frame_end = 10
    beside_body.keyframe_insert("rotation_quaternion", frame=0)
    beside_body.keyframe_insert("rotation_quaternion", frame=10)
    bpy.ops.object.empty_add()
    beside_track = bpy.context.object
    beside_track.name = motion_io.MOTION_NAME
    beside_track.ls3d_is_motion_track = True
    beside_track.keyframe_insert("location", frame=0)
    beside_track.location = (2.0, 0.0, 0.0)
    beside_track.keyframe_insert("location", frame=10)

    anim_written = getattr(bpy.ops.export_scene, "5ds")(
        filepath=os.path.join(beside_dir, "MyWalk.5ds"), write_motion=True)
    after_anim = sorted(os.listdir(beside_dir))

    # Writing the animation alongside is off unless asked for: exporting a
    # model is usually about the model, and it would overwrite the .5ds
    # already sitting beside it.
    getattr(bpy.ops.export_scene, "4ds")(
        filepath=os.path.join(beside_dir, "Quiet.4ds"))
    after_default = sorted(os.listdir(beside_dir))
    model_written = getattr(bpy.ops.export_scene, "4ds")(
        filepath=os.path.join(beside_dir, "MyModel.4ds"), write_animation=True)
    after_model = sorted(os.listdir(beside_dir))

    # With nothing animated there is no animation to put beside the model.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.object.name = "still"
    static_written = getattr(bpy.ops.export_scene, "4ds")(
        filepath=os.path.join(beside_dir, "Static.4ds"), write_animation=True)
    after_static = sorted(os.listdir(beside_dir))
    check("a model, its animation and its travel are written under one name",
          anim_written == {"FINISHED"}
          and after_anim == ["MyWalk.5ds", "MyWalk.tck"]
          and "Quiet.4ds" in after_default
          and "Quiet.5ds" not in after_default
          and model_written == {"FINISHED"}
          and "MyModel.5ds" in after_model
          and static_written == {"FINISHED"}
          and "Static.5ds" not in after_static,
          f"after the animation {after_anim}; with the toggle left off "
          f"{after_default}; after the model {after_model}; "
          f"after a model with nothing animated {after_static}")

    # Neither export is offered at all when there is nothing of its kind to
    # write - grayed out in the menu rather than clickable and then refused.
    fresh_scene()
    offered_empty = (getattr(bpy.ops.export_scene, "5ds").poll(),
                     bpy.ops.export_scene.tck.poll())
    bpy.ops.mesh.primitive_cube_add()
    inert = bpy.context.object
    inert.name = "inert"
    offered_unkeyed = (getattr(bpy.ops.export_scene, "5ds").poll(),
                       bpy.ops.export_scene.tck.poll())

    # A scene where only the travel is keyed has a .tck to write but no .5ds.
    bpy.ops.object.empty_add()
    lone_track = bpy.context.object
    lone_track.name = motion_io.MOTION_NAME
    lone_track.ls3d_is_motion_track = True
    lone_track.keyframe_insert("location", frame=0)
    lone_track.location = (1.0, 0.0, 0.0)
    lone_track.keyframe_insert("location", frame=10)
    offered_motion_only = (getattr(bpy.ops.export_scene, "5ds").poll(),
                           bpy.ops.export_scene.tck.poll())
    counted_motion_only = anim_io.scene_animation_summary(
        bpy.context.scene, frames=False)["tracks"]

    inert.rotation_mode = "QUATERNION"
    inert.keyframe_insert("rotation_quaternion", frame=0)
    offered_keyed = (getattr(bpy.ops.export_scene, "5ds").poll(),
                     bpy.ops.export_scene.tck.poll())

    # A named cue is a list on a frame rather than keys on a curve, so an
    # animation made only of those has to count as one all the same.
    fresh_scene()
    bpy.ops.ls3d.add_dummy()
    wordy = bpy.context.object
    wordy.name = anim_format.NOTIFY_TRACK
    offered_bare = getattr(bpy.ops.export_scene, "5ds").poll()
    lone_cue = wordy.ls3d_named_events.add()
    lone_cue.frame = 5
    lone_cue.text = "reload"
    offered_named_only = getattr(bpy.ops.export_scene, "5ds").poll()
    check("an export is only offered when it has something to write",
          offered_empty == (False, False)
          and offered_unkeyed == (False, False)
          and offered_motion_only == (False, True)
          and counted_motion_only == []
          and offered_keyed == (True, True)
          and offered_bare is False and offered_named_only is True,
          f"empty scene {offered_empty}; unkeyed model {offered_unkeyed}; "
          f"only the travel keyed {offered_motion_only} counting "
          f"{counted_motion_only}; model keyed {offered_keyed}; "
          f"bare notify {offered_bare}, with one named cue "
          f"{offered_named_only}")

    # Cues are drawn in their own color so they read apart from movement
    # keys, and one can be sent to a frame that is typed rather than scrubbed.
    fresh_scene()
    bpy.ops.ls3d.add_dummy()
    colored = bpy.context.object
    colored.name = anim_format.NOTIFY_TRACK
    bpy.context.scene.frame_end = 30
    for at, kind in ((5, "1"), (12, "2")):
        bpy.context.scene.frame_current = at
        bpy.context.scene.ls3d_event_kind = kind
        bpy.ops.ls3d.add_event_cue()
    colored_bag = anim_io.channelbag(colored)
    cue_types = [p.type for p in
                 anim_io.curve(colored_bag,
                               f'["{anim_io.NOTE_PROPERTY}"]', 0)
                 .keyframe_points]
    colored.rotation_mode = "QUATERNION"
    colored.keyframe_insert("rotation_quaternion", frame=0)
    move_types = [p.type for p in
                  anim_io.curve(colored_bag, "rotation_quaternion", 0)
                  .keyframe_points]
    bpy.ops.ls3d.move_event_cue(frame=12, to_frame=20)
    moved = cue_ops.event_cues(colored)
    # The color is presentation only - the file must not notice it.
    colored_path = os.path.join(out_dir, "colored.5ds")
    colored_saved = getattr(bpy.ops.export_scene, "5ds")(filepath=colored_path)
    colored_notes = None
    if colored_saved == {"FINISHED"}:
        for candidate in anim_format.read_animation_file(colored_path).tracks:
            if candidate.notes:
                colored_notes = candidate.notes
    check("cues are drawn apart from movement keys and can be typed to a frame",
          cue_types == [anim_io.CUE_KEY_TYPE] * 2
          and move_types == ["KEYFRAME"]
          and moved == [(5, 1), (20, 2)]
          and colored_notes == [(5, 1), (20, 2)],
          f"cue keys {cue_types}, movement keys {move_types}, "
          f"after moving {moved}, in the file {colored_notes}")

    # The flag word a 5DS track starts with, shown behind the addon
    # preference. On automatic flags it is worked out from the keys, so what
    # it shows has to follow them rather than anything stored.
    fresh_scene()
    bpy.ops.object.empty_add()
    raw_target = bpy.context.object
    raw_target.name = "raw thing"
    raw_target.rotation_mode = "QUATERNION"
    raw_target.keyframe_insert("rotation_quaternion", frame=0)
    raw_target.keyframe_insert("location", frame=0)

    class _RawRecorder:
        """Collects what the panel would draw, so it can be read headlessly."""

        def __init__(self):
            self.lines = []
            self.enabled = True

        def box(self):
            return self

        def row(self, **_kwargs):
            return self

        def column(self, **_kwargs):
            return self

        def label(self, text="", icon="", **_kwargs):
            self.lines.append(("label", text, icon))

        def prop(self, _target, name, text="", **_kwargs):
            self.lines.append(("prop", name, text))

    ui_5ds = ls3d_module("5ds.ui")

    def raw_lines(show, automatic):
        original = io_mafia_toolkit.get_preferences
        io_mafia_toolkit.get_preferences = lambda: type(
            "P", (), {"show_raw_flags": show, "show_reserved_flags": False})()
        try:
            raw_target.ls3d_anim_auto_flags = automatic
            recorder = _RawRecorder()
            ui_5ds.The5DSAnimationPanel._draw_raw_flags(
                ui_5ds.The5DSAnimationPanel, recorder, bpy.context,
                raw_target, raw_target, None)
            return recorder.lines
        finally:
            io_mafia_toolkit.get_preferences = original

    hidden = raw_lines(False, True)
    automatic_lines = raw_lines(True, True)
    raw_target.ls3d_anim_flags = 0x2A
    manual_lines = raw_lines(True, False)
    raw_target.ls3d_anim_flags = 0x41
    odd_lines = raw_lines(True, False)
    check("the raw 5DS flag word appears only behind the preference",
          hidden == []
          and ("label", "0x00000006", "") in automatic_lines
          and ("label", "Rotation + Position", "") in automatic_lines
          and not any(kind == "prop" for kind, *_ in automatic_lines)
          and ("prop", "ls3d_anim_flags_str", "Value") in manual_lines
          and ("label", "Position + Scale + Named Events", "") in manual_lines
          and any(icon == "ERROR" for _k, _t, icon in odd_lines),
          f"hidden {hidden}; automatic {automatic_lines}; "
          f"manual {manual_lines}; undefined bit {odd_lines}")

    # Selected Objects Only means the same thing in all three formats.
    fresh_scene()
    sel_dir = os.path.join(out_dir, "selection")
    os.makedirs(sel_dir, exist_ok=True)
    for stale in os.listdir(sel_dir):
        os.remove(os.path.join(sel_dir, stale))
    bpy.context.scene.frame_start = 0
    bpy.context.scene.frame_end = 10
    for name in ("keepme", "leaveme"):
        bpy.ops.mesh.primitive_cube_add()
        picked = bpy.context.object
        picked.name = name
        picked.rotation_mode = "QUATERNION"
        picked.keyframe_insert("rotation_quaternion", frame=0)
        picked.keyframe_insert("rotation_quaternion", frame=10)
    bpy.ops.object.empty_add()
    sel_track = bpy.context.object
    sel_track.name = motion_io.MOTION_NAME
    sel_track.ls3d_is_motion_track = True
    sel_track.keyframe_insert("location", frame=0)
    sel_track.location = (2.0, 0.0, 0.0)
    sel_track.keyframe_insert("location", frame=10)

    bpy.ops.object.select_all(action="DESELECT")
    bpy.data.objects["keepme"].select_set(True)

    model_path = os.path.join(sel_dir, "sel.4ds")
    getattr(bpy.ops.export_scene, "4ds")(filepath=model_path,
                                         selection_only=True,
                                         write_animation=False)
    model_frames = [f.name for f in read_file(model_path).frames]

    clip_path = os.path.join(sel_dir, "sel.5ds")
    getattr(bpy.ops.export_scene, "5ds")(filepath=clip_path,
                                         selection_only=True,
                                         write_motion=False)
    picked_tracks = [t.name for t in
                     anim_format.read_animation_file(clip_path).tracks]
    whole_path = os.path.join(sel_dir, "whole.5ds")
    getattr(bpy.ops.export_scene, "5ds")(filepath=whole_path,
                                         write_motion=False)
    whole_tracks = sorted(t.name for t in
                          anim_format.read_animation_file(whole_path).tracks)

    # A .tck written on its own takes the scene's one track whatever is
    # selected, since there is nothing to choose between.
    track_path = os.path.join(sel_dir, "sel.tck")
    bpy.ops.object.select_all(action="DESELECT")
    alone = getattr(bpy.ops.export_scene, "tck")(filepath=track_path)

    # Written beside an animation it follows that export's own selection.
    beside_5ds = os.path.join(sel_dir, "beside.5ds")
    bpy.data.objects["keepme"].select_set(True)
    getattr(bpy.ops.export_scene, "5ds")(filepath=beside_5ds,
                                         selection_only=True,
                                         write_motion=True)
    without_track = os.path.exists(os.path.join(sel_dir, "beside.tck"))
    sel_track.select_set(True)
    with_5ds = os.path.join(sel_dir, "with.5ds")
    getattr(bpy.ops.export_scene, "5ds")(filepath=with_5ds,
                                         selection_only=True,
                                         write_motion=True)
    with_track = os.path.exists(os.path.join(sel_dir, "with.tck"))
    check("Selected Objects Only narrows every format the same way",
          model_frames == ["keepme"]
          and picked_tracks == ["keepme"]
          and whole_tracks == ["keepme", "leaveme"]
          and alone == {"FINISHED"}
          and without_track is False and with_track is True,
          f"4ds frames {model_frames}; 5ds selected {picked_tracks} vs whole "
          f"{whole_tracks}; tck on its own {alone}; beside a selected export "
          f"without the track={without_track}, with it={with_track}")

    # Selected Objects Only has to reach everything an export says, not just
    # what it writes. The animation's easing and long-turn checks and its
    # movement-parenting check all looked at the whole scene, so leaving an
    # object out still produced warnings about it - and an armature left out
    # could refuse the export outright. The model export's Write Its Animation
    # ignored the selection entirely and wrote every animated object beside a
    # model of only some of them.
    import contextlib as _contextlib
    import io as _stringio

    def _report_of(call):
        heard = _stringio.StringIO()
        with _contextlib.redirect_stdout(heard):
            outcome = call()
        return outcome, [line for line in heard.getvalue().splitlines()
                         if line.startswith("[MBT/")]

    def _keyed_cube(name, x, eased, degrees):
        bpy.ops.mesh.primitive_cube_add(location=(x, 0.0, 0.0))
        cube = bpy.context.object
        cube.name = name
        cube.rotation_mode = "XYZ"
        cube.keyframe_insert("rotation_euler", frame=0)
        cube.rotation_euler = (0.0, math.radians(degrees), 0.0)
        cube.keyframe_insert("rotation_euler", frame=40)
        for fcurve in ls3d_module("5ds.io").channelbag(cube).fcurves:
            for point in fcurve.keyframe_points:
                point.interpolation = "BEZIER" if eased else "LINEAR"
        return cube

    fresh_scene()
    bpy.context.scene.render.fps = 25
    bpy.context.scene.render.fps_base = 1.0
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 0, 40
    chosen_cube = _keyed_cube("CHOSEN", 0.0, eased=False, degrees=45.0)
    _keyed_cube("LEFT_OUT", 4.0, eased=True, degrees=270.0)
    for name, x in (("CHOSEN_SHADOW", 0.0), ("LEFT_OUT_SHADOW", 4.0)):
        bpy.ops.mesh.primitive_plane_add(location=(x, 0.0, -1.0))
        bpy.context.object.name = name
        bpy.context.object.ls3d_is_shadow = True
    for obj in bpy.context.scene.objects:
        obj.select_set(obj.name.startswith("CHOSEN"))
    bpy.context.view_layer.objects.active = chosen_cube

    anim_sel = os.path.join(sel_dir, "selected_only.5ds")
    anim_outcome, anim_lines = _report_of(
        lambda: getattr(bpy.ops.export_scene, "5ds")(filepath=anim_sel,
                                                     selection_only=True))
    anim_tracks = [t.name for t in
                   ls3d_module("5ds.codec").read_animation_file(anim_sel).tracks]

    model_sel = os.path.join(sel_dir, "selected_only.4ds")
    model_outcome, model_lines = _report_of(
        lambda: getattr(bpy.ops.export_scene, "4ds")(
            filepath=model_sel, selection_only=True, write_animation=True))
    beside_tracks = [t.name for t in ls3d_module("5ds.codec").read_animation_file(
        model_sel[:-4] + ".5ds").tracks]

    shadow_sel = os.path.join(sel_dir, "selected_only.6ds")
    shadow_outcome, shadow_lines = _report_of(
        lambda: getattr(bpy.ops.export_scene, "6ds")(filepath=shadow_sel,
                                                     selection_only=True))

    def _mentions_left_out(lines):
        return [line for line in lines if "LEFT_OUT" in line
                or "bezier" in line]

    check("Selected Objects Only reaches every check and report, not just the file",
          anim_outcome == {"FINISHED"} and anim_tracks == ["CHOSEN"]
          and not _mentions_left_out(anim_lines)
          and model_outcome == {"FINISHED"} and beside_tracks == ["CHOSEN"]
          and not _mentions_left_out(model_lines)
          and shadow_outcome == {"FINISHED"}
          and any("Selected objects:" in line for line in shadow_lines)
          and not _mentions_left_out(shadow_lines),
          f"animation {anim_outcome} tracks {anim_tracks}, stray lines "
          f"{_mentions_left_out(anim_lines)}; model {model_outcome} animation "
          f"beside it {beside_tracks}, stray lines "
          f"{_mentions_left_out(model_lines)}; shadow {shadow_outcome} says "
          f"selection {any('Selected objects:' in l for l in shadow_lines)}")

    # An armature that is not under the movement track is an error - but only
    # for an armature that is being exported. One left out by the selection
    # must not refuse an export that never touches it.
    fresh_scene()
    bpy.context.scene.render.fps = 25
    bpy.context.scene.render.fps_base = 1.0
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 0, 40
    bpy.ops.object.empty_add()
    track_empty = bpy.context.object
    track_empty.name = "movement"
    track_empty.ls3d_is_motion_track = True
    track_empty.keyframe_insert("location", frame=0)
    track_empty.location = (0.0, 5.0, 0.0)
    track_empty.keyframe_insert("location", frame=40)
    bpy.ops.object.armature_add(location=(8.0, 0.0, 0.0))
    stray_rig = bpy.context.object
    stray_rig.name = "stray_rig"                       # not under the track
    prop = _keyed_cube("prop", 0.0, eased=False, degrees=45.0)
    for obj in bpy.context.scene.objects:
        obj.select_set(obj is prop)
    bpy.context.view_layer.objects.active = prop

    prop_path = os.path.join(sel_dir, "prop_only.5ds")
    prop_outcome, prop_lines = _report_of(
        lambda: getattr(bpy.ops.export_scene, "5ds")(filepath=prop_path,
                                                     selection_only=True,
                                                     write_motion=False))
    whole_outcome, whole_lines = _report_of(
        lambda: getattr(bpy.ops.export_scene, "5ds")(
            filepath=os.path.join(sel_dir, "whole_scene.5ds"),
            write_motion=False))
    check("an armature the selection left out cannot refuse the export",
          prop_outcome == {"FINISHED"}
          and not any("stray_rig" in line for line in prop_lines)
          and whole_outcome == {"CANCELLED"}
          and any("stray_rig" in line for line in whole_lines),
          f"prop alone {prop_outcome}, mentions the rig "
          f"{any('stray_rig' in l for l in prop_lines)}; whole scene "
          f"{whole_outcome}, names the rig "
          f"{any('stray_rig' in l for l in whole_lines)}")

    # The flag word is the file's full 32 bits, and the checkboxes are views
    # onto it - typing a value moves them, ticking one moves the value.
    fresh_scene()
    bpy.ops.object.empty_add()
    wide = bpy.context.object
    wide.name = "wide"
    wide.ls3d_anim_auto_flags = False
    wide.ls3d_anim_flags_str = "0x80000000"
    kept_high = wide.ls3d_anim_flags_str
    wide.ls3d_anim_flags_str = "0x2A"
    lit = sorted(attr for _m, attr, _l, _d in anim_format.KEY_FLAG_TABLE
                 if getattr(wide, f"af_{attr}"))
    wide.ls3d_anim_flags_str = "0x0"
    wide.af_scale = True
    from_toggle = wide.ls3d_anim_flags_str
    check("the 5DS flag word holds all 32 bits and drives the checkboxes",
          kept_high == "0x80000000"
          and lit == ["note_string", "position", "scale"]
          and from_toggle == "0x00000008",
          f"bit 31 kept as {kept_high}; 0x2A lights {lit}; "
          f"ticking Scale gives {from_toggle}")

    # The game plays one frame every 40 ms and nothing else, so the sidebar
    # offers to put the scene on that rate when it is not already there.
    fresh_scene()
    # Set it wrong on purpose: an earlier import may already have put the
    # scene on 25, and fresh_scene keeps the render settings.
    bpy.context.scene.render.fps = 24
    bpy.context.scene.render.fps_base = 1.0
    default_rate = anim_io.scene_frame_rate(bpy.context.scene)
    offered_wrong = bpy.ops.ls3d.set_frame_rate.poll()
    bpy.ops.ls3d.set_frame_rate()
    after_press = anim_io.scene_frame_rate(bpy.context.scene)
    offered_right = bpy.ops.ls3d.set_frame_rate.poll()
    # A rate carried in fps_base, the way 23.976 and its kin are, has to be
    # cleared as well rather than left dividing the new one.
    bpy.context.scene.render.fps = 24
    bpy.context.scene.render.fps_base = 1.001
    awkward = round(anim_io.scene_frame_rate(bpy.context.scene), 3)
    bpy.ops.ls3d.set_frame_rate()
    settled = (anim_io.scene_frame_rate(bpy.context.scene),
               bpy.context.scene.render.fps_base)
    check("the sidebar can put the scene on the game's frame rate",
          offered_wrong is True and after_press == anim_format.FRAMES_PER_SECOND
          and offered_right is False and awkward == 23.976
          and settled == (anim_format.FRAMES_PER_SECOND, 1.0),
          f"scene started at {default_rate:g}, offered={offered_wrong}, "
          f"became {after_press:g}, offered again={offered_right}; "
          f"{awkward} fps settled to {settled}")

    # The sidebar is redrawn constantly - every hover redraws it - so a name
    # the draw only binds on one branch is an error repeating forever. Draw it
    # against everything it can be pointed at.
    fresh_scene()
    ui_5ds = ls3d_module("5ds.ui")

    class _Layout:
        """Swallows a panel's drawing calls and counts them."""

        def __init__(self):
            self.rows = 0
            self.enabled = True
            self.alert = False
            self.active = True
            self.alignment = ""
            self.scale_y = 1.0

        def box(self):
            return self

        def row(self, **_kwargs):
            return self

        def column(self, **_kwargs):
            return self

        def grid_flow(self, **_kwargs):
            return self

        def split(self, **_kwargs):
            return self

        def separator(self, **_kwargs):
            pass

        def label(self, **_kwargs):
            self.rows += 1

        def prop(self, *_args, **_kwargs):
            self.rows += 1

        def template_list(self, *_args, **_kwargs):
            self.rows += 1

        def operator(self, *_args, **_kwargs):
            self.rows += 1
            return type("Button", (), {"frame": 0, "name": ""})()

        def panel(self, *_args, **_kwargs):
            return self, self

    def sidebar_draws():
        """Run the sidebar's draw, returning the failure or the row count."""
        body = {k: v for k, v in ui_5ds.The5DSAnimationPanel.__dict__.items()
                if not k.startswith("bl_")}
        panel = type("Panel", (), body)()
        panel.layout = _Layout()
        try:
            ui_5ds.The5DSAnimationPanel.draw(panel, bpy.context)
            return panel.layout.rows
        except Exception as problem:      # noqa: BLE001 - reported by check
            return f"{type(problem).__name__}: {problem}"

    drew = {}
    bpy.ops.mesh.primitive_cube_add()
    plain = bpy.context.object
    plain.name = "plain"
    drew["mesh"] = sidebar_draws()
    plain.rotation_mode = "QUATERNION"
    plain.keyframe_insert("rotation_quaternion", frame=0)
    drew["mesh with a key"] = sidebar_draws()
    bpy.ops.object.empty_add()
    bpy.context.object.ls3d_is_motion_track = True
    drew["movement track"] = sidebar_draws()
    bpy.ops.ls3d.add_dummy()
    bpy.context.object.name = anim_format.NOTIFY_TRACK
    drew["notify dummy"] = sidebar_draws()
    bpy.ops.object.armature_add()
    drew["armature"] = sidebar_draws()
    bpy.ops.object.mode_set(mode="POSE")
    drew["armature posing"] = sidebar_draws()
    bpy.ops.object.mode_set(mode="OBJECT")
    check("the 5DS sidebar draws whatever it is pointed at",
          all(isinstance(v, int) and v > 0 for v in drew.values()),
          f"{drew}")

    # Making an animation active leaves the scene range exactly as it is, for
    # one key or for thirty - it is the user's setting and the export reads it.
    # Fitting the range to the keys is its own button.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    single = bpy.context.object
    single.rotation_mode = "QUATERNION"
    single.keyframe_insert("rotation_quaternion", frame=0)
    # Name it, rather than trusting the list index: actions from earlier
    # checks outlive fresh_scene and would be picked instead.
    single_action = single.animation_data.action.name
    was = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    bpy.ops.ls3d.activate_action(name=single_action)
    kept = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    single.keyframe_insert("rotation_quaternion", frame=30)
    bpy.ops.ls3d.activate_action(name=single_action)
    held_still = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    fitted_once = bpy.ops.ls3d.fit_scene_range()
    stretched = (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    check("making an animation active leaves the scene range alone",
          kept == was and held_still == was
          and fitted_once == {"FINISHED"} and stretched == (0, 30),
          f"range was {was}, after a single key {kept}, after a key at 30 "
          f"{held_still}, after fitting {stretched}")

    # An animation can be started from the sidebar as well as imported.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    starter = bpy.context.object
    starter.name = "startbody"
    started = bpy.ops.ls3d.add_action(name="startwalk")
    first_named = starter.animation_data.action.name
    bpy.ops.ls3d.add_action(name="startrun")
    both = sorted(a.name for a in bpy.data.actions
                  if a.name.startswith("start"))

    # Several objects at once share the animation's name, and the movement
    # track is never swept into it - its travel belongs in the .tck.
    bpy.ops.object.empty_add()
    bpy.context.object.name = "starthat"
    bpy.ops.object.empty_add()
    started_track = bpy.context.object
    started_track.name = "starttrack"
    started_track.ls3d_is_motion_track = True
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.ls3d.add_action(name="startjump")
    shared = sorted(a.name for a in bpy.data.actions
                    if a.name.startswith("startjump"))
    check("an animation can be started from the sidebar",
          started == {"FINISHED"} and first_named == "startwalk"
          and both == ["startrun", "startwalk"]
          and shared == ["startjump (startbody)", "startjump (starthat)"],
          f"first {first_named}; two of them {both}; "
          f"across a selection {shared}")

    # Apply Movement mutes the travel so a walk cycle can be worked on in
    # place. It must not touch the transform: writing a zero in would move the
    # model, and that zero is what would stay on screen when it is switched
    # back on.
    fresh_scene()
    bpy.context.scene.frame_start = 0
    bpy.context.scene.frame_end = 10
    bpy.ops.object.empty_add()
    traveling = bpy.context.object
    traveling.name = motion_io.MOTION_NAME
    traveling.ls3d_is_motion_track = True
    traveling.rotation_mode = "QUATERNION"
    traveling.location = (0.0, 0.0, 0.0)
    traveling.keyframe_insert("location", frame=0)
    traveling.location = (2.0, 0.0, 4.0)
    traveling.keyframe_insert("location", frame=10)

    def swept():
        seen = []
        for at in (0, 5, 10):
            bpy.context.scene.frame_set(at)
            seen.append(tuple(round(v, 2) for v in traveling.location))
        return seen

    moving = swept()
    traveling.ls3d_motion_enabled = False
    where_off = tuple(round(v, 2) for v in traveling.location)
    held = swept()
    traveling.ls3d_motion_enabled = True
    where_on = tuple(round(v, 2) for v in traveling.location)
    moving_again = swept()
    keys_kept = [len(fc.keyframe_points)
                 for fc in anim_io.channelbag(traveling).fcurves]
    check("Apply Movement mutes the travel without moving anything",
          moving == [(0.0, 0.0, 0.0), (1.0, 0.0, 2.0), (2.0, 0.0, 4.0)]
          and where_off != (0.0, 0.0, 0.0)
          and len(set(held)) == 1
          and where_on != (0.0, 0.0, 0.0)
          and moving_again == moving
          and keys_kept == [2, 2, 2],
          f"moving {moving}; on switching off it sat at {where_off} and held "
          f"{held}; on switching back on {where_on}, moving {moving_again}; "
          f"keys {keys_kept}")

    # There is one movement track in a scene and it is easy to lose in a full
    # outliner, so the sidebar names it whatever is selected. Its own switch
    # sits first in its box, so it does not move under the pointer.
    fresh_scene()

    class _Said(_Layout):
        """A layout that keeps the text as well as counting it."""

        def __init__(self):
            super().__init__()
            self.text = []

        def label(self, text="", icon="", **kwargs):
            super().label(text=text, icon=icon, **kwargs)
            self.text.append(text)

        def prop(self, _target, name, text="", **kwargs):
            super().prop(_target, name, text=text, **kwargs)
            self.text.append(f"[{name}]")

        def operator(self, idname, text="", **kwargs):
            self.text.append(f"[{idname}]")
            return super().operator(idname, text=text, **kwargs)

    def sidebar_text():
        body = {k: v for k, v in ui_5ds.The5DSAnimationPanel.__dict__.items()
                if not k.startswith("bl_")}
        panel = type("Panel", (), body)()
        panel.layout = _Said()
        ui_5ds.The5DSAnimationPanel.draw(panel, bpy.context)
        return [t for t in panel.layout.text if t]

    bpy.ops.mesh.primitive_cube_add()
    named_body = bpy.context.object
    named_body.name = "namedbody"
    named_body.rotation_mode = "QUATERNION"
    named_body.keyframe_insert("rotation_quaternion", frame=0)
    without = sidebar_text()
    bpy.ops.object.empty_add()
    named_track = bpy.context.object
    named_track.name = "namedtrack"
    named_track.ls3d_is_motion_track = True
    bpy.context.view_layer.objects.active = named_body
    from_elsewhere = sidebar_text()
    bpy.context.view_layer.objects.active = named_track
    on_the_track = sidebar_text()
    # In its own box the switch comes before the movement options.
    switch = on_the_track.index("[ls3d_is_motion_track]")
    shown = on_the_track.index("[ls3d_motion_enabled]")
    check("the sidebar names the movement track and leads with its switch",
          "none" in without and "namedtrack" not in without
          and "namedtrack" in from_elsewhere
          and "Track:" in from_elsewhere
          and switch < shown,
          f"with no track it says {[t for t in without if t == 'none']}; "
          f"from another object it names "
          f"{[t for t in from_elsewhere if t == 'namedtrack']}; "
          f"switch at {switch}, movement option at {shown}")

    # The travel is a transform above the model, which is why the import
    # parents the model under the empty it makes. Outside it, the empty
    # wanders off on its own and the animation carries the travel instead.
    fresh_scene()
    parent_dir = os.path.join(out_dir, "parenting")
    os.makedirs(parent_dir, exist_ok=True)
    for stale in os.listdir(parent_dir):
        os.remove(os.path.join(parent_dir, stale))

    def rigged(parented, with_track=True):
        fresh_scene()
        bpy.context.scene.frame_start = 0
        bpy.context.scene.frame_end = 10
        bpy.ops.object.armature_add()
        rig = bpy.context.object
        rig.name = "parentrig"
        # A joint of the model moves; an armature is not a frame of its own,
        # so keying the armature itself would give the export nothing to write.
        joint = rig.pose.bones[0]
        joint.rotation_mode = "QUATERNION"
        joint.keyframe_insert("rotation_quaternion", frame=0)
        joint.keyframe_insert("rotation_quaternion", frame=10)
        if not with_track:
            return rig, None
        bpy.ops.object.empty_add()
        carrier = bpy.context.object
        carrier.name = motion_io.MOTION_NAME
        carrier.ls3d_is_motion_track = True
        carrier.keyframe_insert("location", frame=0)
        carrier.location = (2.0, 0.0, 0.0)
        carrier.keyframe_insert("location", frame=10)
        if parented:
            rig.parent = carrier
        return rig, carrier

    rigged(parented=True)
    under = (getattr(bpy.ops.export_scene, "5ds")(
                 filepath=os.path.join(parent_dir, "under.5ds")),
             getattr(bpy.ops.export_scene, "tck")(
                 filepath=os.path.join(parent_dir, "under.tck")))
    rigged(parented=False)
    loose = (getattr(bpy.ops.export_scene, "5ds")(
                 filepath=os.path.join(parent_dir, "loose.5ds")),
             getattr(bpy.ops.export_scene, "tck")(
                 filepath=os.path.join(parent_dir, "loose.tck")))
    wrote_loose = [f for f in os.listdir(parent_dir) if f.startswith("loose")]

    # A model hanging deeper under the track still counts, and a scene with
    # no track at all is nobody's business.
    rig, carrier = rigged(parented=False)
    bpy.ops.object.empty_add()
    between = bpy.context.object
    between.name = "between"
    between.parent = carrier
    rig.parent = between
    deeper = getattr(bpy.ops.export_scene, "5ds")(
        filepath=os.path.join(parent_dir, "deeper.5ds"))
    rigged(parented=False, with_track=False)
    trackless = getattr(bpy.ops.export_scene, "5ds")(
        filepath=os.path.join(parent_dir, "trackless.5ds"))
    check("the model has to hang under the movement track",
          under == ({"FINISHED"}, {"FINISHED"})
          and loose == ({"CANCELLED"}, {"CANCELLED"})
          and wrote_loose == []
          and deeper == {"FINISHED"} and trackless == {"FINISHED"},
          f"under the track {under}; outside it {loose} writing "
          f"{wrote_loose}; parented deeper {deeper}; with no track "
          f"{trackless}")

    # A target frame's Track To beats whatever the animation says, so the
    # sidebar can call the whole lot off at once while animating. The links
    # are untouched by it and still export.
    fresh_scene()
    aimers = []
    for name in ("aimone", "aimtwo"):
        bpy.ops.ls3d.add_target()
        aimer = bpy.context.object
        aimer.name = name
        aimers.append(aimer)
    for aimer, watched in zip(aimers, ("aimedone", "aimedtwo")):
        bpy.ops.mesh.primitive_cube_add()
        bpy.context.object.name = watched
        bpy.context.view_layer.objects.active = aimer
        aimer.ls3d_target_add_name = watched
        bpy.ops.ls3d.add_target_object()

    def aiming():
        live = []
        for watched in ("aimedone", "aimedtwo"):
            live += [not getattr(c, "mute", False)
                     for c in bpy.data.objects[watched].constraints
                     if c.type == "TRACK_TO"]
        return live

    steering = aiming()
    bpy.context.scene.ls3d_targets_ignored = True
    ignored = aiming()
    bpy.context.scene.ls3d_targets_ignored = False
    steering_again = aiming()
    # The scene-wide switch wins over a target's own.
    bpy.context.scene.ls3d_targets_ignored = True
    aimers[0].ls3d_target_enabled = True
    overridden = aiming()
    bpy.context.scene.ls3d_targets_ignored = False
    aimers[0].ls3d_target_enabled = False
    one_off = aiming()
    aimers[0].ls3d_target_enabled = True

    # Switched off or not, the links are still written into the model.
    aimed_path = os.path.join(out_dir, "aiming.4ds")
    bpy.context.scene.ls3d_targets_ignored = True
    aimed_written = export_op(aimed_path)
    aimed_frames = ([f.name for f in read_file(aimed_path).frames]
                    if aimed_written == {"FINISHED"} else [])
    links_kept = [len(a.ls3d_target_objects) for a in aimers]
    check("target frames can be told to stop aiming, without losing the links",
          steering == [True, True] and ignored == [False, False]
          and steering_again == [True, True]
          and overridden == [False, False] and one_off == [False, True]
          and links_kept == [1, 1]
          and aimed_written == {"FINISHED"}
          and "aimone" in aimed_frames and "aimedone" in aimed_frames,
          f"aiming {steering}; ignored {ignored}; back {steering_again}; "
          f"scene switch over a target's own {overridden}; one off {one_off}; "
          f"links {links_kept}; exported frames {sorted(aimed_frames)[:4]}")

    # Each target frame's own switch is reachable from the sidebar, not only
    # by finding the frame in the outliner and opening its object properties.
    # Muting the lot is the blunt instrument; usually it is one frame fighting
    # the animation.
    bpy.context.scene.ls3d_targets_ignored = False
    for aimer in aimers:
        aimer.ls3d_target_enabled = True
    listed_all = [t for t in sidebar_text() if t == "[ls3d_target_enabled]"]
    aimers[0].ls3d_target_enabled = False
    one_muted = aiming()
    # The scene switch wins while it is on, but must not wipe what each frame
    # is set to - turning it off again leaves the one muted frame muted.
    bpy.context.scene.ls3d_targets_ignored = True
    listed_while_ignored = [t for t in sidebar_text()
                            if t == "[ls3d_target_enabled]"]
    bpy.context.scene.ls3d_targets_ignored = False
    survived = aiming()
    for aimer in aimers:
        aimer.ls3d_target_enabled = True
    check("the sidebar can mute one target frame without muting the rest",
          len(listed_all) == len(aimers) and one_muted == [False, True]
          and len(listed_while_ignored) == len(aimers)
          and survived == one_muted,
          f"{len(listed_all)} switch(es) listed for {len(aimers)} target(s); "
          f"one muted {one_muted}; still listed under the scene switch "
          f"{len(listed_while_ignored)}; after it went off {survived}")

    # The .4DS carries a count of animated objects, which is how the game
    # knows to look for a .5DS beside it. The sidebar shows it where it is
    # being changed, and says when it has drifted from the scene.
    fresh_scene()
    bpy.context.scene.ls3d_animated_object_count = 0
    # The scene half of the sidebar stands on its own with nothing selected.
    nothing_selected = sidebar_text()
    bpy.ops.mesh.primitive_cube_add()
    quiet = sidebar_text()
    bpy.ops.object.delete()
    bpy.ops.mesh.primitive_cube_add()
    counted_body = bpy.context.object
    counted_body.name = "countedbody"
    counted_body.rotation_mode = "QUATERNION"
    counted_body.keyframe_insert("rotation_quaternion", frame=0)
    # The count follows the scene through the depsgraph handler.
    bpy.context.view_layer.update()
    bpy.context.scene.frame_set(bpy.context.scene.frame_current)
    followed = bpy.context.scene.ls3d_animated_object_count
    agreeing = sidebar_text()
    bpy.context.scene.ls3d_animated_object_count = 7
    drifted = sidebar_text()

    # The handler has to survive a file being opened, or the count stops
    # following the scene from the first New File onwards.
    survives = _on_update_registered = any(
        getattr(h, "__name__", "") == "_on_depsgraph_update"
        for h in bpy.app.handlers.depsgraph_update_post)
    check("the sidebar carries the model's animated-object count",
          "Follows the scene upwards" in " ".join(quiet)
          and followed == 1
          and "Matches the 1" in " ".join(agreeing)
          and "are animated" in " ".join(drifted)
          and survives
          and any("Model" in t for t in nothing_selected)
          and any("Select something" in t for t in nothing_selected),
          f"with nothing animated {[t for t in quiet if 'Follows' in t]}; "
          f"count followed to {followed}; agreeing "
          f"{[t for t in agreeing if 'Matches' in t]}; drifted "
          f"{' '.join(drifted)[:60]}; "
          f"handler still registered={survives}; with nothing selected it "
          f"still drew {len(nothing_selected)} row(s)")

    # The animated-object count is the model's own word, carried in its
    # .4DS. Loading an animation onto that model says nothing about it - the
    # animation may drive two of its frames or twenty - so an import has to
    # pass through without touching the number.
    fresh_scene()
    held_dir = os.path.join(out_dir, "held")
    os.makedirs(held_dir, exist_ok=True)
    held_clip = anim_format.Animation(end_frame=10)
    for who in ("heldbody", "heldhat"):
        piece = anim_format.Track(name=who)
        piece.rotations = [(0, (1.0, 0.0, 0.0, 0.0)),
                           (10, (0.0, 1.0, 0.0, 0.0))]
        held_clip.tracks.append(piece)
    held_path = os.path.join(held_dir, "held.5ds")
    anim_format.write_animation_file(held_clip, held_path)
    held_track = track_format.MotionTrack(duration=400, key_period=20)
    for index in range(held_track.expected_key_count):
        held_track.positions.append((index * 0.1, 0.0, 0.0))
    held_tck = os.path.join(held_dir, "held.tck")
    track_format.write_track_file(held_track, held_tck)

    def tick():
        bpy.context.view_layer.update()
        bpy.context.scene.frame_set(bpy.context.scene.frame_current)

    def with_declared(number):
        fresh_scene()
        for who in ("heldbody", "heldhat"):
            bpy.ops.object.empty_add()
            bpy.context.object.name = who
        bpy.context.scene.ls3d_animated_object_count = number

    with_declared(5)
    getattr(bpy.ops.import_scene, "5ds")(filepath=held_path)
    after_clip = bpy.context.scene.ls3d_animated_object_count
    tick()
    settled_clip = bpy.context.scene.ls3d_animated_object_count

    with_declared(5)
    getattr(bpy.ops.import_scene, "tck")(filepath=held_tck)
    after_travel = bpy.context.scene.ls3d_animated_object_count
    tick()
    settled_travel = bpy.context.scene.ls3d_animated_object_count

    # Authoring still raises it, and it never drops on its own.
    fresh_scene()
    bpy.context.scene.ls3d_animated_object_count = 0
    for who in ("counta", "countb"):
        bpy.ops.object.empty_add()
        keyed = bpy.context.object
        keyed.name = who
        keyed.rotation_mode = "QUATERNION"
        keyed.keyframe_insert("rotation_quaternion", frame=0)
    tick()
    raised = bpy.context.scene.ls3d_animated_object_count
    bpy.data.objects["countb"].animation_data_clear()
    tick()
    kept = bpy.context.scene.ls3d_animated_object_count
    check("an import never rewrites the model's animated-object count",
          after_clip == 5 and settled_clip == 5
          and after_travel == 5 and settled_travel == 5
          and raised == 2 and kept == 2,
          f"declared 5, after a .5ds {after_clip} settling to {settled_clip}; "
          f"after a .tck {after_travel} settling to {settled_travel}; "
          f"authoring two raised it to {raised}, removing one left {kept}")

    # Console lines carry one stem so the addon's output can be picked out of
    # a shared console, and the format after it so a message that names no
    # file still says which side it came from.
    marked = ls3d_module("common.report")
    nested = marked.Report().begin("Exporting a model", "4DS")
    model_line = nested.prefix
    with nested.as_format("5DS"):
        animation_line = nested.prefix
        with nested.as_format("TCK"):
            travel_line = nested.prefix
        back_inside = nested.prefix
    back_outside = nested.prefix
    check("console lines say which format they came from",
          marked.console_prefix() == "[MBT]"
          and model_line == "[MBT/4DS]"
          and animation_line == "[MBT/5DS]"
          and travel_line == "[MBT/TCK]"
          and back_inside == "[MBT/5DS]" and back_outside == "[MBT/4DS]",
          f"plain {marked.console_prefix()}; model {model_line}; nested "
          f"{animation_line} then {travel_line}, unwinding to {back_inside} "
          f"and {back_outside}")

    # A .6ds is the coarse mesh the engine casts shadows from, one piece per
    # frame of the model. It carries geometry and nothing else - no materials,
    # no UVs - so a round trip has to reproduce the vertices and triangles.
    shadow_codec = ls3d_module("6ds.codec")
    shadow_io = ls3d_module("6ds.io")
    shadow_ops = ls3d_module("6ds.ops")
    fresh_scene()
    shadow_dir = os.path.join(out_dir, "shadow")
    os.makedirs(shadow_dir, exist_ok=True)

    made_shadow = shadow_codec.Shadow(timestamp=bytes(range(8)))
    for piece_name, corner in (("l_foot", 0.0), ("r_foot", 2.0)):
        piece = shadow_codec.ShadowGroup(name=piece_name)
        piece.vertices = [(corner, 0.0, 0.0), (corner + 1.0, 0.0, 0.0),
                          (corner, 1.0, 0.0), (corner, 0.0, 1.0)]
        piece.faces = [(0, 1, 2), (0, 2, 3)]
        made_shadow.groups.append(piece)
    shadow_path = os.path.join(shadow_dir, "cast.6ds")
    shadow_codec.write_shadow_file(made_shadow, shadow_path)

    # A frame for one of the pieces, so the placement can be checked: the
    # file's coordinates are local to the frame whose name a piece carries.
    bpy.ops.object.empty_add(location=(5.0, 0.0, 0.0))
    bpy.context.object.name = "l_foot"

    # Left alone by default: the pieces arrive where the file puts them.
    loose_import = getattr(bpy.ops.import_scene, "6ds")(filepath=shadow_path)
    left_alone = sum(1 for obj in shadow_io.shadow_pieces(bpy.context.scene)
                     if obj.parent)
    for stray in list(shadow_io.shadow_pieces(bpy.context.scene)):
        bpy.data.objects.remove(stray, do_unlink=True)

    brought = getattr(bpy.ops.import_scene, "6ds")(filepath=shadow_path,
                                                   parent_to_frames=True)
    pieces = shadow_io.shadow_pieces(bpy.context.scene)
    piece_names = sorted(shadow_io.frame_name(obj) for obj in pieces)
    hung = {shadow_io.frame_name(o): (o.parent.name if o.parent else None)
            for o in pieces}
    # Blender renames the piece to avoid colliding with that very frame; the
    # name written must be the frame's, not Blender's.
    collided = sorted(obj.name for obj in pieces)
    # A shadow piece is not a frame of the model, so it must never be written
    # into one.
    model_path = os.path.join(shadow_dir, "cast.4ds")
    model_written = export_op(model_path)
    model_frames = ([f.name for f in read_file(model_path).frames]
                    if model_written == {"FINISHED"} else [])

    again = os.path.join(shadow_dir, "cast_again.6ds")
    written_back = getattr(bpy.ops.export_scene, "6ds")(filepath=again)
    same_bytes = (open(shadow_path, "rb").read() == open(again, "rb").read()
                  if written_back == {"FINISHED"} else False)

    # An empty scene has no shadow to write, and the menu says so.
    fresh_scene()
    offered_empty = bpy.ops.export_scene.__getattr__("6ds").poll()
    check("a shadow round-trips and stays out of the model",
          loose_import == {"FINISHED"} and left_alone == 0
          and brought == {"FINISHED"}
          and piece_names == ["l_foot", "r_foot"]
          and collided == ["l_foot.001", "r_foot"]
          and hung["l_foot"] == "l_foot" and hung["r_foot"] is None
          and len(pieces) == 2
          and model_written == {"FINISHED"}
          # The stand-in frame is a real frame and belongs in the model; the
          # shadow pieces hanging off it do not.
          and model_frames == ["l_foot"]
          and "l_foot.001" not in model_frames
          and written_back == {"FINISHED"} and same_bytes
          and offered_empty is False,
          f"parented on import by default: {left_alone}; asked to, "
          f"imported {piece_names} as {collided}, hung from {hung}; "
          f"the model got {sorted(model_frames)}; "
          f"rewritten byte-for-byte={same_bytes}; offered with nothing "
          f"marked={offered_empty}")

    # The format's own limits, checked without Blender.
    refused = []
    huge = shadow_codec.Shadow()
    big = shadow_codec.ShadowGroup(name="x" * 300)
    big.vertices = [(0.0, 0.0, 0.0)]
    big.faces = [(0, 0, 5)]
    huge.groups.append(big)
    shadow_codec.validate_shadow(
        huge, lambda message, fix=None: refused.append(message),
        lambda *a, **k: None)
    check("a shadow is checked against what the game will read",
          any("single byte" in m for m in refused)
          and any("outside its own" in m for m in refused),
          f"refused {[m[:48] for m in refused]}")

    # A model arrives in a collection of its own, so two can be open side by
    # side and one can be hidden without hunting through its frames.
    model_for_collection = os.path.join(models_dir, "vjezdtov1.4ds")
    if os.path.isfile(model_for_collection):
        fresh_scene()
        for stale in list(bpy.data.collections):
            bpy.data.collections.remove(stale)
        getattr(bpy.ops.import_scene, "4ds")(filepath=model_for_collection)
        named = [c.name for c in bpy.data.collections]
        gathered = sum(len(c.objects) for c in bpy.data.collections)
        loose = len(bpy.context.scene.collection.objects)
        # Still exports the same, wherever the objects are filed.
        collected_path = os.path.join(out_dir, "collected.4ds")
        collected = export_op(collected_path)
        frames_now = (len(read_file(collected_path).frames)
                      if collected == {"FINISHED"} else 0)

        fresh_scene()
        for stale in list(bpy.data.collections):
            bpy.data.collections.remove(stale)
        getattr(bpy.ops.import_scene, "4ds")(filepath=model_for_collection,
                                             own_collection=False)
        without = [c.name for c in bpy.data.collections]
        loose_without = len(bpy.context.scene.collection.objects)
        check("a model is filed in a collection named after it",
              named == ["vjezdtov1"] and gathered > 0 and loose == 0
              and collected == {"FINISHED"} and frames_now > 0
              and without == [] and loose_without > 0,
              f"collections {named} holding {gathered} object(s), "
              f"{loose} loose; exported {frames_now} frame(s); with the "
              f"toggle off {without} and {loose_without} loose")

    # A box whose only contents sit behind a preference must not leave its
    # heading behind when that preference is off.
    fresh_scene()
    ui_4ds = ls3d_module("4ds.ui")
    lonely = bpy.data.materials.new("lonelymat")

    class _MatLayout(_Layout):
        """A layout that keeps the text, and answers the material calls."""

        def __init__(self, said=None):
            super().__init__()
            self.said = said if said is not None else []

        def label(self, text="", icon="", **kwargs):
            super().label(text=text, icon=icon, **kwargs)
            self.said.append(("label", text))

        def prop(self, _target, name, text="", **kwargs):
            super().prop(_target, name, text=text, **kwargs)
            self.said.append(("prop", name))

        def template_ID(self, *_args, **_kwargs):
            self.said.append(("template_ID", ""))

    def material_rows(show_raw):
        original = io_mafia_toolkit.get_preferences
        io_mafia_toolkit.get_preferences = lambda: type(
            "P", (), {"show_raw_flags": show_raw,
                      "show_reserved_flags": False})()
        try:
            recorder = _MatLayout()
            ui_4ds.draw_material_body(recorder, lonely)
            return recorder.said
        finally:
            io_mafia_toolkit.get_preferences = original

    hidden_rows = material_rows(False)
    shown_rows = material_rows(True)
    headings = lambda rows: [t for kind, t in rows if kind == "label"]
    order = [n for kind, n in hidden_rows if kind == "prop"]
    # The alpha box leads with the texture switch, the enable sitting under it.
    leads_with = order.index("ls3d_flag_alphatex")
    then = order.index("ls3d_flag_alpha_enable")
    check("a flag box behind a preference takes its heading with it",
          "Global Material Flags" not in headings(hidden_rows)
          and "Global Material Flags" in headings(shown_rows)
          and ("prop", "ls3d_material_flags_str") in shown_rows
          and ("prop", "ls3d_material_flags_str") not in hidden_rows
          and leads_with < then,
          f"heading with the preference off="
          f"{'Global Material Flags' in headings(hidden_rows)}, on="
          f"{'Global Material Flags' in headings(shown_rows)}; alpha box "
          f"leads with the texture switch={leads_with < then}")

    # ── material preview ─────────────────────────────────────────────────────
    # Four switches decide how a surface reaches the screen and they override
    # one another, with an emission color joining in from outside the list. The
    # preview graph has to land on the same one the game does, because getting
    # it wrong means fire and lamp halos darken the scene instead of lighting
    # it - which is what the previous build did to 784 of the game's materials.
    fresh_scene()
    preview = ls3d_module("4ds.materials")
    swatch = bpy.data.images.new("SWATCH.BMP", 4, 4)
    mask = bpy.data.images.new("MASK.BMP", 4, 4)

    def previewed(flags, **props):
        made = bpy.data.materials.new("preview")
        for key, value in props.items():
            setattr(made, key, value)
        made.ls3d_material_flags = _to_signed(flags)
        preview.rebuild_material_nodes(made)
        return made

    def surface_of(made):
        node = preview.find_node(made.node_tree.nodes, preview.NL_OUTPUT)
        return node.inputs["Surface"].links[0].from_node.label

    def alpha_from(made):
        node = preview.find_node(made.node_tree.nodes, preview.NL_OPACITY_MUL)
        wired = node.inputs[0].links
        return wired[0].from_node.label if wired else ""

    DIFF = C.MTL_DIFFUSE_ENABLE
    paths = []
    for want, flags, props in (
            ("solid", DIFF, {}),
            ("key", DIFF | C.MTL_ALPHA_COLORKEY, {}),
            ("fade", DIFF | C.MTL_ALPHATEX, {}),
            ("fade", DIFF, {"ls3d_opacity": 0.5}),
            # A gray emission is what the game's fires and glows carry.
            ("add", DIFF | C.MTL_ALPHATEX,
             {"ls3d_emission_color": (0.498, 0.498, 0.498)}),
            ("add", DIFF | C.MTL_ALPHA_ADDITIVE, {}),
            # Additive is tried first, so it wins over the other two.
            ("add", DIFF | C.MTL_ALPHA_ADDITIVE | C.MTL_ALPHA_COLORKEY, {}),
            # A keyed material with mipmaps: the game loads the texture with a
            # whole alpha channel and blends it rather than cutting it, so it
            # draws as a keyed surface with edges that soften with distance -
            # and an emission color makes it additive like any blended one.
            ("softkey", DIFF | C.MTL_ALPHA_COLORKEY | C.MTL_DIFFUSE_MIPMAP, {}),
            ("add", DIFF | C.MTL_ALPHA_COLORKEY | C.MTL_DIFFUSE_MIPMAP,
             {"ls3d_emission_color": (0.498, 0.498, 0.498)}),
            # Mipmaps alone change nothing about how a surface is drawn.
            ("solid", DIFF | C.MTL_DIFFUSE_MIPMAP, {})):
        made = previewed(flags, ls3d_diffuse_tex=swatch, **props)
        landed = preview.blend_path(made)
        wired = surface_of(made)
        blended = made.surface_render_method == (
            "BLENDED" if landed in ("add", "fade", "softkey") else "DITHERED")
        paths.append((landed == want, landed, want,
                      wired == (preview.NL_GLOW_ADD if want == "add"
                                else preview.NL_BSDF),
                      blended))
    check("a material previews the way the game draws it",
          all(ok and routed and blended for ok, _g, _w, routed, blended in paths),
          "; ".join(f"{got} (wanted {want})" for ok, got, want, _r, _b in paths
                    if not ok) or f"{len(paths)} paths")

    # A keyed surface is cut; a keyed surface with mipmaps is blended. The
    # difference is which of the two the shader takes its alpha from: the
    # cut-off test, or the key mask as it stands.
    def alpha_source(made):
        node = preview.find_node(made.node_tree.nodes, preview.NL_BSDF)
        wired = node.inputs["Alpha"].links
        return wired[0].from_node.label if wired else ""

    cut = previewed(DIFF | C.MTL_ALPHA_COLORKEY, ls3d_diffuse_tex=swatch)
    softened = previewed(DIFF | C.MTL_ALPHA_COLORKEY | C.MTL_DIFFUSE_MIPMAP,
                         ls3d_diffuse_tex=swatch)
    cut_label = preview.BLEND_PATH_LABELS[preview.blend_path(cut)][0]
    soft_label = preview.BLEND_PATH_LABELS[preview.blend_path(softened)][0]
    soft_reason = preview.blend_path_reason(softened)
    check("a keyed material with mipmaps is blended, and says so",
          alpha_source(cut) == preview.NL_ALPHA_TEST
          and alpha_source(softened) == preview.NL_OPACITY_MUL
          and cut.surface_render_method == "DITHERED"
          and softened.surface_render_method == "BLENDED"
          and cut_label == "Color Keyed" and "Blended" in soft_label
          and "Diffuse MipMap" in soft_reason,
          f"cut takes its alpha from {alpha_source(cut) or 'nothing'} "
          f"({cut.surface_render_method}), mipmapped from "
          f"{alpha_source(softened) or 'nothing'} "
          f"({softened.surface_render_method}); panel says '{soft_label}' - "
          f"{soft_reason}")

    # Each kind of transparency reads from its own place, and additive reads
    # from none: its texture carries no transparency, and its dark areas
    # disappear because adding black adds nothing.
    sources = [
        (preview.NL_KEY_INVERT, DIFF | C.MTL_ALPHA_COLORKEY, {}),
        (preview.NL_DIFFUSE_TEX, DIFF | C.MTL_ALPHATEX | C.MTL_ALPHA_IN_TEX, {}),
        (preview.NL_ALPHA_LUMA, DIFF | C.MTL_ALPHATEX,
         {"ls3d_alpha_tex": mask}),
        ("", DIFF | C.MTL_ALPHA_ADDITIVE, {}),
        ("", DIFF, {}),
    ]
    got_sources = [(alpha_from(previewed(f, ls3d_diffuse_tex=swatch, **p)), want)
                   for want, f, p in sources]
    check("each kind of transparency is read from where the game reads it",
          all(got == want for got, want in got_sources),
          "; ".join(f"{got or 'nothing'} (wanted {want or 'nothing'})"
                    for got, want in got_sources if got != want))

    # Blend and mapping are small numbers in the flag word, not single bits:
    # 3 is "multiply", which is bits 8 and 9 together, and reading them apart
    # turned it into something else entirely.
    #
    # The share column is how much of the map arrives unlit. The game lights
    # the texture and only then puts the map on top, so in the two blended
    # modes and in add it comes through whole however dark the surface is -
    # which is what makes glass read as frosted rather than as a gray tube.
    # The multiply modes are the exception: there it multiplies the lit result.
    blends = []
    for value, want_type, want_double, want_share in (
            (0, "MIX", 0.0, 0.0), (1, "MIX", 0.0, 0.25),
            (2, "MIX", 0.0, None), (3, "MULTIPLY", 0.0, 0.0),
            (4, "MULTIPLY", 1.0, 0.0), (5, "MIX", 0.0, 1.0),
            # 6 and 7 name no blend in the game; like None, nothing is shown.
            (6, "MIX", 0.0, 0.0), (7, "MIX", 0.0, 0.0)):
        made = previewed(DIFF | C.MTL_ENV_ENABLE
                         | (value << C.MTL_ENV_BLEND_SHIFT),
                         ls3d_diffuse_tex=swatch, ls3d_env_tex=swatch,
                         ls3d_env_amount=0.25)
        nodes = made.node_tree.nodes
        mixer = preview.find_node(nodes, preview.NL_ENV_MIX)
        doubler = preview.find_node(nodes, preview.NL_ENV_SCALE)
        unlit = preview.find_node(nodes, preview.NL_ENV_EMIT)
        if want_share is None:
            # Mode 2 takes its share from the surface's own transparency.
            share_ok = bool(unlit.inputs["Color2"].links)
            share = "linked"
        else:
            share = unlit.inputs["Color2"].default_value[0]
            share_ok = abs(share - want_share) < 1e-6
        blends.append((mixer.blend_type == want_type
                       and doubler.inputs["Fac"].default_value == want_double
                       and share_ok, value, mixer.blend_type, share))
    check("every environment blend mode is read as a number, not as bits",
          all(ok for ok, _v, _t, _s in blends),
          "; ".join(f"mode {v} gave {t} share {s}"
                    for ok, v, t, s in blends if not ok))

    # With the environment texture on, the game has a blend for five values
    # only. A material exported with another is a valid file that draws
    # unpredictably, so it is a warning - and the five are not warned about.
    import contextlib as _env_context
    import io as _env_io
    env_said = {}
    for value in (C.ENV_BLEND_NONE, C.ENV_BLEND_MULTIPLY, 6):
        # No fresh scene here: the preview checks around this one share
        # images, so only the cube made for it is taken away again.
        bpy.ops.mesh.primitive_cube_add()
        blended_cube = bpy.context.object
        blended = bpy.data.materials.new(f"env_blend_{value}")
        blended.ls3d_material_flags = _to_signed(
            C.MTL_ENV_ENABLE | (value << C.MTL_ENV_BLEND_SHIFT))
        blended_cube.data.materials.append(blended)
        heard = _env_io.StringIO()
        with _env_context.redirect_stdout(heard):
            outcome = export_op(os.path.join(out_dir, f"env_blend_{value}.4ds"))
        env_said[value] = (outcome, "which the game has no blend for"
                           in heard.getvalue())
        bpy.data.objects.remove(blended_cube, do_unlink=True)
    check("an environment blend the game has no blend for is warned about",
          env_said[C.ENV_BLEND_NONE] == ({"FINISHED"}, True)
          and env_said[6] == ({"FINISHED"}, True)
          and env_said[C.ENV_BLEND_MULTIPLY] == ({"FINISHED"}, False)
          and [item[0] for item in C.ENV_BLEND_ITEMS]
          == [str(n) for n in range(C.ENV_BLEND_ADD + 1)]
          and [item[0] for item in C.LIGHT_TYPE_ITEMS]
          == [str(n) for n in range(C.LIGHT_TYPE_COUNT)],
          f"exported and warned: {env_said}")

    # The reflect modes get the game's own arithmetic rather than Blender's
    # ready-made spherical projection. The maps they read are nearly black
    # with a few bright streaks, so sampling them a different way does not
    # soften the highlight, it puts it somewhere else on the model.
    reflects = []
    for value, pole, across, down, sign in ((0, "Y", "X", "Z", 1.0),
                                            (1, "Z", "X", "Y", -1.0)):
        made = previewed(DIFF | C.MTL_ENV_ENABLE
                         | (1 << C.MTL_ENV_BLEND_SHIFT)
                         | (value << C.MTL_ENV_UV_SHIFT),
                         ls3d_diffuse_tex=swatch, ls3d_env_tex=swatch)
        nodes = made.node_tree.nodes
        split = preview.find_node(nodes, preview.NL_ENV_SEP)
        got = (split.inputs["Vector"].links[0].from_socket.name,
               preview.find_node(nodes, preview.NL_ENV_POLE)
               .inputs[0].links[0].from_socket.name,
               preview.find_node(nodes, preview.NL_ENV_U)
               .inputs[0].links[0].from_socket.name,
               preview.find_node(nodes, preview.NL_ENV_V)
               .inputs[0].links[0].from_socket.name,
               preview.find_node(nodes, preview.NL_ENV_PROJ_V)
               .inputs[1].default_value,
               preview.find_node(nodes, preview.NL_ENV_TEX).projection)
        reflects.append((got == ("Reflection", pole, across, down, sign, "FLAT"),
                         value, got))
    planar = previewed(DIFF | C.MTL_ENV_ENABLE | (1 << C.MTL_ENV_BLEND_SHIFT)
                       | (2 << C.MTL_ENV_UV_SHIFT),
                       ls3d_diffuse_tex=swatch, ls3d_env_tex=swatch)
    from_position = (preview.find_node(planar.node_tree.nodes, preview.NL_ENV_SEP)
                     .inputs["Vector"].links[0].from_socket.name)
    check("a reflection is wrapped onto the map the way the game wraps it",
          all(ok for ok, _v, _g in reflects) and from_position == "Object",
          "; ".join(f"mode {v} gave {g}" for ok, v, g in reflects if not ok)
          or f"planar reads {from_position}")

    # Emission is added to the light landing on the surface and the texture is
    # multiplied by the sum, so it brightens the texture instead of laying a
    # flat color over it - a lit window keeps its panes. 93 of the game's
    # materials carry one on a surface it still lights.
    #
    # The doubling the surface goes through on its way to the screen is
    # canceled by the scale its lighting is measured on, so neither appears
    # here: what reaches the screen is the texture times the light on it.
    self_lit = previewed(DIFF, ls3d_diffuse_tex=swatch,
                         ls3d_emission_color=(0.498, 0.498, 0.498))
    nodes = self_lit.node_tree.nodes
    lamp = preview.find_node(nodes, preview.NL_SELF_LIGHT)
    glow = preview.find_node(nodes, preview.NL_GLOW_TINT)
    surface = lamp.inputs["Color1"].links[0].from_node.label
    check("emission multiplies the surface rather than washing over it",
          lamp.blend_type == "MULTIPLY" and surface == preview.NL_ENV_SCALE
          and abs(lamp.inputs["Color2"].default_value[0] - 0.498) < 1e-3
          and abs(glow.inputs["Color2"].default_value[0] - 0.498) < 1e-3,
          f"lit path {lamp.blend_type} over {surface} at "
          f"{lamp.inputs['Color2'].default_value[0]:.3f}, additive path at "
          f"{glow.inputs['Color2'].default_value[0]:.3f}")

    # A material with no texture is painted in its own diffuse color. Showing
    # it black instead loses the only color those 306 materials have.
    plain = previewed(0, ls3d_diffuse_color=(1.0, 0.0, 0.0))
    tinted = preview.find_node(plain.node_tree.nodes, preview.NL_DIFF_TINT)
    textured = previewed(DIFF, ls3d_diffuse_tex=swatch,
                         ls3d_diffuse_color=(1.0, 0.0, 0.0))
    over = preview.find_node(textured.node_tree.nodes, preview.NL_DIFF_TINT)
    check("an untextured material shows its own color, a textured one does not",
          tinted.inputs["Fac"].default_value == 1.0
          and tuple(round(c, 3) for c in tinted.inputs["Color2"].default_value)
          == (1.0, 0.0, 0.0, 1.0)
          and over.inputs["Fac"].default_value == 0.0,
          f"untextured mix={tinted.inputs['Fac'].default_value}, "
          f"textured mix={over.inputs['Fac'].default_value}")

    # Opacity is the one material setting the game reads per mesh rather than
    # per material: one alpha goes on every vertex, taken from whichever
    # material leads the mesh's draw order - the last one on it that asks for
    # no transparency of its own. Reading a material's own number at face value
    # is what made the whisky glass in FMVwhiskymrph vanish: it is set to 0,
    # and the game never looks at it.
    fresh_scene()
    swatch = bpy.data.images.new("SWATCH.BMP", 4, 4)      # the old one went with it
    bpy.ops.mesh.primitive_cube_add()
    mixed = bpy.context.object
    solid = previewed(DIFF, ls3d_diffuse_tex=swatch)
    ghost = previewed(DIFF | C.MTL_ALPHATEX, ls3d_diffuse_tex=swatch,
                      ls3d_opacity=0.0)
    mixed.data.materials.append(ghost)       # slot 0: asks for transparency
    mixed.data.materials.append(solid)       # slot 1: does not, so it leads
    for index, polygon in enumerate(mixed.data.polygons):
        polygon.material_index = index % 2
    preview.sync_material_flags(ghost)
    leader = preview.leading_material(mixed)
    held = preview.find_node(ghost.node_tree.nodes,
                             preview.NL_OPACITY_VAL).outputs[0].default_value

    # On its own, with nothing solid beside it, the same material does fade.
    bpy.ops.mesh.primitive_cube_add()
    alone = bpy.context.object
    alone.data.materials.append(ghost)
    mixed.data.materials.pop(index=1)
    for polygon in mixed.data.polygons:
        polygon.material_index = 0
    preview.sync_material_flags(ghost)
    alone_holds = preview.find_node(ghost.node_tree.nodes,
                                    preview.NL_OPACITY_VAL).outputs[0].default_value
    check("opacity comes from the mesh's leading material, not the material",
          leader is solid and held == 1.0
          and alone_holds == 0.0 and ghost.ls3d_opacity == 0.0,
          f"beside a solid material it is drawn at {held}, alone at "
          f"{alone_holds}, its own stays {ghost.ls3d_opacity}")

    # The animation block is six fields, not two with padding between them:
    # a frame count for each texture as a single byte, then a wrap-to frame and
    # a frame time for each. Reading the first four bytes as one number swept
    # up the environment count and half the wrap-to frame, so the panel showed
    # 276 frames for a 20-frame water animation - and writing it back put 99
    # there, losing both of the fields it had eaten. Six of the game's models
    # were affected, `explosion.4ds` among them, which wraps back to frame 10.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    animated = bpy.data.materials.new("anim")
    animated.ls3d_material_flags = _to_signed(
        C.MTL_DIFFUSE_ENABLE | C.MTL_DIFFUSE_ANIMATED)
    written = {"ls3d_anim_frames": 30, "ls3d_env_anim_frames": 1,
               "ls3d_anim_loop_start": 10, "ls3d_anim_period": 50,
               "ls3d_env_anim_loop_start": 3, "ls3d_env_anim_period": 40}
    for name, value in written.items():
        setattr(animated, name, value)
    bpy.context.object.data.materials.append(animated)
    anim_path = os.path.join(out_dir, "animated.4ds")
    exported = export_op(anim_path)
    read_back = read_file(anim_path).materials[0] if exported == {"FINISHED"} else None
    came_back = {} if read_back is None else {
        "ls3d_anim_frames": read_back.anim_frames,
        "ls3d_env_anim_frames": read_back.env_anim_frames,
        "ls3d_anim_loop_start": read_back.anim_loop_start,
        "ls3d_anim_period": read_back.anim_period,
        "ls3d_env_anim_loop_start": read_back.env_anim_loop_start,
        "ls3d_env_anim_period": read_back.env_anim_period,
    }
    check("every field of the texture animation block survives a round trip",
          came_back == written,
          f"wrote {written}, read {came_back}")

    # An animated material plays in the viewport, on a timer of its own rather
    # than by keyframing anything - the game's frame times are milliseconds and
    # have nothing to do with the scene's timeline. What steps is the image on
    # the preview node; the material's own texture stays on frame 00, because
    # that is the name the export writes and parking playback mid-animation
    # would otherwise rewrite it.
    texanim = ls3d_module("4ds.ops_texanim")
    names = texanim.frame_names("2FIRESM00.BMP", 16)
    alpha_names = texanim.frame_names("FLAME1_0000.BMP", 5)
    check("an animation's frame names follow the game's numbering",
          names[0] == "2FIRESM00.BMP" and names[-1] == "2FIRESM15.BMP"
          and len(names) == 16
          and alpha_names[-1] == "FLAME1_0004.BMP"
          # Six characters is the shortest the counter fits in; below that the
          # game gives up on the name and so does this.
          and texanim.frame_names("AB.BMP", 2) == ["00.BMP", "01.BMP"]
          and texanim.frame_names("A.BMP", 4) == [],
          f"{names[:2]} .. {names[-1]}; {alpha_names[-1]}; "
          f"six chars -> {texanim.frame_names('AB.BMP', 2)}; "
          f"five -> {texanim.frame_names('A.BMP', 4)}")

    # Wrapping follows the game: back to the loop frame, not to the beginning.
    plain = [texanim.frame_at(step * 70, 20, 70, 0) for step in range(22)]
    looped = [texanim.frame_at(step * 70, 20, 70, 10) for step in range(22)]
    random_run = {texanim.frame_at(step * 70, 20, 70, -1) for step in range(60)}
    steady = (texanim.frame_at(5 * 70, 20, 70, -1)
              == texanim.frame_at(5 * 70, 20, 70, -1))
    check("frames wrap to the loop frame, and -1 picks at random",
          plain[:3] == [0, 1, 2] and plain[19] == 19 and plain[20] == 0
          and looped[19] == 19 and looped[20] == 10 and looped[21] == 11
          and len(random_run) > 1 and steady,
          f"plain wraps to {plain[20]}, looped to {looped[20]}, random hit "
          f"{len(random_run)} frame(s), repeatable={steady}")

    # Playing changes the preview only. Nothing is keyed, and the material's
    # texture - what the file records - never moves.
    fresh_scene()
    # Earlier checks in this session left actions behind, so what matters is
    # that playing adds none - not that there are none at all.
    actions_before = len(bpy.data.actions)
    swatch = bpy.data.images.new("ANIM00.BMP", 4, 4)
    playing = previewed(DIFF | C.MTL_DIFFUSE_ANIMATED, ls3d_diffuse_tex=swatch,
                        ls3d_anim_frames=8, ls3d_anim_period=60)
    # Opening a model must not set every fire and water surface running, so
    # nothing plays until it is asked to.
    quiet_on_arrival = not texanim.is_playing(playing) and texanim.can_play(playing)
    texanim.start(playing)
    started = texanim.is_playing(playing)
    texanim.show_frame(playing, 3 * 60)
    texanim.pause(playing)
    held = texanim.is_paused(playing) and not texanim.is_playing(playing)
    playing.ls3d_anim_period = 70          # a nudge must not resume it
    still_held = texanim.is_paused(playing)
    playing.ls3d_flag_diffuse_animated = False
    reset = (not texanim.is_playing(playing) and not texanim.is_paused(playing)
             and preview.find_node(playing.node_tree.nodes,
                                   preview.NL_DIFFUSE_TEX).image is swatch)
    kept = (playing.ls3d_anim_frames, playing.ls3d_anim_period)
    check("playing a texture animation keys nothing and moves no file name",
          quiet_on_arrival and started and held and still_held
          and reset and kept == (8, 70)
          and len(bpy.data.actions) == actions_before
          and playing.ls3d_diffuse_tex is swatch,
          f"quiet until asked={quiet_on_arrival}, started={started}, "
          f"paused={held}, survived a nudge={still_held}, "
          f"reset={reset}, settings kept {kept}, actions "
          f"{actions_before} -> {len(bpy.data.actions)}")

    # An animation set up before its frames are on disk used to stay broken for
    # the rest of the session: the first lookup stood frame 00 in for every
    # missing file and that answer was cached for good, so copying the frames
    # in changed nothing and only restarting Blender helped.
    import shutil as _shutil
    staging = os.path.join(out_dir, "anim_frames")
    if os.path.isdir(staging):
        _shutil.rmtree(staging)
    os.makedirs(staging)

    # Six frames of a real animated texture, one of which is put in place now
    # and the rest only afterwards.
    wanted = [f"2VLNY{n:02d}.BMP" for n in range(6)]
    on_disk = {}
    maps_dir = os.path.join(os.path.dirname(models_dir), "maps")
    if os.path.isdir(maps_dir):
        for entry in os.listdir(maps_dir):
            if entry.upper() in wanted:
                on_disk[entry.upper()] = os.path.join(maps_dir, entry)

    if len(on_disk) == len(wanted):
        fresh_scene()
        # Only the folder the first frame sits in, so the game's own texture
        # folder cannot quietly supply the frames this test is withholding.
        saved_texanim_prefs = io_mafia_toolkit.get_preferences

        class _FramesOnly:
            textures_path = ""
            show_raw_flags = True
            show_reserved_flags = False

        io_mafia_toolkit.get_preferences = lambda: _FramesOnly()
        texanim._frames_cache.clear()
        texanim._folder_index.clear()

        _shutil.copy(on_disk[wanted[0]], os.path.join(staging, wanted[0]))
        frame_zero = bpy.data.images.load(os.path.join(staging, wanted[0]))

        before = texanim.load_frames(frame_zero, 6)
        started_short = len({f.name for f in before})

        for name in wanted[1:]:
            _shutil.copy(on_disk[name], os.path.join(staging, name))
        after = [f.name.upper() for f in texanim.load_frames(frame_zero, 6)]
        found_later = len(set(after))

        # And once the set is whole it is cached, so the tick is not walking
        # the folder twenty times a second.
        reads = {"count": 0}
        real_listdir = os.listdir

        def counted(path):
            reads["count"] += 1
            return real_listdir(path)

        os.listdir = counted
        try:
            for _ in range(20):
                texanim.load_frames(frame_zero, 6)
        finally:
            os.listdir = real_listdir

        io_mafia_toolkit.get_preferences = saved_texanim_prefs

        # Opening another file frees every image datablock while the cache
        # still holds them. Asking a freed one for its name raises, and the
        # guard meant to notice used getattr's default - which covers a missing
        # attribute, not a dead reference - so it raised instead of noticing.
        # Frame 00 stays, standing in for the material's own texture, which
        # survives because the material still points at it.
        for frame in list(bpy.data.images):
            if frame.name.upper() in wanted[1:]:
                bpy.data.images.remove(frame)
        try:
            after_removal = texanim.load_frames(frame_zero, 6)
            survived_removal = True
        except ReferenceError:
            after_removal = []
            survived_removal = False

        check("an animation finds frames that arrive after the first look",
              started_short == 1 and found_later == 6
              and after == wanted
              and reads["count"] == 0
              and survived_removal
              and [f.name.upper() for f in after_removal] == wanted,
              f"before the frames arrived {started_short} distinct image(s), "
              f"after {found_later}; twenty further lookups read the folder "
              f"{reads['count']} time(s); survived its images being "
              f"freed {survived_removal}")

    # The color key is not typed in - it is palette entry 0 of the diffuse
    # texture - so it has to be re-read whenever the material is pointed at a
    # different one. It used to be sampled only while still black, which left
    # the key on whichever texture the material happened to have first: swap
    # the texture and the old one went on deciding what was see-through.
    fresh_scene()

    def keyed_bmp(name, entry0):
        """An 8-bit BMP on disk whose palette entry 0 is *entry0*."""
        palette = bytearray(bytes((entry0[2], entry0[1], entry0[0], 0)))
        for index in range(1, 256):
            palette += bytes((index, index, index, 0))
        pixels = bytes(16)
        offset = 14 + 40 + len(palette)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as handle:
            handle.write(struct.pack("<2sIHHI", b"BM", offset + len(pixels),
                                     0, 0, offset))
            handle.write(struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 8, 0,
                                     len(pixels), 2835, 2835, 256, 256))
            handle.write(bytes(palette) + pixels)
        return bpy.data.images.load(path)

    def key_in_graph(material):
        nodes = material.node_tree.nodes
        return tuple(round(preview.find_node(nodes, label)
                           .inputs[1].default_value, 4)
                     for label in (preview.NL_KEY_CMP_R, preview.NL_KEY_CMP_G,
                                   preview.NL_KEY_CMP_B))

    red_key = keyed_bmp("key_red.bmp", (255, 0, 0))
    blue_key = keyed_bmp("key_blue.bmp", (0, 0, 255))
    keyed = previewed(DIFF | C.MTL_ALPHA_COLORKEY, ls3d_diffuse_tex=red_key)
    on_red = key_in_graph(keyed)
    keyed.ls3d_diffuse_tex = blue_key
    on_blue = key_in_graph(keyed)
    keyed.ls3d_flag_alpha_colorkey = False
    keyed.ls3d_flag_alpha_colorkey = True
    after_toggle = key_in_graph(keyed)
    check("the color key follows the texture it is read from",
          on_red == (1.0, 0.0, 0.0) and on_blue == (0.0, 0.0, 1.0)
          and after_toggle == on_blue,
          f"red-keyed {on_red}, blue-keyed {on_blue}, after toggling "
          f"{after_toggle}")

    # A pixel is the key's color when every channel is nearer the key's byte
    # than any other byte. The window used to be a third of that or less in
    # the middle tones - worked out as if the linear key were a byte value -
    # and Blender's viewport reads a texture's bytes a little off, so a key
    # like znpompeii's (118, 100, 85) missed every one of its own pixels on
    # green and the sign drew solid. Rendered, not reasoned about: the key's
    # own texel is cut, and a texel a single byte off on any channel is not,
    # at a middle tone, near white and at black, cut out and blended.
    def key_row_bmp(name, key):
        """A 7 x 1 paletted BMP: the key, then six colors one byte off it."""
        colors = [key]
        for channel in range(3):
            for step in (-1, 1):
                near = list(key)
                near[channel] = min(max(near[channel] + step, 0), 255)
                if tuple(near) == tuple(key):
                    near[channel] = key[channel] - 2 * step
                colors.append(tuple(near))
        palette = bytearray()
        for red, green, blue in colors + [(0, 0, 0)] * (256 - len(colors)):
            palette += bytes((blue, green, red, 0))
        pixels = bytes(range(7)) + bytes(1)
        offset = 14 + 40 + len(palette)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as handle:
            handle.write(struct.pack("<2sIHHI", b"BM", offset + len(pixels),
                                     0, 0, offset))
            handle.write(struct.pack("<IiiHHIIiiII", 40, 7, 1, 1, 8, 0,
                                     len(pixels), 2835, 2835, 256, 256))
            handle.write(bytes(palette) + pixels)
        return bpy.data.images.load(path)

    fresh_scene()
    # Rendered in the test scene - a second scene draws nothing in the
    # background - with every render setting put back afterwards.
    scene = bpy.context.scene
    image_settings = scene.render.image_settings
    kept_settings = [(scene.render, name, getattr(scene.render, name))
                     for name in ("engine", "film_transparent", "resolution_x",
                                  "resolution_y", "resolution_percentage",
                                  "use_border", "filter_size", "filepath")]
    # Put back in reverse: the format first, since the depths it offers
    # depend on it.
    kept_settings += [(image_settings, name, getattr(image_settings, name))
                      for name in ("color_depth", "file_format")]
    kept_settings += [(scene.eevee, "taa_render_samples",
                       scene.eevee.taa_render_samples),
                      (scene, "camera", scene.camera)]
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.film_transparent = True
    scene.render.resolution_x, scene.render.resolution_y = 7 * 16, 16
    scene.render.resolution_percentage = 100
    scene.render.use_border = False
    scene.render.filter_size = 0.01
    scene.eevee.taa_render_samples = 1
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    shot_camera = bpy.data.objects.new("shot", bpy.data.cameras.new("shot"))
    shot_camera.data.type = "ORTHO"
    shot_camera.data.ortho_scale = 7.0
    shot_camera.location = (0.0, 0.0, 5.0)
    scene.collection.objects.link(shot_camera)
    scene.camera = shot_camera
    # Linked straight into the scene, whichever collection is active.
    strip_mesh = bpy.data.meshes.new("key strip")
    strip_mesh.from_pydata([(-3.5, -0.5, 0.0), (3.5, -0.5, 0.0),
                            (3.5, 0.5, 0.0), (-3.5, 0.5, 0.0)],
                           [], [(0, 1, 2, 3)])
    strip_uv = strip_mesh.uv_layers.new()
    for corner, uv in zip(strip_uv.data, ((0.0, 0.0), (1.0, 0.0),
                                          (1.0, 1.0), (0.0, 1.0))):
        corner.uv = uv
    strip = bpy.data.objects.new("key strip", strip_mesh)
    scene.collection.objects.link(strip)
    cut_wrong = []
    for key in ((118, 100, 85), (251, 251, 251), (0, 0, 0)):
        image = key_row_bmp(f"key_row_{key[0]}.bmp", key)
        for mipmapped in (False, True):
            flags = DIFF | C.MTL_ALPHA_COLORKEY
            if mipmapped:
                flags |= C.MTL_DIFFUSE_MIPMAP
            made = previewed(flags, ls3d_diffuse_tex=image)
            strip.data.materials.clear()
            strip.data.materials.append(made)
            scene.render.filepath = os.path.join(out_dir, "key_row.exr")
            bpy.ops.render.render(write_still=True)
            shot = bpy.data.images.load(scene.render.filepath,
                                        check_existing=False)
            alphas = shot.pixels[3::4]
            shot_size = tuple(shot.size)
            # The middle of each texel, half way up the strip.
            seen = [alphas[8 * shot.size[0] + column * 16 + 8] < 0.5
                    for column in range(7)]
            bpy.data.images.remove(shot)
            if seen != [True] + [False] * 6:
                cut_wrong.append((key, "blended" if mipmapped else "cut",
                                  seen, tuple(shot_size),
                                  strip.visible_get()))
    for holder, name, value in reversed(kept_settings):
        setattr(holder, name, value)
    check("a color key cuts its own pixels and not the byte beside them",
          not cut_wrong,
          f"cut the wrong texels (key texel first, then one byte off on red, "
          f"green, blue): {cut_wrong}")

    # A graph left over from an older build has none of the nodes above, so it
    # is replaced rather than half-updated into something with loose ends.
    swatch = bpy.data.images.new("SWATCH.BMP", 4, 4)   # the scene was cleared above
    stale = previewed(DIFF, ls3d_diffuse_tex=swatch)
    del stale.node_tree[preview.GRAPH_STAMP]
    stale.node_tree.nodes.remove(
        preview.find_node(stale.node_tree.nodes, preview.NL_GLOW_ADD))
    preview.sync_material_flags(stale)
    check("a preview graph from an older build is rebuilt, not patched",
          preview.find_node(stale.node_tree.nodes, preview.NL_GLOW_ADD)
          is not None)

    # The sun flare preset carries what the game's own sky models carry, so
    # what it builds has to reach the file unchanged.
    fresh_scene()
    create_ops = ls3d_module("4ds.ops_create")
    added = bpy.ops.ls3d.add_sun_flare()
    built = {}
    for flare_name in ("glow", "lensflere"):
        made = bpy.data.objects.get(flare_name)
        built[flare_name] = [(round(g.position, 3),
                              g.material.name if g.material else None)
                             for g in made.ls3d_glows] if made else None

    bpy.ops.mesh.primitive_cube_add()
    flare_path = os.path.join(out_dir, "sunflare.4ds")
    flare_written = export_op(flare_path)
    in_file = {}
    if flare_written == {"FINISHED"}:
        flare_doc = read_file(flare_path)
        for frame in flare_doc.frames:
            if frame.lens_flare is None:
                continue
            in_file[frame.name] = [
                (round(g.position, 3),
                 flare_doc.materials[g.material_id - 1].diffuse_texture)
                for g in frame.lens_flare.glows]

    wanted_glow = [(round(o, 3), t) for o, t in create_ops.SUN_GLOW]
    wanted_flare = [(round(o, 3), t) for o, t in create_ops.SUN_FLARE]
    check("the sun flare preset reaches the file as the game's own does",
          added == {"FINISHED"}
          and built["glow"] == wanted_glow
          and built["lensflere"] == wanted_flare
          and in_file.get("glow") == wanted_glow
          and in_file.get("lensflere") == wanted_flare,
          f"built {built['glow']} and {len(built['lensflere'] or [])} "
          f"element(s); in the file "
          f"{len(in_file.get('glow', []))} and "
          f"{len(in_file.get('lensflere', []))}")

    # The character preset is Tommy's skeleton, so it has to be exactly the
    # frames his model stores, and has to reach a file as them.
    character_preset = ls3d_module("4ds.character_preset")
    preset_doc = character_preset.character_skeleton()

    def frames_by_name(doc):
        names = {i: f.name for i, f in enumerate(doc.frames, start=1)}
        described = {}
        for f in doc.frames:
            described[f.name] = dict(
                type=f.frame_type, parent=names.get(f.parent_id),
                position=f.position, rotation=f.rotation, scale=f.scale,
                cull=f.cull_flags, props=f.user_props,
                box=(f.dummy.bbox_min, f.dummy.bbox_max) if f.dummy else None,
                target=((f.target.flags, [names[i] for i in f.target.links])
                        if f.target else None),
                matrix=f.joint.matrix if f.joint else None)
        return described

    def frame_mismatches(got, wanted, skip_base_payload):
        def near(a, b):
            if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
                return len(a) == len(b) and all(near(x, y) for x, y in zip(a, b))
            if isinstance(a, float) or isinstance(b, float):
                return abs(a - b) <= 2e-5
            return a == b

        wrong = []
        for name, want in wanted.items():
            have = got.get(name)
            if have is None:
                wrong.append(f"{name} missing")
                continue
            for key, value in want.items():
                if skip_base_payload and name == "base" and key == "props":
                    continue
                # The mesh frame is the character's own mesh now, not one the
                # preset hands over, so its culling byte is whatever the mesh
                # carries rather than the one Tommy's mesh has.
                if name == "base" and key == "cull":
                    continue
                if key == "rotation":
                    # q and -q are the same turn.
                    if near(have[key], value) or near(
                            tuple(-c for c in have[key]), value):
                        continue
                if not near(have[key], value):
                    wrong.append(f"{name}.{key}")
        return wrong

    tommy_path = model("Tommy.4ds")
    if os.path.isfile(tommy_path):
        transcribed = frame_mismatches(frames_by_name(preset_doc),
                                       frames_by_name(read_file(tommy_path)),
                                       skip_base_payload=True)
        check("the character preset holds Tommy's skeleton as his model does",
              not transcribed and len(preset_doc.frames) == 24,
              f"{len(preset_doc.frames)} frame(s); differs at {transcribed[:6]}")

    def character_mesh(rig, mesh_data=None, name="base", place=None):
        """The character's own mesh, named and bound the way the preset says.

        The preset leaves the skeleton and nothing else: a character's mesh is
        its own, so the tests bring one the way anybody would - with its
        origin at the world's, unless *place* puts it somewhere else. Where
        its origin is makes no difference to the file.
        """
        mesh = mesh_data if mesh_data is not None else bpy.data.meshes.new(name)
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        if place is not None:
            obj.location = place
        obj.ls3d_frame_type = str(C.FRAME_VISUAL)
        obj.visual_type = str(C.VISUAL_SINGLEMESH)
        obj.modifiers.new("Armature", "ARMATURE").object = rig
        obj.parent = rig
        return obj

    fresh_scene()
    # Opening a model sets the count from its file; the preset must not.
    count_before = bpy.context.scene.ls3d_animated_object_count
    bpy.context.scene.ls3d_animated_object_count = 5
    added = bpy.ops.ls3d.add_character_skeleton()
    count_after = bpy.context.scene.ls3d_animated_object_count
    # Scene settings outlive fresh_scene, and a model saying it has animated
    # objects draws a warning on every export that follows.
    bpy.context.scene.ls3d_animated_object_count = count_before
    # The preset is the skeleton alone: a character's mesh is the character's
    # own, so no empty stand-in for it is left in the scene.
    left_behind = sorted(o.name for o in bpy.context.scene.objects)
    rig = bpy.data.objects.get("base_Armature")
    # No bone stands for the mesh frame: the spine and both legs are the top of
    # the skeleton and deform the mesh, so they hang from its frame - which
    # the armature holds where Tommy's is, at hip height.
    joints = [b.name for b in rig.data.bones] if rig else []
    hung = (sorted(b.name for b in rig.data.bones
                   if b.parent is None and b.use_deform) if rig else [])
    hips = ls3d_module("common.convert").to_blender_vector(ls3d_module(
        "4ds.character_preset").BASE_POSITION)
    frame_place = tuple(rig.delta_location) if rig else None
    hands = {name: (bpy.data.objects[name].parent_bone
                    if name in bpy.data.objects else None)
             for name in ("gun1", "gun2")}
    built = (added == {"FINISHED"} and rig is not None
             and "base" not in left_behind
             and not any(o.type == "MESH" for o in bpy.context.scene.objects)
             and len(joints) == 18
             and hung == ["back1", "l_thigh", "r_thigh"]
             and frame_place is not None
             and max(abs(a - b) for a, b in zip(frame_place, hips)) < 1e-6
             and hands == {"gun1": "r_hand", "gun2": "l_hand"}
             and all(name in bpy.data.objects
                     for name in ("notify", "blnd", "targetN"))
             and bpy.context.view_layer.objects.active is rig
             and count_after == 5)
    check("the character preset builds the skeleton and leaves the mesh to you",
          built,
          f"result {added}, {len(joints)} joint(s), hung from the mesh "
          f"{hung}, mesh frame at {frame_place}, hands {hands}, animated "
          f"count {count_after}, left behind {left_behind}")

    written_frames = {}
    # The game's character animations move the body through the frame called
    # base, matched by its exact name, and only turn the joints; a skinned
    # mesh called anything else keeps still through a crouch or a roll. The
    # export says so, and writes it all the same - the game's birds and dogs
    # have animations of their own keyed to their own names.
    unnamed_heard = []
    named_warned = renamed_warned = None
    renamed_result = None
    original_warn = report_module.Report.warn

    def _hear_name(self, message, fix=None):
        unnamed_heard.append(message)
        return original_warn(self, message, fix=fix)

    def _about_name(messages, name):
        return [m for m in messages if f"'{name}' is not called 'base'" in m]

    if built:
        base = character_mesh(rig)
        base.data.from_pydata([(0.0, 0.0, 0.0), (0.1, 0.0, 0.0),
                               (0.0, 0.0, 0.1)], [], [(0, 1, 2)])
        base.data.update()
        base.vertex_groups.new(name="back1").add([0, 1, 2], 1.0, "REPLACE")
        base.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
        character_path = os.path.join(out_dir, "character_preset.4ds")
        report_module.Report.warn = _hear_name
        try:
            if export_op(character_path) == {"FINISHED"}:
                written_frames = frames_by_name(read_file(character_path))
            named_warned = _about_name(unnamed_heard, "base")
            unnamed_heard.clear()
            base.name = "Pomni"
            renamed_result = export_op(
                os.path.join(out_dir, "character_renamed.4ds"))
            renamed_warned = _about_name(unnamed_heard, "Pomni")
        finally:
            report_module.Report.warn = original_warn
            base.name = "base"
    reached = frame_mismatches(written_frames, frames_by_name(preset_doc),
                               skip_base_payload=False)
    check("the character preset reaches the file as Tommy's frames",
          written_frames and not reached,
          f"{len(written_frames)} frame(s) written; differs at {reached[:6]}")
    check("a skinned mesh not called base is written, with a warning why",
          named_warned == [] and renamed_result == {"FINISHED"}
          and len(renamed_warned or []) == 1,
          f"called base: {named_warned}; called Pomni: {renamed_result}, "
          f"{renamed_warned}")

    from mathutils import Matrix as _JMatrixProbe
    # The armature stands where the mesh frame is, so the mesh object's own
    # origin makes no difference to the file: Tommy's mesh with its origin
    # moved to one side writes the same model, its vertices brought back into
    # the frame's terms - within a rounding step, the one thing a moved origin
    # costs. Left where the import puts it, it is written to the bit.
    tommy_model = model("Tommy.4ds")
    if os.path.isfile(tommy_model):
        fresh_scene()
        import_op(tommy_model)
        as_opened = os.path.join(out_dir, "origin_as_opened.4ds")
        export_op(as_opened)
        body = bpy.data.objects["base"]
        bpy.ops.object.select_all(action="DESELECT")
        body.select_set(True)
        bpy.context.view_layer.objects.active = body
        bpy.context.scene.cursor.location = (0.4, -0.3, 0.2)
        bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
        bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
        moved_origin = os.path.join(out_dir, "origin_moved.4ds")
        moved_result = export_op(moved_origin)
        drift = None
        others = ["not written"]
        if moved_result == {"FINISHED"}:
            before, after = read_file(as_opened), read_file(moved_origin)
            after.timestamp = before.timestamp
            left, right = _bits(before), _bits(after)
            changed = [key for key in left.keys() | right.keys()
                       if left.get(key) != right.get(key)]
            others = sorted({re.sub(r"\[\d+\]", "[]", key) for key in changed
                             if ".vertices[" not in key and ".bbox" not in key
                             and ".center" not in key and ".radius" not in key
                             and ".morph." not in key})
            drift = max((max(abs(a - b) for a, b in zip(u.position, v.position))
                         for fu, fv in zip(before.frames, after.frames)
                         if fu.geometry and fv.geometry
                         for lu, lv in zip(fu.geometry.lods, fv.geometry.lods)
                         for u, v in zip(lu.vertices, lv.vertices)),
                        default=None)
        check("the mesh's origin makes no difference to the file",
              moved_result == {"FINISHED"} and not others
              and drift is not None and drift < 1e-6,
              f"{moved_result}, other fields changed {others[:4]}, vertices "
              f"moved by at most {drift}")

    # The body turns about the mesh frame's own point, as the game turns it: a
    # quarter roll keyed on the armature ten frames apart keeps the hips where
    # they are halfway through. With the armature turning about the feet
    # instead, they would swing 32 cm off - and the game would not.
    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    roller = bpy.data.objects["base_Armature"]
    hips = roller.matrix_world.translation.copy()
    roller.rotation_mode = "QUATERNION"
    roller.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    roller.keyframe_insert("rotation_quaternion", frame=0)
    roller.rotation_quaternion = (0.70710678, 0.0, 0.70710678, 0.0)
    roller.keyframe_insert("rotation_quaternion", frame=10)
    bpy.context.scene.frame_set(5)
    swung = (roller.matrix_world.translation - hips).length
    bpy.context.scene.frame_set(0)
    roller.animation_data_clear()
    roller.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    check("the body turns about the mesh frame, as the game turns it",
          hips.length > 1.0 and swung < 1e-6,
          f"hips at {tuple(round(v, 4) for v in hips)}, moved {swung} m "
          f"halfway through a quarter roll")

    # An animation on the scene changes nothing the model writes: the export
    # takes it off for as long as it runs, and what it keyed goes back to rest
    # - the bones to their bones, the body to where its armature stands, and
    # 'blnd', which the crouch walk moves, to where the model has it, which it
    # kept in its Delta Transform when the animation came on. Unload Animation
    # takes it off for good: the timeline is blank, the model stands at rest,
    # and the animation stays in the list to put back on.
    walk = os.path.join(os.path.dirname(models_dir), "anims", "01ChuzeLajd.5DS")
    if os.path.isfile(tommy_model) and os.path.isfile(walk):
        fresh_scene()
        import_op(tommy_model)
        blnd_rest = bpy.data.objects["blnd"].matrix_world.copy()
        opened = os.path.join(out_dir, "unload_opened.4ds")
        export_op(opened)
        getattr(bpy.ops.import_scene, "5ds")(filepath=walk)
        bpy.context.scene.frame_set(20)
        moved_blnd = (bpy.data.objects["blnd"].matrix_world.translation
                      - blnd_rest.translation).length
        playing = os.path.join(out_dir, "unload_playing.4ds")
        playing_result = export_op(playing)
        still_on = bool(bpy.data.objects["base_Armature"].animation_data.action)
        unloaded = bpy.ops.ls3d.unload_animation()
        left_on = [o.name for o in bpy.context.scene.objects
                   if o.animation_data and o.animation_data.action]
        rig = bpy.data.objects["base_Armature"]
        posed = [b.name for b in rig.pose.bones
                 if b.matrix_basis != _JMatrixProbe.Identity(4)]
        blnd_back = max(abs(a - b) for row_a, row_b in zip(
            bpy.data.objects["blnd"].matrix_world, blnd_rest)
            for a, b in zip(row_a, row_b))
        after = os.path.join(out_dir, "unload_after.4ds")
        after_result = export_op(after)
        kept = bool(bpy.data.actions)

        def differs(path):
            # The Animated Objects count is the scene's own setting, which
            # follows what the scene keys upwards - loading the walk raises it,
            # and it is written as it stands - so it is not the model's
            # frames, and is left out here.
            if not os.path.isfile(path):
                return ["not written"]
            first, second = read_file(opened), read_file(path)
            second.timestamp = first.timestamp
            second.animated_object_count = first.animated_object_count
            left, right = _bits(first), _bits(second)
            return sorted(k for k in left.keys() | right.keys()
                          if left.get(k) != right.get(k))

        while_playing, once_unloaded = differs(playing), differs(after)
        check("an animation changes nothing the model writes, and unloads to "
              "a blank timeline",
              moved_blnd > 1e-3 and playing_result == {"FINISHED"}
              and not while_playing and still_on
              and unloaded == {"FINISHED"} and not left_on and not posed
              and blnd_back < 1e-6 and after_result == {"FINISHED"}
              and not once_unloaded and kept,
              f"blnd moved {moved_blnd:.3f} m by the walk; written while "
              f"playing {playing_result}, differs {while_playing[:3]}, "
              f"animation still on {still_on}; unloaded {unloaded}, left on "
              f"{left_on}, bones still posed {posed[:3]}, blnd back within "
              f"{blnd_back:.1e}; written after {after_result}, differs "
              f"{once_unloaded[:3]}; animations kept {kept}")

    # Set Default Mesh Origin moves nothing in the world, whatever the
    # skeleton is like: a chain of bones connected to each other, built by
    # hand, used to come apart by the hip height - each bone was moved on its
    # own, and a connected child with its parent - and an armature turned or
    # scaled as an object, as rigs made elsewhere often are, was moved by all
    # of that. Bones, the mesh and a dummy hung on a hand all stay put.
    def _world_places():
        bpy.context.view_layer.update()
        places = {}
        for obj in bpy.context.scene.objects:
            if obj.type == "ARMATURE":
                for bone in obj.data.bones:
                    where = obj.matrix_world @ bone.matrix_local
                    places[f"{obj.name}:{bone.name}"] = (
                        where.to_translation(), where.to_quaternion())
            else:
                places[obj.name] = (obj.matrix_world.to_translation(),
                                    obj.matrix_world.to_quaternion())
        return places

    def _moved(before, after):
        return max(max((before[k][0] - after[k][0]).length,
                       before[k][1].rotation_difference(after[k][1]).angle)
                   for k in before)

    origin_moves = {}
    fresh_scene()
    chain = bpy.data.objects.new("chain", bpy.data.armatures.new("chain"))
    bpy.context.scene.collection.objects.link(chain)
    bpy.context.view_layer.objects.active = chain
    bpy.ops.object.mode_set(mode="EDIT")
    spine = chain.data.edit_bones.new("back1")
    spine.head, spine.tail = (0.0, 0.0, 1.0), (0.0, 0.0, 1.3)
    for side, x in (("l", -0.1), ("r", 0.1)):
        thigh = chain.data.edit_bones.new(f"{side}_thigh")
        thigh.head, thigh.tail = (x, 0.0, 0.95), (x, 0.0, 0.5)
    above = spine
    for index, top in enumerate((1.6, 1.8)):
        bone = chain.data.edit_bones.new(f"link{index}")
        bone.head, bone.tail = above.tail, (0.0, 0.0, top)
        bone.parent, bone.use_connect = above, True
        above = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    before = _world_places()
    origin_moves["connected chain"] = (bpy.ops.ls3d.default_mesh_origin(),
                                       _moved(before, _world_places()))
    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    turned = bpy.data.objects["base_Armature"]
    turned.rotation_mode = "XYZ"
    turned.rotation_euler = (0.3, 0.1, 0.7)
    turned.location = (0.5, -0.2, 0.1)
    turned.scale = (1.2, 1.2, 1.2)
    bpy.context.view_layer.objects.active = turned
    before = _world_places()
    origin_moves["turned and scaled"] = (bpy.ops.ls3d.default_mesh_origin(),
                                         _moved(before, _world_places()))
    at_rest = ls3d_module("4ds.joint_space").own_channels_at_rest(turned)
    check("Set Default Mesh Origin moves nothing in the world",
          all(result == {"FINISHED"} and moved < 1e-5
              for result, moved in origin_moves.values()) and at_rest,
          f"{ {k: (r, f'{m:.1e}') for k, (r, m) in origin_moves.items()} }, "
          f"armature's own channels at rest {at_rest}")

    # Set Default Mesh Origin stands the armature between the character's own
    # thighs, placed from them as Tommy's is from his, so it follows the
    # character wherever it stands and whatever its size, and pressing it
    # again changes nothing. It used to stand every armature at one place,
    # Tommy's, measured from the world's center however far off the character
    # was. A skeleton with no thighs has nothing to find it from.
    from mathutils import Matrix as _OMatrix, Vector as _OVector
    _preset = ls3d_module("4ds.character_preset")
    _to_blender = ls3d_module("common.convert").to_blender_vector
    tommy_origin = _OVector(_to_blender(_preset.BASE_POSITION))
    from_thighs = _OVector(_to_blender(_preset.ORIGIN_FROM_THIGHS))

    def origin_of(rig):
        bpy.context.view_layer.update()
        return rig.matrix_world.translation.copy()

    def between_thighs(rig):
        bpy.context.view_layer.update()
        return sum((rig.matrix_world @ rig.data.bones[name].head_local
                    for name in ("l_thigh", "r_thigh")), _OVector()) * 0.5

    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    walked = bpy.data.objects["base_Armature"]
    as_added = origin_of(walked)
    walked.location = (3.0, 2.0, 0.0)            # moved as an object
    bpy.context.view_layer.objects.active = walked
    walked_result = bpy.ops.ls3d.default_mesh_origin()
    walked_at = origin_of(walked)
    walked_again = bpy.ops.ls3d.default_mesh_origin()
    walked_twice = origin_of(walked)
    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    small = bpy.data.objects["base_Armature"]
    # Four fifths of Tommy's size, standing on the same floor.
    floor = small.matrix_world.inverted() @ _OVector((0.0, 0.0, 0.0))
    small.data.transform(_OMatrix.Translation(floor) @ _OMatrix.Scale(0.8, 4)
                         @ _OMatrix.Translation(-floor))
    bpy.context.view_layer.objects.active = small
    small_result = bpy.ops.ls3d.default_mesh_origin()
    small_at = origin_of(small)
    small_want = between_thighs(small) + from_thighs
    # Posed by hand, the joints are the model - the export writes the skeleton
    # bent that way - so the origin is found from where they are posed, not
    # from where they rest. It stayed put before.
    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    posed_rig = bpy.data.objects["base_Armature"]
    bpy.context.view_layer.objects.active = posed_rig
    bpy.ops.object.mode_set(mode="POSE")
    for name in ("l_thigh", "r_thigh"):
        posed_bone = posed_rig.pose.bones[name]
        posed_bone.matrix = (_OMatrix.Translation((0.0, 0.0, -0.25))
                             @ posed_bone.matrix)
        bpy.context.view_layer.update()
    posed_middle = sum((posed_rig.matrix_world @ posed_rig.pose.bones[n].head
                        for n in ("l_thigh", "r_thigh")), _OVector()) * 0.5
    posed_result = bpy.ops.ls3d.default_mesh_origin()
    posed_at = origin_of(posed_rig)
    bpy.ops.object.mode_set(mode="OBJECT")

    fresh_scene()
    bpy.ops.ls3d.add_joint()
    thighless = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.context.view_layer.objects.active = thighless
    thighless_offered = bpy.ops.ls3d.default_mesh_origin.poll()
    check("the default mesh origin stands between the character's own thighs",
          (as_added - tommy_origin).length < 1e-6
          and walked_result == walked_again == {"FINISHED"}
          and (walked_at - tommy_origin - _OVector((3.0, 2.0, 0.0))).length < 1e-5
          and (walked_twice - walked_at).length < 1e-6
          and small_result == {"FINISHED"}
          and (small_at - small_want).length < 1e-5
          and (small_at - tommy_origin).length > 0.1
          and posed_result == {"FINISHED"}
          and (posed_at - posed_middle - from_thighs).length < 1e-5
          and (posed_at - tommy_origin).length > 0.2
          and not thighless_offered,
          f"Tommy's own at {tuple(round(v, 4) for v in as_added)}; moved to "
          f"(3, 2, 0) {walked_result} -> "
          f"{tuple(round(v, 4) for v in walked_at)}, again "
          f"{tuple(round(v, 4) for v in walked_twice)}; four fifths the size "
          f"{small_result} -> {tuple(round(v, 4) for v in small_at)}, wanted "
          f"{tuple(round(v, 4) for v in small_want)}; posed 25 cm down "
          f"{posed_result} -> {tuple(round(v, 4) for v in posed_at)}, wanted "
          f"{tuple(round(v, 4) for v in (posed_middle + from_thighs))}; "
          f"offered with no thighs {thighless_offered}")

    # On a model the game ships, it changes nothing: every one of its
    # characters already stands at its hips, placed by hand up to 6.6 cm from
    # where the thighs alone would put it, and is left exactly as it came -
    # Tommy, a guard 5 cm off that point, one scaled a little, and the cow.
    kept_originals, changed_originals = [], []
    for name in ("Tommy.4ds", "Bguard01.4ds", "civil16.4ds", "krava.4ds"):
        path = model(name)
        if not os.path.isfile(path):
            continue
        fresh_scene()
        import_op(path)
        bpy.context.view_layer.update()
        rig = next(o for o in bpy.data.objects if o.type == "ARMATURE"
                   and any(m.type == "MESH" and m.find_armature() is o
                           for m in bpy.data.objects))
        before = (tuple(rig.delta_location), tuple(rig.delta_rotation_quaternion),
                  tuple(rig.delta_scale), tuple(rig.location),
                  tuple(rig.rotation_quaternion), tuple(rig.scale),
                  [tuple(b.head_local) + tuple(b.tail_local)
                   for b in rig.data.bones])
        bpy.context.view_layer.objects.active = rig
        pressed = bpy.ops.ls3d.default_mesh_origin()
        after = (tuple(rig.delta_location), tuple(rig.delta_rotation_quaternion),
                 tuple(rig.delta_scale), tuple(rig.location),
                 tuple(rig.rotation_quaternion), tuple(rig.scale),
                 [tuple(b.head_local) + tuple(b.tail_local)
                  for b in rig.data.bones])
        (kept_originals if pressed == {"FINISHED"} and after == before
         else changed_originals).append(name)
    check("Set Default Mesh Origin leaves the game's own characters as they came",
          kept_originals and not changed_originals,
          f"left exactly as they came {kept_originals}, changed "
          f"{changed_originals}")

    # Previewing an animation raises the Animated Objects count - it follows
    # what the scene keys - and it can always be set back to zero: typed while
    # the animation is on, it stays zero through playing, unloading and putting
    # the animation back, and the model is written saying zero.
    if os.path.isfile(tommy_model) and os.path.isfile(walk):
        scene = bpy.context.scene
        fresh_scene()
        import_op(tommy_model)
        getattr(bpy.ops.import_scene, "5ds")(filepath=walk)
        bpy.context.view_layer.update()
        raised = scene.ls3d_animated_object_count
        scene.ls3d_animated_object_count = 0
        scene.frame_set(20)
        bpy.ops.ls3d.unload_animation()
        bpy.ops.ls3d.activate_action()
        scene.frame_set(5)
        bpy.context.view_layer.update()
        zero_path = os.path.join(out_dir, "count_zero.4ds")
        zero_result = export_op(zero_path)
        written_count = (read_file(zero_path).animated_object_count
                         if zero_result == {"FINISHED"} else None)
        scene.ls3d_animated_count_pinned = False
        scene.ls3d_animated_object_count = 0
        check("the Animated Objects count can be set back to zero with an "
              "animation loaded",
              raised > 0 and written_count == 0,
              f"raised to {raised} by the preview; written {written_count}")

    # A character whose mesh frame rests off hip height is carried there by
    # every animation, and the whole body with it, so the export says so - and
    # Set Default Mesh Origin puts it back without moving a bone -
    # pressed with the mesh selected, as easily as the armature.
    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    rig = bpy.data.objects["base_Armature"]
    body = character_mesh(rig)
    body.data.from_pydata([(0.0, 0.0, 1.0), (0.1, 0.0, 1.0), (0.0, 0.1, 1.0)],
                          [], [(0, 1, 2)])
    body.data.update()
    body.vertex_groups.new(name="back1").add([0, 1, 2], 1.0, "REPLACE")
    body.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
    # The frame at the feet, the bones left where they are - as a skeleton
    # built from an armature at the world's center stands.
    rig.data.transform(_OMatrix.Translation(rig.delta_location))
    rig.delta_location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    heard = []
    original_warn = report_module.Report.warn

    def _hear_drift(self, message, fix=None):
        heard.append(message)
        return original_warn(self, message, fix=fix)

    report_module.Report.warn = _hear_drift
    try:
        low = export_op(os.path.join(out_dir, "frame_at_feet.4ds"))
        low_warned = [m for m in heard if "from where Tommy's does" in m]
        heard.clear()
        heads = {b.name: (rig.matrix_world @ b.head_local).copy()
                 for b in rig.data.bones}
        placed_mesh = body.matrix_world.copy()
        bpy.context.view_layer.objects.active = body
        bpy.ops.ls3d.default_mesh_origin()
        bpy.context.view_layer.update()
        fixed = export_op(os.path.join(out_dir, "frame_at_hips.4ds"))
        fixed_warned = [m for m in heard if "from where Tommy's does" in m]
    finally:
        report_module.Report.warn = original_warn
    stayed = max(((rig.matrix_world @ b.head_local) - heads[b.name]).length
                 for b in rig.data.bones)
    mesh_moved = max(abs(a - b) for row_a, row_b in zip(body.matrix_world,
                                                         placed_mesh)
                     for a, b in zip(row_a, row_b))
    check("a character's mesh frame off hip height is said, and put back "
          "without moving a bone",
          low == {"FINISHED"} and len(low_warned) == 1
          and fixed == {"FINISHED"} and not fixed_warned
          and stayed < 1e-5 and mesh_moved < 1e-5,
          f"at the feet {low} warned {len(low_warned)}; put back {fixed} "
          f"warned {len(fixed_warned)}; bones moved {stayed:.2e}, mesh moved "
          f"{mesh_moved:.2e}")

    # ── joints as one armature, where the game puts them ──────────────────────
    from mathutils import Matrix as _JMatrix, Quaternion as _JQuaternion
    from mathutils import Vector as _JVector
    _convert = ls3d_module("common.convert")
    _anim_codec = ls3d_module("5ds.codec")

    def file_worlds(doc, keys=None, frame=0):
        """Every frame's world matrix as the game chains it, keys applied."""
        by_id = {i: f for i, f in enumerate(doc.frames, start=1)}
        tracks = keys or {}
        worlds = {}

        def sample(pairs, kind):
            pairs = sorted(pairs)
            if not pairs:
                return None
            if frame <= pairs[0][0]:
                return pairs[0][1]
            if frame >= pairs[-1][0]:
                return pairs[-1][1]
            for (fa, va), (fb, vb) in zip(pairs, pairs[1:]):
                if fa <= frame <= fb:
                    t = (frame - fa) / (fb - fa) if fb != fa else 0.0
                    if kind == "turn":
                        return tuple(_JQuaternion(va).slerp(_JQuaternion(vb), t))
                    return tuple(a + (b - a) * t for a, b in zip(va, vb))
            return pairs[-1][1]

        def world(index):
            if index in worlds:
                return worlds[index]
            f = by_id[index]
            position, rotation, scale = f.position, f.rotation, f.scale
            track = tracks.get(f.name.lower())
            if track is not None:
                position = sample(track.positions, "move") or position
                rotation = sample(track.rotations, "turn") or rotation
                scale = sample(track.scales, "move") or scale
            local = _JMatrix.LocRotScale(
                _JVector(_convert.to_blender_vec(position)),
                _convert.to_blender_quaternion(rotation),
                _JVector(_convert.to_blender_vec(scale)))
            result = local if not f.parent_id else world(f.parent_id) @ local
            worlds[index] = result
            return result

        for index in by_id:
            world(index)
        return worlds

    def game_joint_table(doc, mesh_index):
        """The joints a skinned mesh skins with, by number, as the game gathers
        them: walking down from the mesh, first joint per number."""
        by_id = {i: f for i, f in enumerate(doc.frames, start=1)}
        children = {}
        for i, f in by_id.items():
            children.setdefault(f.parent_id, []).append(i)
        table = {}

        def walk(index):
            for child in children.get(index, ()):
                if by_id[child].joint is not None:
                    table.setdefault(by_id[child].joint.joint_id, child)
                walk(child)

        walk(mesh_index)
        return table

    def only_armature():
        rigs = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
        return rigs[0] if len(rigs) == 1 else None, len(rigs)

    def bone_owner(name):
        """The armature holding the bone *name*, or ``None``.

        A model whose mesh hangs from a joint has its joints on two armatures:
        the ones above the mesh on their own, the character's hung on the
        joint the mesh hangs from.
        """
        return next((o for o in bpy.context.scene.objects
                     if o.type == "ARMATURE" and name in o.data.bones), None)

    # Every bone sits where the game puts its joint: the whole chain of
    # transforms, joint and mesh scale included. Bones used to leave joint
    # scale out, which put Tommy's feet 4.4 cm too high and his hands 4.2 cm
    # short of his wrists.
    placement = {}
    for model_name in ("!TommyHIGH.4ds", "Paulie.4ds", "pes03.4ds",
                       "!hadice model.4ds", "I04Delnik01+.4ds"):
        if not os.path.isfile(model(model_name)):
            continue
        fresh_scene()
        source = read_file(model(model_name))
        import_op(model(model_name))
        bpy.context.view_layer.update()
        worlds = file_worlds(source)
        worst = 0.0
        for index, frame in enumerate(source.frames, start=1):
            if frame.joint is None:
                continue
            owner = bone_owner(frame.name)
            if owner is None:
                worst = float("inf")
                continue
            head = owner.matrix_world @ owner.data.bones[frame.name].head_local
            worst = max(worst, (head - worlds[index].to_translation()).length)
        placement[model_name] = round(worst * 1000.0, 4)
    check("every bone sits where the game puts its joint",
          placement and all(isinstance(v, float) and v < 0.1
                            for v in placement.values()),
          f"worst distance in mm per model: {placement}")

    # A joint above the skinned mesh (I04Delnik01+), a lone joint beside the
    # skeleton (BobAut01) and joints with no skinned mesh at all (pohar) each
    # come back as they went. A joint not below the mesh is an armature of its
    # own, at the model's origin - Delnik's character armature hung on the one
    # its mesh hangs from, the file's own chain - and pohar is one armature.
    def skin_by_joint(doc):
        found = {}
        for mesh_index, frame in enumerate(doc.frames, start=1):
            if frame.skin is None:
                continue
            table = game_joint_table(doc, mesh_index)
            names = {number: doc.frames[i - 1].name for number, i in table.items()}
            for level, lod in enumerate(frame.skin.lods):
                for number, group in enumerate(lod.groups):
                    joint = names.get(number, f"#{number}")
                    parent = (names.get(group.parent_group - 1)
                              if group.parent_group else "mesh")
                    found[(frame.name, level, joint)] = (
                        group.unweighted_count, group.weighted_count,
                        parent if group.weighted_count else None,
                        group.inverse_bind, sorted(group.weights))
                found[(frame.name, level, "root")] = lod.root_unweighted
        return found

    unusual = {}
    unusual_failed = []
    for model_name in ("I04Delnik01+.4ds", "BobAut01.4ds",
                       "Mise06c Tom01 pohar.4ds"):
        if not os.path.isfile(model(model_name)):
            continue
        fresh_scene()
        source = read_file(model(model_name))
        import_op(model(model_name))
        rig, rig_count = only_armature()
        path = os.path.join(out_dir, f"joints_{model_name}")
        if export_op(path) != {"FINISHED"}:
            unusual[model_name] = f"{rig_count} armature(s), export refused"
            unusual_failed.append(model_name)
            continue
        written = read_file(path)
        wrong = frame_mismatches(frames_by_name(written), frames_by_name(source),
                                 skip_base_payload=False)
        before, after = skin_by_joint(source), skin_by_joint(written)
        for key, value in before.items():
            other = after.get(key)
            if not isinstance(value, tuple):
                if other != value:
                    wrong.append(f"{key}")
                continue
            if other is None or value[:3] != other[:3] or not all(
                    abs(a - b) <= 1e-4 for a, b in zip(value[3], other[3])) or \
                    len(value[4]) != len(other[4]) or not all(
                    abs(a - b) <= 1e-5 for a, b in zip(value[4], other[4])):
                wrong.append(f"skin {key}")
        unusual[model_name] = (f"{rig_count} armature(s), "
                               f"{len(written.frames)}/{len(source.frames)} "
                               f"frames, differs at {wrong[:4]}")
        rigs_expected = 1 if model_name == "Mise06c Tom01 pohar.4ds" else 2
        if rig_count != rigs_expected or wrong:
            unusual_failed.append(model_name)
    check("models with joints above, beside or without a mesh come back as they went",
          len(unusual) == 3 and not unusual_failed, f"{unusual}")

    # Animations play as the game plays them: every joint's position and every
    # skinned vertex, worked out from the model's own chain, the animation's
    # keys and the file's skin data, against what Blender shows. The walk only
    # turns joints; 01ChuzeLajd also moves them and sets Tommy's joint scale
    # back to 1.0, shrinking him; SLEPchuz1 turns the joint Delnik's mesh hangs
    # from, which has to carry the whole body. Written back out, the keys come
    # back as they were.
    # A character made on the preset the usual way: a mesh put into 'base' and
    # parented to the armature with automatic weights. Those weights spread a
    # vertex over as many joints as they like and do not add up to 1 - Blender
    # scales them when it moves the mesh, the game does not - so the export
    # names the problem, and after Weight Paint's Limit Total and Normalize All
    # it writes the character. It is then played below with the game's walk.
    preset_character = None
    preset_weights = {}
    if os.path.isfile(model("Tommy.4ds")):
        fresh_scene()
        import_op(model("Tommy.4ds"))
        body = bpy.data.objects["base"].data.copy()
        body.use_fake_user = True
        fresh_scene()
        bpy.ops.ls3d.add_character_skeleton()
        rig = bpy.data.objects["base_Armature"]
        base = character_mesh(rig, body)
        base.shape_key_clear()
        base.vertex_groups.clear()
        base.ls3d_morph_groups.clear()
        for obj in bpy.context.scene.objects:
            obj.select_set(obj in (base, rig))
        bpy.context.view_layer.objects.active = rig
        preset_weights["parent"] = bpy.ops.object.parent_set(type="ARMATURE_AUTO")
        character_out = os.path.join(out_dir, "preset_character.4ds")
        raw, raw_lines = _report_of(lambda: getattr(bpy.ops.export_scene, "4ds")(
            filepath=character_out, fix_multi_influences=True,
            fix_non_parent_child=True))
        preset_weights["raw"] = (raw, any("not 1.0" in line for line in raw_lines))
        bpy.context.view_layer.objects.active = base
        bpy.ops.object.vertex_group_limit_total(group_select_mode="BONE_DEFORM",
                                                limit=2)
        bpy.ops.object.vertex_group_normalize_all(group_select_mode="BONE_DEFORM",
                                                  lock_active=False)
        preset_weights["cleaned"] = getattr(bpy.ops.export_scene, "4ds")(
            filepath=character_out, fix_multi_influences=True,
            fix_non_parent_child=True)
        body.use_fake_user = False
        if preset_weights["cleaned"] == {"FINISHED"}:
            preset_character = character_out
    check("a character made on the preset with automatic weights exports once cleaned up",
          preset_character is not None
          and preset_weights["raw"] == ({"CANCELLED"}, True),
          f"{preset_weights}")

    anims_dir = os.path.join(os.path.dirname(models_dir), "anims")
    playback = {}
    cases = [("Tommy.4ds", "WALK1.5DS", (0, 7, 13)),
             ("Tommy.4ds", "01ChuzeLajd.5DS", (0, 11, 27)),
             ("I04Delnik01+.4ds", "SLEPchuz1.5DS", (0, 7, 13))]
    if preset_character is not None:
        cases.append((preset_character, "WALK1.5DS", (0, 7, 13)))
    for model_name, anim_name, frames in cases:
        anim_path = os.path.join(anims_dir, anim_name)
        if not (os.path.isfile(model(model_name)) and os.path.isfile(anim_path)):
            continue
        fresh_scene()
        source = read_file(model(model_name))
        animation = _anim_codec.read_animation_file(anim_path)
        import_op(model(model_name))
        getattr(bpy.ops.import_scene, "5ds")(filepath=anim_path)
        for rig in (o for o in bpy.context.scene.objects if o.type == "ARMATURE"):
            for pose_bone in rig.pose.bones:
                for constraint in pose_bone.constraints:
                    constraint.mute = True  # target frames pose on their own
        keys = {t.name.lower(): t for t in animation.tracks}
        mesh_index, mesh_frame = next((i, f) for i, f in
                                      enumerate(source.frames, start=1) if f.skin)
        table = game_joint_table(source, mesh_index)
        skin_obj = bpy.data.objects[mesh_frame.name]
        worst_joint = worst_vertex = 0.0
        for frame_number in frames:
            bpy.context.scene.frame_set(frame_number)
            worlds = file_worlds(source, keys, frame_number)
            for index, frame in enumerate(source.frames, start=1):
                if frame.joint is not None:
                    owner = bone_owner(frame.name)
                    head = owner.matrix_world @ owner.pose.bones[frame.name].head
                    worst_joint = max(worst_joint,
                                      (head - worlds[index].to_translation()).length)
            lod = mesh_frame.skin.lods[0]
            vertices = mesh_frame.geometry.lods[0].vertices
            mesh_world = worlds[mesh_index]
            expected, cursor = [], 0
            for number, group in enumerate(lod.groups):
                joint_world = worlds[table[number]] @ _convert.matrix_from_mafia_rows(
                    group.inverse_bind)
                if group.parent_group:
                    other = lod.groups[group.parent_group - 1]
                    parent_world = (worlds[table[group.parent_group - 1]]
                                    @ _convert.matrix_from_mafia_rows(other.inverse_bind))
                else:
                    parent_world = mesh_world
                for _ in range(group.unweighted_count):
                    point = _JVector(_convert.to_blender_vector(vertices[cursor].position))
                    expected.append(joint_world @ point)
                    cursor += 1
                for weight in group.weights:
                    point = _JVector(_convert.to_blender_vector(vertices[cursor].position))
                    expected.append((joint_world @ point) * weight
                                    + (parent_world @ point) * (1.0 - weight))
                    cursor += 1
            for _ in range(lod.root_unweighted):
                point = _JVector(_convert.to_blender_vector(vertices[cursor].position))
                expected.append(mesh_world @ point)
                cursor += 1
            graph = bpy.context.evaluated_depsgraph_get()
            evaluated = skin_obj.evaluated_get(graph)
            mesh = evaluated.to_mesh()
            shown = [skin_obj.matrix_world @ v.co for v in mesh.vertices]
            evaluated.to_mesh_clear()
            for want, have in zip(expected, shown):
                worst_vertex = max(worst_vertex, (want - have).length)

        anim_out = os.path.join(out_dir, f"played_{anim_name}")
        worst_key = None
        if getattr(bpy.ops.export_scene, "5ds")(filepath=anim_out,
                                                write_motion=False) == {"FINISHED"}:
            again = {t.name.lower(): t for t in
                     _anim_codec.read_animation_file(anim_out).tracks}
            worst_key = 0.0
            for name, track in keys.items():
                if name not in again:
                    continue
                for kind in ("rotations", "positions", "scales"):
                    written = dict(getattr(again[name], kind))
                    for frame_number, value in getattr(track, kind):
                        if frame_number not in written:
                            continue
                        other = written[frame_number]
                        delta = max(abs(a - b) for a, b in zip(value, other))
                        if kind == "rotations":
                            delta = min(delta, max(abs(a + b) for a, b in zip(value, other)))
                        worst_key = max(worst_key, delta)
        playback[f"{os.path.basename(model_name)} + {anim_name}"] = (
            round(worst_joint * 1000.0, 4), round(worst_vertex * 1000.0, 4),
            worst_key)
    check("animations play back as the game plays them, and write back the same",
          len(playback) == len(cases) and all(
              joint < 0.1 and vertex < 0.1 and key is not None and key < 1e-5
              for joint, vertex, key in playback.values()),
          "worst joint mm, vertex mm, key difference: " + str(playback))

    # A character made the traditional way: an armature of plain Blender bones
    # and a mesh parented to it with automatic weights. Once as it comes, the
    # mesh frame at the model's origin, and once with the mesh frame set at hip
    # height and the mesh's origin moved somewhere else entirely - which makes
    # no difference to where anything is. Either exports, and imports back
    # with every joint where it was.
    homemade = {}
    for with_frame_bone in (False, True):
        fresh_scene()
        bpy.ops.object.armature_add(enter_editmode=True, location=(0.0, 0.0, 0.0))
        rig = bpy.context.object
        bones = rig.data.edit_bones
        bones[0].name = "back1"
        bones["back1"].head, bones["back1"].tail = (0.0, 0.0, 1.0), (0.0, 0.0, 1.4)
        neck = bones.new("neck")
        neck.head, neck.tail = (0.0, 0.0, 1.4), (0.0, 0.0, 1.7)
        neck.parent = bones["back1"]
        # Thighs, which the default mesh origin is found from.
        for side, x in (("l", -0.1), ("r", 0.1)):
            thigh = bones.new(f"{side}_thigh")
            thigh.head, thigh.tail = (x, 0.0, 0.95), (x, 0.0, 0.5)
        bpy.ops.object.mode_set(mode="OBJECT")
        if with_frame_bone:
            bpy.context.view_layer.objects.active = rig
            bpy.ops.ls3d.default_mesh_origin()
        # Every joint writes an influence box; the painted weights below are
        # what this character exports, so the default one does.
        bpy.context.view_layer.objects.active = rig
        bpy.ops.ls3d.add_influence_boxes()
        wanted = {b.name: (rig.matrix_world @ b.head_local).copy()
                  for b in rig.data.bones}

        bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.15, depth=0.8,
                                            location=(0.0, 0.0, 1.35))
        body = bpy.context.object
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        body.name = "base"
        for _ in range(3):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.subdivide()
            bpy.ops.object.mode_set(mode="OBJECT")
        body.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
        body.ls3d_frame_type = str(C.FRAME_VISUAL)
        body.visual_type = str(C.VISUAL_SINGLEMESH)
        for obj in bpy.context.scene.objects:
            obj.select_set(obj in (body, rig))
        bpy.context.view_layer.objects.active = rig
        parented = bpy.ops.object.parent_set(type="ARMATURE_AUTO")
        # Automatic weights do not add up to 1, which Blender papers over and
        # the game does not; the usual clean-up before a game export.
        bpy.context.view_layer.objects.active = body
        bpy.ops.object.vertex_group_limit_total(group_select_mode="BONE_DEFORM",
                                                limit=2)
        bpy.ops.object.vertex_group_normalize_all(group_select_mode="BONE_DEFORM",
                                                  lock_active=False)
        if with_frame_bone:
            # The mesh's origin to one side, the vertices staying put.
            rig.select_set(False)
            bpy.context.scene.cursor.location = (0.3, -0.2, 0.4)
            bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
            bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)

        label = ("mesh frame at the hips, origin elsewhere" if with_frame_bone
                 else "plain")
        path = os.path.join(out_dir, f"homemade_{with_frame_bone}.4ds")
        written = getattr(bpy.ops.export_scene, "4ds")(
            filepath=path, fix_multi_influences=True, fix_non_parent_child=True)
        if written != {"FINISHED"}:
            homemade[label] = f"parent {parented}, export {written}"
            continue
        fresh_scene()
        import_op(path)
        back, _count = only_armature()
        worst = max(((back.matrix_world @ back.data.bones[name].head_local - where).length
                     for name, where in wanted.items()), default=float("inf"))
        weighted = any(v.groups for v in bpy.data.objects["base"].data.vertices)
        # Exported again, it is the same file: the first export already wrote
        # the values a re-import gives back - influence boxes included, since a
        # box is held to its joint and knows nothing of how long the bone is.
        again = os.path.join(out_dir, f"homemade_{with_frame_bone}_again.4ds")
        getattr(bpy.ops.export_scene, "4ds")(filepath=again)
        first, second = read_file(path), read_file(again)
        second.timestamp = first.timestamp
        left, right = _bits(first), _bits(second)
        changed = sorted({re.sub(r"\[\d+\]", "[]", key) for key in left.keys() | right.keys()
                          if left.get(key) != right.get(key)})
        box_step = max((abs(a - b) for fa, fb in zip(first.frames, second.frames)
                        if fa.joint and fb.joint
                        for a, b in zip(fa.joint.matrix, fb.joint.matrix)), default=0.0)
        # ... and from then on nothing moves at all.
        fresh_scene()
        import_op(again)
        third_path = os.path.join(out_dir, f"homemade_{with_frame_bone}_third.4ds")
        getattr(bpy.ops.export_scene, "4ds")(filepath=third_path)
        third = read_file(third_path)
        third.timestamp = second.timestamp
        settled = _bits(second) == _bits(third)
        same = not changed and settled
        homemade[label] = (worst * 1000.0, weighted, same, changed, box_step)
    check("a character made the traditional way exports, comes back in place, "
          "and exports the same again",
          len(homemade) == 2 and all(isinstance(v, tuple) and v[0] < 1e-6 and v[1]
                                     and v[2] for v in homemade.values()),
          f"worst joint distance mm, skin weights present, same again, changed, "
          f"box step: {homemade}")

    # The game walks from one key to the next in a straight line, with no
    # easing at either end. Blender inserts Bezier keys by default, which slow
    # into every key and speed out of it - the same keys playing at speeds the
    # game will never produce.
    fresh_scene()
    editing = bpy.context.preferences.edit
    editing.keyframe_new_interpolation_type = "BEZIER"
    eased_clip = anim_format.Animation(end_frame=6)
    eased_track = anim_format.Track(name="smoothie")
    eased_track.rotations = [(0, (1.0, 0.0, 0.0, 0.0)),
                             (6, (0.0, 1.0, 0.0, 0.0))]
    eased_clip.tracks = [eased_track]
    eased_path = os.path.join(out_dir, "easing.5ds")
    anim_format.write_animation_file(eased_clip, eased_path)

    bpy.ops.object.empty_add()
    bpy.context.object.name = "smoothie"
    getattr(bpy.ops.import_scene, "5ds")(filepath=eased_path)
    default_now = editing.keyframe_new_interpolation_type
    smoothie = bpy.data.objects["smoothie"]
    bag = anim_io.channelbag(smoothie)
    all_linear = all(point.interpolation == "LINEAR"
                     for curve in bag.fcurves
                     for point in curve.keyframe_points)

    # Ease one by hand and the export has to say so.
    for curve in bag.fcurves:
        curve.keyframe_points[0].interpolation = "BEZIER"
        break
    seen = anim_io.eased_curves(smoothie)
    said = []
    original_warn = report_module.Report.warn

    def _watch(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _watch
    try:
        sent = getattr(bpy.ops.export_scene, "5ds")(
            filepath=os.path.join(out_dir, "easing_out.5ds"))
    finally:
        report_module.Report.warn = original_warn
    check("keys are linear by default, and easing is reported on the way out",
          default_now == "LINEAR" and all_linear and seen == {"BEZIER": 1}
          and sent == {"FINISHED"}
          and any("linear interpolation" in m for m in said),
          f"new-key default {default_now}, imported all linear={all_linear}, "
          f"eased {seen}, warnings {said or 'none'}")

    # Every operator that reports has to put the report on screen. Without
    # that last call it runs, prints to the console nobody has open, and looks
    # like it did nothing at all.
    fresh_scene()
    shown = []
    original_show = report_module.Report.show

    def _count_shown(self):
        shown.append(1)

    report_module.Report.show = _count_shown
    try:
        empty_scene = bpy.ops.ls3d.check_animation()
        seen_when_empty = len(shown)

        bpy.ops.object.empty_add()
        keyed = bpy.context.object
        keyed.name = "keyed"
        keyed.rotation_mode = "QUATERNION"
        keyed.keyframe_insert("rotation_quaternion", frame=0)
        keyed.keyframe_insert("rotation_quaternion", frame=8)
        bpy.context.scene.frame_end = 8
        shown.clear()
        with_animation = bpy.ops.ls3d.check_animation()
        seen_with = len(shown)

        shown.clear()
        sent = getattr(bpy.ops.export_scene, "5ds")(
            filepath=os.path.join(out_dir, "shown.5ds"))
        seen_on_export = len(shown)
    finally:
        report_module.Report.show = original_show
    check("checking and exporting an animation say so on screen",
          empty_scene == {"CANCELLED"} and seen_when_empty == 1
          and with_animation == {"FINISHED"} and seen_with == 1
          and sent == {"FINISHED"} and seen_on_export == 1,
          f"empty scene {empty_scene} shown {seen_when_empty}, "
          f"with animation {with_animation} shown {seen_with}, "
          f"export {sent} shown {seen_on_export}")

    # A mirror reflects along its object's local +Y, and the mesh only says
    # where that reflection is painted. A surface turned any other way is drawn
    # in one place and reflects from another, so it is worth saying so.
    from mathutils import Matrix as _Matrix

    def _mirror(name, turn):
        bpy.ops.mesh.primitive_plane_add()
        obj = bpy.context.object
        obj.name = name
        if turn is not None:
            obj.data.transform(turn)
            obj.data.update()
        obj.ls3d_frame_type = str(C.FRAME_VISUAL)
        obj.visual_type = str(C.VISUAL_MIRROR)
        obj.ls3d_mirror_box_center = (0.0, 1.0, 0.0)
        obj.ls3d_mirror_box_x = (1.0, 0.0, 0.0)
        obj.ls3d_mirror_box_y = (0.0, 1.0, 0.0)
        obj.ls3d_mirror_box_z = (0.0, 0.0, 1.0)
        return obj

    fresh_scene()
    _mirror("facing", _Matrix.Rotation(math.radians(-90.0), 4, "X"))
    _mirror("flipped", _Matrix.Rotation(math.radians(90.0), 4, "X"))
    _mirror("tilted", None)          # Blender's own plane, facing local +Z
    said = []
    original_warn = report_module.Report.warn

    def _watch(self, message, fix=None):
        said.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _watch
    try:
        result = export_op(os.path.join(out_dir, "mirror_facing.4ds"))
    finally:
        report_module.Report.warn = original_warn
    about = {name: [m for m in said if f"'{name}'" in m]
             for name in ("facing", "flipped", "tilted")}
    check("a mirror turned away from its local +Y is reported",
          result == {"FINISHED"} and not about["facing"]
          and any("-Y" in m for m in about["flipped"])
          and any("degrees off" in m for m in about["tilted"]),
          f"facing={about['facing']}, flipped={about['flipped']}, "
          f"tilted={about['tilted']}")

    # ... and the mirror the Add menu builds is already the right way round,
    # with the view box out in front where what it reflects actually is.
    fresh_scene()
    # Scene settings outlive fresh_scene. Left above zero by an earlier model,
    # the count draws its own export warning, which says nothing about mirrors.
    bpy.context.scene.ls3d_animated_object_count = 0
    bpy.ops.ls3d.add_mirror()
    made = bpy.context.object
    made.data.calc_loop_triangles()
    normal = made.data.polygons[0].normal
    box_center = tuple(made.ls3d_mirror_box_center)
    said = []
    report_module.Report.warn = _watch
    try:
        result = export_op(os.path.join(out_dir, "mirror_made.4ds"))
    finally:
        report_module.Report.warn = original_warn
    stored = None
    if result == {"FINISHED"}:
        for frame in read_file(os.path.join(out_dir, "mirror_made.4ds")).frames:
            if frame.mirror is not None:
                stored = frame.mirror
    check("the Add menu builds a mirror facing +Y with its box in front",
          result == {"FINISHED"} and not said
          and normal.y > 0.99 and box_center[1] > 0.0
          and stored is not None and stored.view_matrix[14] > 0.0,
          f"mesh normal {tuple(round(c, 3) for c in normal)}, box at "
          f"{tuple(round(c, 3) for c in box_center)}, warnings "
          f"{said or 'none'}, stored view box z "
          f"{stored.view_matrix[14] if stored else None}")

    # A billboard from the menu turns freely around the upright axis, which is
    # what 199 of the game's 293 billboard frames do - more than all the other
    # combinations together - and it reaches the file that way rather than as
    # the property defaults, which would have written axis 0 locked.
    fresh_scene()
    bpy.ops.ls3d.add_billboard()
    board = bpy.context.object
    board_path = os.path.join(out_dir, "billboard.4ds")
    result = export_op(board_path)
    written = None
    if result == {"FINISHED"}:
        for frame in read_file(board_path).frames:
            if frame.billboard is not None:
                written = frame.billboard
    check("the Add menu builds a billboard the game's own way",
          result == {"FINISHED"} and written is not None
          and written.axis == 2 and not written.axis_locked
          and int(board.visual_type) == C.VISUAL_BILLBOARD
          and board.render_flags2 == C.DEFAULT_RENDER_FLAGS2_BILLBOARD,
          f"axis {getattr(written, 'axis', None)}, locked "
          f"{getattr(written, 'axis_locked', None)}, visual type "
          f"{board.visual_type}, render flags2 {board.render_flags2:#04x}")

    # Morph groups are shaped in the viewport, not set up once and left, so the
    # panel lives in the sidebar beside the animation one. Its poll stays open
    # so the tab does not appear and vanish with the selection - what it cannot
    # draw it explains instead.
    morph_panel = ui_4ds.The4DSMorphPanel
    # A plain class carrying the panel's methods, so the self._ready lookup
    # inside draw() resolves the way it does on the real panel.
    _MorphMethods = type("MorphMethods", (), dict(morph_panel.__dict__))

    def morph_rows_for(obj):
        recorder = _MatLayout()
        panel = _MorphMethods()
        panel.layout = recorder
        _MorphMethods.draw(panel, bpy.context)
        return recorder

    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    plain = bpy.context.object
    plain_rows = morph_rows_for(plain)

    plain.ls3d_frame_type = str(C.FRAME_VISUAL)
    plain.visual_type = str(C.VISUAL_MORPH)
    morph_rows = morph_rows_for(plain)
    said = lambda rows: [t for kind, t in rows.said if kind == "label"]
    check("the morph panel is a sidebar tab that says what it needs",
          morph_panel.bl_space_type == "VIEW_3D"
          and morph_panel.bl_region_type == "UI"
          and morph_panel.bl_category
          and morph_panel.poll(bpy.context)
          and any("not a morph" in text for text in said(plain_rows))
          and any(text == f"'{plain.name}'" for text in said(morph_rows)),
          f"{morph_panel.bl_space_type}/{morph_panel.bl_region_type} tab "
          f"'{morph_panel.bl_category}'; plain mesh said {said(plain_rows)}; "
          f"morph mesh said {said(morph_rows)[:2]}")

    # Which vertices a morph group covers used to be a vertex group somebody
    # picked in the panel. It is not a choice anybody should have to make: a
    # group that came out of a file has the region the file listed for it, a
    # group made here takes the vertices its targets move, and picking a
    # group puts its region up as the mesh's active vertex group so Blender's
    # own tools reach it. The panel says which of the two it is and offers
    # nothing.
    class _MorphLayout(_MatLayout):
        """The recorder above, plus what the panel searched for."""

        def __init__(self, said=None):
            super().__init__(said)
            self.searched = []

        def prop_search(self, _target, name, *_args, **_kwargs):
            self.searched.append(name)

    def morph_layout_for(obj):
        recorder = _MorphLayout()
        panel = _MorphMethods()
        panel.layout = recorder
        _MorphMethods.draw(panel, bpy.context)
        return recorder

    bpy.context.view_layer.objects.active = plain
    bpy.ops.ls3d.morph_group(action="ADD")
    fresh_group = morph_layout_for(plain)

    # A region the way an import leaves one: a vertex group of its own, named
    # on the morph group.
    region = plain.vertex_groups.new(name="Morph Region 1")
    region.add([v.index for v in plain.data.vertices], 1.0, "REPLACE")
    elsewhere = plain.vertex_groups.new(name="Elsewhere")
    plain.ls3d_morph_groups[0].vertex_group = region.name
    from_file = morph_layout_for(plain)

    bpy.ops.ls3d.morph_group(action="ADD")          # a second, to switch away
    plain.ls3d_active_morph_group = 1
    plain.vertex_groups.active_index = elsewhere.index
    plain.ls3d_active_morph_group = 0
    followed = plain.vertex_groups.active_index == region.index

    plain.vertex_groups.remove(region)              # the region's group, gone
    orphaned = morph_layout_for(plain)
    text_of = lambda rows: " ".join(t for kind, t in rows.said
                                    if kind == "label")
    check("a morph group's region is worked out, not asked for",
          not fresh_group.searched and not from_file.searched
          and not orphaned.searched
          and "every vertex its targets move" in text_of(fresh_group)
          and "the vertices the file listed for it" in text_of(from_file)
          and "gone" in text_of(orphaned) and followed,
          f"the panel searched for {fresh_group.searched}; a new group said "
          f"{text_of(fresh_group)[:60]!r}; one from a file said "
          f"{text_of(from_file)[:60]!r}; with its vertex group gone it said "
          f"{text_of(orphaned)[-70:]!r}; picking a group brought its region "
          f"forward {followed}")

    # Adding a target has to work on a mesh with no shape keys at all, which
    # is every mesh somebody starts a morph on. It used to open a picker of
    # the keys already there, find none, and say so - no way in, and a button
    # that looked broken. Nothing to pick now means making one, named the way
    # an import names them: the group's basis first, then its targets.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    starting = bpy.context.object
    starting.ls3d_frame_type = str(C.FRAME_VISUAL)
    starting.visual_type = str(C.VISUAL_MORPH)
    bpy.ops.ls3d.morph_group(action="ADD")
    bare = starting.data.shape_keys is None
    added = [bpy.ops.ls3d.morph_target(action="ADD") for _ in range(3)]
    first_group = [t.shape_key_name for t in starting.ls3d_morph_groups[0].targets]
    blocks = [k.name for k in starting.data.shape_keys.key_blocks]
    basis = starting.data.shape_keys.reference_key.name
    bpy.ops.ls3d.morph_group(action="ADD")
    made_by_hand = bpy.ops.ls3d.morph_new_target()
    second_group = [t.shape_key_name
                    for t in starting.ls3d_morph_groups[1].targets]
    check("a morph target can be added to a mesh with no shape keys",
          bare and all(result == {"FINISHED"} for result in added)
          and first_group == ["G1_Basis", "G1_Target1", "G1_Target2"]
          and blocks == first_group and basis == "G1_Basis"
          and made_by_hand == {"FINISHED"}
          and second_group == ["G2_Basis"],
          f"the mesh started with no shape keys {bare}; three presses gave "
          f"{first_group}, and the mesh carries {blocks} with {basis!r} as "
          f"its reference; a second group's own key {second_group}")

    # Removing a target removes the morph: the shape key comes off the mesh
    # with it, rather than being left loose for nothing to play. A key
    # another group still holds stays, because deleting it would empty a
    # group that is in use. And emptying a group leaves nothing loose behind,
    # so Add just makes another - it used to answer with a picker of the very
    # keys that had been thrown away.
    fresh_scene()
    bpy.ops.mesh.primitive_cube_add()
    morphed = bpy.context.object
    morphed.ls3d_frame_type = str(C.FRAME_VISUAL)
    morphed.visual_type = str(C.VISUAL_MORPH)
    bpy.ops.ls3d.morph_group(action="ADD")
    only_group = morphed.ls3d_morph_groups[0]
    for _ in range(3):
        bpy.ops.ls3d.morph_target(action="ADD")

    def key_names():
        keys = morphed.data.shape_keys
        return [block.name for block in keys.key_blocks] if keys else []

    def target_names(group):
        return [target.shape_key_name for target in group.targets]

    built = key_names()
    only_group.active_target_index = 2
    bpy.ops.ls3d.morph_target(action="REMOVE")
    after_one = key_names()

    while len(only_group.targets):
        only_group.active_target_index = len(only_group.targets) - 1
        bpy.ops.ls3d.morph_target(action="REMOVE")
    emptied = key_names()
    made_again = bpy.ops.ls3d.morph_target(action="ADD")
    after_add = target_names(only_group)

    # A key two groups hold: the one being removed lets go, the mesh keeps it.
    bpy.ops.ls3d.morph_target(action="ADD")
    bpy.ops.ls3d.morph_group(action="ADD")
    other_group = morphed.ls3d_morph_groups[1]
    shared = target_names(only_group)[1]
    other_group.targets.add().shape_key_name = shared
    morphed.ls3d_active_morph_group = 0
    only_group.active_target_index = 1
    bpy.ops.ls3d.morph_target(action="REMOVE")
    shared_kept = shared in key_names()

    # And the panel offers the picker on a button of its own.
    class _ButtonLayout(_MorphLayout):
        """The recorder above, keeping which operators the panel offered."""

        def __init__(self, said=None):
            super().__init__(said)
            self.buttons = []

        def operator(self, name, **kwargs):
            self.buttons.append(name)
            return type("Button", (), {"action": "", "frame": 0,
                                       "name": ""})()

    buttons = _ButtonLayout()
    offering = _MorphMethods()
    offering.layout = buttons
    _MorphMethods.draw(offering, bpy.context)
    picker_button = buttons.buttons
    check("removing a morph target takes its shape key with it",
          built == ["G1_Basis", "G1_Target1", "G1_Target2"]
          and after_one == ["G1_Basis", "G1_Target1"]
          and emptied == [] and made_again == {"FINISHED"}
          and after_add == ["G1_Basis"]
          and shared_kept and target_names(other_group) == [shared]
          and "ls3d.morph_add_existing" in picker_button,
          f"built {built}; after removing one {after_one}; emptied "
          f"{emptied}; adding again gave {after_add}; a key two groups hold "
          f"stayed {shared_kept}; the panel's buttons {sorted(set(picker_button))}")

    # Making nothing has more than one reason, and they are not the same
    # thing to fix. A joint whose weights are painted is left alone on
    # purpose; a box with no thickness holds nothing; a box that simply
    # misses the mesh weights nothing either. All three used to come back as
    # "every joint already has weights painted", which sent people looking
    # for weights that were never there.
    fresh_scene()
    bpy.ops.ls3d.add_joint()
    lonely_rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(3.0, 0.0, 0.0))
    away = bpy.context.object
    away.name = "base"
    away.ls3d_frame_type = str(C.FRAME_VISUAL)
    away.visual_type = str(C.VISUAL_SINGLEMESH)
    away.parent = lonely_rig
    away.modifiers.new("Armature", "ARMATURE").object = lonely_rig
    bpy.context.view_layer.objects.active = away
    away.select_get() or away.select_set(True)

    missed = bpy.ops.ls3d.weights_from_boxes()
    # What it would have said, read where it is worked out: an operator's own
    # report goes through Blender's C side, where nothing here can hear it.
    creating = ls3d_module("4ds.ops_create")
    _space = ls3d_module("4ds.joint_space")
    _influence = ls3d_module("4ds.influence")
    space = _space.JointSpace(lonely_rig, away,
                              _space.edit_rest(lonely_rig))
    numbered = creating.numbered_joints(space)
    made, trouble, why = creating._paint_from_boxes(
        away, away.data, lonely_rig, space, numbered,
        creating.joint_parents(lonely_rig, numbered),
        _influence.ancestors_of(lonely_rig))
    check("making no weights says which nothing it is",
          missed == {"CANCELLED"} and not away.vertex_groups
          and not made and not trouble
          and "no vertex of it lies in any joint's box" in why
          and "already has weights painted" not in why
          and "Fit Influence Box" in why,
          f"the operator said {missed}; {len(away.vertex_groups)} group(s) "
          f"made; the reason given was {why[:110]!r}")

    # Every format names what it is working on as it goes, so a run that stops
    # partway says which item it stopped on rather than only which stage.
    said = []

    def _record(self, kind, index, total, description=""):
        said.append((kind, index, total, description))

    original_item = report_module.Report.item
    report_module.Report.item = _record
    try:
        fresh_scene()
        bpy.ops.mesh.primitive_cube_add()
        bpy.context.object.data.materials.append(
            bpy.data.materials.new("LOGGED.BMP"))
        export_op(os.path.join(out_dir, "logged.4ds"))
    finally:
        report_module.Report.item = original_item
    kinds = [kind for kind, _i, _t, _d in said]
    check("the console names each material and frame as it is processed",
          "Mat" in kinds and "Frame" in kinds
          and all(1 <= index <= total for _k, index, total, _d in said)
          and all(text for _k, _i, _t, text in said),
          f"logged {said}")

    _facing = ls3d_module("4ds.validation").mirror_facing
    states = {}
    fresh_scene()
    for name, turn in (("facing", _Matrix.Rotation(math.radians(-90.0), 4, "X")),
                       ("flipped", _Matrix.Rotation(math.radians(90.0), 4, "X")),
                       ("tilted", None)):
        states[name] = _facing(_mirror(name, turn))
    check("the mirror panel says which way the surface points",
          states == {"facing": "FACING", "flipped": "BACK",
                     "tilted": "TILTED"},
          f"read {states}")

    # The projector panel is deliberately terse: what each setting does is in
    # its tooltip and the shape it makes is drawn in the viewport, so the panel
    # only speaks up when something is wrong. It used to run to a paragraph.
    fresh_scene()
    bpy.ops.ls3d.add_projector()
    shaped = bpy.context.object

    def projector_rows(obj):
        body = {k: v for k, v in ui_4ds.The4DSObjectPanel.__dict__.items()
                if not k.startswith("bl_")}
        panel = type("P", (), body)()
        recorder = _MatLayout()
        panel._draw_projector(recorder, obj)
        return recorder.said

    said = lambda rows: " ".join(t for kind, t in rows if kind == "label")
    props = lambda rows: [n for kind, n in rows if kind == "prop"]

    no_material = projector_rows(shaped)
    shaped.ls3d_projector_material = bpy.data.materials.new("PANEL.BMP")
    no_texture = projector_rows(shaped)
    shaped.ls3d_projector_material.ls3d_diffuse_tex = bpy.data.images.new(
        "PANEL.BMP", 4, 4)
    shaped.ls3d_projector_material.ls3d_flag_diffuse_enable = True
    settled = projector_rows(shaped)
    shaped.ls3d_projector_mode = 12
    odd_mode = projector_rows(shaped)

    check("the projector panel says only what cannot be seen or hovered",
          "No material" in said(no_material)
          and "no diffuse texture" not in said(no_material)
          and "no diffuse texture" in said(no_texture)
          and "No material" not in said(no_texture)
          and "No material" not in said(settled)
          and "no diffuse texture" not in said(settled)
          and "Mode 12" in said(odd_mode)
          and "Mode 12" not in said(settled)
          # every control, and nothing that needs a paragraph to explain
          and props(settled) == ["ls3d_show_projector_material",
                                 "ls3d_projector_orthogonal",
                                 "ls3d_projector_falloff",
                                 "ls3d_projector_blend",
                                 "ls3d_projector_mode"]
          and any(kind == "template_ID" for kind, _t in settled)
          # a heading, the material, four controls, the folded material
          # disclosure and two notes. The paragraph this replaced ran to 28.
          and len(settled) <= 12,
          f"{len(settled)} row(s) when all is well; "
          f"props {props(settled)}; said {said(settled)!r}")

    # A projector has no mesh, so its material has no slot and never reaches
    # Blender's Material tab. It is edited from here instead, the way a lens
    # flare's elements are - folded away until asked for.
    shaped.ls3d_projector_mode = 4
    folded = projector_rows(shaped)
    shaped.ls3d_show_projector_material = True
    opened = projector_rows(shaped)
    material_props = [n for n in props(opened) if n.startswith("ls3d_")
                      and "projector" not in n]
    check("the projected material is editable from the projector panel",
          "Material Settings" in said(folded)
          and len(opened) > len(folded) + 10
          and "ls3d_material_flags_str" in material_props
          and "ls3d_opacity" in material_props
          and any(n.startswith("ls3d_flag_") for n in material_props),
          f"folded {len(folded)} row(s), open {len(opened)}; "
          f"{len(material_props)} material control(s)")

    # A lens flare is a list of elements, and every element's material has to
    # reach the material table even though no mesh uses it.
    fresh_scene()
    flare = bpy.data.objects.new("lensflare", None)
    bpy.context.collection.objects.link(flare)
    flare.empty_display_type = "SPHERE"     # what makes an empty a flare carrier
    flare.ls3d_frame_type = str(C.FRAME_VISUAL)
    flare.visual_type = str(C.VISUAL_LENSFLARE)
    for name, position in (("head.bmp", 0.0), ("ghost1.bmp", -0.6),
                           ("ghost2.bmp", 12.5)):
        entry = flare.ls3d_glows.add()
        entry.name = name
        entry.position = position
        entry.material = bpy.data.materials.new(name)
    path = os.path.join(out_dir, "lensflare.4ds")
    result = export_op(path)
    written, names = [], []
    if result == {"FINISHED"}:
        doc = read_file(path)
        for frame in doc.frames:
            if frame.lens_flare is not None:
                written = [(round(g.position, 3), g.material_id)
                           for g in frame.lens_flare.glows]
                names = [doc.materials[g.material_id - 1].diffuse_texture
                         for g in frame.lens_flare.glows
                         if 0 < g.material_id <= len(doc.materials)]
    check("every lens flare element is written with its own material",
          result == {"FINISHED"} and len(written) == 3
          and [p for p, _m in written] == [0.0, -0.6, 12.5]
          and len({m for _p, m in written}) == 3
          and all(m > 0 for _p, m in written),
          f"wrote {written}, textures {names}")

    # An element with no material would write index 0, which the game reads off
    # the front of its material table.
    fresh_scene()
    flare = bpy.data.objects.new("lensflare", None)
    bpy.context.collection.objects.link(flare)
    flare.empty_display_type = "SPHERE"     # what makes an empty a flare carrier
    flare.ls3d_frame_type = str(C.FRAME_VISUAL)
    flare.visual_type = str(C.VISUAL_LENSFLARE)
    flare.ls3d_glows.add().position = 0.0
    empty = bpy.data.objects.new("lensflare2", None)
    bpy.context.collection.objects.link(empty)
    empty.empty_display_type = "SPHERE"
    empty.ls3d_frame_type = str(C.FRAME_VISUAL)
    empty.visual_type = str(C.VISUAL_LENSFLARE)
    no_material = export_op(os.path.join(out_dir, "flare_nomat.4ds"))
    check("a flare element without a material is refused",
          no_material == {"CANCELLED"}, f"result={no_material}")

    # Same for a projector, and for a harder reason: the game subtracts one
    # from the stored id and follows it without checking against zero, so a
    # projector naming material 0 does not draw nothing - it reads a pointer
    # out of the word in front of the material table and writes through it.
    # Neither the panel nor the table is trusted on its own here.
    fresh_scene()
    bpy.ops.ls3d.add_projector()
    bare = export_op(os.path.join(out_dir, "projector_nomat.4ds"))
    bpy.context.object.ls3d_projector_material = bpy.data.materials.new(
        "GIVEN.BMP")
    given = export_op(os.path.join(out_dir, "projector_mat.4ds"))
    wrote = read_file(os.path.join(out_dir, "projector_mat.4ds"))
    written_id = next((f.projector.material_id for f in wrote.frames
                       if f.projector is not None), None)

    # And the format layer refuses it too, whatever asks it to write.
    _visual = ls3d_module("4ds.codec.visual")
    _types = ls3d_module("4ds.codec.types")
    _binary = ls3d_module("common.binary")
    refused = {}
    for bad in (0, -1, -32768):
        try:
            _visual.write_projector(_binary.BinaryWriter(),
                                    _types.Projector(material_id=bad), "test")
            refused[bad] = "written"
        except _binary.FormatError:
            refused[bad] = "refused"
    try:
        _visual.write_projector(_binary.BinaryWriter(),
                                _types.Projector(material_id=1), "test")
        refused[1] = "written"
    except _binary.FormatError:
        refused[1] = "refused"

    check("a projector's material id can never be zero or negative",
          bare == {"CANCELLED"} and given == {"FINISHED"} and written_id == 1
          and refused == {0: "refused", -1: "refused", -32768: "refused",
                          1: "written"},
          f"no material={bare}, with one={given} wrote id {written_id}; "
          f"format layer {refused}")

    # A face whose corners meet covers no area, and Blender cannot hold one, so
    # the import gives the repeated corner a copy of the vertex and the export
    # writes the copy as the vertex it repeats. That only holds while the two
    # really are one vertex twice: a corner weighted to another joint, or moved
    # elsewhere by a morph target, parts from its twin as soon as the model
    # moves, and is written on its own.
    def doubled_corner(dress=None, label="plain"):
        """A square, plus a face whose last two corners sit on each other."""
        fresh_scene()
        mesh = bpy.data.meshes.new("probe")
        mesh.from_pydata([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
                          (1.0, 1.0, 0.0), (1.0, 1.0, 0.0)],
                         [], [(0, 1, 2), (1, 3, 4)])
        mesh.update()
        uv = mesh.uv_layers.new(name="UVMap")
        for polygon in mesh.polygons:            # the doubled face's corners
            if polygon.index == 1:               # share a UV as well as a spot
                for loop_index in polygon.loop_indices:
                    uv.data[loop_index].uv = (0.5, 0.5)
        mesh.materials.append(bpy.data.materials.new("TEST.BMP"))
        obj = bpy.data.objects.new("probe", mesh)
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        if dress is not None:
            dress(obj)
        path = os.path.join(out_dir, f"doubled_{label}.4ds")
        result = export_op(path)
        if result != {"FINISHED"}:
            return result, None, None, []
        frame = next(f for f in read_file(path).frames if f.geometry)
        lod = frame.geometry.lods[0]
        return (result, frame, lod,
                [tuple(face) for g in lod.face_groups for face in g.faces])

    def weighted_elsewhere(obj):
        bpy.ops.object.armature_add(enter_editmode=True, location=(0.0, 0.0, 0.0))
        rig = bpy.context.object
        bones = rig.data.edit_bones
        bones[0].name = "one"
        bones["one"].head, bones["one"].tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.4)
        second = bones.new("two")
        second.head, second.tail = (0.0, 0.0, 0.4), (0.0, 0.0, 0.8)
        second.parent = bones["one"]
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.ls3d_frame_type = str(C.FRAME_VISUAL)
        obj.visual_type = str(C.VISUAL_SINGLEMESH)
        obj.vertex_groups.new(name="one").add([0, 1, 2, 3], 1.0, "REPLACE")
        obj.vertex_groups.new(name="two").add([4], 1.0, "REPLACE")
        obj.modifiers.new("Armature", "ARMATURE").object = rig
        obj.parent = rig

    def moved_by_a_target(obj):
        obj.ls3d_frame_type = str(C.FRAME_VISUAL)
        obj.visual_type = str(C.VISUAL_MORPH)
        basis = obj.shape_key_add(name="Basis", from_mix=False)
        target = obj.shape_key_add(name="Target", from_mix=False)
        target.data[4].co = (1.0, 1.0, 0.5)      # the doubled corner lifts away
        group = obj.ls3d_morph_groups.add()
        for name in (basis.name, target.name):
            group.targets.add().shape_key_name = name

    plain_out, _plain_frame, plain_lod, plain_faces = doubled_corner()
    skin_out, skin_frame, skin_lod, skin_faces = doubled_corner(
        weighted_elsewhere, "weighted")
    morph_out, morph_frame, morph_lod, morph_faces = doubled_corner(
        moved_by_a_target, "morphed")

    def flat(faces):
        return [face for face in faces if len(set(face)) < 3]

    alone = []
    if skin_frame is not None and skin_frame.skin:
        alone = [g.unweighted_count for g in skin_frame.skin.lods[0].groups
                 if g.unweighted_count]
    opens = False
    if morph_frame is not None and morph_frame.morph:
        region = morph_frame.morph.lods[0].regions[0]
        opens = (len(region.positions) > 1
                 and region.positions[0] != region.positions[1])
    check("a doubled corner is written once, unless it moves on its own",
          plain_out == {"FINISHED"} and len(plain_lod.vertices) == 5
          and len(flat(plain_faces)) == 1
          and skin_out == {"FINISHED"} and len(skin_lod.vertices) == 6
          and not flat(skin_faces) and 1 in alone
          and morph_out == {"FINISHED"} and len(morph_lod.vertices) == 6
          and not flat(morph_faces) and opens,
          f"alike: {len(plain_lod.vertices) if plain_lod else 0} vertices, "
          f"{len(flat(plain_faces))} face(s) with a repeated corner; weighted "
          f"elsewhere: {len(skin_lod.vertices) if skin_lod else 0} vertices, "
          f"groups holding {alone}; moved by a target: "
          f"{len(morph_lod.vertices) if morph_lod else 0} vertices, target "
          f"opens the face={opens}")

    # 'Detektiv02' is the one model in the game that is skinned, morphed *and*
    # holds faces whose corners meet. Its copies have to come back out as the
    # vertices they repeat with their weights and their targets intact, so
    # every morph region must name the same vertices and put each of them in
    # the same place. The region's own listing order is not kept - the file
    # lists its vertices unsorted and the export sorts them - which moves
    # nothing, since each position travels with its vertex.
    detektiv = model("Detektiv02.4ds")
    if os.path.exists(detektiv):
        fresh_scene()
        import_op(detektiv)
        again = os.path.join(out_dir, "detektiv_again.4ds")
        detektiv_out = export_op(again)
        missing, moved, corners = [], 0.0, None
        if detektiv_out == {"FINISHED"}:
            before, after = read_file(detektiv), read_file(again)
            for fa, fb in zip(before.frames, after.frames):
                if not (fa.morph and fa.morph.target_count):
                    continue
                for la, lb in zip(fa.morph.lods, fb.morph.lods):
                    for ra, rb in zip(la.regions, lb.regions):
                        if set(ra.indices) != set(rb.indices):
                            missing.append((len(ra.indices), len(rb.indices)))
                            continue
                        for target, (positions_a, positions_b) in enumerate(
                                zip(ra.positions, rb.positions)):
                            where_a = dict(zip(ra.indices, positions_a))
                            where_b = dict(zip(rb.indices, positions_b))
                            for index in where_a:
                                moved = max(moved, max(
                                    abs(x - y) for x, y in
                                    zip(where_a[index], where_b[index])))
            first = next(f for f in after.frames if f.geometry and f.skin)
            corners = [face for g in first.geometry.lods[0].face_groups
                       for face in g.faces if len(set(face)) < 3]
    check("a skinned, morphed mesh whose corners meet comes back whole",
          not os.path.exists(detektiv)
          or (detektiv_out == {"FINISHED"} and not missing and moved == 0.0
              and len(corners) == 2),
          f"export {detektiv_out if os.path.exists(detektiv) else 'no model'}, "
          f"regions that lost vertices {missing}, worst move {moved}, faces "
          f"with a repeated corner {corners}")

    # A re-export writes back what a model came with, worked out from the scene
    # alone. These cover zero-area faces, values that are not numbers, morphs,
    # billboards, lens flares, instances, lights, sectors, a projector and a
    # car, and every one comes back byte for byte - the save date aside, which
    # is the time of the export.
    def _changed(name):
        """Field paths that changed on a re-export, save date left out."""
        fresh_scene()
        source = model(name)
        import_op(source)
        target = os.path.join(out_dir, "exact_" + re.sub(r"[^\w.-]", "_", name))
        if export_op(target) != {"FINISHED"}:
            return None
        before = read_file(source)
        after = read_file(target)
        after.timestamp = before.timestamp
        left, right = _bits(before), _bits(after)
        return sorted({re.sub(r"\[\d+\]", "[]", key)
                       for key in left.keys() | right.keys()
                       if left.get(key) != right.get(key)})

    exact_models = ("licht.4ds", "2m1903.4ds", "2tombston.4ds", "bigfoot00.4ds",
                    "9plakat.4ds", "okno1.4ds", "strela.4ds",
                    "xsvetlo 05on.4ds", "ohen.4ds", "okno.4ds",
                    "Stargate.4ds", "chevroletm6H00.4ds", "test_projector.4ds")
    present = [name for name in exact_models if os.path.exists(model(name))]
    if not present:
        SKIPPED.append("re-exports are byte for byte")
    else:
        differing = {}
        for name in present:
            changed = _changed(name)
            if changed != []:
                differing[name] = changed if changed is not None else "export failed"
        check("imported models re-export byte for byte, the save date aside",
              not differing,
              f"{len(present) - len(differing)}/{len(present)} exact; {differing}")

    # A morph target's normals are worked out from its shape, not kept. On a
    # simple morph they come back as the game's files have them to a hundredth
    # of a degree. (Characters' face morphs do not: the modeling tool made some
    # of theirs from shapes the file does not hold.)
    morphs = [name for name in ("pradlo02mor.4ds", "FMVroleta.4ds")
              if os.path.exists(model(name))]
    if morphs:
        angles = []
        for name in morphs:
            fresh_scene()
            import_op(model(name))
            target = os.path.join(out_dir, "normals_" + name)
            export_op(target)
            before = {f.name: f for f in read_file(model(name)).frames}
            for frame in read_file(target).frames:
                if frame.morph is None or not frame.morph.target_count:
                    continue
                source = before[frame.name].morph
                for lod_a, lod_b in zip(source.lods, frame.morph.lods):
                    for region_a, region_b in zip(lod_a.regions, lod_b.regions):
                        for normals_a, normals_b in zip(region_a.normals, region_b.normals):
                            for a, b in zip(normals_a, normals_b):
                                dot = sum(x * y for x, y in zip(a, b))
                                size = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
                                angles.append(math.degrees(math.acos(max(-1.0, min(1.0, dot / size))))
                                              if size else 0.0)
        close = sum(1 for a in angles if a < 0.01)
        check("a simple morph's target normals are worked out as the game's files have them",
              angles and close == len(angles),
              f"{close}/{len(angles)} within a hundredth of a degree, "
              f"worst {max(angles, default=0.0):.4f} deg")

    # A re-export of a re-export changes nothing: once a model has been through
    # Blender, it comes out the same however often it goes round.
    settled = {}
    for name in ("Tommy.4ds", "pes03.4ds", "Mise06c Tom01 pohar.4ds",
                 "I04Delnik01+.4ds", "pradlo02mor.4ds", "prejimka.4ds"):
        if not os.path.exists(model(name)):
            continue
        fresh_scene()
        import_op(model(name))
        once = os.path.join(out_dir, "settle_once.4ds")
        export_op(once)
        fresh_scene()
        import_op(once)
        twice = os.path.join(out_dir, "settle_twice.4ds")
        export_op(twice)
        first, second = read_file(once), read_file(twice)
        second.timestamp = first.timestamp
        left, right = _bits(first), _bits(second)
        settled[name] = sorted({re.sub(r"\[\d+\]", "[]", key)
                                for key in left.keys() | right.keys()
                                if left.get(key) != right.get(key)})
    check("a re-export of a re-export changes nothing",
          settled and not any(settled.values()),
          f"changed on the second round: { {k: v for k, v in settled.items() if v} }")

    # A character comes back the same everywhere but where its joints are: a
    # bone holds a joint's place in the armature's space, rounded to 32 bits
    # there, so joint positions, and the bind matrices and bone group boxes
    # worked out from them, can come back a rounding step or two off. The
    # influence box is not among them any more: it is kept on the joint as the
    # file holds it, so it comes back to the bit (measured on its own below).
    limited = {".frames[].position[]", ".frames[].skin.lods[].groups[].inverse_bind[]",
               ".frames[].skin.lods[].groups[].bbox_min[]",
               ".frames[].skin.lods[].groups[].bbox_max[]"}
    characters = [name for name in ("Milenka1.4ds", "civil05.4ds")
                  if os.path.exists(model(name))]
    if characters:
        beyond = {}
        for name in characters:
            changed = _changed(name)
            extra = ("export failed" if changed is None
                     else [c for c in changed if c not in limited])
            if extra:
                beyond[name] = extra
        check("a character re-exports unchanged but for its joints' rounding",
              not beyond, f"changed beyond joint rounding: {beyond}")

    # ── influence boxes ──────────────────────────────────────────────────────
    _influence = ls3d_module("4ds.influence")
    _C = ls3d_module("common.constants")
    tommy = model("Tommy.4ds")
    if os.path.exists(tommy):
        # Every joint comes in carrying its box - on the joint, not as an
        # object of its own - and writes the same sixteen numbers back.
        fresh_scene()
        import_op(tommy)
        rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        found = _influence.boxes_of(rig)
        objects = len(bpy.data.objects)
        once = os.path.join(out_dir, "influence_once.4ds")
        export_op(once)
        before = {f.name: f.joint.matrix for f in read_file(tommy).frames if f.joint}
        after = {f.name: f.joint.matrix for f in read_file(once).frames if f.joint}
        moved = max(max(abs(a - b) for a, b in zip(before[n], after[n])) for n in before)
        # Nothing in the scene stands for a box, so opening a character adds
        # no objects for them: the mesh, its LODs and the armature, no more.
        check("every joint comes in carrying its influence box and writes it "
              "back to the bit",
              len(found) == 18 and moved == 0.0 and objects <= 10
              and all(_influence.has_box(rig, name) for name in found),
              f"{len(found)} joints with boxes, largest change {moved:.2e}, "
              f"{objects} objects in the scene")

        # A box is drawn on the joint it belongs to, near enough that the two
        # read as one thing, and nothing about it is written as a frame.
        _vp = ls3d_module("4ds.viewport")
        bpy.context.view_layer.update()
        placed = _vp.influence_box_matrix(rig, "l_hand", found["l_hand"])
        head = rig.matrix_world @ rig.data.bones["l_hand"].head_local
        names = {f.name for f in read_file(once).frames}
        check("a box is drawn on its joint and written as no frame of its own",
              placed is not None and (placed.translation - head).length < 0.2
              and not any("influence" in name for name in names)
              and len(_vp.influence_box_corners(placed)) == 8,
              f"box center {tuple(round(v, 3) for v in placed.translation)} "
              f"against a joint at {tuple(round(v, 3) for v in head)}")

        # With no weights painted, the boxes weight Tommy as his own file does.
        fresh_scene()
        import_op(tommy)
        body = bpy.data.objects["base"]
        for group in body.vertex_groups:
            group.remove(range(len(body.data.vertices)))
        made = os.path.join(out_dir, "influence_weights.4ds")
        result = export_op(made)

        def weights_by_spot(document):
            frames = document.frames
            mesh = next(f for f in frames if f.skin is not None)
            kids = {}
            for i, f in enumerate(frames, 1):
                kids.setdefault(f.parent_id, []).append(i)
            names = {}

            def walk(fid):
                for c in kids.get(fid, ()):
                    if frames[c - 1].joint is not None:
                        names.setdefault(frames[c - 1].joint.joint_id, frames[c - 1].name)
                    walk(c)
            walk(frames.index(mesh) + 1)
            lod, skin = mesh.geometry.lods[0], mesh.skin.lods[0]
            spots, cursor = {}, 0
            for g, group in enumerate(skin.groups):
                for _ in range(group.unweighted_count):
                    spots[tuple(lod.vertices[cursor].position)] = (names.get(g), 1.0)
                    cursor += 1
                for w in group.weights:
                    spots[tuple(lod.vertices[cursor].position)] = (names.get(g), w)
                    cursor += 1
            for v in lod.vertices[cursor:]:
                spots[tuple(v.position)] = (None, 1.0)
            return spots

        right = total = 0
        if result == {"FINISHED"}:
            want, got = weights_by_spot(read_file(tommy)), weights_by_spot(read_file(made))
            total = len(want)
            right = sum(1 for spot, (joint, w) in want.items()
                        if spot in got and got[spot][0] == joint
                        and abs(got[spot][1] - w) < 1e-4)
        check("with nothing painted, the influence boxes weight a character as "
              "the game's own file does",
              result == {"FINISHED"} and total and right / total > 0.98,
              f"{right}/{total} spots weighted as Tommy's file has them")

        # A painted joint keeps its weights; only the unpainted one is made.
        # The left hand's vertices lose every weight, not only the hand's -
        # half a blend left behind is a vertex whose weights no longer add up.
        fresh_scene()
        import_op(tommy)
        body = bpy.data.objects["base"]
        hand_group = body.vertex_groups["l_hand"].index
        touched = [v.index for v in body.data.vertices
                   if any(g.group == hand_group and g.weight > 0.0 for g in v.groups)]
        for group in body.vertex_groups:
            group.remove(touched)
        mixed = os.path.join(out_dir, "influence_mixed.4ds")
        elsewhere, hand = ["export refused"], 0
        if export_op(mixed) == {"FINISHED"}:
            want, got = weights_by_spot(read_file(tommy)), weights_by_spot(read_file(mixed))
            elsewhere = [spot for spot, (joint, _w) in want.items() if joint != "l_hand"
                         and got.get(spot) != want[spot]]
            hand = sum(1 for spot, (joint, _w) in got.items() if joint == "l_hand")
        check("painted weights export as they are, and an unpainted joint's "
              "come from its box",
              not elsewhere and hand > 0,
              f"{len(elsewhere)} painted spots changed, {hand} spots on l_hand")

        # The game gives a vertex one whole weight from no more than two
        # joints: it is either wholly one joint's, or shared between a joint
        # and the one above it. A file says so by listing each vertex once -
        # either among a joint's own, or with a share of it, the rest of which
        # goes to the joint above - so what has to hold is that no vertex is
        # listed twice and no share is a whole one or nothing.
        def weight_tally(path):
            document = read_file(path)
            frame = next(f for f in document.frames if f.skin is not None)
            listed = shares = 0
            worst = []
            for lod, skin in zip(frame.geometry.lods, frame.skin.lods):
                for group in skin.groups:
                    listed += group.unweighted_count + len(group.weights)
                    shares += len(group.weights)
                    worst += [w for w in group.weights if not 0.0 < w < 1.0]
                if listed > len(lod.vertices):
                    worst.append(f"{listed} of {len(lod.vertices)} vertices listed")
            return listed, shares, worst

        made_listed, made_shares, made_bad = weight_tally(made)
        mixed_listed, mixed_shares, mixed_bad = weight_tally(mixed)
        check("weights made from the boxes add up to one on every vertex, from "
              "two joints at the most",
              not made_bad and not mixed_bad and made_shares and mixed_shares,
              f"made {made_listed} vertices, {made_shares} shared, {made_bad[:3]}; "
              f"mixed {mixed_listed}, {mixed_shares} shared, {mixed_bad[:3]}")

    # A joint made in Blender carries the box the game's own tool gave a new
    # joint - Bone01's in I04Delnik01+ - and is refused without one.
    fresh_scene()
    bpy.ops.ls3d.add_joint()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.ops.mesh.primitive_cube_add()
    lone = os.path.join(out_dir, "new_joint.4ds")
    lone_result = export_op(lone)
    gap = None
    if lone_result == {"FINISHED"}:
        joint = next(f for f in read_file(lone).frames if f.joint is not None)
        gap = max(abs(a - b) for a, b in zip(joint.joint.matrix, _C.DEFAULT_JOINT_BOX))
    for bone in rig.data.bones:
        _influence.clear_box(rig, bone.name)
    said = []
    original_error = report_module.Report.error

    def _hear(self, message, fix=None):
        said.append(message)
        return original_error(self, message, fix)

    report_module.Report.error = _hear
    try:
        boxless = export_op(os.path.join(out_dir, "boxless.4ds"))
    finally:
        report_module.Report.error = original_error
    check("a new joint carries the tool's own default influence box, and none "
          "is refused",
          lone_result == {"FINISHED"} and gap is not None and gap < 1e-8
          and boxless == {"CANCELLED"} and any("no influence box" in m for m in said),
          f"new joint {lone_result}, off Bone01's box by {gap}; without one "
          f"{boxless}: {[m[:50] for m in said]}")

    # A joint holds one influence box and there is nowhere to put a second:
    # the box is the joint's own sixteen numbers. The buttons say so - neither
    # offers a box to a joint that has one - and taking one away leaves the
    # joint to whatever is painted on it.
    fresh_scene()
    bpy.ops.ls3d.add_joint()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.view_layer.objects.active = rig
    joint_name = rig.data.bones[0].name
    rig.data.bones.active = rig.data.bones[joint_name]
    offered = bpy.ops.ls3d.add_influence_box.poll()
    removable = bpy.ops.ls3d.remove_influence_box.poll()
    bpy.ops.ls3d.add_influence_boxes()
    after_all = len(_influence.boxes_of(rig))
    bpy.ops.ls3d.remove_influence_box()
    gone = _influence.has_box(rig, joint_name)
    offered_after = bpy.ops.ls3d.add_influence_box.poll()
    check("a joint holds one influence box, and only one",
          not offered and removable and after_all == 1 and not gone
          and offered_after,
          f"the button offered a second={offered}, remove offered={removable}, "
          f"add-missing left {after_all} box(es), removed={not gone}, offered "
          f"again={offered_after}")

    # The boxes can be painted onto the mesh rather than left to the export:
    # the button makes exactly the weights the export would have made, so the
    # file does not change by pressing it, and from then on they are ordinary
    # vertex groups to look at and paint over.
    if os.path.exists(tommy):
        def stripped_tommy():
            fresh_scene()
            import_op(tommy)
            body = bpy.data.objects["base"]
            for group in body.vertex_groups:
                group.remove(range(len(body.data.vertices)))
            return body

        body = stripped_tommy()
        straight = os.path.join(out_dir, "boxes_straight.4ds")
        straight_out = export_op(straight)

        body = stripped_tommy()
        offered = bpy.ops.ls3d.weights_from_boxes.poll()
        made = bpy.ops.ls3d.weights_from_boxes()
        painted_now = [v for v in body.data.vertices if v.groups]
        totals = [sum(g.weight for g in v.groups) for v in painted_now]
        influences = [len(v.groups) for v in painted_now]
        # The game takes one whole weight from no more than two joints, so
        # what is painted has to obey that before it is written.
        whole = all(abs(total - 1.0) < 1e-4 for total in totals)
        painted_path = os.path.join(out_dir, "boxes_painted.4ds")
        painted_out = export_op(painted_path)
        changed = []
        if straight_out == {"FINISHED"} and painted_out == {"FINISHED"}:
            before, after = read_file(straight), read_file(painted_path)
            after.timestamp = before.timestamp
            left, right = _bits(before), _bits(after)
            changed = sorted({key for key in left.keys() | right.keys()
                              if left.get(key) != right.get(key)})
        # Pressing it again has nothing to do: every joint is painted now.
        again = bpy.ops.ls3d.weights_from_boxes()
        check("the boxes can be painted onto the mesh, and change nothing when "
              "they are",
              offered and made == {"FINISHED"} and len(painted_now) == 778
              and whole and max(influences, default=0) == 2
              and not changed and again == {"CANCELLED"},
              f"offered={offered}, {made} painted {len(painted_now)} vertices, "
              f"whole weights={whole}, most influences {max(influences, default=0)}, "
              f"the file changed in {len(changed)} field(s) {changed[:3]}, "
              f"pressing it again {again}")

    # Clear All Weights takes every weight off the selected meshes and leaves
    # the groups, emptied: the order of a mesh's vertex groups is what numbers
    # its joints, so deleting them would renumber the skin. Tommy is a Single
    # Morph, and his morph regions are vertex groups too, which say what a
    # morph moves rather than what a vertex follows - so cleared, the file is
    # the one written with every weight but those taken off by hand.
    if os.path.exists(tommy):
        def regions_of(obj):
            return {group.vertex_group for group in obj.ls3d_morph_groups
                    if group.vertex_group}

        fresh_scene()
        import_op(tommy)
        body = bpy.data.objects["base"]
        for group in body.vertex_groups:
            if group.name not in regions_of(body):
                group.remove(range(len(body.data.vertices)))
        by_hand = os.path.join(out_dir, "boxes_cleared_by_hand.4ds")
        by_hand_out = export_op(by_hand)

        fresh_scene()
        import_op(tommy)
        body = bpy.data.objects["base"]
        order_before = [group.name for group in body.vertex_groups]
        regions = regions_of(body)
        region_members = sorted(
            (v.index, body.vertex_groups[g.group].name)
            for v in body.data.vertices for g in v.groups
            if body.vertex_groups[g.group].name in regions)
        bpy.ops.object.select_all(action="DESELECT")
        body.select_set(True)
        bpy.context.view_layer.objects.active = body
        cleared = bpy.ops.ls3d.clear_weights()
        order_after = [group.name for group in body.vertex_groups]
        still = sum(1 for v in body.data.vertices for g in v.groups
                    if body.vertex_groups[g.group].name not in regions)
        regions_after = sorted(
            (v.index, body.vertex_groups[g.group].name)
            for v in body.data.vertices for g in v.groups
            if body.vertex_groups[g.group].name in regions)
        regions_kept = bool(region_members) and regions_after == region_members
        cleared_path = os.path.join(out_dir, "boxes_cleared.4ds")
        cleared_out = export_op(cleared_path)
        differs = ["not written"]
        if cleared_out == {"FINISHED"} and by_hand_out == {"FINISHED"}:
            before, after = read_file(by_hand), read_file(cleared_path)
            after.timestamp = before.timestamp
            left, right = _bits(before), _bits(after)
            differs = sorted({key for key in left.keys() | right.keys()
                              if left.get(key) != right.get(key)})
        # Only the selected meshes are cleared, and a morph region's vertex
        # group - which says what a morph moves, not what a vertex follows -
        # is not a weight.
        fresh_scene()
        bpy.ops.mesh.primitive_cube_add()
        morphed = bpy.context.object
        bpy.ops.mesh.primitive_cube_add(location=(3.0, 0.0, 0.0))
        other = bpy.context.object
        for obj in (morphed, other):
            obj.vertex_groups.new(name="back1").add(range(8), 1.0, "REPLACE")
        morphed.vertex_groups.new(name="Region").add([0, 1], 1.0, "REPLACE")
        morphed.ls3d_morph_groups.add().vertex_group = "Region"
        bpy.ops.object.select_all(action="DESELECT")
        morphed.select_set(True)
        bpy.context.view_layer.objects.active = morphed
        bpy.ops.object.mode_set(mode="EDIT")
        in_edit = bpy.ops.ls3d.clear_weights.poll()
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.ls3d.clear_weights()

        def members(obj, name):
            index = obj.vertex_groups[name].index
            return sum(1 for v in obj.data.vertices
                       if any(g.group == index for g in v.groups))

        kept = (members(morphed, "back1"), members(morphed, "Region"),
                members(other, "back1"))
        check("Clear All Weights empties the selected meshes' weights and "
              "nothing else",
              cleared == {"FINISHED"} and order_after == order_before
              and still == 0 and regions_kept and not differs and not in_edit
              and kept == (0, 2, 8),
              f"{cleared}; groups kept in order={order_after == order_before}, "
              f"{still} weight(s) left, morph regions kept={regions_kept} "
              f"({len(region_members)} member(s)), the file differs from one "
              f"cleared by hand in {differs[:3]}; offered in Edit Mode="
              f"{in_edit}; weights, region and the unselected mesh: {kept}")

    # A mesh built from separate pieces weights as one. A region grows along
    # the mesh's edges, so a piece joined to nothing - a sleeve over an arm, a
    # collar round a joint, an ornament hanging clear of it - used to be cut
    # off from it and left following the mesh frame, however plainly it sat
    # on a limb. What a region stops at behind its box in such a piece goes up
    # the chain, to the nearest joint it does not lie behind as well; a piece
    # no region gets into takes the weights of the nearest place one did; and
    # the body beside them weights exactly as it did without them - nothing is
    # taken from one piece for another.
    fresh_scene()
    limb = bpy.data.objects.new("limb", bpy.data.armatures.new("limb"))
    bpy.context.scene.collection.objects.link(limb)
    bpy.context.view_layer.objects.active = limb
    bpy.ops.object.mode_set(mode="EDIT")
    upper = limb.data.edit_bones.new("upper")
    upper.head, upper.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.1)
    lower = limb.data.edit_bones.new("lower")
    lower.head, lower.tail = (0.0, 0.0, 0.5), (0.0, 0.0, 0.6)
    lower.parent = upper
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.05, depth=1.0,
                                        location=(0.0, 0.0, 0.5))
    pieced = bpy.context.object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=9)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    pieced.name = "base"
    pieced.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
    pieced.ls3d_frame_type = str(C.FRAME_VISUAL)
    pieced.visual_type = str(C.VISUAL_SINGLEMESH)
    pieced.modifiers.new("Armature", "ARMATURE").object = limb
    pieced.parent = limb
    _fit = ls3d_module("4ds.ops_create").fit_influence_box
    for bone_name in ("upper", "lower"):
        _fit(bpy.context, limb, bone_name)

    def weights_of(obj):
        names = {g.index: g.name for g in obj.vertex_groups}
        return [{names[g.group]: g.weight for g in v.groups if g.weight > 0.0}
                for v in obj.data.vertices]

    bpy.ops.object.select_all(action="DESELECT")
    pieced.select_set(True)
    bpy.context.view_layer.objects.active = pieced
    alone_made = bpy.ops.ls3d.weights_from_boxes()
    alone = weights_of(pieced)
    body_count = len(pieced.data.vertices)
    # The pieces: a sleeve beyond the lower joint's box, a collar reaching
    # from inside that box back toward the upper joint, and a piece hanging
    # clear of the limb.
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.06, depth=0.08,
                                        location=(0.0, 0.0, 0.8))
    sleeve = bpy.context.object
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.045, depth=0.25,
                                        location=(0.0, 0.0, 0.425))
    collar = bpy.context.object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=4)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cube_add(size=0.04, location=(0.3, 0.0, 0.8))
    hanging = bpy.context.object
    # And a strip hanging beside the limb from the lower joint down past the
    # upper one's reach - hair down to the nape - resting mostly on the lower.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(-0.2, 0.0, 0.65),
                                    scale=(0.01, 0.02, 0.35))
    strip = bpy.context.object
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=12)
    bpy.ops.object.mode_set(mode="OBJECT")
    for obj in bpy.context.scene.objects:
        obj.select_set(obj in (pieced, sleeve, collar, hanging, strip))
    bpy.context.view_layer.objects.active = pieced
    bpy.ops.object.join()
    bpy.ops.object.select_all(action="DESELECT")
    pieced.select_set(True)
    bpy.context.view_layer.objects.active = pieced
    cleared = bpy.ops.ls3d.clear_weights()
    pieced_made = bpy.ops.ls3d.weights_from_boxes()
    together = weights_of(pieced)
    # The joined object keeps its own vertices first; the pieces follow.
    pieces = range(body_count, len(pieced.data.vertices))
    at = [pieced.data.vertices[i].co for i in range(len(pieced.data.vertices))]
    hanging_ids = [i for i in pieces if at[i].x > 0.2]
    strip_ids = [i for i in pieces if at[i].x < -0.1]
    sleeve_ids = [i for i in pieces
                  if -0.1 <= at[i].x <= 0.2 and at[i].z > 0.7]
    collar_ids = [i for i in pieces
                  if -0.1 <= at[i].x <= 0.2 and at[i].z < 0.6]
    # The lower joint's box reaches back to 40 cm; the collar behind that.
    behind_ids = [i for i in collar_ids if at[i].z < 0.39]

    def following(indices, joint):
        return sum(1 for i in indices if together[i].get(joint, 0.0) > 0.5)

    sleeve_follows = following(sleeve_ids, "lower")
    hanging_follows = following(hanging_ids, "lower")
    # Whole: the part level with the upper joint goes with the rest, or the
    # strip would tear where it passes from one to the other.
    strip_whole = sum(1 for i in strip_ids
                      if abs(together[i].get("lower", 0.0) - 1.0) < 1e-6)
    behind_follows = following(behind_ids, "upper")
    collar_weighted = sum(1 for i in collar_ids if together[i])
    body_changed = sum(
        1 for i in range(body_count)
        if together[i].keys() != alone[i].keys()
        or any(abs(together[i][k] - alone[i][k]) > 1e-6 for k in alone[i]))
    check("a mesh built from separate pieces weights as one",
          alone_made == cleared == pieced_made == {"FINISHED"}
          and sleeve_ids and hanging_ids and behind_ids
          and sleeve_follows == len(sleeve_ids)
          and hanging_follows == len(hanging_ids)
          and strip_ids and strip_whole == len(strip_ids)
          and behind_follows == len(behind_ids)
          and collar_weighted == len(collar_ids) and body_changed == 0,
          f"{alone_made} {cleared} {pieced_made}; the sleeve "
          f"{sleeve_follows} of {len(sleeve_ids)} and the piece hanging clear "
          f"{hanging_follows} of {len(hanging_ids)} follow the joint they sit "
          f"on; the strip resting mostly on it {strip_whole} of "
          f"{len(strip_ids)} wholly; the collar behind the lower box "
          f"{behind_follows} of "
          f"{len(behind_ids)} follow the upper joint, {collar_weighted} of "
          f"{len(collar_ids)} weighted; the limb's own weights changed by the "
          f"pieces beside it: {body_changed}")

    # A skirt round both legs splits between them down the middle. Where the
    # regions of two legs meet - joints no kin of each other - the nearer one
    # along the surface takes a vertex. Counted in edges alone, a region runs
    # ahead wherever the triangles lean its way, and on snowWh one leg took
    # most of both sides of the hem.
    fresh_scene()
    legs = bpy.data.objects.new("legs", bpy.data.armatures.new("legs"))
    bpy.context.scene.collection.objects.link(legs)
    bpy.context.view_layer.objects.active = legs
    bpy.ops.object.mode_set(mode="EDIT")
    for side, x in (("l", -0.1), ("r", 0.1)):
        thigh = legs.data.edit_bones.new(f"{side}_thigh")
        thigh.head, thigh.tail = (x, 0.0, 1.0), (x, 0.0, 0.6)
        shin = legs.data.edit_bones.new(f"{side}_shin")
        shin.head, shin.tail = (x, 0.0, 0.55), (x, 0.0, 0.15)
        shin.parent = thigh
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.3, depth=0.8,
                                        location=(0.0, 0.0, 0.55),
                                        end_fill_type="NOTHING")
    skirt = bpy.context.object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=15)
    # Every square cut along the same diagonal, the way a mesh often is.
    bpy.ops.mesh.quads_convert_to_tris(quad_method="FIXED")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    skirt.name = "base"
    skirt.data.materials.append(bpy.data.materials.new("DRESS.BMP"))
    skirt.ls3d_frame_type = str(C.FRAME_VISUAL)
    skirt.visual_type = str(C.VISUAL_SINGLEMESH)
    skirt.modifiers.new("Armature", "ARMATURE").object = legs
    skirt.parent = legs
    for bone_name in ("l_thigh", "l_shin", "r_thigh", "r_shin"):
        _fit(bpy.context, legs, bone_name)
    # Fitted on what is nearer its own leg, a box stops at the other one; on
    # the whole skirt, it reached right across it.
    _box_view = ls3d_module("4ds.viewport")
    past_other_leg = []
    for bone_name in ("l_thigh", "l_shin", "r_thigh", "r_shin"):
        placed = _box_view.influence_box_matrix(
            legs, bone_name, _influence.box_of(legs, bone_name))
        corners_x = [corner.x for corner in
                     _box_view.influence_box_corners(placed)]
        other_leg = 0.1 if bone_name[0] == "l" else -0.1
        if (max(corners_x) > other_leg + 1e-4 if bone_name[0] == "l"
                else min(corners_x) < other_leg - 1e-4):
            past_other_leg.append(bone_name)
    bpy.ops.object.select_all(action="DESELECT")
    skirt.select_set(True)
    bpy.context.view_layer.objects.active = skirt
    skirt_made = bpy.ops.ls3d.weights_from_boxes()
    skirt_weights = weights_of(skirt)
    crossed = sides = 0
    for vertex, parts in zip(skirt.data.vertices, skirt_weights):
        if abs(vertex.co.x) < 0.03 or not parts:
            continue
        sides += 1
        joint = max(parts, key=parts.get)
        if joint[0] != ("l" if vertex.co.x < 0.0 else "r"):
            crossed += 1
    check("a skirt round both legs splits between them down the middle",
          skirt_made == {"FINISHED"} and sides > 400 and crossed == 0
          and not past_other_leg,
          f"{skirt_made}; {crossed} of {sides} vertices off the middle follow "
          f"the leg on the other side; boxes reaching past the other leg: "
          f"{past_other_leg}")

    # A vertex shared with the mesh frame keeps the mesh's share in the vertex
    # group the Armature modifier holds back from moving. With none named, the
    # button makes one after the mesh and sets the modifier to it, inverted,
    # so Blender shows the vertex moving as far as the game will.
    fresh_scene()
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)   # the joint on the mesh
    bpy.ops.ls3d.add_joint()
    lonely_rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    lonely_joint = lonely_rig.data.bones[0].name
    bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.2, depth=0.6,
                                        location=(0.0, 0.0, 0.0))
    lonely_mesh = bpy.context.object
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    lonely_mesh.name = "base"
    lonely_mesh.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
    lonely_mesh.ls3d_frame_type = str(C.FRAME_VISUAL)
    lonely_mesh.visual_type = str(C.VISUAL_SINGLEMESH)
    lonely_mesh.modifiers.new("Armature", "ARMATURE").object = lonely_rig
    lonely_mesh.parent = lonely_rig
    # A box big enough to hold the mesh, so its vertices are shared with the
    # mesh frame by where they sit along the box's Y.
    _influence.set_bone_parts(_influence.joint_of(lonely_rig, lonely_joint),
                              center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0),
                              turn=(0.0, 0.0, 0.0))
    said = []
    original_error = report_module.Report.error

    try:
        shared = bpy.ops.ls3d.weights_from_boxes()
    except RuntimeError as exc:
        shared = {"CANCELLED"}
        said.append(str(exc))
    held = next(m for m in lonely_mesh.modifiers if m.type == "ARMATURE")
    share_index = (lonely_mesh.vertex_groups["base"].index
                   if "base" in lonely_mesh.vertex_groups else None)
    parts = [{g.group: g.weight for g in v.groups}
             for v in lonely_mesh.data.vertices if v.groups]
    kept = sum(1 for p in parts if p.get(share_index, 0.0) > 0.0)
    whole = all(abs(sum(p.values()) - 1.0) < 1e-5 for p in parts)
    check("painting the boxes keeps the mesh's own share where the modifier "
          "holds it back",
          shared == {"FINISHED"} and share_index is not None
          and held.vertex_group == "base" and held.invert_vertex_group
          and parts and kept and whole,
          f"{shared}, modifier group {held.vertex_group!r} inverted "
          f"{held.invert_vertex_group}, {kept} of {len(parts)} vertices keep a "
          f"share, weights whole={whole}: {[m[:70] for m in said]}")

    # A box is fitted the way the game's own sit: on the joint, its weight axis
    # aimed at the joint below it so the weight flows down the chain, and a
    # fifth of the way there either side. The last joint of a chain has no
    # child and carries on the way its parent led to it instead. A box added
    # by hand used to copy a lone joint's box verbatim, whose axis pointed
    # straight up - ninety degrees off any chain that did not.
    from mathutils import Vector as _FitVec
    _fit_view = ls3d_module("4ds.viewport")

    def aimed(rig, bone):
        """Degrees between a box's weight axis and the way down the chain."""
        placed = _fit_view.influence_box_matrix(
            rig, bone.name, _influence.box_of(rig, bone.name))
        axis = (placed.to_3x3() @ _FitVec((0.0, 1.0, 0.0))).normalized()
        child = _influence.fitting_child(rig, bone.name)
        head = rig.matrix_world @ bone.head_local
        if child is not None:
            toward = rig.matrix_world @ rig.data.bones[child].head_local - head
        else:
            toward = head - rig.matrix_world @ bone.parent.head_local
        return math.degrees(axis.angle(toward.normalized()))

    fresh_scene()
    bpy.context.scene.cursor.location = (0.0, 0.0, 1.0)
    bpy.ops.ls3d.add_joint()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.context.view_layer.objects.active = rig
    rig.data.bones.active = rig.data.bones[0]
    bpy.ops.ls3d.add_joint()                       # the joint below it
    bpy.context.view_layer.update()
    upper, lower = rig.data.bones[0], rig.data.bones[1]
    before = aimed(rig, upper)
    rig.data.bones.active = upper
    fitted = bpy.ops.ls3d.fit_influence_box()
    rig.data.bones.active = lower
    fitted_lower = bpy.ops.ls3d.fit_influence_box()
    bpy.context.view_layer.update()
    after_upper, after_lower = aimed(rig, upper), aimed(rig, lower)
    gap = (lower.head_local - upper.head_local).length
    _center, size, _turn = _influence.bone_parts(
        _influence.joint_of(rig, upper.name))
    reach = size[1] * 0.5 / gap
    # A box added to a joint that already has one below it is fitted too.
    _influence.clear_box(rig, upper.name)
    rig.data.bones.active = upper
    bpy.ops.ls3d.add_influence_box()
    bpy.context.view_layer.update()
    added = aimed(rig, upper)
    check("a box is fitted on its joint, aimed down the chain",
          before > 45.0 and fitted == fitted_lower == {"FINISHED"}
          and after_upper < 1e-3 and after_lower < 1e-3
          and abs(reach - _influence.FIT_REACH) < 1e-4 and added < 1e-3,
          f"a copied box sat {round(before, 1)} deg off; fitted, the upper "
          f"joint {round(after_upper, 4)} deg and the last one "
          f"{round(after_lower, 4)} deg; reach {round(reach, 3)} of the way; "
          f"added by hand {round(added, 4)} deg")

    # A joint made from the Add menu lands where the cursor is and leaves the
    # armature object itself at rest, because an armature's own move or turn
    # is written nowhere - and an export is refused rather than write a
    # skeleton standing where the rest of the model is not.
    from mathutils import Vector as _JointVec
    fresh_scene()
    bpy.context.scene.cursor.location = (1.0, 2.0, 3.0)
    bpy.ops.ls3d.add_joint()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
    at_cursor = rig.data.bones[0].head_local.copy()
    rested = tuple(round(v, 6) for v in rig.matrix_world.translation)
    placed = os.path.join(out_dir, "joint_at_cursor.4ds")
    placed_result = export_op(placed)
    came_back = None
    if placed_result == {"FINISHED"}:
        fresh_scene()
        import_op(placed)
        back = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        came_back = (back.matrix_world
                     @ back.data.bones[0].head_local - at_cursor).length
        back.location = (0.0, 0.0, 4.0)
        moved_said = []
        original_error = report_module.Report.error

        def _hear_moved(self, message, fix=None):
            moved_said.append(message)
            return original_error(self, message, fix)

        report_module.Report.error = _hear_moved
        try:
            moved_rig = export_op(os.path.join(out_dir, "rig_moved.4ds"))
        finally:
            report_module.Report.error = original_error
    else:
        moved_rig, moved_said = "export failed", []
    check("a joint is added at the cursor with the armature left at rest, and "
          "an armature moved as an object is refused",
          placed_result == {"FINISHED"} and rested == (0.0, 0.0, 0.0)
          and (at_cursor - _JointVec((1.0, 2.0, 3.0))).length < 1e-6
          and came_back is not None and came_back < 1e-5
          and moved_rig == {"CANCELLED"}
          and any("moved" in m and "written nowhere" in m for m in moved_said),
          f"bone at {tuple(round(v, 3) for v in at_cursor)}, armature at "
          f"{rested}, back within {came_back}, moved armature {moved_rig}: "
          f"{[m[:60] for m in moved_said]}")

    # The box is moved, turned and resized by its handles, which are drawn on
    # the active joint's box and nowhere else. Each writes back into the
    # joint's own sixteen numbers: a face moves alone, an arrow slides the
    # whole box without resizing it, and a ring turns it without moving it.
    _handles = ls3d_module("4ds.gizmo_influence")
    _view = ls3d_module("4ds.viewport")
    fresh_scene()
    bpy.ops.ls3d.add_joint()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.ops.mesh.primitive_cube_add()
    handle_joint = rig.data.bones[0].name
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    rig.data.bones.active = rig.data.bones[handle_joint]
    bpy.ops.object.mode_set(mode="POSE")
    bpy.context.view_layer.update()
    offered = _handles.active_joint(bpy.context) is not None
    bpy.ops.object.mode_set(mode="OBJECT")

    def parts():
        return _influence.bone_parts(_influence.joint_of(rig, handle_joint))

    faces_before = [_handles.face_offset(rig, handle_joint, i) for i in range(6)]
    _handles.set_face_offset(rig, handle_joint, 1, faces_before[1] + 0.05)
    faces_after = [_handles.face_offset(rig, handle_joint, i) for i in range(6)]
    # The face moves by what it was dragged, and no other face follows it.
    face_moved = abs(faces_after[1] - faces_before[1] - 0.05) < 1e-5
    others_still = all(abs(faces_after[i] - faces_before[i]) < 1e-6
                       for i in (0, 2, 3, 4, 5))

    _center, size_before, _turn = parts()
    moves_before = [_handles.move_offset(rig, handle_joint, i) for i in range(3)]
    _handles.set_move_offset(rig, handle_joint, 0, moves_before[0] + 0.1)
    moves_after = [_handles.move_offset(rig, handle_joint, i) for i in range(3)]
    _center, size_moved, _turn = parts()
    slid = (abs(moves_after[0] - moves_before[0] - 0.1) < 1e-5
            and all(abs(moves_after[i] - moves_before[i]) < 1e-6 for i in (1, 2))
            and all(abs(a - b) < 1e-6 for a, b in zip(size_before, size_moved)))

    center_before, size_turned, turn_before = parts()
    spun = _handles.turned_box(rig, handle_joint, (center_before, size_turned,
                                                   turn_before), 2, 0.5)
    _influence.set_bone_parts(_influence.joint_of(rig, handle_joint),
                              center=spun[0], size=spun[1], turn=spun[2])
    center_after, size_after, turn_after = parts()
    # A turn moves nothing and resizes nothing; it only turns.
    turned = (max(abs(a - b) for a, b in zip(center_before, center_after)) < 1e-6
              and max(abs(a - b) for a, b in zip(size_turned, size_after)) < 1e-5
              and max(abs(a - b) for a, b in zip(turn_before, turn_after)) > 0.1)

    # What the handles wrote is what the export writes.
    handled = os.path.join(out_dir, "handled_box.4ds")
    handled_out = export_op(handled)
    written = None
    if handled_out == {"FINISHED"}:
        joint = next(f for f in read_file(handled).frames if f.joint is not None)
        written = joint.joint.matrix
    box_now = _influence.file_box(rig, handle_joint)
    # The handles belong to the joint that is picked, and let go with it.
    # Blender leaves a bone active after it is deselected, and selected after
    # Pose Mode is left, so either on its own would keep them on screen with
    # nothing picked; and while the armature is being edited the bones are
    # not where the handles think they are. The box is drawn brighter exactly
    # while its handles are on.
    def handles_on():
        return _handles.active_joint(bpy.context) is not None

    def box_lit():
        return any(color == _view.INFLUENCE_BOX_COLOR_SELECTED
                   for _matrix, color, _joint in
                   _view._influence_boxes(bpy.context))

    in_object_mode = handles_on() or box_lit()
    bpy.ops.object.mode_set(mode="POSE")
    posed = handles_on() and box_lit()
    bpy.ops.pose.select_all(action="DESELECT")      # as clicking empty space
    let_go = handles_on() or box_lit()
    still_active = rig.data.bones.active is not None
    rig.pose.bones[handle_joint].select = True
    picked_again = handles_on() and box_lit()
    bpy.ops.object.mode_set(mode="EDIT")
    while_editing = handles_on()
    bpy.ops.object.mode_set(mode="OBJECT")
    still_selected = rig.pose.bones[handle_joint].select
    back_again = handles_on() or box_lit()
    bpy.ops.object.mode_set(mode="POSE")
    rig.select_set(False)
    without_the_rig = handles_on() or box_lit()
    rig.select_set(True)
    setattr(bpy.context.scene, C.SHOW_INFLUENCE_BOXES_PROP, False)
    while_hidden = handles_on()
    setattr(bpy.context.scene, C.SHOW_INFLUENCE_BOXES_PROP, True)
    bpy.ops.object.mode_set(mode="OBJECT")
    check("the box handles belong to the joint that is picked, in Pose Mode",
          not in_object_mode and posed and not let_go and still_active
          and picked_again and not while_editing and still_selected
          and not back_again and not without_the_rig and not while_hidden,
          f"in object mode {in_object_mode}, posed {posed}, after deselecting "
          f"{let_go} (the bone is still active={still_active}), picked again "
          f"{picked_again}, while editing the armature {while_editing}, back "
          f"in object mode {back_again} (the bone still selected="
          f"{still_selected}), with the armature deselected "
          f"{without_the_rig}, with the boxes hidden {while_hidden}")

    # A joint's box hangs off the joint and can sit anywhere, so it is marked
    # the way a dummy's box is: a dot at its middle, and a dashed line back to
    # the joint while the two are apart. A box sitting on its joint gets no
    # line - there would be nothing to see.
    from types import SimpleNamespace as _DotView
    from mathutils import Matrix as _DotMatrix
    _dot_parts = _influence.bone_parts(_influence.joint_of(rig, handle_joint))
    _dot_region = _DotView(width=1000)
    _dot_look = _DotView(window_matrix=_DotMatrix.Diagonal((0.2, 0.2, 1.0, 1.0)),
                         perspective_matrix=_DotMatrix.Identity(4))

    def _drawn_box():
        """What the overlay carries for this joint's box: middle, and joint."""
        placed = _view.influence_box_matrix(
            rig, handle_joint, _influence.box_of(rig, handle_joint))
        for matrix, _color, joint in _view._influence_boxes(bpy.context):
            if (matrix.translation - placed.translation).length < 1e-6:
                return matrix, joint
        return None, None

    _influence.set_bone_parts(_influence.joint_of(rig, handle_joint),
                              center=(0.3, 0.0, 0.0))
    bpy.context.view_layer.update()
    off_box, off_joint = _drawn_box()
    joint_place = _view.joint_world_matrix(rig, handle_joint).translation
    off_dot = (_view.box_center(_view.influence_box_corners(off_box))
               if off_box is not None else None)
    joined = ([] if off_joint is None else
              _view.link_lines(off_box.translation, off_joint,
                               _dot_region, _dot_look))
    _influence.set_bone_parts(_influence.joint_of(rig, handle_joint),
                              center=(0.0, 0.0, 0.0))
    bpy.context.view_layer.update()
    on_box, on_joint = _drawn_box()
    on_dot = (_view.box_center(_view.influence_box_corners(on_box))
              if on_box is not None else None)
    _influence.set_bone_parts(_influence.joint_of(rig, handle_joint),
                              center=_dot_parts[0], size=_dot_parts[1],
                              turn=_dot_parts[2])
    bpy.context.view_layer.update()
    check("a joint's box carries a dot, and a line to the joint while apart",
          off_box is not None and on_box is not None
          and off_joint is not None and on_joint is None
          and (off_joint - joint_place).length < 1e-6
          and (off_dot - off_box.translation).length < 1e-6
          and (on_dot - on_box.translation).length < 1e-6
          and len(joined) % 2 == 0 and len(joined) >= 2
          and (joined[0] - off_box.translation).length < 1e-4
          and (joined[-1] - off_joint).length < 1e-4,
          f"the moved box's dot sits on its middle "
          f"{None if off_dot is None else (off_dot - off_box.translation).length < 1e-6}"
          f", a centered box's "
          f"{None if on_dot is None else (on_dot - on_box.translation).length < 1e-6}"
          f"; the joint it is joined to "
          f"{None if off_joint is None else off_joint.to_tuple(3)} against the "
          f"joint at {joint_place.to_tuple(3)}, in {len(joined) // 2} dash(es) "
          f"from the middle to the joint; a box on its joint is joined to "
          f"{on_joint}")

    # Every handle has to land on the box: a handle drawn somewhere else is a
    # handle nobody can hit. The faces sit on their faces, the arrows out
    # beyond them, the rings on the center.
    placed_box = _view.influence_box_matrix(
        rig, handle_joint, _influence.box_of(rig, handle_joint))
    reach = max(placed_box.col[i].to_3d().length for i in range(3))
    faces_at = [_handles.face_handle_matrix(rig, handle_joint, i) for i in range(6)]
    arrows_at = [_handles.move_handle_matrix(rig, handle_joint, i) for i in range(3)]
    rings_at = [_handles.ring_matrix(rig, handle_joint, i) for i in range(3)]
    on_box = all((m.translation - placed_box.translation).length <= reach * 2.5
                 for m in faces_at + arrows_at + rings_at)
    rings_centered = all((m.translation - placed_box.translation).length < 1e-6
                         for m in rings_at)
    # ... and a box follows its joint while the model is posed.
    bpy.ops.object.mode_set(mode="POSE")
    posed_bone = rig.pose.bones[handle_joint]
    posed_bone.location = (0.0, 0.0, 0.3)
    bpy.context.view_layer.update()
    _view.forget_joint_spaces()
    posed_box = _view.influence_box_matrix(
        rig, handle_joint, _influence.box_of(rig, handle_joint))
    followed = (posed_box.translation - placed_box.translation).length
    posed_bone.location = (0.0, 0.0, 0.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()
    _view.forget_joint_spaces()
    back_box = _view.influence_box_matrix(
        rig, handle_joint, _influence.box_of(rig, handle_joint))
    returned = (back_box.translation - placed_box.translation).length
    check("the handles sit on the box, and the box follows the joint's pose",
          on_box and rings_centered and abs(followed - 0.3) < 1e-4
          and returned < 1e-6,
          f"handles on the box={on_box}, rings centered={rings_centered}, "
          f"posing the joint 30 cm moved the box {followed:.4f} m, back to "
          f"{returned:.2e} m")

    # A joint's box is a few centimeters across, and handles placed on a box
    # that small all land within a few pixels of each other - six faces, three
    # arrows and three rings in one clump, where every click is a guess. Given
    # a view to measure against, each kind is held its own distance out, and a
    # handle standing end-on to the camera - drawn over the middle, saying
    # nothing about a distance - is taken away until the view moves.
    import itertools as _itertools
    from bpy_extras.view3d_utils import location_3d_to_region_2d as _to_screen

    class _Region:
        width, height = 1920, 1080

    class _View:
        """A view looking down -Z from *distance* away, as a 3D view would."""

        def __init__(self, distance, rotation=None):
            near, far, focal = 0.1, 100.0, 2.0
            window = _JMatrixProbe(((focal, 0, 0, 0), (0, focal * 16 / 9, 0, 0),
                                    (0, 0, (far + near) / (near - far),
                                     2 * far * near / (near - far)),
                                    (0, 0, -1, 0)))
            self.view_rotation = rotation or _JQuaternion((1.0, 0.0, 0.0, 0.0))
            turn = self.view_rotation.to_matrix().to_4x4()
            self.perspective_matrix = (
                window @ _JMatrixProbe.Translation(_JVector((0.0, 0.0, -distance)))
                @ turn.inverted())

    class _Look:
        def __init__(self, distance, rotation=None):
            self.region = _Region()
            self.region_data = _View(distance, rotation)
            self.scene = bpy.context.scene

    fresh_scene()
    # At the world's middle, where the stand-in view below is looking.
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
    bpy.ops.ls3d.add_joint()
    spaced_rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    spaced_joint = spaced_rig.data.bones[0].name
    _influence.set_bone_parts(_influence.joint_of(spaced_rig, spaced_joint),
                             center=(0.0, 0.0, 0.0), size=(0.02, 0.02, 0.02),
                             turn=(0.0, 0.0, 0.0))
    bpy.context.view_layer.update()
    # Looked at from a corner, so no axis is end-on and every handle counts.
    corner = _JVector((1.0, 1.0, 1.0)).normalized().to_track_quat("Z", "Y")
    looking = _Look(3.0, corner)

    def _places(context):
        found = {}
        for index in range(6):
            found[f"face{index}"] = _handles.face_handle_matrix(
                spaced_rig, spaced_joint, index, context).translation.copy()
        for index in range(3):
            found[f"arrow{index}"] = _handles.move_handle_matrix(
                spaced_rig, spaced_joint, index, context).translation.copy()
        return found

    def _on_screen(places, context):
        out = {}
        for name, place in places.items():
            point = _to_screen(context.region, context.region_data, place)
            if point is not None:
                out[name] = point
        return out

    tight = _on_screen(_places(None), looking)
    spread = _on_screen(_places(looking), looking)
    middle = _to_screen(looking.region, looking.region_data,
                        _JVector((0.0, 0.0, 0.0)))
    closest_tight = min((a - b).length
                        for a, b in _itertools.combinations(tight.values(), 2))
    closest_spread = min((a - b).length
                         for a, b in _itertools.combinations(spread.values(), 2))
    face_out = min((spread[f"face{i}"] - middle).length for i in range(6))
    arrow_out = min((spread[f"arrow{i}"] - middle).length for i in range(3))
    ring_radius = _handles.ring_matrix(spaced_rig, spaced_joint, 0,
                                       looking).to_scale().x * _handles.RING_RADIUS
    ring_pixels = ring_radius / _handles.world_per_pixel(
        looking, _JVector((0.0, 0.0, 0.0)))

    # A box big enough on screen is left where it is: the handles stay on it.
    _influence.set_bone_parts(_influence.joint_of(spaced_rig, spaced_joint),
                             center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0),
                             turn=(0.0, 0.0, 0.0))
    bpy.context.view_layer.update()
    big_looking = _Look(6.0, corner)
    on_box = max(
        (_handles.face_handle_matrix(spaced_rig, spaced_joint, i,
                                     big_looking).translation
         - _handles.face_handle_matrix(spaced_rig, spaced_joint, i,
                                       None).translation).length
        for i in range(6))

    # End-on and edge-on handles are taken away.
    toward = _JVector((0.0, 0.0, -1.0))
    end_on = _handles.points_at_camera(toward, _JVector((0.0, 0.0, 0.0)),
                                       _JVector((0.0, 0.0, 0.3)))
    across = _handles.points_at_camera(toward, _JVector((0.0, 0.0, 0.0)),
                                       _JVector((0.3, 0.0, 0.0)))
    flat_ring = _handles.stands_edge_on(
        toward, _JMatrixProbe.Rotation(math.radians(90.0), 4, "X"))
    facing_ring = _handles.stands_edge_on(toward, _JMatrixProbe.Identity(4))
    check("a joint box's handles keep their distance on screen",
          closest_tight < 12.0 and closest_spread > 24.0
          and face_out > 40.0 and arrow_out > face_out + 24.0
          and ring_pixels > arrow_out + 24.0 and on_box < 1e-6
          and end_on and not across and flat_ring and not facing_ring,
          f"on a 2 cm box the closest two handles were {closest_tight:.1f} px "
          f"apart and are now {closest_spread:.1f}; faces {face_out:.0f} px "
          f"out, arrows {arrow_out:.0f}, the ring {ring_pixels:.0f}; on a big "
          f"box the handles moved {on_box:.1e} m; end-on hidden={end_on}, "
          f"across kept={not across}, edge-on ring hidden={flat_ring}, "
          f"facing ring kept={not facing_ring}")

    # A drag is one step of its own: letting go of a handle leaves something
    # Ctrl+Z can take back, and a cancelled drag leaves nothing at all.
    _dummy_gizmos = ls3d_module("4ds.gizmo_dummy")
    pushes = []
    original_push = _dummy_gizmos.push_undo

    class _FakeRing:
        armature = spaced_rig
        bone_name = spaced_joint
        start_parts = None

    class _FakeHandle:
        def __init__(self, start, now):
            self.start_value = start
            self._value = now

        def target_get_value(self, _name):
            return self._value

        def target_set_value(self, _name, value):
            self._value = value

    try:
        _dummy_gizmos.push_undo = lambda message: pushes.append(message)
        _handles.push_undo = _dummy_gizmos.push_undo
        joint_now = _influence.joint_of(spaced_rig, spaced_joint)
        _FakeRing.start_parts = _influence.bone_parts(joint_now)
        # Nothing moved: nothing to take back.
        _handles.LS3D_GT_BoxRing.exit(_FakeRing(), bpy.context, False)
        unmoved = len(pushes)
        _influence.set_bone_parts(joint_now, center=(0.05, 0.0, 0.0))
        _handles.LS3D_GT_BoxRing.exit(_FakeRing(), bpy.context, False)
        turned_push = len(pushes)
        _handles.LS3D_GT_BoxRing.exit(_FakeRing(), bpy.context, True)
        cancelled_push = len(pushes)
        moved_handle = _FakeHandle(0.0, 0.25)
        _dummy_gizmos.LS3D_GT_DummyHandle.exit(moved_handle, bpy.context, False)
        dragged_push = len(pushes)
        still_handle = _FakeHandle(0.25, 0.25)
        _dummy_gizmos.LS3D_GT_DummyHandle.exit(still_handle, bpy.context, False)
        still_push = len(pushes)
    finally:
        _dummy_gizmos.push_undo = original_push
        _handles.push_undo = original_push
    check("letting go of a handle leaves a step to take back",
          unmoved == 0 and turned_push == 1 and cancelled_push == 1
          and dragged_push == 2 and still_push == 2 and pushes,
          f"pushes after: nothing moved {unmoved}, a turn {turned_push}, a "
          f"cancelled turn {cancelled_push}, a dragged face {dragged_push}, "
          f"a face let go where it started {still_push}; messages {pushes}")

    check("a box is moved, turned and resized by its handles",
          offered and face_moved and others_still and slid and turned
          and written is not None
          and max(abs(a - b) for a, b in zip(written, box_now)) == 0.0,
          f"handles offered={offered}, face moved alone={face_moved and others_still}, "
          f"slid without resizing={slid}, turned in place={turned}, export "
          f"{handled_out}")

    # A box is written as it is drawn, however it is turned or scaled: an
    # uneven scale is part of its shape, not something to even out. Measured
    # on the joint, which is what the file keeps - where the model as a whole
    # sits is another question, and other tests ask it.
    _view = ls3d_module("4ds.viewport")

    def corners_on_joint(armature, bone_name):
        box = _influence.boxes_of(armature)[bone_name]
        at_rest = (armature.matrix_world
                   @ armature.data.bones[bone_name].matrix_local).inverted()
        placed = _view.influence_box_matrix(armature, bone_name, box)
        return [(at_rest @ corner).copy()
                for corner in _view.influence_box_corners(placed)]

    fresh_scene()
    bpy.ops.ls3d.add_joint()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    bpy.ops.mesh.primitive_cube_add()
    joint_name = rig.data.bones[0].name
    skew_joint = _influence.joint_of(rig, joint_name)
    _influence.set_bone_parts(skew_joint, center=(0.12, -0.4, 0.25),
                              size=(0.35, 1.8, 0.6), turn=(0.3, -0.7, 1.1))
    bpy.context.view_layer.update()
    corners = corners_on_joint(rig, joint_name)
    skewed = os.path.join(out_dir, "influence_skewed.4ds")
    skewed_result = export_op(skewed)
    skew_values, skew_worst = None, float("inf")
    if skewed_result == {"FINISHED"}:
        skew_values = next(f for f in read_file(skewed).frames
                           if f.joint is not None).joint.matrix
        fresh_scene()
        import_op(skewed)
        back = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        bpy.context.view_layer.update()
        skew_worst = max((a - b).length for a, b in
                         zip(corners, corners_on_joint(back, joint_name)))
    check("a box turned and scaled unevenly is written as it is drawn",
          skew_values is not None and skew_worst < 1e-5
          and max(abs(a - b) for a, b in zip(skew_values, _C.DEFAULT_JOINT_BOX)) > 0.01,
          f"export {skewed_result}, corners off by {skew_worst:.2e} m")

    # A joint painted by hand needs no box of its own - nothing in the game
    # reads one - so it is written the box a new joint is made with, and the
    # export says so.
    fresh_scene()
    bpy.ops.object.armature_add(enter_editmode=True, location=(0.0, 0.0, 0.0))
    rig = bpy.context.object
    rig.data.edit_bones[0].name = "back1"
    rig.data.edit_bones["back1"].head = (0.0, 0.0, 1.0)
    rig.data.edit_bones["back1"].tail = (0.0, 0.0, 1.4)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.15, depth=0.8,
                                        location=(0.0, 0.0, 1.2))
    body = bpy.context.object
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    body.name = "base"
    body.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
    body.ls3d_frame_type = str(C.FRAME_VISUAL)
    body.visual_type = str(C.VISUAL_SINGLEMESH)
    body.vertex_groups.new(name="back1").add(
        range(len(body.data.vertices)), 1.0, "REPLACE")
    body.modifiers.new("Armature", "ARMATURE").object = rig
    body.parent = rig
    heard = []
    original_warn = report_module.Report.warn

    def _hear_warning(self, message, fix=None):
        heard.append(message)
        return original_warn(self, message, fix)

    report_module.Report.warn = _hear_warning
    try:
        painted_path = os.path.join(out_dir, "painted_no_box.4ds")
        painted_result = export_op(painted_path)
    finally:
        report_module.Report.warn = original_warn
    default_gap = None
    if painted_result == {"FINISHED"}:
        joint = next(f for f in read_file(painted_path).frames if f.joint is not None)
        default_gap = max(abs(a - b) for a, b in
                          zip(joint.joint.matrix, _C.DEFAULT_JOINT_BOX))
    check("a joint painted by hand exports without a box of its own, on the "
          "default one",
          painted_result == {"FINISHED"} and default_gap is not None
          and default_gap < 1e-8
          and any("no influence box" in m for m in heard),
          f"export {painted_result}, off the default box by {default_gap}, "
          f"said {[m[:60] for m in heard]}")

    # A skin numbers its joints in the order of the mesh's own vertex groups,
    # so a character whose groups go in Tommy's order is numbered as Tommy is
    # - and the preset carries his boxes whatever the numbering.
    fresh_scene()
    bpy.ops.ls3d.add_character_skeleton()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    preset_boxes = len(_influence.boxes_of(rig))
    bpy.ops.mesh.primitive_cube_add(size=0.3, location=(0.0, 0.0, 1.2))
    cube = bpy.context.object
    base = character_mesh(rig, cube.data)
    bpy.data.objects.remove(cube, do_unlink=True)
    base.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
    preset_module = ls3d_module("4ds.character_preset")
    in_order = sorted(preset_module.JOINT_NUMBERS,
                      key=preset_module.JOINT_NUMBERS.get)
    for joint_name in in_order:
        base.vertex_groups.new(name=joint_name)
    base.vertex_groups["back1"].add(range(len(base.data.vertices)), 1.0,
                                    "REPLACE")
    preset_path = os.path.join(out_dir, "preset_numbers.4ds")
    preset_result = export_op(preset_path)
    numbers = {}
    if preset_result == {"FINISHED"}:
        numbers = {f.name: f.joint.joint_id for f in read_file(preset_path).frames if f.joint}
    tommy_numbers = ({f.name: f.joint.joint_id for f in read_file(tommy).frames if f.joint}
                     if os.path.exists(tommy) else None)
    check("the character preset numbers its joints as Tommy does, boxes and all",
          preset_result == {"FINISHED"} and preset_boxes == 18
          and (tommy_numbers is None or numbers == tommy_numbers),
          f"{preset_boxes} boxes, numbers {numbers}")



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True,
                        help="folder of .4ds files, or a text file listing them")
    parser.add_argument("--maps", default="", help="texture folder")
    parser.add_argument("--out", default="", help="where to write test output")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--suite", choices=("all", "round-trip", "regression"),
                        default="all")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = parser.parse_args(argv)

    io_mafia_toolkit.register()
    if args.maps:
        class _Preferences:
            """Stand-in for the real AddonPreferences in a headless run.

            It must carry every field the operators read, or the export raises.
            """
            textures_path = args.maps
            show_raw_flags = True
        io_mafia_toolkit.get_preferences = lambda: _Preferences()

    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "_test_output")
    os.makedirs(out_dir, exist_ok=True)

    if os.path.isdir(args.models):
        models_dir = args.models
        models = sorted(os.path.join(models_dir, f) for f in os.listdir(models_dir)
                        if f.lower().endswith(".4ds"))
    elif args.models.lower().endswith(".4ds"):
        models = [args.models]
        models_dir = os.path.dirname(args.models)
    else:                                   # a text file listing model paths
        models = [l.strip() for l in open(args.models, encoding="utf-8") if l.strip()]
        models_dir = os.path.dirname(models[0]) if models else ""
    if args.limit:
        models = models[:args.limit]

    if args.suite in ("all", "round-trip"):
        run_round_trip(models, out_dir)
    if args.suite in ("all", "regression"):
        run_regressions(models_dir, out_dir)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  failed: {name}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
