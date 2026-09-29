"""Build a 4DS document from the current Blender scene and write it out.

The export is a two-phase operation on purpose:

1. **Build** the complete :class:`~io_mafia_toolkit.format.Document` in memory,
   validating as it goes.
2. **Write** it, in one atomic replace.

Nothing touches the destination path until phase 1 has finished without errors.
The previous design opened the target file with ``"wb"`` up front and validated
while writing, so any validation failure truncated a working model to a partial
file while the UI reported only that the export had failed.
"""


import bpy
from mathutils import Quaternion, Vector

from ..common import constants as C
from ..common import describe
from .codec import Document, write_file
from .codec.document import current_filetime
from .codec.types import Frame
from .export_frames import FramesMixin
from .joint_space import (edit_rest, is_posed, mesh_frame_channels,
                          pose_rest,
                          skinned_meshes_of)
from .export_materials import MaterialsMixin
from .export_payloads import PayloadsMixin
from .export_util import ExportError
from .validation import (validate_armature, validate_influence_boxes,
                         validate_text_length)

class _Silent:
    """A report that hears everything and says nothing.

    Planning runs the export's own ordering, which reports as it goes - and a
    viewport redraw is no place for any of it.
    """

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def plan_instances(objects=None):
    """``{copy: master}`` for every frame the export would write as an instance.

    Runs the export's own planning - the LOD chains, then the frame order,
    including the move that puts the object with the most LOD levels first in
    its group - and nothing after it, so no geometry is built and nothing is
    written. The first user of a mesh owns it; a later one with no LOD levels of
    its own becomes a copy of it, and one that has levels of its own is written
    in full.

    A model imported from a file and one built by hand get the same answer,
    because nothing recorded at import time is consulted: this is the question
    the export asks, asked early.
    """
    if objects is None:
        objects = list(bpy.context.scene.objects)
    planner = Exporter(objects, _Silent())
    planner._collect_lods()
    planner._order_frames()

    owners = {}
    copies = {}
    for obj in planner.frame_order:
        if (isinstance(obj, tuple) or obj.type != "MESH"
                or not planner._is_instanceable(obj)):
            continue
        chain = planner.lods.get(obj, [obj])
        key = chain[0].data
        master = owners.get(key)
        if master is None:
            owners[key] = obj
        elif len(chain) == 1:
            copies[obj] = master
    return copies


class Exporter(MaterialsMixin, FramesMixin, PayloadsMixin):
    def __init__(self, objects, report,
                 fix_multi_influences=False, fix_non_parent_child=False):
        self.report = report
        self.fix_multi_influences = fix_multi_influences
        self.fix_non_parent_child = fix_non_parent_child
        self.source_objects = list(objects)
        self.materials = []                 # ordered Blender materials
        self.material_index = {}            # Blender material -> 1-based id
        self.lods = {}                      # base Object -> [Object per LOD]
        self.frame_ids = {}                 # Object or (armature, bone) -> id
        self.ordered = []                   # exported objects, in scene order
        self.frame_order = []               # objects and (armature, bone) in write order
        self._candidate_set = set()
        self.joint_maps = {}                # armature -> {bone name: 1-based}
        self.instance_sources = {}          # mesh chain -> frame id that owns it
        self.errors = []
        self._joint_spaces = {}             # armature -> JointSpace
        self._influence_lists = {}          # armature -> {bone: box in joint space}
        self.rest = {}                      # armature -> {bone: (head, tail, roll)}
        self.posed = set()                  # the armatures written as posed
        self._posed_matrices = {}           # armature -> {bone: where posed}
        self.animated_now = None            # tracks counted before the
        #                                     animation was taken off

    # ── entry point ───────────────────────────────────────────────────────────
    def build(self):
        """Assemble the document. Raises :class:`ExportError` on any problem."""
        self._read_bones()
        self._collect_lods()
        self._collect_materials()
        self._order_frames()
        self._assign_frame_ids()
        self._raise_if_errors()

        doc = Document(version=C.VERSION_MAFIA, timestamp=current_filetime())

        self.report.stage("Materials")
        self.report.span(5, 20)
        total = len(self.materials)
        for index, source in enumerate(self.materials, start=1):
            built = self._build_material(source)
            self.report.item("Mat", index, total, describe.material_line(
                source.name, built.flags,
                (built.diffuse_texture, built.alpha_texture,
                 built.env_texture)))
            doc.materials.append(built)
        scene = bpy.context.scene
        doc.animated_object_count = scene.ls3d_animated_object_count
        self._check_animated_count(scene)

        self._validate_armatures()

        self.report.stage("Frames")
        self.report.span(20, 85)
        total = len(self.frame_order)
        for index, entry in enumerate(self.frame_order, start=1):
            frame = self._build_frame(entry)
            self.report.item("Frame", index, total,
                             f"'{frame.name}' " + describe.frame_kind(
                                 frame.frame_type, frame.visual_type))
            doc.frames.append(frame)

        self._validate_names(doc)
        self._raise_if_errors()
        return doc

    def _read_bones(self):
        """Every exported armature's bones exactly, before anything reads them.

        A posed skeleton is written as it is posed: the model is what is on
        screen, so posing a joint moves it in the file, and the skinned mesh
        goes out bent the same way. Its influence boxes ride along - a box is
        kept in its joint's own space, so it is wherever its joint is.

        A skeleton nobody has posed is read in Edit Mode instead. A bone's roll
        is only kept exactly there, and a joint's values are only the ones that
        give back the same bone if they are read from it exactly. Done first,
        because switching an armature in and out of Edit Mode rebuilds its
        bones.
        """
        for armature in (o for o in self.source_objects if o.type == "ARMATURE"):
            if is_posed(armature):
                rest = pose_rest(armature)
                if rest is not None:
                    self.rest[armature] = rest
                    self.posed.add(armature)
                    self.report.info(
                        f"Armature '{armature.name}' is posed, so the model is "
                        f"written as it is posed.")
                    self._warn_about_animation(armature)
                    continue
            rest = edit_rest(armature)
            if rest is None:
                self.report.warn(
                    f"Armature '{armature.name}' could not be read in Edit Mode, "
                    f"so its joints are written as near as its bones give them "
                    f"and may shift a rounding step on the next import.",
                    fix="Make the armature visible and export from Object Mode.")
                continue
            self.rest[armature] = rest

    def _warn_about_animation(self, armature):
        """Say what a pose does to the animations that go with the model.

        An animation's keys are measured against where the bones rest, not
        against the pose the model was written from. Write a model from a pose
        and the two no longer agree: in game the animation would carry the
        model from its own rest, not from the pose that was exported.
        """
        animation = armature.animation_data
        keyed = animation is not None and (animation.action is not None
                                           or len(animation.nla_tracks) > 0)
        if not keyed:
            return
        self.report.warn(
            f"Armature '{armature.name}' is posed and carries animation. The "
            f"model is written from the pose, while an animation's keys are "
            f"measured from where the bones rest, so the two will not line up "
            f"in game.",
            fix="Clear the pose before exporting the model, or apply it as the "
                "rest pose so the bones and the keys agree.")

    def _validate_names(self, doc):
        """Frame names and user props against the format's length byte.

        Run over the finished document so it covers every frame however it was
        built, and during the build rather than the write so Check 4DS Scene
        catches it too.
        """
        for frame in doc.frames:
            shown = frame.name[:40]
            validate_text_length(f"Frame name '{shown}'", frame.name, self.fail)
            validate_text_length(f"User props on frame '{shown}'",
                                 frame.user_props, self.fail)

    def _check_animated_count(self, scene):
        """The count the model carries against the animation the scene holds.

        The game reads this number to know there is a .5ds beside the model,
        and in every animated model it ships the number is exactly the track
        count of that animation. Nothing is said about a scene with no
        animation in it - a count left over from somewhere is the model's
        business, not this export's.
        """
        from ..packages import module
        anim_io = module("5ds.io")

        # Counted over what is being exported rather than the whole scene, so
        # a model written with Selected Objects Only is measured against the
        # animation it would actually go out with - and counted before the
        # export took that animation off, or there would be nothing left to
        # count and a model full of movement would go out saying it has none.
        animated = (self.animated_now if self.animated_now is not None
                    else anim_io.count_animated_objects(scene,
                                                        self.source_objects))
        if not animated or animated == scene.ls3d_animated_object_count:
            return
        self.report.warn(
            f"The model says it has {scene.ls3d_animated_object_count} "
            f"animated object(s), but {animated} of the things being exported "
            f"are animated.",
            fix="Set Animated Objects to "
                f"{animated} under Model, or leave it - it is worked out from "
                f"the scene as you animate.")

    def _validate_armatures(self):
        """Mesh frame and skinned-mesh rules, once per armature."""
        for armature in (o for o in self.ordered if o.type == "ARMATURE"):
            if (self._parent_entry(armature) is not None
                    and not self._sits_on_parent(armature)):
                continue
            # Binding is by parent *or* Armature modifier, matching what the
            # rest of the exporter accepts - requiring a parent here would
            # reject a mesh bound the modifier way, which exports fine.
            skinned = skinned_meshes_of(armature, self.source_objects)
            validate_armature(armature, skinned, self.fail, self.report.warn)
            validate_influence_boxes(armature, skinned, self.fail,
                                     self.report.warn)

    def _sits_on_parent(self, armature):
        """An armature hung on a frame has to stand on it as its mesh frame
        says, or say so.

        Where it stands against that frame is its mesh frame, kept in its
        Delta Transform; the parent compensation Blender adds on top has to
        leave it there, as the import hangs one on the joint a mesh hangs
        from.
        """
        location, rotation, scale = self._local_transform(armature)
        want_location, want_rotation, want_scale = mesh_frame_channels(armature)
        turned = (Quaternion(want_rotation).normalized().rotation_difference(
            rotation.normalized()).angle if rotation.magnitude else 3.2)
        turned = min(turned, 2.0 * 3.141592653589793 - turned)
        if ((location - Vector(want_location)).length <= PARENT_TOLERANCE
                and turned <= PARENT_TOLERANCE
                and (scale - Vector(want_scale)).length <= PARENT_TOLERANCE):
            return True
        self.fail(f"Armature '{armature.name}' hangs from "
                  f"'{armature.parent_bone or armature.parent.name}' but does "
                  f"not stand on it where its mesh frame says: something has "
                  f"moved it off.",
                  fix="Clear the armature's transform (Object > Clear > "
                      "Location, Rotation and Scale), and hang it on the joint "
                      "again with the parent's own transform kept.")
        return False

    def fail(self, message, fix=None):
        self.errors.append(message)
        self.report.error(message, fix=fix)

    def _raise_if_errors(self):
        if self.errors:
            raise ExportError(f"{len(self.errors)} problem(s) blocked the export")

# ── evaluation helpers ────────────────────────────────────────────────────────
#: How far an armature hung on a frame may sit off it and still sit on it:
#: the parent compensation is a 32-bit matrix, so it lands a rounding step off.
PARENT_TOLERANCE = 1.0e-5


class neutralised_animation:
    """Take animation off so the export sees the model at rest.

    4DS stores rest transforms. A loaded 5DS animation keys pose bones and
    objects' own channels, and those hold whatever frame is showing, so every
    animation in the scene is taken off for the length of the export and
    what it keyed goes back to rest - a pose bone to its bone, an object to
    where its Delta Transform stands it. It all goes back on afterwards.

    A skeleton somebody has posed by hand, on bones the animation does not
    key, is left alone: that pose is the model, and its mesh has to come out
    bent the way the pose bends it.

    An object that keeps no place in its Delta Transform has none to go back
    to, and is written standing where the viewport shows it at the moment of
    the export. That is said rather than done quietly: it is the one case
    where what is written depends on where the timeline is sitting. Said only
    of what is being exported, though it is done to the whole scene - a frame
    left out of a selection still has to stand where it stands, since the ones
    being written may hang from it.
    """

    def __init__(self, objects, report=None):
        self.objects = objects
        self.report = report
        self._written = {obj.name for obj in objects or ()}
        self._poses = {}
        self._taken = {}

    def __enter__(self):
        from ..packages import module
        anim_io = module("5ds.io")
        scene = bpy.context.scene
        #: How many tracks the scene would write, read while the animation is
        #: still on - once it is off there is nothing left to count.
        self.animated = anim_io.count_animated_objects(scene, self.objects)
        for obj in scene.objects:
            kept = anim_io.take_off_animation(obj)
            if kept is None:
                continue
            self._taken[obj] = kept
            if (kept.get("stands_where_it_is") and self.report is not None
                    and obj.name in self._written):
                self.report.info(
                    f"'{obj.name}' is animated on its own place, turn or size "
                    f"and keeps none of it in its Delta Transform, so the "
                    f"model takes it where it stands at frame "
                    f"{scene.frame_current}.")
        for obj in scene.objects:
            if obj.type == "ARMATURE" and not is_posed(obj):
                self._poses[obj] = obj.data.pose_position
                obj.data.pose_position = "REST"
        if self._poses or self._taken:
            bpy.context.view_layer.update()
        return self

    def __exit__(self, exc_type, exc, tb):
        from ..packages import module
        anim_io = module("5ds.io")
        for obj, pose in self._poses.items():
            obj.data.pose_position = pose
        for obj, kept in self._taken.items():
            anim_io.put_back_animation(obj, kept)
        if self._poses or self._taken:
            scene = bpy.context.scene
            scene.frame_set(scene.frame_current)
        return False


def check_4ds(objects, report, fix_multi_influences=False,
              fix_non_parent_child=False):
    """Run every export check without writing anything.

    The same build the exporter does, stopped just before the file is written,
    so a problem can be found while you are still working on the model rather
    than at the moment you try to ship it.
    """
    exporter = Exporter(objects, report,
                        fix_multi_influences=fix_multi_influences,
                        fix_non_parent_child=fix_non_parent_child)
    report.span(0, 100)
    with neutralised_animation(objects, report) as suspended:
        exporter.animated_now = suspended.animated
        return exporter.build()


def export_4ds(filepath, objects, report,
               fix_multi_influences=False, fix_non_parent_child=False):
    """Build and write a 4DS file. Nothing is written unless the build succeeds."""
    exporter = Exporter(objects, report,
                        fix_multi_influences=fix_multi_influences,
                        fix_non_parent_child=fix_non_parent_child)
    report.span(0, 85)
    with neutralised_animation(objects, report) as suspended:
        exporter.animated_now = suspended.animated
        document = exporter.build()

    def on_truncate(context, original, kept):
        report.warn(f"{context} is longer than {kept} bytes and was truncated.")

    report.span(85, 100)
    report.stage("Writing")
    size = write_file(document, filepath, on_truncate=on_truncate)
    report.info(f"Wrote {size} bytes")
    return document
