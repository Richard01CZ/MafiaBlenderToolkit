"""The texture a projector paints, drawn where the game paints it.

A projector carries no mesh. It is a volume and a material, and the game works
out as it draws which surfaces fall inside that volume, maps the material's
texture across it and paints them. Blender has nothing of the kind: its own
lights cannot paint a decal, and a texture can only reach a surface through
that surface's own material, which is not where this one lives.

So the surfaces are drawn a second time here, inside the volume and nowhere
else, with the texture mapped across it the way the frame's transform maps it,
the depth falloff the mode asks for and the blend the mode asks for. Drawn
against the depth the viewport already has, so the paint lands on what can be
seen and stops at the first surface, as a projector does.

Nothing here reaches the file. It is a view of what a projector covers, and
the switch that turns it off changes only whether it is drawn.
"""

import struct

import bpy
import gpu
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from ..common import constants as C
from . import viewport

#: The volume's corners in its own terms: two units across, one unit along the
#: projection axis, which is local +Y.
_VOLUME_CORNERS = tuple(
    Vector((x * C.PROJECTOR_HALF_WIDTH, y * C.PROJECTOR_REACH,
            z * C.PROJECTOR_HALF_WIDTH))
    for x in (-1.0, 1.0) for y in (0.0, 1.0) for z in (-1.0, 1.0))

#: How many objects one projector paints before the rest are left out, and how
#: many triangles all of them together may come to. A projector covering a
#: whole mission would otherwise redraw it every time the view moves.
PAINTED_OBJECT_LIMIT = 80
PAINTED_TRIANGLE_LIMIT = 400000

#: Below this the texture is treated as a hole rather than paint. The game
#: draws a projector with its alpha test on and the bar set one step above
#: nothing, so only a texel that is fully clear is a hole - not anything merely
#: faint.
ALPHA_CUTOFF = 1.0 / 255.0
#: How far toward the eye the paint is pulled, as a fraction of the distance to
#: it. A surface drawn twice lands on the same depth both times, and the second
#: draw then wins or loses each pixel by chance, which speckles it. This is
#: enough to settle that and far too little to show through anything.
DEPTH_BIAS = 2.0e-5
#: How near a pixel has to be to the key color to count as that color. The
#: game's textures are paletted, so a keyed pixel is the key exactly; this is
#: room for the conversion into Blender's own float pixels.
COLOR_KEY_SLACK = 0.02

#: What the shader reads its settings out of. Four slots of sixteen bytes, so
#: every field lands where the layout rules put it without padding of its own.
_SETTINGS_STRUCT = """
struct ProjectorPaint {
  vec4 shape;       /* reach, half width, alpha cutoff, color key slack */
  vec4 tint;        /* the key color, and the depth bias in the fourth */
  ivec4 modes;      /* falloff, blended rather than added, straight on, keyed */
  ivec4 flags;      /* transparency kept in a second image */
};
"""

_VERTEX_SOURCE = """
void main()
{
    /* The object's own place is already in both matrices, so the position
       arrives in the terms each of them wants. */
    local = (to_projector * vec4(pos, 1.0)).xyz;
    gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);
    /* The same surface drawn a second time lands on the same depth, and two
       equal depths are a coin toss - which speckles the paint with whatever
       is underneath. Pulled a hair toward the eye so the paint wins its own
       pixels outright, by a hair being the point: enough to settle the tie,
       too little to reach through anything real. */
    gl_Position.z -= settings.tint.w * gl_Position.w;
}
"""

_FRAGMENT_SOURCE = """
void main()
{
    /* Along the projection axis, from the near face to the far one. */
    float depth = local.y / settings.shape.x;
    if (depth < 0.0 || depth > 1.0) { discard; }

    /* Straight through at the same width, or spreading from the frame's own
       point, which is what the two volume shapes are. */
    float spread = (settings.modes.z != 0) ? 1.0 : max(depth, 1e-4);
    vec2 across = local.xz / (settings.shape.y * spread);
    if (abs(across.x) > 1.0 || abs(across.y) > 1.0) { discard; }

    vec2 uv = across * 0.5 + 0.5;
    vec2 at = vec2(uv.x, 1.0 - uv.y);
    vec4 paint_color = texture(paint, at);
    /* Where the material keeps its transparency in a second image, that is
       where the paint's own transparency comes from. */
    if (settings.flags.x != 0) { paint_color.a = texture(mask, at).r; }

    if (settings.modes.w != 0) {
        vec3 gap = abs(paint_color.rgb - settings.tint.rgb);
        if (max(gap.r, max(gap.g, gap.b)) <= settings.shape.w) { discard; }
    }
    if (paint_color.a < settings.shape.z) { discard; }

    float weight = 1.0;
    if (settings.modes.x == 1) { weight = 1.0 - depth; }
    else if (settings.modes.x == 2) { weight = 1.0 - abs(2.0 * depth - 1.0); }

    if (settings.modes.y != 0) {
        /* Drawn over the surface, hiding it by however opaque the texture is
           and however much of the falloff is left. */
        fragColor = vec4(paint_color.rgb, paint_color.a * weight);
    } else {
        /* Added to the surface in the game, which the viewport cannot do: an
           overlay is composited over the scene rather than summed into it. So
           the thing addition does is carried in the opacity instead - black
           adds nothing and so shows nothing, the brightest paint shows fully,
           and the falloff fades the paint out rather than blackening it,
           which is what fading a sum toward nothing looks like. */
        float peak = max(paint_color.r, max(paint_color.g, paint_color.b));
        if (peak * weight < (1.0 / 255.0)) { discard; }
        /* The colour goes on at its full strength and the brightness rides in
           the opacity. Dimming the colour instead would darken the surface on
           its way out, and what this stands for only ever brightens. */
        fragColor = vec4(paint_color.rgb / peak, peak * weight);
    }
}
"""

_shader = None
_batches = {}
_textures = {}


def shader():
    """The projection shader, built once."""
    global _shader
    if _shader is not None:
        return _shader
    interface = gpu.types.GPUStageInterfaceInfo("ls3d_projection")
    interface.smooth("VEC3", "local")

    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant("MAT4", "ModelViewProjectionMatrix")
    info.push_constant("MAT4", "to_projector")
    # Everything else rides in a block of its own. A backend only has to give
    # 128 bytes of push constants, and two matrices are exactly that; the
    # settings on top of them drew a warning on every build.
    info.typedef_source(_SETTINGS_STRUCT)
    info.uniform_buf(0, "ProjectorPaint", "settings")
    info.sampler(0, "FLOAT_2D", "paint")
    info.sampler(1, "FLOAT_2D", "mask")
    info.vertex_in(0, "VEC3", "pos")
    info.vertex_out(interface)
    info.fragment_out(0, "VEC4", "fragColor")
    info.vertex_source(_VERTEX_SOURCE)
    info.fragment_source(_FRAGMENT_SOURCE)
    _shader = gpu.shader.create_from_info(info)
    return _shader


def forget():
    """Drop what was built for the last draw. Anything changed is rebuilt."""
    _batches.clear()
    _textures.clear()


def _texture_for(image):
    """*image* as something the shader can sample, or ``None``."""
    if image is None:
        return None
    found = _textures.get(image.name)
    if found is None:
        try:
            found = gpu.texture.from_image(image)
        except Exception:
            return None
        _textures[image.name] = found
    return found


def _triangles_of(obj, depsgraph):
    """*obj*'s triangles in its own terms, as a batch, or ``None``.

    Read from the object as the scene evaluates it, so a mesh the modifiers
    change is painted the way it is seen. Kept in the object's own terms with
    its place handed to the shader, so moving it does not rebuild anything.
    """
    found = _batches.get(obj.name)
    if found is not None:
        return found
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except Exception:
        return None
    if mesh is None:
        return None
    try:
        mesh.calc_loop_triangles()
        if not mesh.loop_triangles:
            return None
        places = [tuple(vertex.co) for vertex in mesh.vertices]
        corners = [corner for triangle in mesh.loop_triangles
                   for corner in triangle.vertices]
        batch = batch_for_shader(shader(), "TRIS", {"pos": places},
                                 indices=[tuple(corners[at:at + 3])
                                          for at in range(0, len(corners), 3)])
        count = len(mesh.loop_triangles)
    finally:
        evaluated.to_mesh_clear()
    _batches[obj.name] = (batch, count)
    return _batches[obj.name]


def _world_bounds(corners):
    """The smallest box around *corners*, as (low, high)."""
    low = Vector((min(c.x for c in corners), min(c.y for c in corners),
                  min(c.z for c in corners)))
    high = Vector((max(c.x for c in corners), max(c.y for c in corners),
                   max(c.z for c in corners)))
    return low, high


def _overlaps(first, second):
    """Whether two (low, high) boxes share any room at all."""
    return all(first[0][axis] <= second[1][axis]
               and second[0][axis] <= first[1][axis] for axis in range(3))


def _painted_by(projector, objects):
    """The mesh objects a projector's volume reaches into."""
    place = projector.matrix_world
    volume = _world_bounds([place @ corner for corner in _VOLUME_CORNERS])
    found = []
    for obj in objects:
        if obj.type != "MESH" or not obj.visible_get():
            continue
        corners = [obj.matrix_world @ Vector(corner)
                   for corner in obj.bound_box]
        if _overlaps(volume, _world_bounds(corners)):
            found.append(obj)
            if len(found) >= PAINTED_OBJECT_LIMIT:
                break
    return found


def paint_of(projector):
    """What a projector paints: (image, mask, keyed, key color), or ``None``.

    A projector paints its material's diffuse texture and nothing else, so a
    projector with no material, or one whose material writes no diffuse
    texture, paints nothing at all - which is what the object panel and the
    export both say of it. Read the same way here, so what is drawn and what
    those two say can never disagree.
    """
    material = getattr(projector, "ls3d_projector_material", None)
    if material is None:
        return None
    flags = getattr(material, "ls3d_material_flags", 0) & 0xFFFFFFFF
    if not flags & C.MTL_DIFFUSE_ENABLE:
        return None
    image = getattr(material, "ls3d_diffuse_tex", None)
    if image is None:
        return None

    # What the game hands a projector is not the diffuse image on its own: a
    # material builds one texture and the projector draws that, so where the
    # material keeps its transparency in a second image, that is part of what
    # is painted. A color key, a truecolor texture carrying its own alpha, and
    # an additive material each settle the matter on their own and the second
    # image is left out - the same three the material itself leaves it out for.
    mask = None
    if flags & C.MTL_ALPHATEX and not flags & C.MTL_NO_ALPHA_MASK:
        mask = getattr(material, "ls3d_alpha_tex", None)

    keyed = bool(flags & C.MTL_ALPHA_COLORKEY)
    key = tuple(getattr(material, "ls3d_color_key", (0.0, 0.0, 0.0)))
    return image, mask, keyed, key


def blend_for(_projector):
    """How a projector's paint meets the surface under it.

    Both kinds are drawn over it. Half the game's modes add to the surface
    instead, and the viewport has no way to do that - what a draw handler puts
    down is composited over the scene, not summed into it - so the adding is
    carried in the paint's own opacity, which the shader works out. See the
    note there.
    """
    return "ALPHA"


def settings_block(projector, paint):
    """A projector's settings packed the way the shader's block reads them."""
    _image, mask, keyed, key = paint
    falloff = int(getattr(projector, "ls3d_projector_falloff", "0"))
    alpha = int(getattr(projector, "ls3d_projector_blend", "0"))
    orthogonal = bool(getattr(projector, "ls3d_projector_orthogonal", False))
    packed = struct.pack(
        "<4f4f4i4i",
        C.PROJECTOR_REACH, C.PROJECTOR_HALF_WIDTH, ALPHA_CUTOFF,
        COLOR_KEY_SLACK,
        key[0], key[1], key[2], DEPTH_BIAS,
        falloff, 1 if alpha else 0, 1 if orthogonal else 0, 1 if keyed else 0,
        1 if mask is not None else 0, 0, 0, 0)
    return gpu.types.GPUUniformBuf(packed)


def apply_paint(drawn, projector, paint):
    """Put everything about *projector* on the shader.

    The one place that turns a projector's settings into what is drawn, so the
    viewport and anything checking it can never disagree about what a mode
    means. The block is handed back as well as bound: it has to outlive the
    draw, and nothing else keeps hold of it.
    """
    block = settings_block(projector, paint)
    drawn.uniform_block("settings", block)
    return block


def _draw():
    """Paint every projector that can be seen, onto what it covers."""
    context = bpy.context
    scene = context.scene
    if not getattr(scene, C.PROJECT_TEXTURES_PROP, True):
        return
    space = getattr(context, "space_data", None)
    if space is not None and getattr(space, "type", "") == "VIEW_3D":
        if space.shading.type == "WIREFRAME":
            return

    projectors = [obj for obj in context.view_layer.objects
                  if viewport.is_projector(obj) and obj.visible_get()]
    if not projectors:
        return
    depsgraph = context.evaluated_depsgraph_get()
    objects = list(context.view_layer.objects)

    drawn = shader()
    gpu.state.depth_test_set("LESS_EQUAL")
    gpu.state.depth_mask_set(False)
    # Only the faces turned toward the eye take paint. With both sides
    # drawn, a thin surface has its front and its back landing on all
    # but the same depth, and the two flicker against each other.
    gpu.state.face_culling_set("BACK")
    try:
        painted_triangles = 0
        for projector in projectors[:viewport.PROJECTOR_LIMIT]:
            paint = paint_of(projector)
            if paint is None:
                continue
            image, mask, _keyed, _key = paint
            texture = _texture_for(image)
            if texture is None:
                continue
            # Every sampler the shader declares has to be bound, whether or
            # not the flag tells it to read one, so the diffuse stands in for
            # a material that keeps its transparency nowhere else.
            mask_texture = _texture_for(mask) if mask is not None else None
            if mask_texture is None:
                mask_texture = texture
            try:
                to_projector = projector.matrix_world.inverted()
            except ValueError:
                continue                      # flattened onto nothing

            # The blend goes on before the shader is bound, not after: the
            # backend settles a pipeline's blending as it binds, and anything
            # set past that point is read too late and the paint lands opaque.
            gpu.state.blend_set(blend_for(projector))
            drawn.bind()
            drawn.uniform_sampler("paint", texture)
            drawn.uniform_sampler("mask", mask_texture)
            # Kept in hand until the last batch is drawn: the block is read as
            # the draw runs, not as it is bound.
            block = apply_paint(drawn, projector, paint)

            for obj in _painted_by(projector, objects):
                built = _triangles_of(obj, depsgraph)
                if built is None:
                    continue
                batch, count = built
                if painted_triangles + count > PAINTED_TRIANGLE_LIMIT:
                    break
                painted_triangles += count
                # The object's own place rides in both matrices: Blender's
                # own, which the viewport matrix stack carries, and the one
                # that takes a point into the projector's terms.
                with gpu.matrix.push_pop():
                    gpu.matrix.multiply_matrix(obj.matrix_world)
                    drawn.uniform_float(
                        "to_projector", to_projector @ obj.matrix_world)
                    batch.draw(drawn)
    finally:
        gpu.state.blend_set("NONE")
        gpu.state.depth_mask_set(True)
        gpu.state.depth_test_set("NONE")
        gpu.state.face_culling_set("NONE")


_handle = None


@persistent
def _on_scene_changed(*_arguments):
    """Drop what was built, so the next draw reads the scene as it now is.

    It touches no ID and walks nothing - it empties two dictionaries - so it is
    safe in a handler that fires while a file is being torn down. Persistent,
    because Blender empties the handler lists when a file is opened and the
    paint would otherwise keep drawing the last scene's meshes.
    """
    forget()


def register():
    global _handle
    for handlers in (bpy.app.handlers.depsgraph_update_post,
                     bpy.app.handlers.load_post):
        if _on_scene_changed not in handlers:
            handlers.append(_on_scene_changed)
    if _handle is None:
        # Behind the outlines, so a volume's own lines stay readable over the
        # paint inside it.
        _handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), "WINDOW", "POST_VIEW")


def unregister():
    global _handle, _shader
    for handlers in (bpy.app.handlers.depsgraph_update_post,
                     bpy.app.handlers.load_post):
        while _on_scene_changed in handlers:
            handlers.remove(_on_scene_changed)
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    forget()
    _shader = None
