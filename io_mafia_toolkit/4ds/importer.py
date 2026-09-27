"""Build Blender data from a parsed 4DS document.

The import runs in stages, in this order, because each depends on the last:

1. materials  - so face groups can reference them
2. frames     - meshes, empties and their payloads
3. instances  - frames that reuse another frame's mesh
4. armature   - one for the whole model, holding every joint frame
5. skinning   - vertex groups and armature modifiers
6. morphs     - shape keys plus their per-target normals
7. hierarchy  - parenting and local transforms
8. targets    - look-at links, mirrored as Track To constraints

Failures are raised, not swallowed. The operator turns them into a clear
"import failed" result; the previous behavior of catching everything and still
reporting success left people with a half-loaded scene and no indication.
"""

import os

import bpy

from ..common import constants as C
from .codec import read_file
from .import_frames import FramesMixin
from .import_geometry import GeometryMixin
from .import_rigging import RiggingMixin



class Importer(GeometryMixin, FramesMixin, RiggingMixin):
    def __init__(self, filepath, report, texture_dir=None,
                 own_collection=True):
        self.filepath = filepath
        self.report = report
        self.texture_dir = texture_dir if texture_dir and os.path.isdir(texture_dir) else None
        # A model of its own, so several can be opened side by side and told
        # apart - and so one can be hidden without hunting through the
        # outliner for its hundred frames.
        self.collection = bpy.context.collection
        if own_collection:
            stem = os.path.splitext(os.path.basename(filepath))[0]
            made = bpy.data.collections.new(stem)
            bpy.context.scene.collection.children.link(made)
            self.collection = made

        self.texture_cache = {}
        self.materials = [None]             # index 0 unused; file ids are 1-based

        self.objects_by_frame = {}          # frame id -> Object or None
        self.frame_types = {}               # frame id -> frame type
        self.local_matrices = {}            # frame id -> local Matrix
        self.local_transforms = {}          # frame id -> (loc, rot, scale)
        self.parent_of = {}                 # frame id -> parent frame id
        self.skipped_frames = {}            # Sound/Area frame id -> its parent id
        self.rehung_frames = []             # frame ids moved up past a skipped one
        self.lod_objects = {}               # frame id -> [Object per LOD]
        self.vertex_copies = {}             # Mesh -> {copy vertex: vertex it repeats}

        self.joints = []                    # dicts describing FRAME_JOINT frames
        self.bone_name_by_frame = {}        # frame id -> Blender bone name
        self.bone_world_by_frame = {}       # frame id -> WITH-SCALE world Matrix
        self.armature = None                # the armature the skinned mesh binds to
        self.armatures = []                 # every armature, the one above first
        self.armature_by_frame = {}         # bone frame id -> Armature Object
        self.armature_by_mesh = {}          # mesh Object -> Armature Object
        self.skin_frames = set()            # skinned mesh frames with joints below
        self.skin_jobs = []                 # (objects, Skin, frame id)
        self.morph_jobs = []                # (objects, Morph, frame id)
        self.target_jobs = []               # (Object, Target)
        self.instance_jobs = []             # (Object, source frame id)

    # ── entry point ───────────────────────────────────────────────────────────
    def run(self):
        doc = read_file(self.filepath)
        self.build(doc)

        # The file's own number, and a fresh baseline: whatever was typed for
        # the last model says nothing about this one.
        bpy.context.scene.ls3d_animated_count_pinned = False
        bpy.context.scene.ls3d_animated_object_count = doc.animated_object_count
        bpy.context.scene.ls3d_animated_count_pinned = False
        self.report.info(f"Animated object count: {doc.animated_object_count}")
        return doc

    def build(self, doc):
        """Every stage, for a document already in hand.

        Split from :meth:`run` so the Add menu's presets can be built by the
        same code a file is, without taking over the scene's animated object
        count the way opening a model does.
        """
        self.report.info(f"Version {doc.version}, "
                         f"{len(doc.materials)} material(s), "
                         f"{len(doc.frames)} frame(s)")

        self.report.stage("Materials")
        self.report.span(0, 25)
        self._build_materials(doc)

        self.report.stage("Frames")
        self.report.span(25, 85)
        self._build_frames(doc)

        self.report.stage("Post-processing")
        self.report.span(85, 100)
        self._resolve_instances()
        if self.joints:
            self.report.substage(f"Building armature ({len(self.joints)} joint(s))")
            self._build_armatures()
        self._apply_hierarchy()
        if self.skin_jobs:
            self.report.substage(f"Applying skinning ({len(self.skin_jobs)} mesh(es))")
            self._apply_skinning()
        if self.morph_jobs:
            self.report.substage(f"Applying morphs ({len(self.morph_jobs)} mesh(es))")
            self._apply_morphs()
        self._resolve_targets()
        self._settle_opacity()
        self._arrange_scene_order(len(doc.frames))
        if self.armature is not None:
            self._settle_bones()
            self.report.substage("Influence boxes")
            self._build_influence_boxes()

    def _arrange_scene_order(self, frame_count):
        """Lay the model's objects out in the scene so the export keeps their order.

        The export writes frames down the scene's order, each as soon as the
        frame it hangs from is out, with what hangs from a frame straight after
        it and joints hanging from nothing at their armature's place (see the
        export's frame walk). Joints are not objects, so where they fall
        between the objects has to come from the order the objects and the
        armature sit in. This works that order out from the file's: an object
        the file writes straight after the frame it hangs from is put ahead of
        whatever sends that frame out, and the armature goes where the first
        joint hanging from nothing does. A model with no joints is already in
        the file's order.
        """
        if self.armature is None:
            return
        joint = {frame_id: self.frame_types.get(frame_id) == C.FRAME_JOINT
                 for frame_id in range(1, frame_count + 1)}

        def parent(frame_id):
            above = self.parent_of.get(frame_id, 0)
            if (joint[frame_id] and above and not joint.get(above)
                    and above not in self.skin_frames):
                return 0            # a joint Blender could not hang from it
            return above

        joint_children = {}
        for frame_id in range(1, frame_count + 1):
            if joint[frame_id] and parent(frame_id):
                joint_children.setdefault(parent(frame_id), []).append(frame_id)
        roots = [frame_id for frame_id in range(1, frame_count + 1)
                 if joint[frame_id] and not parent(frame_id)]

        written = []
        done = set()
        scene = []
        trigger = [0]

        def upcoming():
            return len(written) + 1

        def write(frame_id):
            if upcoming() != frame_id:
                raise LookupError(frame_id)
            written.append(frame_id)
            done.add(frame_id)
            for child in joint_children.get(frame_id, ()):
                if child not in done:
                    write(child)
            while (upcoming() <= frame_count and not joint[upcoming()]
                   and parent(upcoming()) == frame_id):
                child = upcoming()
                scene.insert(trigger[0], child)
                trigger[0] += 1
                write(child)

        try:
            armature_placed = False
            while upcoming() <= frame_count:
                frame_id = upcoming()
                if joint[frame_id]:
                    if armature_placed:
                        raise LookupError(frame_id)
                    armature_placed = True
                    scene.append("armature")
                    trigger[0] = len(scene) - 1
                    for root in roots:
                        if root not in done:
                            write(root)
                    continue
                if self.objects_by_frame.get(frame_id) is None:
                    raise LookupError(frame_id)
                scene.append(frame_id)
                trigger[0] = len(scene) - 1
                above = parent(frame_id)
                if above and above not in done:
                    raise LookupError(frame_id)
                write(frame_id)
            if not armature_placed:
                scene.append("armature")
        except (LookupError, RecursionError):
            self.report.warn(
                "The model's frames come in an order the scene cannot lay out, "
                "so an export will write them in another order.")
            return

        objects = []
        for item in scene:
            if item == "armature":
                objects.extend(self.armatures)
            else:
                objects.append(self.objects_by_frame[item])
        links = self.collection.objects
        for obj in objects:
            links.unlink(obj)
        for obj in objects:
            links.link(obj)

def import_4ds(filepath, report, texture_dir=None, own_collection=True):
    """Import *filepath* into the current scene."""
    return Importer(filepath, report, texture_dir, own_collection).run()
