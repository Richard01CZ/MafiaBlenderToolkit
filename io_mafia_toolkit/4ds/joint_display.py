"""How joints are drawn: the shared custom shape and its scale driver.

A joint's scale is a 4DS property with no Blender equivalent - the real bone
scale stays locked at 1 so editing it never deforms the mesh - so the only way
to see it is to draw it. Both the importer and the Add menu go through here, so
a joint made by hand looks like one that came out of a file.

How big the markers are drawn is the scene's own business, not the model's: a
size that reads well on a character buries a car's door hinge and disappears on
a building. So the driver multiplies the joint's own scale by a figure from the
scene, which the 4DS Model panel offers as a slider. It changes nothing that is
written - a marker is drawn, never exported.
"""

import bpy

from ..common import constants as C

#: Name of the empty every joint borrows as its custom shape. One object,
#: shared by every joint in the file.
JOINT_SHAPE_NAME = "LS3D_JointShape"

#: Display size per unit of joint scale, chosen so a scale of 1 draws a marker
#: small enough to sit inside a character's limb.
JOINT_DISPLAY_SCALE = 0.03


def shared_shape():
    """The empty every joint uses as its custom shape, made once and reused.

    Deliberately not linked into any collection: a custom shape keeps the
    object alive on its own, and leaving it in the scene would make it an
    exportable frame - a sphere-display empty, which is what a lens flare looks
    like, so it would be written out as a stray flare.
    """
    shape = bpy.data.objects.get(JOINT_SHAPE_NAME)
    if shape is None:
        shape = bpy.data.objects.new(JOINT_SHAPE_NAME, None)
        shape.empty_display_type = "SPHERE"
        shape.empty_display_size = 1.0
    for collection in list(shape.users_collection):
        collection.objects.unlink(shape)
    return shape


def _add_driver(armature, path, index):
    try:
        return armature.driver_add(path, index).driver
    except (RuntimeError, TypeError):
        return None


def _driver_property(driver, name, id_type, target_id, data_path):
    """Attach (or reuse) a single-property driver variable."""
    variable = driver.variables.get(name)
    if variable is None:
        variable = driver.variables.new()
        variable.name = name
    variable.type = "SINGLE_PROP"
    target = variable.targets[0]
    target.id_type = id_type
    target.id = target_id
    target.data_path = data_path
    driver.type = "SCRIPTED"


def _scene_of(armature):
    """The scene a driver reads the marker size from.

    The one the armature is in, so a rig moved between scenes follows the one
    it is shown in; the current scene otherwise, which is what a rig in no
    scene at all will be put into.
    """
    for scene in bpy.data.scenes:
        if armature.name in scene.objects:
            return scene
    return getattr(bpy.context, "scene", None)


def drive_display(armature, bone_name):
    """Drive the joint's marker from its own scale and the scene's setting."""
    path = f'pose.bones["{bone_name}"].custom_shape_scale_xyz'
    scene = _scene_of(armature)
    for axis in range(3):
        driver = _add_driver(armature, path, axis)
        if driver is None:
            return
        driver.expression = (f"joint_scale[{axis}] * {JOINT_DISPLAY_SCALE}"
                             f" * marker_size")
        _driver_property(driver, "joint_scale", "OBJECT", armature,
                         f'pose.bones["{bone_name}"]["ls3d_joint_scale"]')
        _driver_property(driver, "marker_size", "SCENE", scene,
                         C.JOINT_DISPLAY_SCALE_PROP)


def apply(armature, bone_name, shape=None):
    """Give one joint the shared shape, its scale seed and the driver.

    Safe to call on a joint that already has them - the shape is looked up
    rather than made, and the driver is rebuilt in place.
    """
    pose_bone = armature.pose.bones.get(bone_name)
    if pose_bone is None:
        return False
    if "ls3d_joint_scale" not in pose_bone:
        pose_bone["ls3d_joint_scale"] = (1.0, 1.0, 1.0)
    pose_bone.lock_scale = (True, True, True)
    pose_bone.custom_shape = shape if shape is not None else shared_shape()
    pose_bone.use_custom_shape_bone_size = False
    drive_display(armature, bone_name)
    return True
