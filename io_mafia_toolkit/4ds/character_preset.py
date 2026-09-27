"""Tommy's skeleton, for the Add menu's character preset.

Every human the game ships is built on the same eighteen joints, named and
chained the same way, and 221 of its 276 human models have Tommy's
proportions to within a few millimeters. So the player's own model,
``Tommy.4ds``, is where the preset comes from: its joints, the skinned mesh
they hang from, and the five frames around them a character carries.

Everything here is in the file's own terms - X right, Y up, Z forward, and
rotations as (w, x, y, z) - and goes into a document the importer turns into
objects. The preset therefore comes out exactly as importing Tommy would make
it, minus his mesh and materials.
"""

from ..common import constants as C
from .codec import (BoneGroup, Document, Dummy, Frame, Geometry, Joint, LOD,
                    Skin, SkinLOD, Target)

#: The skinned mesh every joint chain starts from. Characters are found by this
#: name, so it is kept as it is.
BASE_NAME = C.CHARACTER_MESH_NAME
#: Where Tommy's mesh sits: its origin at hip height, a touch forward.
BASE_POSITION = (0.00020038089, 1.0814548, 0.035779137)
#: Tommy's mesh carries the same culling bits as his joints.
BASE_CULL_FLAGS = C.DEFAULT_CULL_FLAGS_JOINT

_IDENTITY = (1.0, 0.0, 0.0, 0.0)


def _joint(name, parent, position, scale, matrix, user_props=""):
    """One joint, unturned at rest, as every joint of Tommy's is.

    *scale* is one number for all three axes. Tommy's ``back1`` and thighs
    carry 1.0506, which stretches everything below them by 5%; the lengths
    themselves are stored at 100%. *matrix* is sixteen numbers of the joint's
    own: its influence box, which Tommy's weights were made from - so a
    character built on the preset with nothing painted is weighted as his.
    """
    return ("joint", name, parent, position, _IDENTITY, (scale,) * 3,
            matrix, user_props)


def _dummy(name, parent, position, rotation, scale, half_size):
    """A dummy with a cube box *half_size* out from its center on every side."""
    return ("dummy", name, parent, position, rotation, scale, half_size, "")


def _target(name, position, rotation, links, user_props):
    """A target, turning the frames named in *links* toward itself."""
    return ("target", name, None, position, rotation, (1.0, 1.0, 1.0), links,
            user_props)


#: The number each joint carries in Tommy's file. The skin's groups follow
#: these, and so does the order of a character's vertex groups.
JOINT_NUMBERS = {
    "back1": 0, "back2": 1, "back3": 2, "l_shoulder": 3, "l_arm": 4,
    "l_elbow": 5, "l_hand": 6, "r_shoulder": 7, "r_arm": 8, "r_elbow": 9,
    "r_hand": 10, "neck": 11, "l_thigh": 12, "l_shin": 13, "l_foot": 14,
    "r_thigh": 15, "r_shin": 16, "r_foot": 17,
}

#: Tommy's frames after his mesh, in the order his model stores them.
#:
#: ``gun1`` and ``gun2`` are where a weapon held in the right and left hand
#: attaches; ``notify`` receives the animations' events, such as footsteps;
#: ``blnd`` is moved by the animations, which use it to join one onto the next;
#: and ``targetN`` is what the head turns toward.
FRAMES = (
    _joint("r_thigh", "base",
           (0.09376188, -0.06657529, -0.003906481), 1.0505999,
           (-0.10299366, 3.8173225e-09, 9.524695e-09, 0.0,
            -1.753555e-08, -0.03419404, -0.17591332, 0.0,
            -1.0115309e-09, -0.052993868, 0.010300952, 0.0,
            0.0140439365, -0.11643514, -0.0061094845, 1.0)),
    _joint("r_shin", "r_thigh",
           (0.010500003, -0.441, -0.018740682), 1.0,
           (-0.09349454, 5.8956466e-09, 1.4069207e-08, 0.0,
            -1.68725e-08, 2.004922e-08, -0.112123355, 0.0,
            -3.7159955e-09, -0.058929097, -7.024896e-09, 0.0,
            -0.0028427495, 0.0030401477, -0.016510753, 1.0)),
    _joint("r_foot", "r_shin",
           (-0.010499993, -0.42524996, -0.047960717), 1.0,
           (-0.08526699, 3.7771595e-09, 1.370569e-08, 0.0,
            -1.8555804e-08, 0.019118581, -0.12070982, 0.0,
            -2.3130227e-09, -0.03315851, -0.0052517992, 0.0,
            -0.00038471224, 0.0014558745, 0.00022104924, 1.0)),
    _joint("l_thigh", "base",
           (-0.09377018, -0.06657529, -0.003906492), 1.0505999,
           (-0.10299366, 3.8173225e-09, 9.524695e-09, 0.0,
            -1.753555e-08, -0.03419404, -0.17591332, 0.0,
            -1.0115309e-09, -0.052993868, 0.010300952, 0.0,
            -0.014063936, -0.11643514, -0.0061094486, 1.0)),
    _joint("l_shin", "l_thigh",
           (-0.010500001, -0.441, -0.018740684), 1.0,
           (-0.09349454, 5.8956466e-09, 1.4069207e-08, 0.0,
            -1.68725e-08, 2.004922e-08, -0.112123355, 0.0,
            -3.7159955e-09, -0.058929097, -7.024896e-09, 0.0,
            0.0032586232, 0.0030401477, -0.016510723, 1.0)),
    _joint("l_foot", "l_shin",
           (0.01050001, -0.42524996, -0.047960717), 1.0,
           (-0.08601495, 3.8129135e-09, 1.3823676e-08, 0.0,
            -1.8552079e-08, 0.019118581, -0.12070982, 0.0,
            -2.3138966e-09, -0.03315851, -0.0052517992, 0.0,
            0.00019127726, 0.0014558745, 0.00022107833, 1.0)),
    _joint("back1", "base",
           (-4.150782e-06, 0.03727126, -0.0065819994), 1.0505999,
           (-0.21036269, -1.1494327e-08, 3.2712986e-08, 0.0,
            3.5476365e-08, 0.016354457, 0.23387915, 0.0,
            -5.903117e-09, 0.09010368, -0.006300698, 0.0,
            0.0012422041, 0.011844747, -0.012787264, 1.0)),
    _joint("back2", "back1",
           (9.343796e-10, 0.20187804, -0.0071253697), 1.0,
           (-0.21036273, -1.786188e-08, 2.8268886e-08, 0.0,
            4.637981e-08, -0.059749022, 0.3073823, 0.0,
            -3.4045782e-09, 0.057912026, 0.011256944, 0.0,
            0.0012422028, -0.06336598, 0.018788759, 1.0)),
    _joint("back3", "back2",
           (2.35622e-09, 0.22434112, -0.017968008), 1.0,
           (-0.159228, -1.2670981e-08, 2.3296193e-08, 0.0,
            3.8000003e-08, -0.06808495, 0.22269571, 0.0,
            -4.145446e-09, 0.11896136, 0.03637014, 0.0,
            -0.00013274058, -0.035219207, 0.03765835, 1.0)),
    _joint("neck", "back3",
           (-2.5054965e-09, 0.10806094, 0.01910636), 1.0,
           (-0.09694137, -1.0494454e-08, 1.0798595e-08, 0.0,
            1.6074292e-08, -0.047028888, 0.09859815, 0.0,
            -4.9312343e-10, 0.008945719, 0.004266888, 0.0,
            0.0012422025, 0.019767202, 0.0071107596, 1.0),
           "trgt=targetN"),
    _joint("r_shoulder", "back3",
           (0.06824999, -0.029600982, 0.013534139), 1.0,
           (-0.027025778, -1.7042121e-09, 4.06688e-09, 0.0,
            4.0668815e-09, -3.2217238e-09, 0.027025782, 0.0,
            -1.7042124e-09, 0.027025782, -3.2217238e-09, 0.0,
            0.0009792313, 0.00033724983, -0.0019527391, 1.0)),
    _joint("r_arm", "r_shoulder",
           (0.0945, 0.04725006, -0.0017055237), 1.0,
           (-0.010020543, 0.11453554, -1.3705856e-08, 0.0,
            -1.2555325e-08, 1.2555325e-08, 0.1053217, 0.0,
            0.017687764, 0.0015474765, 2.1166e-09, 0.0,
            0.020150853, -0.031905204, -0.01896319, 1.0)),
    _joint("r_elbow", "r_arm",
           (0.2759422, -0.022963228, -0.02404453), 1.0,
           (-1.0383223e-08, 0.08710079, 0.0, 0.0,
            7.473409e-09, 0.0, 0.08358865, 0.0,
            0.029994903, 2.6817533e-09, -3.575671e-09, 0.0,
            0.001880979, -0.007426766, 0.005081319, 1.0)),
    _joint("r_hand", "r_elbow",
           (0.27824995, -0.010500031, -0.009518372), 1.0,
           (-8.373567e-09, 0.07024257, 0.0, 0.0,
            6.2801755e-09, 0.0, 0.07024257, 0.0,
            0.010050581, 8.985919e-10, -1.1981226e-09, 0.0,
            0.007148759, -0.01709958, 0.008293587, 1.0)),
    _dummy("gun1", "r_hand", (0.06389665, -0.016927667, 0.011408526),
           (-0.50000006, 0.49999997, 0.5, 0.49999997),
           (0.95183706, 0.9518371, 0.9518371), 0.03527055),
    _joint("l_shoulder", "back3",
           (-0.06825, -0.029600982, 0.013534126), 1.0,
           (-0.027025778, -1.7042121e-09, 4.06688e-09, 0.0,
            4.0668815e-09, -3.2217238e-09, 0.027025782, 0.0,
            -1.7042124e-09, 0.027025782, -3.2217238e-09, 0.0,
            0.0015082103, 0.00033724983, -0.0019527155, 1.0)),
    _joint("l_arm", "l_shoulder",
           (-0.0945, 0.04725006, -0.0017055392), 1.0,
           (-0.0110196555, -0.11444371, 3.426463e-09, 0.0,
            1.5694157e-08, 0.0, 0.1053217, 0.0,
            -0.018515185, 0.0017828055, 2.2173903e-09, 0.0,
            -0.020617856, -0.030390052, -0.018963128, 1.0)),
    _joint("l_elbow", "l_arm",
           (-0.27594483, -0.022963228, -0.0240445), 1.0,
           (-1.0383223e-08, -0.08710078, -2.5958058e-09, 0.0,
            4.9822715e-09, 0.0, 0.083588645, 0.0,
            -0.030052284, 4.478139e-09, 3.5825114e-09, 0.0,
            -0.0023091387, -0.007426766, 0.0050813956, 1.0)),
    _joint("l_hand", "l_elbow",
           (-0.27824992, -0.010500031, -0.00951837), 1.0,
           (-8.373567e-09, -0.07024257, -2.0933917e-09, 0.0,
            4.1867834e-09, 0.0, 0.07024257, 0.0,
            -0.009912854, 1.4771303e-09, 1.1817043e-09, 0.0,
            -0.0069028605, -0.01744872, 0.008293709, 1.0)),
    _dummy("gun2", "l_hand", (-0.06490612, -0.016927635, 0.0114086615),
           (0.50000006, -0.49999997, 0.49999997, 0.5),
           (0.95183694, 0.9518371, 0.9518371), 0.03527055),
    _dummy("notify", None, (9.580702e-05, 0.24623276, -1.0771552e-08),
           (7.549791e-08, 3.0908623e-08, 0.7071068, 0.7071068),
           (1.0, 1.0, 1.0), 0.23635486),
    _target("targetN", (4.370528e-08, 1.670495, 0.9998602),
            (-0.5000001, -0.5, 0.5, 0.5), ("neck",), "TRGT"),
    _dummy("blnd", None, (-0.012829958, 0.20309997, -7.75616e-09),
           (7.549791e-08, 3.0908623e-08, 0.7071068, 0.7071068),
           (0.02594759, 0.02594759, 0.02594759), 6.8279223),
)


#: The joints a character's mesh origin is found from.
THIGHS = ("l_thigh", "r_thigh")


def _origin_from_thighs():
    thighs = [entry[3] for entry in FRAMES
              if entry[0] == "joint" and entry[1] in THIGHS]
    return tuple(-(a + b) * 0.5 for a, b in zip(*thighs))


#: Where Tommy's mesh origin sits from the point midway between his thighs:
#: 6.66 cm above it and 3.9 mm forward. His thighs hang straight from it,
#: unturned, so this is their place from it, reversed.
ORIGIN_FROM_THIGHS = _origin_from_thighs()


def character_skeleton():
    """A document holding Tommy's skeleton around an empty skinned mesh.

    The mesh has one level of detail with nothing in it, so the importer binds
    it to the armature the way it binds Tommy's, and there is somewhere ready
    for a character's own mesh to go.
    """
    frames = [Frame(
        frame_type=C.FRAME_VISUAL,
        visual_type=C.VISUAL_SINGLEMESH,
        render_flags=C.DEFAULT_RENDER_FLAGS,
        render_flags2=C.DEFAULT_RENDER_FLAGS2,
        position=BASE_POSITION,
        rotation=_IDENTITY,
        cull_flags=BASE_CULL_FLAGS,
        name=BASE_NAME,
        geometry=Geometry(lods=[LOD()]),
        # One group per joint, in Tommy's numbering, with nothing in them yet:
        # the mesh's vertex groups come in that order, and the export numbers
        # the joints by it.
        skin=Skin(lods=[SkinLOD(groups=[BoneGroup() for _ in JOINT_NUMBERS])]),
    )]
    frame_ids = {BASE_NAME: 1}

    for kind, name, parent, position, rotation, scale, payload, props in FRAMES:
        frame = Frame(
            parent_id=frame_ids[parent] if parent else 0,
            position=position,
            rotation=rotation,
            scale=scale,
            name=name,
            user_props=props,
        )
        if kind == "joint":
            frame.frame_type = C.FRAME_JOINT
            frame.cull_flags = C.DEFAULT_CULL_FLAGS_JOINT
            frame.joint = Joint(matrix=payload, joint_id=JOINT_NUMBERS[name])
        elif kind == "dummy":
            frame.frame_type = C.FRAME_DUMMY
            frame.cull_flags = C.DEFAULT_CULL_FLAGS
            frame.dummy = Dummy(bbox_min=(-payload,) * 3,
                                bbox_max=(payload,) * 3)
        else:
            frame.frame_type = C.FRAME_TARGET
            frame.cull_flags = C.DEFAULT_CULL_FLAGS
            frame.target = Target(flags=C.TF_LOOK_AT,
                                  links=[frame_ids[link] for link in payload])
        frames.append(frame)
        frame_ids[name] = len(frames)

    return Document(frames=frames)
