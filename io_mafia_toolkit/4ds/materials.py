"""Shader node graph that previews LS3D material flags in Blender.

The game draws these materials with a fixed-function pipeline: a diffuse
texture, an optional environment map combined in one of five ways, and one of
several transparency sources feeding one of three blend equations. None of that
maps onto Principled BSDF directly, so the graph below reproduces it explicitly.

Transparency is where Blender and the game differ most, and the graph is built
to match the game rather than to look like a modern shader:

* Three switches pick how the surface is combined with what is already on
  screen, and the first one set wins - *Alpha Additive*, then *Use Alpha
  Texture*, then *Color Key*. An opacity below 1 counts as the middle one.
  Nothing set draws the surface solid.
* An additive surface is **added** to the screen rather than covering it, so
  its dark areas disappear on their own and its bright areas brighten whatever
  is behind. Its texture carries no transparency of its own at all.
* A faded surface blends the ordinary way - unless its emission color is
  anything but black, which turns it additive as well. Most of the game's
  fires, sparks, muzzle flashes and lamp halos are exactly that: an ordinary
  faded material with a gray emission color, and drawing them as a plain fade
  darkens the scene where the game brightens it.
* A color-keyed surface is not blended at all. Each pixel is drawn solid or
  thrown away outright, and the cut-off is low - about an eighth - not a half.

The graph is built once with every branch present and wired, then
:func:`sync_material_flags` switches branches on and off by assigning images,
adjusting mix factors, and moving the one link into the material output.
Rebuilding from scratch on every flag change would drop any hand edits and is
far slower.

This is preview only: export reads the flag and image properties, never the
nodes.
"""

import contextlib
import os

import bpy

from ..common import constants as C

#: Depth of :func:`holding_updates`. Every material property rebuilds or
#: resyncs the graph as it is set, which is what keeps the preview live under
#: the user's hands - but an import sets a dozen of them in a row on each of
#: several hundred materials, and building the graph a dozen times over to
#: throw eleven away is most of what an import spends its time on.
_held = 0


@contextlib.contextmanager
def holding_updates():
    """Fill a material in without rebuilding its preview at every step.

    The caller builds the graph once when it is done. Nested holds are counted,
    so an inner one does not let go early.
    """
    global _held
    _held += 1
    try:
        yield
    finally:
        _held -= 1


#: Bumped whenever the node set below changes shape. A graph built by an older
#: build carries an older number - or none at all - and is rebuilt rather than
#: half-updated into something with dangling branches.
GRAPH_VERSION = 4
#: Where that number is kept, on the node tree itself.
GRAPH_STAMP = "ls3d_graph_version"

# ── Node labels ───────────────────────────────────────────────────────────────
# Labels rather than names, because Blender uniquifies names but leaves labels
# alone, so these survive a copy of the material.
NL_OUTPUT = "LS3D_OUTPUT"
NL_BSDF = "LS3D_BSDF"

NL_DIFFUSE_TEX = "LS3D_DIFFUSE_TEX"
NL_ALPHA_TEX = "LS3D_ALPHA_TEX"
NL_ENV_TEX = "LS3D_ENV_TEX"

NL_DIFF_COORD = "LS3D_DIFF_COORD"
NL_DIFF_SEP = "LS3D_DIFF_SEP"
NL_DIFF_CLAMP_U = "LS3D_DIFF_CLAMP_U"
NL_DIFF_CLAMP_V = "LS3D_DIFF_CLAMP_V"
NL_DIFF_COMB = "LS3D_DIFF_COMB"
NL_DIFF_TINT = "LS3D_DIFF_TINT"

NL_ENV_COORD = "LS3D_ENV_COORD"
NL_ENV_SEP = "LS3D_ENV_SEP"
NL_ENV_POLE = "LS3D_ENV_POLE"
NL_ENV_FLOOR = "LS3D_ENV_FLOOR"
NL_ENV_ROOT = "LS3D_ENV_ROOT"
NL_ENV_PROJ = "LS3D_ENV_PROJ"
NL_ENV_PROJ_V = "LS3D_ENV_PROJ_V"
NL_ENV_U = "LS3D_ENV_U"
NL_ENV_V = "LS3D_ENV_V"
NL_ENV_COMB = "LS3D_ENV_COMB"
NL_ENV_MAPPING = "LS3D_ENV_MAPPING"
NL_ENV_MIX = "LS3D_ENV_MIX"
NL_ENV_SCALE = "LS3D_ENV_SCALE"

NL_KEY_SPLIT = "LS3D_COLOR_KEY_SEP_D"
NL_KEY_CMP_R = "LS3D_COLOR_KEY_CMP_R"
NL_KEY_CMP_G = "LS3D_COLOR_KEY_CMP_G"
NL_KEY_CMP_B = "LS3D_COLOR_KEY_CMP_B"
NL_KEY_AND_A = "LS3D_COLOR_KEY_MUL_A"
NL_KEY_AND_B = "LS3D_COLOR_KEY_MUL_B"
NL_KEY_INVERT = "LS3D_COLOR_KEY_INV"

NL_ALPHA_LUMA = "LS3D_ALPHA_LUMA"
NL_OPACITY_VAL = "LS3D_OPACITY_VAL"
NL_OPACITY_MUL = "LS3D_OPACITY_MUL"
NL_ALPHA_TEST = "LS3D_ALPHA_TEST"

NL_ENV_EMIT = "LS3D_ENV_EMIT"
NL_SELF_LIGHT = "LS3D_SELF_LIGHT"
NL_EMIT_SUM = "LS3D_EMIT_SUM"
NL_SURFACE_SUM = "LS3D_SURFACE_SUM"

NL_GLOW_TINT = "LS3D_GLOW_TINT"
NL_GLOW_PREMUL = "LS3D_GLOW_PREMUL"
NL_GLOW_EMIT = "LS3D_GLOW_EMIT"
NL_GLOW_CLEAR = "LS3D_GLOW_CLEAR"
NL_GLOW_ADD = "LS3D_GLOW_ADD"

# ── Numbers the game draws by ─────────────────────────────────────────────────
#: How the game folds a color down to a single channel, for an alpha texture
#: and for the see-through test its bullets and light rays use. It spells the
#: same weights two ways that differ in the third decimal; the difference is
#: far below anything the eye can pick out of an alpha value.
LUMA_WEIGHTS = (0.30, 0.60, 0.10)

#: A color-keyed pixel is drawn only where its alpha beats this. The game reads
#: it as a byte, 32 of 255 - low enough that a keyed edge stays crisp instead of
#: eating into the shape the way a half-way cut-off would.
COLOR_KEY_ALPHA_REF = 32.0 / 255.0

#: Clamp bounds standing in for "no clamping" on a tiled axis.
UNCLAMPED = 10000.0

#: Which two components of the vertex position become U and V, per planar mode,
#: in Blender's axes.
PLANAR_AXES = {
    C.ENV_UV_PLANAR_XY: ("X", "Y"),
    C.ENV_UV_PLANAR_ZY: ("Z", "Y"),
    C.ENV_UV_PLANAR_XZ: ("X", "Z"),
    # No dummy to project from in a preview, so this one stands in for it.
    C.ENV_UV_FRAME: ("X", "Y"),
}

#: The two reflect modes, as ``(pole axis, U axis, V axis, V sign)`` in
#: Blender's axes. The game wraps the reflected direction onto the map by
#: pushing it away from one pole axis; the other two become U and V. Its V runs
#: down from the top of the image where Blender's runs up from the bottom, so
#: one of the two comes out with its sign already turned around.
REFLECT_AXES = {
    C.ENV_UV_REFLECT_Y_POLE: ("Y", "X", "Z", 1.0),
    C.ENV_UV_REFLECT_Z_POLE: ("Z", "X", "Y", -1.0),
}

#: How wide the reflection is spread across the map, before the repeat count
#: stretches it. One over two root two - the usual wrap for a map painted as
#: a sphere seen head-on.
REFLECT_SPREAD = 0.35355339

#: The reflection pointing straight at the pole would divide by zero, and the
#: game gives up and takes the middle of the map. This keeps the arithmetic
#: finite and lands in the same place.
POLE_FLOOR = 1.0e-4


# ── Small helpers ─────────────────────────────────────────────────────────────
def find_node(nodes, label):
    for node in nodes:
        if node.label == label:
            return node
    return None


def _set_input(node, names, value):
    """Set the first input in *names* that exists.

    Principled BSDF socket names move between Blender versions ("Specular" ->
    "Specular IOR Level", "Emission" -> "Emission Color"), so each lookup lists
    the spellings we accept instead of guessing one and swallowing the error.
    """
    if node is None:
        return False
    for name in names:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return True
    return False


def srgb_to_linear(value):
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value):
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * value ** (1.0 / 2.4) - 0.055


# ── Opacity belongs to the mesh, not to the material ──────────────────────────
# Opacity is the one material setting the game does not read per material. It
# puts a single alpha on every vertex of a mesh and takes it from one of that
# mesh's materials - the one whose faces lead the mesh's draw order. Every other
# material on the mesh is drawn at that alpha and its own Opacity is ignored.
#
# Which one leads is not the first in the list either. The game moves any
# material that asks for transparency - Color Key, Use Alpha Texture, Alpha
# Additive, or an Opacity below 1 - behind the ones that do not, so the leader
# is the *last* solid material on the mesh, and only a mesh with nothing solid
# on it is led by its first. A solid material has Opacity 1 by definition, so
# in practice: put one solid material anywhere on a mesh and every Opacity on
# that mesh stops meaning anything.
#
# It matters rarely and enormously. Only 25 of the game's 19831 materials set
# an opacity at all, and 8 of those uses are overridden this way - but two of
# them are set to 0, so reading the number at face value makes the whisky
# glass in FMVwhiskymrph vanish where the game draws it frosted.


def sorts_behind(mat):
    """True when the game draws this material after the solid ones."""
    flags = mat.ls3d_material_flags & 0xFFFFFFFF
    return bool(flags & (C.MTL_ALPHA_COLORKEY | C.MTL_ALPHATEX
                         | C.MTL_ALPHA_ADDITIVE)) or mat.ls3d_opacity < 1.0


def leading_material(obj):
    """The material whose Opacity the whole of *obj* is drawn at.

    Only the slots some face actually uses count, in slot order, since that is
    the order the faces are written in and the order the game reads them back.
    """
    mesh = obj.data
    slots = [slot.material for slot in obj.material_slots]
    if not slots:
        return None
    used = sorted({polygon.material_index for polygon in mesh.polygons})

    leader = None
    for index in used:
        material = slots[index] if index < len(slots) else None
        if leader is None:
            leader = material
        elif material is not None and not sorts_behind(material):
            leader = material
    return leader


def effective_opacity(mat):
    """The opacity *mat* is really drawn at, once its meshes have their say.

    A material nothing uses yet - one just created, or one on a lens flare -
    keeps its own, since there is no mesh to take it from. Where meshes
    disagree, the most opaque wins: a preview that hides geometry outright is
    the worse of the two mistakes, and it is the one that brought this to light.
    """
    own = mat.ls3d_opacity
    if own >= 1.0:
        # The overwhelming majority, and worth the early exit: the scan below
        # walks every mesh in the file, and this runs on every flag change.
        return own

    verdicts = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if not any(slot.material is mat for slot in obj.material_slots):
            continue
        leader = leading_material(obj)
        verdicts.append(leader.ls3d_opacity if leader is not None else 1.0)
    return max(verdicts) if verdicts else own


#: BMP bit depths that carry a color palette. Anything deeper is truecolor
#: and has no palette entry to use as a key.
PALETTE_BIT_DEPTHS = (1, 4, 8)

#: The key to assume for a color-keyed texture that has no palette. Eight of
#: the 336 color-keyed textures in the game are 24-bit, so there is simply no
#: entry to read; black is what the engine keys on and what 0.6.2 fell back to.
DEFAULT_COLOR_KEY = (0.0, 0.0, 0.0)


#: What the last look at each BMP found, keyed by the file and how it stood at
#: the time. Every flag change on a color-keyed material asks again, and a
#: model brings thousands of them, so the answer is kept rather than re-read.
#: A file edited in place lands under a different key and is read afresh.
_BMP_CACHE = {}
#: Enough for the biggest model in the game several times over, and small
#: enough that nothing has to sweep it.
_BMP_CACHE_LIMIT = 4096


def _bmp_info(image):
    """``(bit_depth, key)`` for a BMP on disk, or ``None`` when unreadable.

    *key* is palette entry 0 as linear RGB for a paletted file, and
    :data:`DEFAULT_COLOR_KEY` for a truecolor one, which has no palette to read.
    """
    if image is None or not image.filepath:
        return None
    path = image.filepath_from_user()
    if not path or not os.path.isfile(path):
        return None
    try:
        stat = os.stat(path)
        stamp = (path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if stamp in _BMP_CACHE:
        return _BMP_CACHE[stamp]

    found = _read_bmp_info(path)
    if len(_BMP_CACHE) >= _BMP_CACHE_LIMIT:
        _BMP_CACHE.clear()
    _BMP_CACHE[stamp] = found
    return found


def _read_bmp_info(path):
    """The header read itself, without the caching around it."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(1078)    # header + a full 256-entry palette
    except OSError:
        return None
    if data[:2] != b"BM" or len(data) < 58:
        return None

    dib_size = int.from_bytes(data[14:18], "little")
    bit_depth = int.from_bytes(data[28:30], "little")
    if bit_depth not in PALETTE_BIT_DEPTHS:
        return bit_depth, DEFAULT_COLOR_KEY

    table = 14 + dib_size
    if table + 3 > len(data):
        return None
    # Palette entries are stored BGRA.
    return bit_depth, (srgb_to_linear(data[table + 2] / 255.0),
                       srgb_to_linear(data[table + 1] / 255.0),
                       srgb_to_linear(data[table] / 255.0))


def read_bmp_color_key(image):
    """Palette entry 0 of a BMP as linear RGB.

    Mafia marks transparency with the first palette entry rather than an alpha
    channel, and Blender does not expose palettes, so the file is read directly.

    Returns :data:`DEFAULT_COLOR_KEY` for a valid truecolor BMP, which has no
    palette to read. ``None`` is reserved for a file that could not be read at
    all, so callers can tell "nothing to look up" from "look-up failed" and only
    warn about the latter.
    """
    info = _bmp_info(image)
    return None if info is None else info[1]


def has_color_palette(image):
    """True when a BMP carries a palette, ``None`` when it cannot be read.

    Worth knowing because the game keys a paletted texture on the whole color
    and a truecolor one on its blue channel alone.
    """
    info = _bmp_info(image)
    return None if info is None else info[0] in PALETTE_BIT_DEPTHS


def key_window(component):
    """``(middle, reach)``: the values of one channel that count as the key's.

    The game compares whole bytes, so a pixel is the key's color when each
    channel is nearer the key's byte than any other byte - reaching halfway
    to the byte on either side, and not a step past it. The node graph is
    handed linear values, where those halfway points sit unevenly, closer
    together near black than near white, so they are worked out from the byte
    the key was read from. The window has to be the whole of that: Blender's
    viewport does not turn a texture's bytes into exactly the linear values
    they stand for, and near white is off by most of the way to the halfway
    point.
    """
    level = min(max(round(linear_to_srgb(max(component, 0.0)) * 255.0), 0), 255)
    here = srgb_to_linear(level / 255.0)
    up = (srgb_to_linear((level + 1) / 255.0) - here) if level < 255 else None
    down = (here - srgb_to_linear((level - 1) / 255.0)) if level > 0 else None
    # At either end there is no byte beyond, and the window is as wide
    # outward as it is inward.
    up = down if up is None else up
    down = up if down is None else down
    low, high = here - down * 0.5, here + up * 0.5
    return (low + high) * 0.5, (high - low) * 0.5


#: Fed to the two channel comparisons the game leaves out for a truecolor
#: texture, so they always match and the blue one decides on its own.
ALWAYS_MATCHES = 1e6


# ── Graph construction ────────────────────────────────────────────────────────
def rebuild_material_nodes(mat):
    """Build the full preview graph from scratch, discarding any existing nodes."""
    if mat is None or _held:
        return

    mat.use_nodes = True
    tree = mat.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    def add(node_type, label, x, y):
        node = nodes.new(node_type)
        node.label = label
        node.location = (x, y)
        return node

    # ── Surface color ────────────────────────────────────────────────────────
    # Diffuse UVs, with per-axis clamping standing in for the tiling flags.
    uv_coord = add("ShaderNodeTexCoord", NL_DIFF_COORD, -1700, 500)
    uv_split = add("ShaderNodeSeparateXYZ", NL_DIFF_SEP, -1500, 500)
    clamp_u = add("ShaderNodeClamp", NL_DIFF_CLAMP_U, -1320, 560)
    clamp_v = add("ShaderNodeClamp", NL_DIFF_CLAMP_V, -1320, 400)
    uv_join = add("ShaderNodeCombineXYZ", NL_DIFF_COMB, -1120, 500)
    diffuse = add("ShaderNodeTexImage", NL_DIFFUSE_TEX, -920, 500)

    links.new(uv_coord.outputs["UV"], uv_split.inputs["Vector"])
    links.new(uv_split.outputs["X"], clamp_u.inputs["Value"])
    links.new(uv_split.outputs["Y"], clamp_v.inputs["Value"])
    links.new(clamp_u.outputs["Result"], uv_join.inputs["X"])
    links.new(clamp_v.outputs["Result"], uv_join.inputs["Y"])
    links.new(uv_join.outputs["Vector"], diffuse.inputs["Vector"])

    # The material's own diffuse color. The game paints with it only where
    # there is no texture to paint with instead, so the mix below usually
    # passes the texture through untouched.
    tint = add("ShaderNodeMixRGB", NL_DIFF_TINT, -620, 500)
    tint.blend_type = "MULTIPLY"
    tint.inputs["Fac"].default_value = 0.0
    tint.inputs["Color1"].default_value = (1.0, 1.0, 1.0, 1.0)
    tint.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(diffuse.outputs["Color"], tint.inputs["Color1"])

    # ── Environment map ──────────────────────────────────────────────────────
    # One image node, reached either through the reflection direction or
    # through two axes of the vertex position, depending on the mapping mode.
    env_coord = add("ShaderNodeTexCoord", NL_ENV_COORD, -2100, -100)
    env_split = add("ShaderNodeSeparateXYZ", NL_ENV_SEP, -1900, -100)

    # The reflect modes' own arithmetic, rather than a ready-made spherical
    # projection: the game pushes the reflected direction away from one pole
    # axis and reads the map flat, and the maps it reads are nearly black with
    # a few bright streaks - so sampling them a different way does not blur
    # the highlight, it puts it somewhere else entirely.
    env_pole = add("ShaderNodeMath", NL_ENV_POLE, -1720, -60)
    env_pole.operation = "ADD"
    env_pole.inputs[1].default_value = 1.0
    env_floor = add("ShaderNodeMath", NL_ENV_FLOOR, -1560, -60)
    env_floor.operation = "MAXIMUM"
    env_floor.inputs[1].default_value = POLE_FLOOR
    env_root = add("ShaderNodeMath", NL_ENV_ROOT, -1400, -60)
    env_root.operation = "SQRT"
    env_proj = add("ShaderNodeMath", NL_ENV_PROJ, -1240, -60)
    env_proj.operation = "DIVIDE"
    env_proj.inputs[0].default_value = REFLECT_SPREAD
    env_proj_v = add("ShaderNodeMath", NL_ENV_PROJ_V, -1240, -240)
    env_proj_v.operation = "MULTIPLY"
    env_proj_v.inputs[1].default_value = 1.0

    env_u = add("ShaderNodeMath", NL_ENV_U, -1060, 60)
    env_v = add("ShaderNodeMath", NL_ENV_V, -1060, -160)
    for node in (env_u, env_v):
        node.operation = "MULTIPLY_ADD"
        node.inputs[2].default_value = 0.5

    links.new(env_pole.outputs["Value"], env_floor.inputs[0])
    links.new(env_floor.outputs["Value"], env_root.inputs[0])
    links.new(env_root.outputs["Value"], env_proj.inputs[1])
    links.new(env_proj.outputs["Value"], env_proj_v.inputs[0])
    links.new(env_proj.outputs["Value"], env_u.inputs[1])
    links.new(env_proj_v.outputs["Value"], env_v.inputs[1])

    env_join = add("ShaderNodeCombineXYZ", NL_ENV_COMB, -880, -100)
    env_mapping = add("ShaderNodeMapping", NL_ENV_MAPPING, -700, -100)
    env_tex = add("ShaderNodeTexImage", NL_ENV_TEX, -520, -100)
    env_tex.projection = "FLAT"
    env_tex.extension = "REPEAT"

    links.new(env_coord.outputs["Reflection"], env_split.inputs["Vector"])
    links.new(env_join.outputs["Vector"], env_mapping.inputs["Vector"])
    links.new(env_mapping.outputs["Vector"], env_tex.inputs["Vector"])

    env_mix = add("ShaderNodeMixRGB", NL_ENV_MIX, -380, 340)
    env_mix.blend_type = "MIX"
    env_mix.inputs["Fac"].default_value = 0.0
    env_mix.inputs["Color2"].default_value = (0.0, 0.0, 0.0, 1.0)
    links.new(tint.outputs["Color"], env_mix.inputs["Color1"])
    links.new(env_tex.outputs["Color"], env_mix.inputs["Color2"])

    # "Multiply twice" is the plain multiply with the result doubled, so it
    # rides on the same mix node rather than needing one of its own.
    env_scale = add("ShaderNodeMixRGB", NL_ENV_SCALE, -180, 340)
    env_scale.blend_type = "MULTIPLY"
    env_scale.inputs["Fac"].default_value = 0.0
    env_scale.inputs["Color2"].default_value = (2.0, 2.0, 2.0, 1.0)
    links.new(env_mix.outputs["Color"], env_scale.inputs["Color1"])

    # The half of the environment map that never sees a light. The game lights
    # the diffuse texture and only then puts the environment on top, so in the
    # blend and add modes the reflection arrives at full strength however dark
    # the surface under it is - which is what makes a glass read as frosted
    # rather than as a dim gray tube. The multiply modes are the exception:
    # there the reflection multiplies the lit surface, so it stays above.
    env_emit = add("ShaderNodeMixRGB", NL_ENV_EMIT, -180, 20)
    env_emit.blend_type = "MULTIPLY"
    env_emit.inputs["Fac"].default_value = 1.0
    env_emit.inputs["Color2"].default_value = (0.0, 0.0, 0.0, 1.0)
    links.new(env_tex.outputs["Color"], env_emit.inputs["Color1"])

    # Light the surface gives off on its own. The game adds the emission color
    # to the light landing on the surface and multiplies the texture by the
    # sum, so emission brightens the texture rather than washing a flat color
    # over it: a lit window shows its own panes, not a gray rectangle.
    self_light = add("ShaderNodeMixRGB", NL_SELF_LIGHT, -20, 20)
    self_light.blend_type = "MULTIPLY"
    self_light.inputs["Fac"].default_value = 1.0
    self_light.inputs["Color2"].default_value = (0.0, 0.0, 0.0, 1.0)
    links.new(env_scale.outputs["Color"], self_light.inputs["Color1"])

    emit_sum = add("ShaderNodeMixRGB", NL_EMIT_SUM, 160, 20)
    emit_sum.blend_type = "ADD"
    emit_sum.inputs["Fac"].default_value = 1.0
    links.new(self_light.outputs["Color"], emit_sum.inputs["Color1"])
    links.new(env_emit.outputs["Color"], emit_sum.inputs["Color2"])

    # ── Transparency sources ─────────────────────────────────────────────────
    # A separate alpha texture is folded down to one channel by weight, not by
    # taking a channel or averaging the three.
    alpha_tex = add("ShaderNodeTexImage", NL_ALPHA_TEX, -920, -700)
    alpha_luma = add("ShaderNodeVectorMath", NL_ALPHA_LUMA, -620, -700)
    alpha_luma.operation = "DOT_PRODUCT"
    alpha_luma.inputs[1].default_value = LUMA_WEIGHTS
    links.new(alpha_tex.outputs["Color"], alpha_luma.inputs[0])

    # Color key: transparent where the pixel matches the key on every channel
    # the game looks at, so the comparisons are multiplied together and the
    # result inverted.
    key_split = add("ShaderNodeSeparateColor", NL_KEY_SPLIT, -920, -180)
    key_r = add("ShaderNodeMath", NL_KEY_CMP_R, -740, -60)
    key_g = add("ShaderNodeMath", NL_KEY_CMP_G, -740, -200)
    key_b = add("ShaderNodeMath", NL_KEY_CMP_B, -740, -340)
    for node in (key_r, key_g, key_b):
        node.operation = "COMPARE"
        node.inputs[1].default_value, node.inputs[2].default_value = (
            key_window(0.0))

    key_and_a = add("ShaderNodeMath", NL_KEY_AND_A, -560, -130)
    key_and_a.operation = "MULTIPLY"
    key_and_b = add("ShaderNodeMath", NL_KEY_AND_B, -400, -130)
    key_and_b.operation = "MULTIPLY"
    key_invert = add("ShaderNodeMath", NL_KEY_INVERT, -240, -130)
    key_invert.operation = "SUBTRACT"
    key_invert.inputs[0].default_value = 1.0

    links.new(diffuse.outputs["Color"], key_split.inputs[0])
    links.new(key_split.outputs["Red"], key_r.inputs[0])
    links.new(key_split.outputs["Green"], key_g.inputs[0])
    links.new(key_split.outputs["Blue"], key_b.inputs[0])
    links.new(key_r.outputs["Value"], key_and_a.inputs[0])
    links.new(key_g.outputs["Value"], key_and_a.inputs[1])
    links.new(key_and_a.outputs["Value"], key_and_b.inputs[0])
    links.new(key_b.outputs["Value"], key_and_b.inputs[1])
    links.new(key_and_b.outputs["Value"], key_invert.inputs[1])

    # Whichever source the flags pick, scaled by the material's own opacity.
    # Input 0 is left free for that source; nothing wired means fully opaque.
    opacity_value = add("ShaderNodeValue", NL_OPACITY_VAL, -60, -320)
    opacity_value.outputs[0].default_value = mat.ls3d_opacity
    opacity_mul = add("ShaderNodeMath", NL_OPACITY_MUL, 120, -320)
    opacity_mul.operation = "MULTIPLY"
    opacity_mul.inputs[0].default_value = 1.0
    links.new(opacity_value.outputs[0], opacity_mul.inputs[1])

    # The cut-off a color-keyed pixel has to beat to be drawn at all.
    alpha_test = add("ShaderNodeMath", NL_ALPHA_TEST, 300, -320)
    alpha_test.operation = "GREATER_THAN"
    alpha_test.inputs[1].default_value = COLOR_KEY_ALPHA_REF
    links.new(opacity_mul.outputs["Value"], alpha_test.inputs[0])

    # ── The two ways a surface reaches the screen ────────────────────────────
    # Solid, keyed or faded: an ordinary shader with an alpha.
    bsdf = add("ShaderNodeBsdfPrincipled", NL_BSDF, 520, 300)
    _set_input(bsdf, ("Specular IOR Level", "Specular"), 0.0)
    _set_input(bsdf, ("Roughness",), 1.0)
    links.new(env_scale.outputs["Color"], bsdf.inputs["Base Color"])
    for name in ("Emission Color", "Emission"):
        if name in bsdf.inputs:
            links.new(emit_sum.outputs["Color"], bsdf.inputs[name])
            break
    _set_input(bsdf, ("Emission Strength",), 1.0)

    # Additive: light let through untouched, with the surface added on top of
    # it. That is what the game does, and it is the one thing an alpha cannot
    # express - an alpha darkens the background where this brightens it.
    glow_tint = add("ShaderNodeMixRGB", NL_GLOW_TINT, 120, -60)
    glow_tint.blend_type = "MULTIPLY"
    glow_tint.inputs["Fac"].default_value = 1.0
    glow_tint.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(env_scale.outputs["Color"], glow_tint.inputs["Color1"])

    # The reflection joins after the tint, not before it: the game puts the
    # environment map on the surface once the texture has already been through
    # the lighting stage, so nothing the surface is lit by reaches it.
    surface_sum = add("ShaderNodeMixRGB", NL_SURFACE_SUM, 240, -60)
    surface_sum.blend_type = "ADD"
    surface_sum.inputs["Fac"].default_value = 1.0
    links.new(glow_tint.outputs["Color"], surface_sum.inputs["Color1"])
    links.new(env_emit.outputs["Color"], surface_sum.inputs["Color2"])

    glow_premul = add("ShaderNodeMixRGB", NL_GLOW_PREMUL, 380, -60)
    glow_premul.blend_type = "MULTIPLY"
    glow_premul.inputs["Fac"].default_value = 1.0
    glow_premul.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(surface_sum.outputs["Color"], glow_premul.inputs["Color1"])
    links.new(opacity_mul.outputs["Value"], glow_premul.inputs["Color2"])

    glow_emit = add("ShaderNodeEmission", NL_GLOW_EMIT, 520, -60)
    glow_emit.inputs["Strength"].default_value = 1.0
    links.new(glow_premul.outputs["Color"], glow_emit.inputs["Color"])

    glow_clear = add("ShaderNodeBsdfTransparent", NL_GLOW_CLEAR, 520, -220)
    glow_add = add("ShaderNodeAddShader", NL_GLOW_ADD, 720, -100)
    links.new(glow_clear.outputs["BSDF"], glow_add.inputs[0])
    links.new(glow_emit.outputs["Emission"], glow_add.inputs[1])

    output = add("ShaderNodeOutputMaterial", NL_OUTPUT, 960, 100)
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    tree[GRAPH_STAMP] = GRAPH_VERSION
    sync_material_flags(mat)


# ── Flag synchronization ──────────────────────────────────────────────────────
def _render_method(mat, blended):
    """Point Blender's transparency setting at the right end of the scale."""
    mat.surface_render_method = "BLENDED" if blended else "DITHERED"


def blend_path(mat):
    """Which of the game's ways of drawing this material applies.

    ``"add"``, ``"fade"``, ``"key"``, ``"softkey"`` or ``"solid"``. The
    switches are tried in the order the game tries them and the first one set
    decides, so a material with several of them set is not ambiguous - it is
    additive.

    A keyed material with Diffuse MipMap on is the odd one out: the game loads
    its texture with a whole alpha channel rather than a single bit, and draws
    it blended, dropping only what is fully transparent. What that looks like
    is a cut-out whose edge softens with distance rather than one cut the same
    way at every size, and an emission color makes it additive the way it does
    any other blended surface.
    """
    flags = mat.ls3d_material_flags & 0xFFFFFFFF
    lit = any(channel != 0.0 for channel in mat.ls3d_emission_color)
    if flags & C.MTL_ALPHA_ADDITIVE:
        return "add"
    if (flags & C.MTL_ALPHATEX) or mat.ls3d_opacity < 1.0:
        # Any emission at all turns a faded surface additive.
        return "add" if lit else "fade"
    if flags & C.MTL_ALPHA_COLORKEY:
        if flags & C.MTL_DIFFUSE_MIPMAP:
            return "add" if lit else "softkey"
        return "key"
    return "solid"


#: What each of those four looks like on screen, for the panel to say out loud.
#: Which one a material lands on is easy to get wrong from the switches alone,
#: since they override one another and an emission color quietly joins in.
BLEND_PATH_LABELS = {
    "add": ("Additive", "Added to what is behind it, so dark areas vanish",
            "LIGHT_SUN"),
    "fade": ("Faded", "Blended with what is behind it", "MOD_OPACITY"),
    "key": ("Color Keyed", "Each pixel is drawn whole or cut away", "MOD_MASK"),
    "softkey": ("Color Keyed, Blended",
                "Cut at the key color, with edges that soften with distance",
                "IMAGE_ALPHA"),
    "solid": ("Solid", "Covers what is behind it", "SHADING_SOLID"),
}


def blend_path_reason(mat):
    """Why :func:`blend_path` landed where it did, or ``""`` when it is plain."""
    flags = mat.ls3d_material_flags & 0xFFFFFFFF
    lit = any(channel != 0.0 for channel in mat.ls3d_emission_color)
    mipmapped = bool(flags & C.MTL_DIFFUSE_MIPMAP)
    if flags & C.MTL_ALPHA_ADDITIVE:
        return ""
    if (flags & C.MTL_ALPHATEX) or mat.ls3d_opacity < 1.0:
        if lit:
            return "The emission color turns a faded surface additive."
        if not flags & C.MTL_ALPHATEX:
            return "Opacity below 1 fades the surface on its own."
    elif flags & C.MTL_ALPHA_COLORKEY:
        if mipmapped and lit:
            return ("Diffuse MipMap blends a keyed surface, and the emission "
                    "color turns a blended surface additive.")
        if mipmapped:
            return ("Diffuse MipMap gives a keyed texture a whole alpha "
                    "channel, so the game blends it instead of cutting it.")
        if lit:
            return "Emission lights a keyed surface but does not blend it."
    return ""


def sync_material_flags(mat):
    """Reconfigure an existing graph to match the material's current flags."""
    if mat is None or _held or not mat.use_nodes or mat.node_tree is None:
        return

    tree = mat.node_tree
    nodes = tree.nodes
    links = tree.links

    if tree.get(GRAPH_STAMP) != GRAPH_VERSION or find_node(nodes, NL_BSDF) is None:
        rebuild_material_nodes(mat)
        return

    flags = mat.ls3d_material_flags & 0xFFFFFFFF

    def flag(bit):
        return bool(flags & bit)

    diffuse_on = flag(C.MTL_DIFFUSE_ENABLE) and bool(mat.ls3d_diffuse_tex)
    color_key_on = flag(C.MTL_ALPHA_COLORKEY)
    alpha_in_diffuse = flag(C.MTL_ALPHA_IN_TEX)
    faded_on = flag(C.MTL_ALPHATEX)
    env_on = flag(C.MTL_ENV_ENABLE) and bool(mat.ls3d_env_tex)
    double_sided = flag(C.MTL_DIFFUSE_DOUBLESIDED)

    env_blend = (flags & C.MTL_ENV_BLEND_MASK) >> C.MTL_ENV_BLEND_SHIFT
    env_uv = (flags & C.MTL_ENV_UV_MASK) >> C.MTL_ENV_UV_SHIFT
    env_tile = max(flags & C.MTL_ENV_TILE_MASK, 1)

    output = find_node(nodes, NL_OUTPUT)
    bsdf = find_node(nodes, NL_BSDF)
    diffuse = find_node(nodes, NL_DIFFUSE_TEX)
    alpha_tex = find_node(nodes, NL_ALPHA_TEX)
    env_tex = find_node(nodes, NL_ENV_TEX)
    tint = find_node(nodes, NL_DIFF_TINT)
    env_coord = find_node(nodes, NL_ENV_COORD)
    env_split = find_node(nodes, NL_ENV_SEP)
    env_pole = find_node(nodes, NL_ENV_POLE)
    env_proj = find_node(nodes, NL_ENV_PROJ)
    env_proj_v = find_node(nodes, NL_ENV_PROJ_V)
    env_u = find_node(nodes, NL_ENV_U)
    env_v = find_node(nodes, NL_ENV_V)
    env_join = find_node(nodes, NL_ENV_COMB)
    env_mapping = find_node(nodes, NL_ENV_MAPPING)
    env_mix = find_node(nodes, NL_ENV_MIX)
    env_scale = find_node(nodes, NL_ENV_SCALE)
    env_emit = find_node(nodes, NL_ENV_EMIT)
    emit_sum = find_node(nodes, NL_EMIT_SUM)
    self_light = find_node(nodes, NL_SELF_LIGHT)
    key_r = find_node(nodes, NL_KEY_CMP_R)
    key_g = find_node(nodes, NL_KEY_CMP_G)
    key_b = find_node(nodes, NL_KEY_CMP_B)
    key_invert = find_node(nodes, NL_KEY_INVERT)
    alpha_luma = find_node(nodes, NL_ALPHA_LUMA)
    clamp_u = find_node(nodes, NL_DIFF_CLAMP_U)
    clamp_v = find_node(nodes, NL_DIFF_CLAMP_V)
    opacity_value = find_node(nodes, NL_OPACITY_VAL)
    opacity_mul = find_node(nodes, NL_OPACITY_MUL)
    alpha_test = find_node(nodes, NL_ALPHA_TEST)
    glow_tint = find_node(nodes, NL_GLOW_TINT)
    glow_add = find_node(nodes, NL_GLOW_ADD)

    def socket(node, key):
        if node is None:
            return None
        if isinstance(key, int):
            return node.inputs[key] if key < len(node.inputs) else None
        return node.inputs[key] if key in node.inputs else None

    def unplug(node, key):
        target = socket(node, key)
        if target is None:
            return
        for link in list(target.links):
            links.remove(link)

    def connect(from_socket, node, key):
        target = socket(node, key)
        if from_socket is None or target is None:
            return
        for link in target.links:
            if link.from_socket is from_socket:
                return
            links.remove(link)
        links.new(from_socket, target)

    # ── Textures ─────────────────────────────────────────────────────────────
    if diffuse:
        diffuse.image = mat.ls3d_diffuse_tex or None
        # A keyed texture is compared against one exact color, so a filtered
        # blend of two texels would key a fringe that is in neither of them.
        diffuse.interpolation = "Closest" if color_key_on else "Linear"
    if alpha_tex:
        alpha_tex.image = mat.ls3d_alpha_tex or None
        if alpha_tex.image:
            # Read as plain numbers: the game folds the raw bytes, and a color
            # transform on the way in would move every alpha it produces.
            alpha_tex.image.colorspace_settings.name = "Non-Color"
    if env_tex:
        env_tex.image = mat.ls3d_env_tex if env_on else None

    # ── Tiling ───────────────────────────────────────────────────────────────
    if clamp_u:
        no_tile_u = flag(C.MTL_DISABLE_U_TILING)
        clamp_u.inputs["Min"].default_value = 0.0 if no_tile_u else -UNCLAMPED
        clamp_u.inputs["Max"].default_value = 1.0 if no_tile_u else UNCLAMPED
    if clamp_v:
        no_tile_v = flag(C.MTL_DISABLE_V_TILING)
        clamp_v.inputs["Min"].default_value = 0.0 if no_tile_v else -UNCLAMPED
        clamp_v.inputs["Max"].default_value = 1.0 if no_tile_v else UNCLAMPED

    mat.use_backface_culling = not double_sided

    # ── Surface color ────────────────────────────────────────────────────────
    # The game paints with the material's diffuse color only where there is no
    # texture to paint with instead, or where Vertex Colors asks for both.
    if tint:
        colored = flag(C.MTL_DIFFUSE_COLORED)
        if diffuse_on:
            connect(diffuse.outputs["Color"], tint, "Color1")
        else:
            unplug(tint, "Color1")
            tint.inputs["Color1"].default_value = (1.0, 1.0, 1.0, 1.0)
        diffuse_color = mat.ls3d_diffuse_color
        tint.inputs["Color2"].default_value = (diffuse_color[0], diffuse_color[1],
                                               diffuse_color[2], 1.0)
        tint.inputs["Fac"].default_value = (
            1.0 if (colored or not diffuse_on) else 0.0)

    # ── Environment map ──────────────────────────────────────────────────────
    if env_tex and env_mapping and env_coord and env_join and env_split:
        unplug(env_join, "X")
        unplug(env_join, "Y")
        reflect = REFLECT_AXES.get(env_uv)
        if reflect is not None:
            # The reflected direction, pushed away from one pole axis and read
            # flat. The repeat count widens that spread rather than tiling the
            # map, which is what the game does with it here.
            pole, across, down, sign = reflect
            connect(env_coord.outputs["Reflection"], env_split, "Vector")
            connect(env_split.outputs[pole], env_pole, 0)
            connect(env_split.outputs[across], env_u, 0)
            connect(env_split.outputs[down], env_v, 0)
            env_proj.inputs[0].default_value = REFLECT_SPREAD * env_tile
            env_proj_v.inputs[1].default_value = sign
            links.new(env_u.outputs["Value"], env_join.inputs["X"])
            links.new(env_v.outputs["Value"], env_join.inputs["Y"])
            env_mapping.inputs["Scale"].default_value = (1.0, 1.0, 1.0)
        else:
            # Two axes of the vertex position, straight onto U and V.
            first, second = PLANAR_AXES.get(env_uv, PLANAR_AXES[C.ENV_UV_PLANAR_XY])
            connect(env_coord.outputs["Object"], env_split, "Vector")
            links.new(env_split.outputs[first], env_join.inputs["X"])
            links.new(env_split.outputs[second], env_join.inputs["Y"])
            repeat = 1.0 / env_tile
            env_mapping.inputs["Scale"].default_value = (repeat, repeat, 1.0)

    if env_mix and env_scale and env_emit:
        # 0 none, 1 by amount, 2 by the surface's own alpha, 3 multiply,
        # 4 multiply doubled, 5 add.
        #
        # The game lights the diffuse texture first and combines the
        # environment map with the result, so the two multiply modes are the
        # only ones where the reflection ends up lit. In the other two it
        # arrives whole, which is why they go to the emission input: a dark
        # glass still catches a bright reflection.
        unplug(env_mix, "Fac")
        unplug(env_mix, "Color2")
        unplug(env_emit, "Color2")
        lit_mix, free_mix = "MIX", (0.0, 0.0, 0.0, 1.0)
        if not env_on or env_blend not in C.ENV_BLENDS_DRAWN:
            # Off, or a value the game has no blend for: what it draws then
            # depends on the surface drawn before, so there is nothing definite
            # to show.
            env_mix.inputs["Fac"].default_value = 0.0
        elif env_blend == C.ENV_BLEND_AMOUNT:
            # What is left of the surface, plus that much of the reflection.
            amount = mat.ls3d_env_amount
            env_mix.inputs["Fac"].default_value = amount
            free_mix = (amount, amount, amount, 1.0)
        elif env_blend == C.ENV_BLEND_SURFACE_ALPHA:
            # The same, with the surface's own transparency as the amount -
            # the finished one, opacity and all, not the texture's alone.
            env_mix.inputs["Fac"].default_value = 1.0
            free_mix = (1.0, 1.0, 1.0, 1.0)
            if opacity_mul:
                connect(opacity_mul.outputs["Value"], env_mix, "Fac")
                connect(opacity_mul.outputs["Value"], env_emit, "Color2")
        elif env_blend == C.ENV_BLEND_ADD:
            # Added whole. The amount is not read here - the game has nowhere
            # to put it in this mode - so a low one does not dim it.
            env_mix.inputs["Fac"].default_value = 0.0
            free_mix = (1.0, 1.0, 1.0, 1.0)
        elif env_blend in (C.ENV_BLEND_MULTIPLY, C.ENV_BLEND_MULTIPLY_TWICE):
            # Twice is the same multiply, doubled by the scale node below.
            lit_mix = "MULTIPLY"
            env_mix.inputs["Fac"].default_value = 1.0
            connect(env_tex.outputs["Color"], env_mix, "Color2")
        env_mix.blend_type = lit_mix
        # Mixing toward black is what takes the blended modes' share out of the
        # lit surface; the multiply modes reach Color2 through the link above.
        if lit_mix == "MIX":
            env_mix.inputs["Color2"].default_value = (0.0, 0.0, 0.0, 1.0)
        if not env_emit.inputs["Color2"].links:
            env_emit.inputs["Color2"].default_value = free_mix
        env_scale.inputs["Fac"].default_value = (
            1.0 if (env_on and env_blend == C.ENV_BLEND_MULTIPLY_TWICE) else 0.0)

    # ── Color key ────────────────────────────────────────────────────────────
    if color_key_on and key_r and key_g and key_b:
        # Read off the texture every time, because that is the only thing the
        # key ever comes from - it is palette entry 0 of the diffuse image, not
        # something anyone types. Sampling it once and keeping it left the key
        # on whichever texture the material happened to have first: point the
        # material at a different one and the old texture's key went on
        # deciding what was see-through.
        #
        # A file that cannot be read at all gives None rather than black, and
        # then the stored key stands: an import samples it while the texture
        # folder is set, and losing that later when the folder is cleared would
        # be worse than keeping a key that may be stale.
        sampled = read_bmp_color_key(mat.ls3d_diffuse_tex)
        if sampled is not None and tuple(mat.ls3d_color_key) != sampled:
            mat.ls3d_color_key = sampled
        key = tuple(mat.ls3d_color_key)
        for node, component in ((key_r, key[0]), (key_g, key[1]), (key_b, key[2])):
            node.inputs[1].default_value, node.inputs[2].default_value = (
                key_window(component))
        # A texture with no palette has no key entry either, and the game ends
        # up testing its blue channel against zero on its own. Widening the
        # other two comparisons until they always match leaves blue deciding.
        paletted = has_color_palette(mat.ls3d_diffuse_tex)
        if paletted is False:
            key_r.inputs[2].default_value = ALWAYS_MATCHES
            key_g.inputs[2].default_value = ALWAYS_MATCHES
            key_b.inputs[1].default_value, key_b.inputs[2].default_value = (
                key_window(0.0))

    # ── Transparency ─────────────────────────────────────────────────────────
    # One source feeds the alpha, picked the way the game picks which kind of
    # transparency to load into the texture. Reset first, so a cleared flag
    # actually removes its wire.
    if opacity_value:
        # Not the material's own number: the one its meshes hand it.
        opacity_value.outputs[0].default_value = effective_opacity(mat)
    if opacity_mul:
        unplug(opacity_mul, 0)
        opacity_mul.inputs[0].default_value = 1.0
        if color_key_on and diffuse_on and key_invert:
            links.new(key_invert.outputs["Value"], opacity_mul.inputs[0])
        elif faded_on and alpha_in_diffuse and diffuse_on:
            links.new(diffuse.outputs["Alpha"], opacity_mul.inputs[0])
        elif faded_on and alpha_luma and mat.ls3d_alpha_tex:
            links.new(alpha_luma.outputs["Value"], opacity_mul.inputs[0])

    path = blend_path(mat)

    # An additive surface has no lights on it worth speaking of in the game
    # either: what it carries is its emission color, and the texture is
    # multiplied by that. Where there is no emission, the texture stands on its
    # own rather than going black - the game would be lighting it from the
    # scene, and a preview has no way to know what that came to.
    emission = mat.ls3d_emission_color
    self_lit = any(channel != 0.0 for channel in emission)
    if glow_tint:
        glow_tint.inputs["Color2"].default_value = (
            (emission[0], emission[1], emission[2], 1.0) if self_lit
            else (1.0, 1.0, 1.0, 1.0))

    # The same color on a surface the game does light: it is added to what the
    # lights bring and the texture is multiplied by the sum, so it brightens
    # the texture instead of laying a flat color over it.
    if self_light:
        self_light.inputs["Color2"].default_value = (
            emission[0], emission[1], emission[2], 1.0)
    _set_input(bsdf, ("Emission Strength",), 1.0)

    if bsdf:
        unplug(bsdf, "Alpha")
        if path == "key" and alpha_test:
            # Drawn solid or not at all - never part way.
            links.new(alpha_test.outputs["Value"], bsdf.inputs["Alpha"])
        elif path in ("fade", "softkey") and opacity_mul:
            # A blended surface, keyed or not, carries its alpha as it is.
            # The game softens a keyed one's edges with the mipmaps it makes;
            # what is drawn here is the edge at full size, which is where the
            # two agree.
            links.new(opacity_mul.outputs["Value"], bsdf.inputs["Alpha"])
        else:
            bsdf.inputs["Alpha"].default_value = 1.0

    if output:
        if path == "add" and glow_add:
            connect(glow_add.outputs["Shader"], output, "Surface")
        elif bsdf:
            connect(bsdf.outputs["BSDF"], output, "Surface")

    _render_method(mat, blended=path in ("add", "fade", "softkey"))

    # Follow the animation switch, last of all: the images assigned above are
    # the material's resting state, and playback moves the preview nodes off it
    # frame by frame. A material that can animate starts, one that no longer
    # can stops and is left where this function just put it.
    from . import ops_texanim
    ops_texanim.on_flag_toggled(mat)


def new_material(name="4ds_material"):
    """A material set up the way the game reads one, ready to be pointed at.

    Blender's own New button makes a material this addon knows nothing about:
    no flags, and none of the graph that previews what the game draws. Every
    place that offers to make one comes through here instead, so a material
    made on a projector is the same thing as one made on a mesh.
    """
    material = bpy.data.materials.new(name=name)
    material.ls3d_material_flags = (
        C.MTL_DIFFUSE_ENABLE | C.MTL_DIFFUSE_MIPMAP | C.MTL_ENV_TILE_DEFAULT)
    rebuild_material_nodes(material)
    return material


class LS3D_OT_CreateMaterial(bpy.types.Operator):
    """Create a new 4DS material, set up the way the game reads one"""

    bl_idname = "ls3d.create_material"
    bl_label = "New 4DS Material"
    bl_options = {"REGISTER", "UNDO"}

    #: Where the new material goes. Empty puts it in the mesh's next slot;
    #: "projector" hands it to the projector on the object; "glow" hands it to
    #: the lens flare element the list has picked.
    target: bpy.props.StringProperty(default="", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object
        if self.target == "projector":
            obj.ls3d_projector_material = new_material()
            made = obj.ls3d_projector_material
        elif self.target == "glow":
            index = obj.ls3d_glows_index
            if not 0 <= index < len(obj.ls3d_glows):
                self.report({"WARNING"}, "No lens flare element is picked.")
                return {"CANCELLED"}
            obj.ls3d_glows[index].material = new_material()
            made = obj.ls3d_glows[index].material
        else:
            if obj.type != "MESH":
                self.report({"WARNING"},
                            "A material slot belongs to a mesh.")
                return {"CANCELLED"}
            made = new_material()
            obj.data.materials.append(made)
            obj.active_material_index = len(obj.data.materials) - 1
        self.report({"INFO"}, f"Created '{made.name}'")
        return {"FINISHED"}


CLASSES = (LS3D_OT_CreateMaterial,)
