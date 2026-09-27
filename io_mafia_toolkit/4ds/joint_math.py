"""Joint transforms in 64-bit arithmetic, done the same way on import and export.

A bone keeps three things exactly: its head, its tail and its roll, each as a
32-bit number. Everything else Blender says about a bone - its matrices, its
local head - is worked out from those in Blender's own 32-bit arithmetic and
comes back a little different every time. So the import places a bone from the
joint's values here, in 64 bits, and hands Blender only the head, tail and roll
to round; and the export asks, for each joint, which 32-bit values put the bone
exactly where it is when placed the same way. Read back that way, a joint's
values give the same bone on the next import, and the same values on the export
after that - a model can go round as often as it likes without anything moving.

Frames are kept in Blender's axes, as ``Frame`` objects holding the linear part
(rotation with scale), the translation and the rotation chain alone.
"""

import math
import struct

_F = struct.Struct("<f")
_I = struct.Struct("<i")

#: How long an imported bone is. A joint is a point; the length only gives the
#: bone something to draw and to aim with.
BONE_LENGTH = 0.1


def f32(value):
    """*value* as a 32-bit float holds it."""
    return _F.unpack(_F.pack(value))[0]


def _step(value, steps):
    """The 32-bit float *steps* representable values away from *value*."""
    if steps == 0:
        return value
    bits = _I.unpack(_F.pack(value))[0]
    # Walk through the signed integer view, which orders positive and negative
    # floats the same way the values do, crossing zero through both zeros.
    if value == 0.0:
        smallest = 1.4012984643e-45
        magnitude = abs(steps) - 1
        start = smallest if steps > 0 else -smallest
        return _step(start, magnitude if steps > 0 else -magnitude)
    direction = 1 if (steps > 0) == (value > 0) else -1
    bits += direction * abs(steps)
    return _F.unpack(_I.pack(bits))[0]


def neighbors(values, radius):
    """Every tuple of 32-bit floats within *radius* steps of *values*, nearest first."""
    offsets = [0]
    for k in range(1, radius + 1):
        offsets += [k, -k]
    count = len(values)
    candidates = []

    def build(index, current, cost):
        if index == count:
            candidates.append((cost, tuple(current)))
            return
        for offset in offsets:
            current.append(_step(values[index], offset))
            build(index + 1, current, cost + abs(offset))
            current.pop()

    build(0, [], 0)
    candidates.sort(key=lambda item: item[0])
    return [values_ for _cost, values_ in candidates]


# ── 3x3 and vectors ───────────────────────────────────────────────────────────
def mat_mul(a, b):
    return [[a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
             for j in range(3)] for i in range(3)]


def mat_vec(m, v):
    return [m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3)]


def mat_inverse(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if det == 0.0:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def normalize(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return [c / length for c in v] if length else [0.0, 0.0, 0.0]


def quat_to_matrix(q):
    """A rotation matrix from a ``(w, x, y, z)`` quaternion, taken as it is."""
    w, x, y, z = q
    return [[1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y)],
            [2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)],
            [2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y)]]


def matrix_to_quat(m):
    """The unit ``(w, x, y, z)`` quaternion of a rotation matrix, w not negative."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        s = 0.5 / math.sqrt(trace + 1.0)
        q = [0.25 / s, (m[2][1] - m[1][2]) * s, (m[0][2] - m[2][0]) * s,
             (m[1][0] - m[0][1]) * s]
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = 2.0 * math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2])
        q = [(m[2][1] - m[1][2]) / s, 0.25 * s, (m[1][0] + m[0][1]) / s,
             (m[0][2] + m[2][0]) / s]
    elif m[1][1] > m[2][2]:
        s = 2.0 * math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2])
        q = [(m[0][2] - m[2][0]) / s, (m[1][0] + m[0][1]) / s, 0.25 * s,
             (m[2][1] + m[1][2]) / s]
    else:
        s = 2.0 * math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1])
        q = [(m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s,
             (m[2][1] + m[1][2]) / s, 0.25 * s]
    if q[0] < 0.0:
        q = [-c for c in q]
    length = math.sqrt(sum(c * c for c in q))
    return [c / length for c in q]


# ── Blender's bone orientation ────────────────────────────────────────────────
def axis_roll_matrix(axis, roll):
    """The orientation a bone with this axis and roll has, the way Blender builds it."""
    x, y, z = axis
    theta = 1.0 + y
    theta_alt = x * x + z * z
    if theta > 6.1e-3 or theta_alt > 2.5e-4 * 2.5e-4:
        if theta <= 6.1e-3:
            theta = theta_alt * 0.5 + theta_alt * theta_alt * 0.125
        base = [[1.0 - x * x / theta, x, -x * z / theta],
                [-x, y, -z],
                [-x * z / theta, z, 1.0 - z * z / theta]]
    else:
        base = [[-1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]]
    c, s = math.cos(roll), math.sin(roll)
    t = 1.0 - c
    turn = [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]
    return mat_mul(turn, base)


def roll_of(rotation):
    """The roll a bone needs to face the way *rotation* does."""
    axis = normalize([rotation[0][1], rotation[1][1], rotation[2][1]])
    unrolled = axis_roll_matrix(axis, 0.0)
    plain = [unrolled[0][2], unrolled[1][2], unrolled[2][2]]
    wanted = [rotation[0][2], rotation[1][2], rotation[2][2]]
    along = sum(a * w for a, w in zip(axis, wanted))
    wanted = normalize([w - along * a for a, w in zip(axis, wanted)])
    crossed = [plain[1] * wanted[2] - plain[2] * wanted[1],
               plain[2] * wanted[0] - plain[0] * wanted[2],
               plain[0] * wanted[1] - plain[1] * wanted[0]]
    return math.atan2(sum(c * a for c, a in zip(crossed, axis)),
                      sum(p * w for p, w in zip(plain, wanted)))


# ── Frames ────────────────────────────────────────────────────────────────────
class Frame:
    """A frame's whole world: linear part, translation, and rotations alone."""

    __slots__ = ("linear", "translation", "rotation")

    def __init__(self, linear=None, translation=None, rotation=None):
        identity = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.linear = linear or identity
        self.translation = translation or [0.0, 0.0, 0.0]
        self.rotation = rotation or [row[:] for row in identity]

    def child(self, location, rotation, scale):
        """The world of a frame hanging from this one with the given values.

        The rotation chain uses each rotation made unit length: a stored
        rotation is only unit to 32 bits, and down a limb those errors add up
        to a turn that is no longer a rotation, which a bone cannot hold.
        """
        turn = quat_to_matrix(rotation)
        local = [[turn[i][j] * scale[j] for j in range(3)] for i in range(3)]
        offset = mat_vec(self.linear, location)
        length = math.sqrt(sum(c * c for c in rotation)) or 1.0
        pure = quat_to_matrix([c / length for c in rotation])
        return Frame(mat_mul(self.linear, local),
                     [self.translation[i] + offset[i] for i in range(3)],
                     mat_mul(self.rotation, pure))

    def matrix(self):
        """As rows of a 4x4, for mathutils."""
        return [self.linear[0] + [self.translation[0]],
                self.linear[1] + [self.translation[1]],
                self.linear[2] + [self.translation[2]],
                [0.0, 0.0, 0.0, 1.0]]


def bone_rest(frame):
    """``(head, tail, roll)`` a joint's bone is given, before Blender rounds them."""
    head = [f32(c) for c in frame.translation]
    axis = normalize([frame.rotation[0][1], frame.rotation[1][1], frame.rotation[2][1]])
    tail = [f32(head[i] + axis[i] * BONE_LENGTH) for i in range(3)]
    return head, tail, f32(roll_of(frame.rotation))


# ── Reading joint values back off a bone ──────────────────────────────────────
#: How many rounding steps either way the export tries around its first guess.
SEARCH_RADIUS = 2


def _cell(value):
    """The range of 64-bit values that round to the 32-bit *value*, a little inside."""
    below = _step(value, -1)
    above = _step(value, 1)
    return (value - (value - below) * 0.45, value + (above - value) * 0.45)


def _axis_through(head, tail):
    """A direction from *head* whose point at bone length rounds to *tail*.

    The straight line from head to tail is the first try. Its length is not
    exactly the bone length once both ends are rounded, so its end can fall
    just outside the tail's rounding; then the end is pulled into that range
    and the direction taken again, until it lands.
    """
    cells = [_cell(c) for c in tail]
    target = list(tail)
    axis = normalize([tail[i] - head[i] for i in range(3)])
    for _ in range(12):
        axis = normalize([target[i] - head[i] for i in range(3)])
        end = [head[i] + axis[i] * BONE_LENGTH for i in range(3)]
        if all(f32(end[i]) == tail[i] for i in range(3)):
            return axis
        target = [min(max(end[i], cells[i][0]), cells[i][1]) for i in range(3)]
    return axis


def _places_head(parent, location, head):
    offset = mat_vec(parent.linear, location)
    return all(f32(parent.translation[i] + offset[i]) == head[i] for i in range(3))


def _places_axis(parent, rotation, head, tail, roll):
    length = math.sqrt(sum(c * c for c in rotation)) or 1.0
    turned = mat_mul(parent.rotation, quat_to_matrix([c / length for c in rotation]))
    axis = normalize([turned[0][1], turned[1][1], turned[2][1]])
    return (all(f32(head[i] + axis[i] * BONE_LENGTH) == tail[i] for i in range(3))
            and f32(roll_of(turned)) == roll)


def _ulp(value):
    return abs(_step(f32(value), 1) - f32(value))


def _rounded_coarse_first(exact, fit):
    """Round *exact* one value at a time, coarsest first, refitting the rest.

    Rounding a value with wide 32-bit steps can throw the result further than
    a value with fine steps ever could, and the fine values are the ones left
    free to take up the slack: after each rounding, *fit(values, fixed)*
    works the unrounded ones out again for what is now fixed.
    """
    values = list(exact)
    fixed = set()
    for index in sorted(range(len(values)), key=lambda i: -_ulp(values[i])):
        values[index] = f32(values[index])
        fixed.add(index)
        if len(fixed) < len(values):
            values = fit(values, fixed)
    return tuple(f32(v) for v in values)


def solve_location(parent, head):
    """The 32-bit position that puts a joint's head at *head*, nearest the true one."""
    target = [head[i] - parent.translation[i] for i in range(3)]
    linear = parent.linear
    # Each axis of the head only has to land within its own rounding, which
    # is far finer on a small coordinate than on a large one; the fit leans on
    # the axes that have the least room.
    weights = [1.0 / (_ulp(head[i]) or 1.0) for i in range(3)]

    def fit(values, fixed):
        free = [j for j in range(3) if j not in fixed]
        rest = [(target[i] - sum(linear[i][j] * values[j] for j in fixed)) * weights[i]
                for i in range(3)]
        columns = [[linear[i][j] * weights[i] for i in range(3)] for j in free]
        # Least squares over the free values: (A^T A) x = A^T b.
        gram = [[sum(a * b for a, b in zip(ci, cj)) for cj in columns] for ci in columns]
        right = [sum(a * b for a, b in zip(c, rest)) for c in columns]
        if len(free) == 1:
            solution = [right[0] / gram[0][0]] if gram[0][0] else [values[free[0]]]
        else:
            det = gram[0][0] * gram[1][1] - gram[0][1] * gram[1][0]
            if det == 0.0:
                return values
            solution = [(right[0] * gram[1][1] - right[1] * gram[0][1]) / det,
                        (right[1] * gram[0][0] - right[0] * gram[1][0]) / det]
        values = list(values)
        for j, value in zip(free, solution):
            values[j] = value
        return values

    exact = mat_vec(mat_inverse(linear), target)
    guess = _rounded_coarse_first(exact, fit)
    for candidate in neighbors(guess, SEARCH_RADIUS + 1):
        if _places_head(parent, candidate, head):
            return candidate, True
    return guess, False


def solve_rotation(parent, head, tail, roll):
    """The 32-bit rotation that gives a bone this tail and roll, nearest the true one."""
    world = axis_roll_matrix(_axis_through(head, tail), roll)
    exact = matrix_to_quat(mat_mul(mat_inverse(parent.rotation), world))

    def fit(values, fixed):
        # A rotation is the same at any length, so the free parts follow the
        # exact rotation at whatever length the rounded parts best fit.
        weight = sum(exact[i] * exact[i] for i in fixed)
        scale = (sum(values[i] * exact[i] for i in fixed) / weight) if weight else 1.0
        return [values[i] if i in fixed else exact[i] * scale for i in range(4)]

    guess = _rounded_coarse_first(exact, fit)
    for candidate in neighbors(guess, SEARCH_RADIUS):
        if _places_axis(parent, candidate, head, tail, roll):
            return candidate, True
    return guess, False


def nearest_joint(parent, head, tail, roll):
    """``(location, rotation)`` nearest a bone, without the exact search."""
    axis = normalize([tail[i] - head[i] for i in range(3)])
    world = axis_roll_matrix(axis, roll)
    rotation = tuple(f32(c) for c in matrix_to_quat(
        mat_mul(mat_inverse(parent.rotation), world)))
    difference = [head[i] - parent.translation[i] for i in range(3)]
    location = tuple(f32(c) for c in mat_vec(mat_inverse(parent.linear), difference))
    return location, rotation


def solve_joint(parent, head, tail, roll):
    """``(location, rotation)`` placing a bone exactly as it is.

    Where no 32-bit values put the bone exactly where it is - a bone drawn by
    hand, which is not the import's length - the nearest values are taken, and
    then the values that place *that* bone exactly: so what is written is
    already what a re-import comes back to.
    """
    location, placed_head = solve_location(parent, head)
    rotation, placed_axis = solve_rotation(parent, head, tail, roll)
    if placed_head and placed_axis:
        return location, rotation
    # These values place a bone exactly - just not this one. Read that bone
    # back the same way, keeping the values in hand wherever the search does
    # not find its own, since they are known to place it.
    head, tail, roll = bone_rest(parent.child(location, rotation, (1.0, 1.0, 1.0)))
    found_location, placed_head = solve_location(parent, head)
    found_rotation, placed_axis = solve_rotation(parent, head, tail, roll)
    return (found_location if placed_head else location,
            found_rotation if placed_axis else rotation)
