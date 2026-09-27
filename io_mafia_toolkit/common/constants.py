"""LS3D 4DS format constants.

Values here are fixed by the Mafia (LS3D) engine's on-disk format. They were
verified against 3263 shipping .4ds files; see ``format/__init__.py`` for the
byte-level layout notes that go with them.
"""

# ── File versions ─────────────────────────────────────────────────────────────
VERSION_MAFIA     = 29
VERSION_HD2       = 41
VERSION_CHAMELEON = 42

SUPPORTED_VERSIONS = (VERSION_MAFIA,)

#: What the versions this addon cannot read belong to, so a file that is simply
#: from a different game says so instead of being called unsupported and left at
#: that.
VERSION_NAMES = {
    VERSION_MAFIA: "Mafia",
    VERSION_HD2: "Hidden & Dangerous 2",
    VERSION_CHAMELEON: "Chameleon",
}

# ── Frame types ───────────────────────────────────────────────────────────────
# Only VISUAL, SECTOR, DUMMY, TARGET and JOINT occur in shipping Mafia models.
# The rest are declared so unknown frames can be named in error messages.
FRAME_VISUAL    = 1
FRAME_LIGHT     = 2
FRAME_CAMERA    = 3
FRAME_SOUND     = 4
FRAME_SECTOR    = 5
FRAME_DUMMY     = 6
FRAME_TARGET    = 7
FRAME_USER      = 8
FRAME_MODEL     = 9
FRAME_JOINT     = 10
FRAME_VOLUME    = 11
FRAME_OCCLUDER  = 12
FRAME_SCENE     = 13
FRAME_AREA      = 14
FRAME_SHADOW    = 15
FRAME_LANDSCAPE = 16
FRAME_EMITOR    = 17

FRAME_TYPE_NAMES = {
    FRAME_VISUAL:    "Visual",
    FRAME_LIGHT:     "Light",
    FRAME_CAMERA:    "Camera",
    FRAME_SOUND:     "Sound",
    FRAME_SECTOR:    "Sector",
    FRAME_DUMMY:     "Dummy",
    FRAME_TARGET:    "Target",
    FRAME_USER:      "User",
    FRAME_MODEL:     "Model",
    FRAME_JOINT:     "Joint",
    FRAME_VOLUME:    "Volume",
    FRAME_OCCLUDER:  "Occluder",
    FRAME_SCENE:     "Scene",
    FRAME_AREA:      "Area",
    FRAME_SHADOW:    "Shadow",
    FRAME_LANDSCAPE: "Landscape",
    FRAME_EMITOR:    "Emitor",
}

#: One past the highest frame type the game will build. The loader refuses
#: anything at or above this before the frame is made - and type 0, which it
#: has no frame for - so a file naming one does not load at all.
FRAME_TYPE_COUNT = 18

#: Frame types that store nothing beyond the header every frame has.
#:
#: Their engine classes do not override the load at all - they take the base
#: one, which reads the transform, the culling byte, the name and the user
#: properties and stops. So a frame of one of these types is a named point in
#: the hierarchy and no more, and reading it is a matter of not looking for a
#: payload that was never written.
PAYLOADLESS_FRAME_TYPES = frozenset({
    FRAME_CAMERA, FRAME_USER, FRAME_MODEL, FRAME_VOLUME,
    FRAME_SCENE, FRAME_SHADOW, FRAME_LANDSCAPE,
})

#: Frame types the game reads as nothing but their type byte and parent id.
#:
#: Sound and Area are built by the game's own code while it runs, never from a
#: model, and loading one from a file consumes no bytes at all - not the
#: transform, the name or the user properties every other frame has. So a file
#: holds three bytes for one, there is no layout to read and nothing to import,
#: and the frame the game makes of it sits at its parent with no offset. The
#: import skips them and says so; nothing ever writes one.
SKIPPED_FRAME_TYPES = frozenset({FRAME_SOUND, FRAME_AREA})

# ── Visual sub-types ──────────────────────────────────────────────────────────
VISUAL_OBJECT      = 0
VISUAL_LITOBJECT   = 1
VISUAL_SINGLEMESH  = 2
VISUAL_SINGLEMORPH = 3
VISUAL_BILLBOARD   = 4
VISUAL_MORPH       = 5
VISUAL_LENSFLARE   = 6
VISUAL_PROJECTOR   = 7
VISUAL_MIRROR      = 8

#: Visual sub-types are looked up in a nine-entry table and the loader refuses
#: anything at or above this outright, so 0-8 is the whole of what a file can
#: name. The engine has three further visual classes - part bases, part elements
#: and land patches - but none of them has a serialized index, so none can
#: appear in a model.
VISUAL_TYPE_COUNT = 9

VISUAL_TYPE_NAMES = {
    VISUAL_OBJECT:      "Object",
    VISUAL_LITOBJECT:   "Lit Object",
    VISUAL_SINGLEMESH:  "Single Mesh",
    VISUAL_SINGLEMORPH: "Single Morph",
    VISUAL_BILLBOARD:   "Billboard",
    VISUAL_MORPH:       "Morph",
    VISUAL_LENSFLARE:   "Lens Flare",
    VISUAL_PROJECTOR:   "Projector",
    VISUAL_MIRROR:      "Mirror",
}

# ── Light ─────────────────────────────────────────────────────────────────────
# A light frame stores forty bytes: a mode word, a type, a power multiplier, a
# color, an attenuation band and a pair of cone angles. Its position and the way
# it faces are the frame's own transform - there is no direction field.
LIGHT_NONE         = 0
LIGHT_POINT        = 1
LIGHT_SPOT         = 2
LIGHT_DIRECTIONAL  = 3
LIGHT_AMBIENT      = 4
LIGHT_FOG          = 5
LIGHT_POINTAMBIENT = 6
LIGHT_POINTFOG     = 7
LIGHT_LAYEREDFOG   = 8
#: One past the last kind, so a file naming anything else is rejected
#: rather than taken for a kind the engine has no case for.
LIGHT_TYPE_COUNT   = 9

#: Only three of the nine shade anything. Fog, point ambient and point fog
#: become the enclosing sector's main light - atmosphere rather than lighting -
#: and null, ambient and layered fog do no per-frame work at all.
LIGHT_TYPE_ITEMS = (
    (str(LIGHT_NONE), "None", "Does nothing. The type a light carries before it is given one"),
    (str(LIGHT_POINT), "Point", "Shines from its position, fading across the range below"),
    (str(LIGHT_SPOT), "Spot", "Shines from its position along the frame's own direction, "
                  "inside the cone below and fading across the range"),
    (str(LIGHT_DIRECTIONAL), "Directional", "Shines along the frame's own direction from no "
                         "particular place. Position and range are unused"),
    (str(LIGHT_AMBIENT), "Ambient", "Adds its color, times its power, evenly to "
                     "everything in the sectors it lights"),
    (str(LIGHT_FOG), "Fog", "Linear fog for the sectors it lights, in its color. "
                 "Far is where the fog is thickest, as a percentage of the view "
                 "distance; Near is where it starts, as a percentage of that"),
    (str(LIGHT_POINTAMBIENT), "Point Ambient", "Exponential fog for the sectors it "
                           "lights, in its color, with Near as the density. "
                           "Despite the name it lights nothing"),
    (str(LIGHT_POINTFOG), "Point Fog", "Exponential-squared fog for the sectors it "
                       "lights, in its color, with Near as the density"),
    (str(LIGHT_LAYEREDFOG), "Layered Fog", "Draws no fog and lights nothing. The "
                         "game only counts it, as light from around it, toward "
                         "how dark a shadow is"),
)

#: The types that light geometry, as against the ones that set a sector's
#: atmosphere or do nothing. What the viewport draws depends on which it is.
LIGHT_SHADING_TYPES = frozenset({LIGHT_POINT, LIGHT_SPOT, LIGHT_DIRECTIONAL})
#: The three that become the sector's main light instead of shading - each of
#: them a fog, drawn in the light's color. Power and the cones are not used.
LIGHT_ATMOSPHERE_TYPES = frozenset({LIGHT_FOG, LIGHT_POINTAMBIENT,
                                    LIGHT_POINTFOG})
#: What Fog multiplies its two ranges by: they are percentages. Far becomes a
#: share of the camera's view distance, and Near a share of that.
LIGHT_ATMOSPHERE_RANGE_SCALE = 0.01
#: The two fogs that read Near as a density, exactly as stored, and Far not at
#: all.
LIGHT_DENSITY_FOG_TYPES = frozenset({LIGHT_POINTAMBIENT, LIGHT_POINTFOG})
#: The kinds whose Near and Far are distances from the light.
LIGHT_RANGED_TYPES = frozenset({LIGHT_POINT, LIGHT_SPOT})

#: The six bits of the mode word anything reads. Each part of the game that
#: uses a light asks for its own bit, so a light can light moving objects
#: without lighting baked ones, or count toward shadows without lighting
#: anything. Bit 2 and bits 7 to 31 are never tested - the engine's default
#: sets bits 8 to 15 as well - so they are carried exactly as they came.
LM_REALTIME      = 1 << 0   # lights ordinary objects while the game runs
LM_VERTEX_BAKE   = 1 << 1   # takes part in a lit object's vertex bake
LM_SHADOW        = 1 << 3   # counted by the shadow box scan
LM_LIT_OBJECTS   = 1 << 4   # lights lit objects while the game runs
LM_BAKE_SHADOW   = 1 << 5   # casts an occlusion ray during the bake
LM_LIGHTNESS     = 1 << 6   # counts toward how lit a place is

LIGHT_MODE_FLAGS = (
    (LM_REALTIME, "lm_realtime", "Lights Objects",
     "Lights ordinary objects, billboards and morphs while the game runs. Lit "
     "objects are not among them - they ask for Lights Lit Objects instead"),
    (LM_LIT_OBJECTS, "lm_lit_objects", "Lights Lit Objects",
     "Lights Lit Object meshes while the game runs, on top of the lighting "
     "baked into them. Off in the engine's own default; the game turns it on "
     "for its car headlights and fire lights"),
    (LM_VERTEX_BAKE, "lm_vertex_bake", "Vertex Bake",
     "Taken into account when a lit object's per-vertex lighting is baked"),
    (LM_BAKE_SHADOW, "lm_bake_shadow", "Casts Baked Shadow",
     "Casts an occlusion ray while a bake runs, so baked lighting shows what "
     "this light cannot reach"),
    (LM_SHADOW, "lm_shadow", "Affects Shadows",
     "Counted when the game picks which way a projected shadow falls and "
     "how dark it is"),
    (LM_LIGHTNESS, "lm_lightness", "Affects Lightness",
     "Counts toward the lightness the game measures at a point, when it asks "
     "how lit a place is"),
)

#: The bits of the mode word nothing reads, carried as they came.
LIGHT_MODE_UNREAD_BITS = 0xFFFFFFFF & ~(
    LM_REALTIME | LM_VERTEX_BAKE | LM_SHADOW | LM_LIT_OBJECTS | LM_BAKE_SHADOW
    | LM_LIGHTNESS)

#: What a light is before a file says otherwise - the engine's own constructor.
DEFAULT_LIGHT_TYPE        = LIGHT_DIRECTIONAL
DEFAULT_LIGHT_MODE        = 0xFF6B
DEFAULT_LIGHT_POWER       = 1.0
DEFAULT_LIGHT_COLOR       = (1.0, 1.0, 1.0)
DEFAULT_LIGHT_RANGE_NEAR  = 1.0
DEFAULT_LIGHT_RANGE_FAR   = 100.0
#: Full angles in radians, as the file stores them - 20 and 40 degrees. The
#: engine halves them itself when it builds the cone.
DEFAULT_LIGHT_CONE_INNER  = 0.34906587
DEFAULT_LIGHT_CONE_OUTER  = 0.69813174

# ── Projector ─────────────────────────────────────────────────────────────────
# A projector paints a material onto whatever surfaces its volume covers. It has
# no mesh of its own: the volume is a fixed unit shape carried by the frame's
# transform, reaching one unit along local +Y (Blender) and two units across.
#: Half-width of the projected volume, in the projector's own local units.
PROJECTOR_HALF_WIDTH = 1.0
#: How far the volume reaches along the projection axis.
PROJECTOR_REACH = 1.0

# The mode byte is one of six settled values, not a bit field: the game tests it
# against each in turn rather than masking it. Two things come out of it - the
# depth falloff applied to each projected vertex, and the frame-buffer blend the
# result is drawn with. The numbering makes the two look like separate bits, and
# every valid value does follow that pattern, but nothing in the game reads it
# that way.
PROJECTOR_FALLOFF_CONSTANT   = 0
PROJECTOR_FALLOFF_LINEAR     = 1
PROJECTOR_FALLOFF_TRIANGULAR = 2

#: Mode byte -> (depth falloff, alpha-blended rather than additive).
PROJECTOR_MODES = {
    0: (PROJECTOR_FALLOFF_CONSTANT,   False),
    1: (PROJECTOR_FALLOFF_CONSTANT,   True),
    4: (PROJECTOR_FALLOFF_LINEAR,     False),
    5: (PROJECTOR_FALLOFF_LINEAR,     True),
    8: (PROJECTOR_FALLOFF_TRIANGULAR, False),
    9: (PROJECTOR_FALLOFF_TRIANGULAR, True),
}
PROJECTOR_MODE_BYTES = {value: key for key, value in PROJECTOR_MODES.items()}

#: What a byte outside those six does. All three tests the game makes ask
#: whether the mode is one of the additive ones, so an unlisted value falls to
#: the alpha-blended side of each and picks up no depth term. That is mode 1
#: exactly, whatever the spare bits look like.
PROJECTOR_MODE_FALLBACK = (PROJECTOR_FALLOFF_CONSTANT, True)

#: How the projected color is weighted across the depth of the volume, from
#: the near face to the far one. The weight it produces lands in the RGB or the
#: alpha of each projected vertex, whichever the blend mode below calls for.
PROJECTOR_FALLOFF_ITEMS = (
    (str(PROJECTOR_FALLOFF_CONSTANT), "Constant",
     "Full strength throughout. No depth term at all"),
    (str(PROJECTOR_FALLOFF_LINEAR), "Linear",
     "Full strength at the near face, falling off evenly to nothing at the far "
     "one. What the game's headlights and railway lamps use"),
    (str(PROJECTOR_FALLOFF_TRIANGULAR), "Triangular",
     "Nothing at either face, full strength at mid-depth"),
)

#: The frame-buffer blend the projected triangles are drawn with, and which
#: channel the falloff weight is written into. Both paths draw the material's
#: diffuse texture modulated by the vertex color, with alpha testing on.
PROJECTOR_BLEND_ITEMS = (
    ("0", "Additive",
     "Adds to the surface, which can only get brighter - black parts of the "
     "texture add nothing. The falloff drives brightness. For light: "
     "headlights, lamp pools, glows"),
    ("1", "Alpha Blend",
     "Draws over the surface, hiding it where the texture is opaque. The "
     "falloff drives opacity. For marks: tyre tracks, scratches, decals"),
)

#: Visual types whose payload is a standard LOD geometry block.
#:
#: A lit object is in here because that is genuinely all it stores. It is a mesh
#: with baked lightmaps, but the bake is not in the model: the mission's own
#: cache carries it and hands it to the frame after loading, so what a .4ds
#: holds for one is byte-for-byte what it holds for a plain object.
GEOMETRY_VISUAL_TYPES = frozenset({
    VISUAL_OBJECT, VISUAL_LITOBJECT, VISUAL_SINGLEMESH, VISUAL_SINGLEMORPH,
    VISUAL_BILLBOARD, VISUAL_MORPH,
})

#: Visual types that carry a skin (bone weight) block after their geometry.
SKINNED_VISUAL_TYPES = frozenset({VISUAL_SINGLEMESH, VISUAL_SINGLEMORPH})

#: Visual types that carry a morph block.
MORPH_VISUAL_TYPES = frozenset({VISUAL_SINGLEMORPH, VISUAL_MORPH})

# ── Material flags ────────────────────────────────────────────────────────────
# The low fifteen bits of the word are not fifteen switches. Three runs of them
# hold small numbers, each read and written whole:
#
#   bits 0-7    how many times the environment texture repeats (0-255)
#   bits 8-10   which blend the environment texture uses (ENV_BLEND_*, 0-7)
#   bits 12-14  how its coordinates are made (ENV_UV_*, 0-7)
#
# Blend 3, Multiply, is bits 8 and 9 together; mapping 1 is bit 12 alone. Named
# one bit at a time - as "Overlay", "Multiply", "Additive", "Global Reflection",
# "Project Y" and "Project Z", which is how they were once labeled - they read
# as switches that combine, and every value but 1, 2 and 4 came out as a mix of
# two of them. Only bit 11 between them is a switch of its own.
MTL_ENV_TILE_MASK       = 0xFF
MTL_ENV_TILE_DEFAULT    = 1
MTL_ENV_BLEND_SHIFT     = 8
MTL_ENV_BLEND_MASK      = 0b111 << MTL_ENV_BLEND_SHIFT
MTL_ENV_UV_SHIFT        = 12
MTL_ENV_UV_MASK         = 0b111 << MTL_ENV_UV_SHIFT

MTL_TEXTURE_MANAGER     = 1 << 11
MTL_ALPHA_ENABLE        = 1 << 15
MTL_DISABLE_U_TILING    = 1 << 16
MTL_DISABLE_V_TILING    = 1 << 17
MTL_DIFFUSE_ENABLE      = 1 << 18
MTL_ENV_ENABLE          = 1 << 19
MTL_FORCE_TRUECOLOR     = 1 << 20
MTL_NO_COMPRESSION      = 1 << 21
MTL_NO_CACHED_TEXTURE   = 1 << 22
MTL_DIFFUSE_MIPMAP      = 1 << 23   # mipmaps for the diffuse texture
MTL_ALPHA_IN_TEX        = 1 << 24
MTL_ALPHA_ANIMATED      = 1 << 25
MTL_DIFFUSE_ANIMATED    = 1 << 26
MTL_DIFFUSE_COLORED     = 1 << 27
MTL_DIFFUSE_DOUBLESIDED = 1 << 28
MTL_ALPHA_COLORKEY      = 1 << 29
MTL_ALPHATEX            = 1 << 30
MTL_ALPHA_ADDITIVE      = 1 << 31

#: The only texture formats the game reads. Its loader picks the format from the
#: letter after the last dot and gives up on anything else, so these two are all
#: the addon accepts: a texture of any other kind is refused when it is picked
#: in a material or named by an imported model.
TEXTURE_EXTENSIONS = (".bmp", ".tga")

#: The BMP info header the loader reads, in bytes. It reads exactly this many
#: and then the palette and pixels from wherever the stream now sits - it never
#: looks at the size the header declares, and never seeks to the pixel offset
#: the file gives. A longer header (BITMAPV4HEADER is 108, BITMAPV5HEADER is
#: 124) leaves the difference in front of the pixels and slides the whole image.
BMP_INFO_HEADER_BYTES = 40

#: BMP depths the loader can expand. Anything else is refused outright.
BMP_BIT_DEPTHS = (8, 16, 24, 32)

#: Targa image types that carry run-length packets. The loader reads Targa
#: pixels straight through without unpacking them.
TGA_COMPRESSED_TYPES = (9, 10, 11)

#: Bit in a Targa's descriptor byte saying the rows run right to left, which the
#: loader notices and then ignores.
TGA_ORIGIN_RIGHT = 0x10

#: An animated texture's frames are named by overwriting the two characters
#: just before the extension with a counter, so the highest frame the game can
#: name is 99 and a count of 100 is the first one it cannot reach.
MAX_ANIM_FRAMES = 100

#: Any of these means no separate alpha-texture name is stored: a color-keyed,
#: truecolor or alpha-tested material carries its transparency in the diffuse
#: texture instead.
MTL_NO_ALPHA_MASK = (MTL_ALPHA_ADDITIVE | MTL_ALPHA_COLORKEY | MTL_ALPHA_IN_TEX)

#: How the environment texture is combined with the surface: a small number in
#: the flag word's blend bits, not a set of bits of its own.
ENV_BLEND_NONE = 0
ENV_BLEND_AMOUNT = 1
ENV_BLEND_SURFACE_ALPHA = 2
ENV_BLEND_MULTIPLY = 3
ENV_BLEND_MULTIPLY_TWICE = 4
ENV_BLEND_ADD = 5

#: The blends the game can draw an environment texture with. With the texture
#: on, any other value - None, or 6 and 7, which nothing names - has no blend
#: in the game: it logs an error and draws the reflection with whatever blend
#: the surface before it left set. No material the game ships pairs an
#: environment texture with one.
ENV_BLENDS_DRAWN = frozenset({
    ENV_BLEND_AMOUNT, ENV_BLEND_SURFACE_ALPHA, ENV_BLEND_MULTIPLY,
    ENV_BLEND_MULTIPLY_TWICE, ENV_BLEND_ADD,
})

#: How the environment texture is mixed with the surface below it.
ENV_BLEND_ITEMS = (
    (str(ENV_BLEND_NONE), "None",
     "No blend. Only right with the environment texture off - with it on, the "
     "game has no blend for this and draws the reflection however the surface "
     "before it was blended"),
    (str(ENV_BLEND_AMOUNT), "Blend by Amount",
     "Mixes in by the Amount slider below"),
    (str(ENV_BLEND_SURFACE_ALPHA), "Blend by Alpha",
     "Mixes in by the surface's own transparency"),
    (str(ENV_BLEND_MULTIPLY), "Multiply",
     "Darkens the surface by the environment texture"),
    (str(ENV_BLEND_MULTIPLY_TWICE), "Multiply Twice",
     "Multiply, at double strength"),
    (str(ENV_BLEND_ADD), "Add",
     "Adds the environment texture on top, brightening the surface"),
)

#: How the environment texture's coordinates are made, again a small number.
#: The two reflect modes differ only in which axis the reflection turns around;
#: the three planar modes pick two axes of the vertex position.
ENV_UV_REFLECT_Y_POLE = 0
ENV_UV_REFLECT_Z_POLE = 1
ENV_UV_PLANAR_XY = 2
ENV_UV_PLANAR_ZY = 3
ENV_UV_PLANAR_XZ = 4
ENV_UV_FRAME = 5

#: How environment texture coordinates are generated.
#: Labels use Blender's axes; each option names the game's own letters too. The
#: file stores the game's numbering unchanged - only the wording is translated,
#: through the same Y/Z swap the rest of the addon uses. The two reflect modes
#: follow the camera; the three planar modes project from world position and
#: stay put as the object moves.
ENV_UV_ITEMS = (
    (str(ENV_UV_REFLECT_Y_POLE), "Reflect XZ", "Spherical reflection. U from the reflected X, V from "
                        "the reflected Z, with Y as the pole. Game axes: "
                        "reflect XY, pole Z"),
    (str(ENV_UV_REFLECT_Z_POLE), "Reflect XY", "Spherical reflection. U from the reflected X, V from "
                        "the reflected Y, with Z as the pole - the upright "
                        "one. What nearly every reflective material in the "
                        "original game uses. Game axes: reflect XZ, pole Y"),
    (str(ENV_UV_PLANAR_XY), "Planar XY", "Flat projection from above. U from the vertex X, V "
                       "from its Y. Game axes: planar XZ"),
    (str(ENV_UV_PLANAR_ZY), "Planar ZY", "Flat projection from the side. U from the vertex Z, V "
                       "from its Y. Game axes: planar YZ. Unused in the "
                       "original game"),
    (str(ENV_UV_PLANAR_XZ), "Planar XZ", "Flat projection from the front. U from the vertex X, V "
                       "from its Z. Game axes: planar XY. Unused in the "
                       "original game"),
    (str(ENV_UV_FRAME), "Dummy Frame", "UVs are the vertex position inside a dummy's local "
                         "space, clamped to one unit: U from its local Y, V "
                         "from its local Z. Uses the first dummy sibling under "
                         "the object's parent. Billboards only - every other "
                         "visual type passes no dummy, so the mode does "
                         "nothing. Game axes: local Z and Y. Unused in the "
                         "original game"),
)

# ── Format limits ─────────────────────────────────────────────────────────────
#: A geometry LOD stores its vertex count in a uint16, but 0xFFFF is not a
#: count there: it is the sentinel that says the LOD reuses LOD 0's vertices and
#: carries only a UV block. Writing 65535 real vertices would be read back as
#: that sentinel and mangle the rest of the mesh, so the last usable count is
#: one below it.
MAX_VERTICES_PER_LOD = 0xFFFE
#: Sector, occluder and mirror hulls count their vertices in a uint32 and index
#: them with a uint16, so the ceiling there is the index range and there is no
#: sentinel in the way.
MAX_HULL_VERTICES = 0xFFFF
#: An occluder's face count has a ceiling the format itself does not state. The
#: game builds an edge-adjacency table for the silhouette walk and holds each
#: face index in it as a uint16, so past this the adjacency wraps and the walk
#: follows the wrong neighbors - silently, since nothing checks. Matched to the
#: vertex limit above rather than derived a second way.
MAX_OCCLUDER_FACES = 0xFFFF
#: A portal's outline is stored in a fixed-size slot of eight corners, so a
#: ninth would not fit.
MAX_PORTAL_VERTICES = 8
#: Joint properties: the box's sixteen numbers, as the file holds them, and
#: whether the joint has a box at all.
JOINT_BOX_PROP = "ls3d_joint_box"
HAS_JOINT_BOX_PROP = "ls3d_has_joint_box"
#: Scene switch: whether the joints' influence boxes are shown.
SHOW_INFLUENCE_BOXES_PROP = "ls3d_show_influence_boxes"
#: Scene switch: whether they are drawn over everything, the mesh included.
INFLUENCE_BOXES_IN_FRONT_PROP = "ls3d_influence_boxes_in_front"
#: Scene setting: which of the box's handles are on the active joint.
INFLUENCE_HANDLES_PROP = "ls3d_influence_handles"
#: Scene setting: how big the joint markers are drawn, as a multiplier.
JOINT_DISPLAY_SCALE_PROP = "ls3d_joint_display_scale"
#: The influence box a joint is made with: the one the game's own tool gave a
#: joint nothing else was linked to, as ``Bone01`` in ``I04Delnik01+`` carries
#: it - a cube 15.1 cm across, 8.1 cm below the joint, its weight running up.
#: Kept to the last digit, the tool's own rounding included.
DEFAULT_JOINT_BOX = (
    -0.07557117193937302, -4.7654249435424845e-09, 1.1372065955583821e-08, 0.0,
    1.137206773194066e-08, -9.008784829234173e-09, 0.07557117193937302, 0.0,
    -4.765425387631694e-09, 0.07557117193937302, -9.008784829234173e-09, 0.0,
    0.0007997043430805206, -0.08094525337219238, 5.309182427026826e-09, 1.0)
#: How many joints the game gathers below a skinned mesh. It stops at this many,
#: so the vertices on any joint past it never move, and a joint numbered below
#: it but found after it leaves an empty slot the game still reads. The game's
#: own models stay at 25 or fewer.
MAX_SKIN_JOINTS = 64
#: The name the game's character animations move the body by. They key the
#: frame called this - its place and its turn - and only turn the joints, so a
#: character's skinned mesh has to carry it, exactly, for them to move it.
CHARACTER_MESH_NAME = "base"
#: Frame names and user properties are length-prefixed with a single byte.
MAX_STRING_BYTES = 0xFF
#: Text encoding used for every string in the format.
STRING_ENCODING = "windows-1250"

# ── Viewport display colors ──────────────────────────────────────────────────
def _hex(h):
    return (((h >> 16) & 0xFF) / 255.0,
            ((h >> 8) & 0xFF) / 255.0,
            (h & 0xFF) / 255.0,
            1.0)


# No COLOR_FRAME_VISUAL: a visual frame is colored by its visual type, never by
# being a visual, so the entry would only ever be a second name for white.
COLOR_FRAME_SECTOR   = _hex(0x00FFFF)
COLOR_FRAME_PORTAL   = _hex(0x9926FF)
COLOR_FRAME_OCCLUDER = _hex(0xFFAA00)
COLOR_FRAME_DUMMY    = _hex(0x0000FF)
COLOR_FRAME_TARGET   = _hex(0x00FF00)
COLOR_FRAME_JOINT    = _hex(0x00B6FF)
COLOR_FRAME_LIGHT    = _hex(0xFFE066)
#: Shared by the frame types that store nothing but a transform. They are
#: bookkeeping rather than content, so they read as one muted family
#: instead of competing for attention with the frames that draw.
COLOR_FRAME_BARE     = _hex(0x8C8C8C)
#: What a dummy's box outline turns when the dummy is picked. Its own
#: color rather than the theme's orange: the outline stands next to the
#: empty's cube, which Blender is already turning orange, and two oranges
#: a shade apart say less than one orange and one red.
COLOR_DUMMY_BOX_SELECTED = _hex(0xFF2626)

COLOR_VISUAL_OBJECT      = _hex(0xFFFFFF)
COLOR_VISUAL_LITOBJECT   = _hex(0xFF0000)
COLOR_VISUAL_SINGLEMESH  = _hex(0xFFB6B2)
COLOR_VISUAL_SINGLEMORPH = _hex(0xFFA2E8)
COLOR_VISUAL_BILLBOARD   = _hex(0x00B600)
COLOR_VISUAL_MORPH       = _hex(0xFF00FF)
COLOR_VISUAL_LENSFLARE   = _hex(0xFFFFAA)
COLOR_VISUAL_MIRROR      = _hex(0x7FFF7F)
COLOR_VISUAL_PROJECTOR   = _hex(0x000000)   # its volume outline carries the
                                            # selection color instead

# ── Naming conventions ────────────────────────────────────────────────────────
#: LOD meshes are discovered as ``<base>_lod<N>`` siblings of the base object.
LOD_SUFFIX_PATTERN    = r"_lod(\d+)$"
MAX_LOD_LEVELS        = 10
#: Portals are sector-typed children of a sector named ``<something>_portal<N>``.
PORTAL_SUFFIX_PATTERN = r"_portal\d+$"
#: The empty shape a light is drawn as. Blender's cone points along the
#: empty's local +Y, which is the way a light shines, so the marker and the beam
#: drawn from it agree.
LIGHT_EMPTY_DISPLAY = "CONE"
#: What lights were drawn as before: a single arrow, which points along +Z and
#: so sat ninety degrees off the beam. Still read as a light, so older scenes
#: keep working, and turned into a cone when a file is opened.

#: Which empty shapes stand for a 4DS frame, and which frame each one means.
#: A dummy is a box, so it is the cube and nothing else, and an empty of any
#: other shape is not part of the model at all - it is a helper somebody put in
#: the scene, and the addon leaves it alone.
EMPTY_FRAME_DISPLAYS = {
    "CUBE":                  "Dummy",
    "SPHERE":                "Lens Flare",
    "ARROWS":                "Projector",
    LIGHT_EMPTY_DISPLAY:     "Light",
    "PLAIN_AXES":            "Target, or a frame that stores only its transform",
}

#: Imported reserved material slots keep this name so their file ID survives.
RESERVED_MATERIAL_PATTERN = r"4ds_material_(\d+)$"


# ── Frame culling flags (u8, every frame type) ────────────────────────────────
# Only two bits reach the game. The rest hold transform bookkeeping that is
# rebuilt while the model loads: bits 2, 4, 5 and 6 are cleared, bit 3 is turned
# on whatever the file said, and bit 1 means nothing at all. They still
# round-trip through the raw value.
CF_ENABLED     = 1 << 0   # frame is drawn, collides, lights and sounds
CF_POS_LOCKED  = 1 << 7   # the frame cannot be moved once the level runs

# ── Visual render flags, byte 1 ───────────────────────────────────
# Bits 0, 1 and 3 do nothing; bit 2 belongs to the particle system, which a
# model frame never reaches; bit 6 says the vertices are already in world space,
# which is only ever true for geometry the game placed itself at runtime.
RF_MANAGED_LOD = 1 << 4   # LOD follows texture residency instead of distance
RF_NO_MIRROR   = 1 << 5   # left out of mirror reflections
RF_WORLD_SPACE = 1 << 6   # vertices are already in world space
RF_FLAT_LIGHT  = 1 << 7   # one averaged light value across the whole object

# ── Visual render flags, byte 2 ───────────────────────────────────
# Shadows and projections are two independent bits each, not a quality setting.
# The first decides whether the object receives anything at all; the second
# decides whether the parts of it using color key, alpha blending, alpha test
# or a partly transparent material receive it too.
LF_ZBIAS                 = 1 << 0   # decal bias against z-fighting
LF_SHADOW_DIFFUSE        = 1 << 1   # receives shadows on solid faces
LF_SHADOW_ALPHA          = 1 << 2   # ... and on transparent faces
LF_IS_MESH_OBJECT        = 1 << 3   # visual is one of the object family - everything but mirrors
LF_NO_TWOSIDED_COLLISION = 1 << 4   # left out of two-sided collision queries
#: Note: the game never issues a two-sided collision query, so on its own this
#: bit changes nothing.
LF_PROJECTION_DIFFUSE    = 1 << 5   # receives projections on solid faces
LF_PROJECTION_ALPHA      = 1 << 6   # ... and on transparent faces
LF_NO_FOG                = 1 << 7

# ── Sector flags (i32 pair) ───────────────────────────────────────────────────
# Three bits are the sector's to set. The rest belong to the game, which uses
# them as bookkeeping while it runs: some it rewrites every frame, two it trusts
# as they arrive and misbehaves on, and four mark the sectors it makes for
# itself. Each is described in RESERVED_SECTOR_FLAGS below.
SF_OCCLUDER          = 1 << 6    # the sector's own shape hides what is behind it
SF_SMALL_PORTAL_CULL = 1 << 8    # skip contents when seen through a small portal
SF_SOUND_REVERB      = 1 << 11   # the sector's echo settings still need applying

SF_VISIBLE           = 1 << 0    # can be seen into this frame
SF_PARSING           = 1 << 1    # the traversal's recursion guard
SF_PREPARED          = 1 << 2    # one-time setup has run
SF_COLLECTED         = 1 << 4    # contents gathered on this visit
SF_VIS_CYCLE         = 1 << 7    # rooms loop back on themselves here
SF_SPACE_PRIMARY     = 1 << 9    # one of the game's own four sectors
SF_REVERB_FADING     = 1 << 10   # the echo is fading out
SF_SPACE_BACKDROP    = 1 << 12   # one of the game's own four sectors
SF_SPACE_OMNI        = 1 << 13   # one of the game's own four sectors
SF_SPACE_NEAR        = 1 << 14   # one of the game's own four sectors

#: Sector bits the traversal wipes from every sector before it runs, so whatever
#: a file stores in them is thrown away and cannot matter.
SECTOR_PER_FRAME_BITS = SF_VISIBLE | SF_COLLECTED | SF_VIS_CYCLE

#: Engine-owned bits. The loader either rewrites them or nothing reads them, so
#: they are hidden unless "Show Engine-Reserved Flags" is on - or unless the
#: loaded file has one set, in which case the panel shows it anyway. They are
#: always stored and written back exactly as read.
RESERVED_CULL_FLAGS = (
    (1 << 1, "cf_res_undefined2", "Undefined 2",
     "Not defined by the engine"),
    (1 << 2, "cf_res_scale_baked", "Local Matrix Scaled",
     "Scale is already folded into the local matrix. Cleared on load"),
    (1 << 3, "cf_res_rotation_valid", "Rotation Valid",
     "Says the stored rotation matches the matrix. The game turns it on while "
     "loading whatever the file says, so setting it changes nothing"),
    (1 << 4, "cf_res_matrix_built", "Local Matrix Built",
     "The rotation block of the local matrix is built. Cleared on load"),
    (1 << 5, "cf_res_world_matrix", "World Matrix Valid",
     "The cached world matrix is current. Cleared on load"),
    (1 << 6, "cf_res_bound_valid", "Bounds Valid",
     "The cached bounding volume is current. Cleared on load"),
)

RESERVED_RENDER_FLAGS = (
    (1 << 0, "rf1_res_undefined1", "Undefined 1", "Not read by the engine"),
    (1 << 1, "rf1_res_undefined2", "Undefined 2", "Not read by the engine"),
    (1 << 2, "rf1_res_particle_color", "Color Cache Dirty",
     "Particle system only. Rebuilds cached vertex colors"),
    (1 << 3, "rf1_res_undefined4", "Undefined 4", "Not read by the engine"),
    (1 << 6, "rf1_res_world_space", "World Space Geometry",
     "The engine treats the vertices as already in world space and skips the "
     "frame transform. It only sets this itself, after baking the transform "
     "into the mesh - a .4ds always stores object-space vertices, so setting "
     "it here makes the object ignore its own position"),
)

RESERVED_SECTOR_FLAGS = (
    (SF_VISIBLE, "sf_res_visible", "Visible",
     "Not the sector's on/off switch - that is Enable under Frame Flags. It "
     "only says the game can see into the sector right now, and every frame "
     "starts by clearing it and working it out again, so whatever the file "
     "holds is thrown away"),
    (SF_PARSING, "sf_res_parsing", "Parsing",
     "NOT wiped, and dangerous. It is the traversal's recursion guard: a "
     "sector that already has it is skipped, and the only place it is cleared "
     "is at the end of a visit that never happens. A sector saved with this "
     "on is never drawn again. Export refuses it"),
    (SF_PREPARED, "sf_res_prepared", "Prepared",
     "NOT wiped. The game takes it to mean the sector's one-time setup has "
     "already run and skips it, so no occluder is built from the hull. Export "
     "refuses it"),
    (SF_COLLECTED, "sf_res_collected", "Enumerated",
     "Wiped every frame. Says the game has already gathered what is inside "
     "this sector on this visit"),
    (SF_VIS_CYCLE, "sf_res_vis_cycle", "Visibility Cycle",
     "Wiped every frame. Set when the rooms loop back on themselves in a way "
     "that makes this sector's occluders untrustworthy"),
    (SF_SPACE_PRIMARY, "sf_res_space_primary", "Space: Primary",
     "Not wiped, but it only marks one of the four sectors the game makes for "
     "itself. On your own sector it stops the game reusing it as the cached "
     "answer to 'which sector is this point in', which costs a little speed "
     "and nothing else"),
    (SF_REVERB_FADING, "sf_res_reverb_fading", "Sound Fade Out",
     "Not wiped. Says the camera has just left this sector, so its echo is "
     "fading rather than cutting off. Saved on, the sector starts out fading; "
     "walking into it clears it"),
    (SF_SPACE_BACKDROP, "sf_res_space_backdrop", "Space: Backdrop",
     "Not wiped. Marks one of the four sectors the game makes for itself. On "
     "your own sector, objects moved inside it skip part of the re-parenting "
     "the game would otherwise do"),
    (SF_SPACE_OMNI, "sf_res_space_omni", "Space: Omni",
     "Not wiped. Marks one of the four sectors the game makes for itself, and "
     "behaves like Space: Backdrop on a sector of your own"),
    (SF_SPACE_NEAR, "sf_res_space_near", "Space: Near",
     "Marks one of the four sectors the game makes for itself. Nothing ever "
     "reads it, so it does nothing on a sector of yours"),
)

# ── Portal flags (u32) ────────────────────────────────────────────────────────
# Two bits are the portal's to set. Of the game's own, bit 1 can stick and is
# warned about, bit 3 is rewritten every frame, bits 5 and 7 are set by the game
# when it finds a broken layout, and bits 0 and 6 have no reader.
PF_ENABLED  = 1 << 2      # portal is open and can be seen through
PF_FAR_CULL = 1 << 4      # hard cutoff past Far Range
PF_OCCLUDED = 1 << 1      # something is blocking the opening right now

RESERVED_PORTAL_FLAGS = (
    (1 << 0, "pf_res_undefined1", "Undefined 1",
     "Nothing reads this bit. Setting it changes nothing"),
    (PF_OCCLUDED, "pf_res_occluded", "Occluded",
     "NOT reliably wiped. Something is blocking this opening right now. It is "
     "only cleared while an occluder pass runs over the sector, so in a sector "
     "with no occluders a portal saved with it on stays blocked and nothing "
     "is drawn through it. Export warns about it"),
    (1 << 3, "pf_res_traversed", "Traversed",
     "Wiped every frame. The game has already looked through this opening on "
     "this pass"),
    (1 << 5, "pf_res_divides_sector", "Divides Sector",
     "Not wiped, but only ever logged. The game switches it on by itself when "
     "a portal's two sides land in the same sector. Saved on, it makes the "
     "game complain about a fault that is not there"),
    (1 << 6, "pf_res_undefined7", "Undefined 7",
     "Nothing reads this bit. Many portals arrive with it set, left behind by "
     "whatever built them; it is harmless either way and is kept as found"),
    (1 << 7, "pf_res_in_portal", "In Portal",
     "The game switches it on by itself when two portals in the same sector "
     "overlap, and logs the fault. Nothing ever reads it back, so setting it "
     "does nothing"),
)


# ── Target flags (u16) ────────────────────────────────────────────────────────
# What a target does to the objects linked to it. Look At wins: while it is on,
# the three follow switches are not read at all.
TF_LOOK_AT         = 1 << 0
TF_FOLLOW_ROTATION = 1 << 1
TF_FOLLOW_POSITION = 1 << 2
TF_FOLLOW_SCALE    = 1 << 3

_TARGET_FOLLOW_NOTE = (". Only read while Look At is off. A linked object with "
                       "no parent copies the target's whole transform as soon "
                       "as any follow switch is on")

TARGET_FLAGS = (
    (TF_LOOK_AT, "tf_look_at", "Look At",
     "Every linked object turns to face this target. While this is on, the "
     "follow switches are not read"),
    (TF_FOLLOW_ROTATION, "tf_follow_rotation", "Follow Rotation",
     "Linked objects copy this target's rotation" + _TARGET_FOLLOW_NOTE),
    (TF_FOLLOW_POSITION, "tf_follow_position", "Follow Position",
     "Linked objects copy this target's position" + _TARGET_FOLLOW_NOTE),
    (TF_FOLLOW_SCALE, "tf_follow_scale", "Follow Scale",
     "Linked objects copy this target's scale" + _TARGET_FOLLOW_NOTE),
)


# ── Flag bytes a newly made frame starts with ─────────────────────────────────
# Blender's own defaults would be zero, and a frame with a zero culling byte has
# its Enable bit clear, so it would load into the game and never appear. These
# are the values the game's own models carry: each is the commonest byte for
# that frame type across every model that ships with Mafia, so a frame built by
# hand starts out like one the game already has.
#: How far from the camera a mirror still reflects. The engine's own default,
#: and what all nine mirrors the game ships store - not one overrides it. It
#: matters that a new mirror starts here rather than at zero: the game compares
#: the camera's distance against this, so a mirror with no range never reflects
#: anything and only ever draws its flat tint.
DEFAULT_MIRROR_RANGE            = 100.0

DEFAULT_CULL_FLAGS              = 0x09   # Enable + Rotation Valid
DEFAULT_CULL_FLAGS_SECTOR       = 0x7D   # ... plus the matrix and bounds bits
DEFAULT_CULL_FLAGS_JOINT        = 0x3D   # ... plus the matrix bits
DEFAULT_RENDER_FLAGS            = 0x00
DEFAULT_RENDER_FLAGS2           = 0x2A   # shadows + renderable object + projections
DEFAULT_RENDER_FLAGS2_MIRROR    = 0x22   # the same without Renderable Object
#: A projector reads none of byte 2. Shadows and projections are receiver
#: settings, and the receiver walk only ever collects the five object visual
#: types - a projector is never one of them - so the 0x22 a mirror carries
#: would be inert noise here. The renderable-object bit must stay clear on top
#: of that, which the exporter enforces whatever this says.
DEFAULT_RENDER_FLAGS2_PROJECTOR = 0x00
DEFAULT_RENDER_FLAGS2_BILLBOARD = 0xAA   # ... and with No Fog
DEFAULT_SECTOR_FLAGS1           = 0x801  # Echo Settings Pending + Visible
DEFAULT_SECTOR_FLAGS2           = 0x0    # every shipped sector's group mask
DEFAULT_PORTAL_FLAGS            = PF_ENABLED
