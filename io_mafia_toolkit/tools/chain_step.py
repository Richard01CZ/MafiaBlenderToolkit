"""One link in an import/export chain: import a 4DS, export it unchanged.

Run inside Blender by :mod:`chain_roundtrip`, one process per link, because
repeatedly importing into a single ``--background`` session eventually corrupts
Blender's own memory regardless of how carefully the scene is cleared.

Usage (from the driver, not by hand)::

    blender --background --factory-startup --python chain_step.py -- \
        --input <in.4ds> --output <out.4ds>

With ``--sample`` instead of ``--input`` it builds a scene from the addon's own
Add > 4DS menu and exports that, which is how the chain gets a model holding
frame types the game itself never shipped.
"""

import argparse
import os
import sys

import bpy

_ADDON_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _ADDON_PARENT not in sys.path:
    sys.path.insert(0, _ADDON_PARENT)

import io_mafia_toolkit                                          # noqa: E402
from io_mafia_toolkit.common import constants as C               # noqa: E402


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for collection in (bpy.data.meshes, bpy.data.armatures, bpy.data.materials,
                       bpy.data.images, bpy.data.objects):
        for block in list(collection):
            if block.users == 0:
                collection.remove(block)


#: Name of the skinned mesh.
SKIN_NAME = "limb"


def build_skinned_figure():
    """Two joints and a mesh bound to them, the way a skin is built by hand.

    No bone is named after the mesh, so nothing stands for the mesh frame and
    the root joint hangs from the mesh itself - the simplest skeleton the
    export takes, and the one a person building their own is likely to make.
    """
    bpy.ops.ls3d.add_joint()
    armature = bpy.context.object
    bpy.ops.ls3d.add_joint()
    names = [bone.name for bone in armature.data.bones]
    tip = names[-1]
    bpy.context.view_layer.objects.active = armature

    bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.15, depth=1.0,
                                        location=(0.0, 0.0, 0.5))
    skin = bpy.context.object
    skin.name = SKIN_NAME
    skin.data.materials.append(bpy.data.materials.new("SKIN.BMP"))
    skin.parent = armature
    skin.matrix_parent_inverse = armature.matrix_world.inverted()
    modifier = skin.modifiers.new("Armature", "ARMATURE")
    modifier.object = armature
    group = skin.vertex_groups.new(name=tip)
    group.add(range(len(skin.data.vertices)), 1.0, "REPLACE")
    skin.ls3d_frame_type = str(C.FRAME_VISUAL)
    skin.visual_type = str(C.VISUAL_SINGLEMESH)
    return armature


def build_sample():
    """A scene holding one of everything the addon can author.

    The game ships no occluder at all and no hand-made lens flare, so a chain
    run only over its own models would never exercise them.
    """
    clear_scene()
    bpy.ops.ls3d.add_sector()
    sector = bpy.context.object
    bpy.ops.ls3d.add_portal()
    bpy.context.view_layer.objects.active = sector
    bpy.ops.ls3d.add_portal()
    bpy.ops.ls3d.add_occluder()
    bpy.ops.ls3d.add_dummy()
    bpy.ops.ls3d.add_target()
    bpy.ops.ls3d.add_mirror()
    bpy.ops.ls3d.add_lens_flare()
    flare = bpy.context.object
    flare.ls3d_glows[0].material = bpy.data.materials.new("FLARE.BMP")
    armature = build_skinned_figure()

    # A plain visual with a material and a second detail level, so the chain
    # carries geometry, UVs and a LOD chain as well as the exotic frames.
    bpy.ops.mesh.primitive_uv_sphere_add(location=(4.0, 0.0, 0.0))
    visual = bpy.context.object
    visual.name = "widget"
    material = bpy.data.materials.new("WOOD.BMP")
    visual.data.materials.append(material)
    visual.ls3d_user_props = "SECT"
    bpy.ops.mesh.primitive_cube_add(location=(4.0, 0.0, 0.0))
    coarse = bpy.context.object
    coarse.name = "widget_lod1"
    coarse.data.materials.append(material)
    coarse.ls3d_lod_distance_m = 25.0
    return armature


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--model",
                        help="a .4ds to load first. An animation carries no "
                             "skeleton, so it is imported onto whatever model "
                             "is already in the scene")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = parser.parse_args(argv)

    io_mafia_toolkit.register()
    animation = args.output.lower().endswith(".5ds")

    if args.sample:
        build_sample()
    else:
        clear_scene()
        if animation:
            result = getattr(bpy.ops.import_scene, "4ds")(filepath=args.model)
            if result != {"FINISHED"}:
                print(f"CHAIN_STEP_FAILED model {result}", flush=True)
                sys.exit(2)
        loader = "5ds" if animation else "4ds"
        result = getattr(bpy.ops.import_scene, loader)(filepath=args.input)
        if result != {"FINISHED"}:
            print(f"CHAIN_STEP_FAILED import {result}", flush=True)
            sys.exit(2)

    writer = "5ds" if animation else "4ds"
    result = getattr(bpy.ops.export_scene, writer)(filepath=args.output)
    if result != {"FINISHED"}:
        print(f"CHAIN_STEP_FAILED export {result}", flush=True)
        sys.exit(3)
    print("CHAIN_STEP_OK", flush=True)


if __name__ == "__main__":
    main()
