"""Plain-data model of a 4DS document.

These types deliberately contain no Blender objects. The format layer converts
between bytes and these structures; ``importer``/``exporter`` convert between
these structures and Blender data. Keeping the split lets the parser be
exercised against real game files without Blender in the loop.

Coordinates are stored exactly as they appear on disk (Mafia axis order:
X right, Y up, Z forward). Axis conversion to Blender's Z-up convention happens
in the Blender adapter layer, never here.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]
Vec4 = Tuple[float, float, float, float]
Mat4 = Tuple[float, ...]  # 16 floats, row-major as stored


# ── Materials ─────────────────────────────────────────────────────────────────
@dataclass
class Material:
    flags: int = 0
    ambient: Vec3 = (0.5, 0.5, 0.5)
    diffuse: Vec3 = (1.0, 1.0, 1.0)
    emission: Vec3 = (0.0, 0.0, 0.0)
    opacity: float = 1.0
    env_amount: float = 0.0
    env_texture: str = ""
    diffuse_texture: str = ""
    alpha_texture: str = ""
    #: How many frames each animated texture has, as a single byte each. A
    #: count below 2 leaves the texture on the name stored above.
    #:
    #: The diffuse count is corroborated by the files on disk - 2SEA00.BMP says
    #: 30 and there are 30 of them. The environment one never exceeds 1 in any
    #: shipping file, so nothing observable pins it down; it is read here
    #: because that is where the game's own field sits.
    anim_frames: int = 0
    env_anim_frames: int = 0
    #: The frame each animation wraps back to at the end, and how long each
    #: frame is held in milliseconds. A wrap-to frame of -1 asks for a frame
    #: picked at random each interval instead of the next one in order; a
    #: period of 0 leaves the texture on its first frame.
    #:
    #: One shipping material wraps to anywhere but the start (explosion.4ds, to
    #: frame 10) and none asks for random. The environment pair is zero in
    #: every shipping file, which is exactly the state the game reads as "not
    #: animated" - so the environment channel never runs in the game at all,
    #: and these two are carried for the sake of a file that does set them.
    anim_loop_start: int = 0
    anim_period: int = 0
    env_anim_loop_start: int = 0
    env_anim_period: int = 0


# ── Geometry ──────────────────────────────────────────────────────────────────
@dataclass
class Vertex:
    position: Vec3 = (0.0, 0.0, 0.0)
    normal: Vec3 = (0.0, 1.0, 0.0)
    uv: Vec2 = (0.0, 0.0)


@dataclass
class FaceGroup:
    """A run of triangles sharing one material."""
    material_id: int = 0            # 1-based into the file's material table; 0 = none
    faces: List[Tuple[int, int, int]] = field(default_factory=list)


@dataclass
class LOD:
    distance: float = 0.0
    vertices: List[Vertex] = field(default_factory=list)
    face_groups: List[FaceGroup] = field(default_factory=list)
    #: A LOD may say it has no vertices of its own and reuse LOD 0's, carrying
    #: nothing but a replacement UV per vertex. The count field marks it with a
    #: value that cannot be a count, and what follows is two floats a vertex
    #: instead of eight. When this is set ``vertices`` is empty and the real
    #: positions and normals are LOD 0's.
    #:
    #: Nothing the game ships uses it, so there is no shipping file to check a
    #: reading against - but a file that did use it would derail the reader
    #: completely, since the sentinel would otherwise be read as a vertex count.
    shared_uv: Optional[List[Vec2]] = None


@dataclass
class Geometry:
    """The standard visual payload: an instance reference or a list of LODs."""
    instance_id: int = 0            # non-zero => this frame reuses frame N's mesh
    lods: List[LOD] = field(default_factory=list)


# ── Skinning ──────────────────────────────────────────────────────────────────
@dataclass
class BoneGroup:
    inverse_bind: Mat4 = tuple([0.0] * 16)
    unweighted_count: int = 0       # vertices fully owned by this bone
    weighted_count: int = 0         # vertices blended with the parent bone
    parent_group: int = 0           # 1-based bone group index; 0 = the mesh root
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)
    weights: List[float] = field(default_factory=list)


@dataclass
class SkinLOD:
    root_unweighted: int = 0        # trailing vertices bound to the mesh root
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)
    groups: List[BoneGroup] = field(default_factory=list)


@dataclass
class Skin:
    lods: List[SkinLOD] = field(default_factory=list)


# ── Morphs ────────────────────────────────────────────────────────────────────
@dataclass
class MorphRegion:
    """One region (the addon's UI calls these "groups") within one LOD.

    ``positions[target][i]`` and ``normals[target][i]`` describe vertex
    ``indices[i]`` in morph target ``target``.

    Normals really are per-target, not shared: 389 vertices in SamHIGH.4ds and
    307 in vodapristavm.4ds carry a different normal for each target. Collapsing
    them to one normal per vertex loses the shading that the engine applies as a
    morph blends, so both lists are indexed the same way.
    """
    indices: List[int] = field(default_factory=list)
    positions: List[List[Vec3]] = field(default_factory=list)
    normals: List[List[Vec3]] = field(default_factory=list)


@dataclass
class MorphLOD:
    regions: List[MorphRegion] = field(default_factory=list)


@dataclass
class Morph:
    target_count: int = 0
    lods: List[MorphLOD] = field(default_factory=list)
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)
    center: Vec3 = (0.0, 0.0, 0.0)
    radius: float = 0.0

    @property
    def region_count(self):
        return len(self.lods[0].regions) if self.lods else 0


# ── Sectors and portals ───────────────────────────────────────────────────────
@dataclass
class Portal:
    flags: int = 0
    near_range: float = 0.0
    far_range: float = 0.0
    plane_normal: Vec3 = (0.0, 0.0, 0.0)
    plane_offset: float = 0.0
    vertices: List[Vec3] = field(default_factory=list)


@dataclass
class Sector:
    flags1: int = 0
    flags2: int = 0
    vertices: List[Vec3] = field(default_factory=list)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)
    portals: List[Portal] = field(default_factory=list)


# ── Other visual payloads ─────────────────────────────────────────────────────
@dataclass
class Mirror:
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)
    center: Vec3 = (0.0, 0.0, 0.0)
    radius: float = 0.0
    view_matrix: Mat4 = tuple([0.0] * 16)
    tint: Vec3 = (1.0, 1.0, 1.0)
    #: The engine's own default, and what all nine shipping mirrors store.
    draw_distance: float = 100.0
    vertices: List[Vec3] = field(default_factory=list)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)


@dataclass
class Glow:
    position: float = 0.0
    material_id: int = 0            # 0-based into the material table, as stored


@dataclass
class LensFlare:
    glows: List[Glow] = field(default_factory=list)


@dataclass
class Billboard:
    axis: int = 1                   # 0=X 1=Y(up) 2=Z in Mafia space
    axis_locked: bool = False


@dataclass
class Light:
    """Forty bytes: what a light is, how strong, how far and how wide.

    Where it sits and which way it faces are not in here - they are the frame's
    own transform, and the engine reads the direction straight off it. The cone
    angles are the *full* angles in radians; the engine halves them itself.
    """
    #: Which of the nine kinds. Only point, spot and directional shade
    #: anything; three more set the enclosing sector's atmosphere and the rest
    #: do no per-frame work at all.
    light_type: int = 3
    #: A bit word. Five bits are read - real-time lighting, the vertex bake,
    #: the shadow scan, the bake's occlusion ray, and the scene's lightness -
    #: and the rest are never tested, so they are carried as they came.
    mode: int = 0xFF6B
    #: A plain multiplier on the color, not a physical quantity.
    power: float = 1.0
    color: Vec3 = (1.0, 1.0, 1.0)
    #: Full strength within the near radius, fading to nothing at the far one.
    range_near: float = 1.0
    range_far: float = 100.0
    cone_inner: float = 0.34906587
    cone_outer: float = 0.69813174


@dataclass
class Projector:
    """A texture painted onto whatever geometry the projector's volume covers.

    There is no mesh here and none is stored: a projector is a shape in space
    plus a material, and the triangles it paints are worked out at run time from
    the surfaces it happens to overlap. The volume is the frame's own transform
    applied to a fixed unit shape - a 2x2 box reaching one unit forward when
    ``orthogonal`` is set, and the pyramid inscribed in that box when it is not.
    """
    #: Straight-on projection, the same width all the way through, against one
    #: that spreads out from a point at the frame's origin.
    orthogonal: bool = True
    #: How the paint fades across the volume, and whether the projector's color
    #: tints the texture or rides in its transparency. One byte; see the
    #: projector constants for how it splits.
    mode: int = 0
    #: 1-based into the file's material table. There is no "no material" value:
    #: the game subtracts one and reads whatever that lands on, so a 0 here
    #: reads off the front of the table.
    material_id: int = 0


@dataclass
class Occluder:
    vertices: List[Vec3] = field(default_factory=list)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)


@dataclass
class Dummy:
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)


@dataclass
class Target:
    flags: int = 0
    links: List[int] = field(default_factory=list)   # 1-based frame ids


@dataclass
class Joint:
    #: 16 floats stored in the FRAME_JOINT payload. Despite looking matrix-like
    #: this is *not* the skinning bind - that lives per bone group in the skin
    #: block. The game loads it and copies it around but never reads it for
    #: rendering or skinning. Its content is opaque authoring data
    #: (non-orthogonal, magnitudes around 0.07-0.21), so it is preserved
    #: verbatim rather than recomputed.
    matrix: Mat4 = tuple([0.0] * 16)
    joint_id: int = 0               # 0-based bone index, as stored


# ── Frames ────────────────────────────────────────────────────────────────────
@dataclass
class Frame:
    frame_type: int = 0
    visual_type: Optional[int] = None
    render_flags: int = 0
    render_flags2: int = 0
    parent_id: int = 0              # 1-based frame id; 0 = no parent
    position: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)
    rotation: Vec4 = (1.0, 0.0, 0.0, 0.0)   # (w, x, y, z) in Mafia axis order
    cull_flags: int = 0
    name: str = ""
    user_props: str = ""

    # Exactly one of the following is set, according to frame/visual type.
    geometry: Optional[Geometry] = None
    skin: Optional[Skin] = None
    morph: Optional[Morph] = None
    sector: Optional[Sector] = None
    mirror: Optional[Mirror] = None
    lens_flare: Optional[LensFlare] = None
    billboard: Optional[Billboard] = None
    projector: Optional[Projector] = None
    light: Optional[Light] = None
    occluder: Optional[Occluder] = None
    dummy: Optional[Dummy] = None
    target: Optional[Target] = None
    joint: Optional[Joint] = None


@dataclass
class Document:
    """A whole .4ds file."""
    version: int = 29
    timestamp: int = 0
    materials: List[Material] = field(default_factory=list)
    frames: List[Frame] = field(default_factory=list)
    animated_object_count: int = 0
    #: Bytes found after the trailing counter. Eight shipping files carry one
    #: extra zero byte; keeping it makes a round-trip byte-identical.
    trailing: bytes = b""
