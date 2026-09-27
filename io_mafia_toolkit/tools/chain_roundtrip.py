"""Import and export a model over and over, and see what moves.

A single round trip proves the addon can read a file and write it back. It says
nothing about whether the *second* pass agrees with the first, which is the
property that actually matters: an addon that loses a little on every trip is
one a modeler notices only after the fifth save.

So this drives a chain. Generation 0 is the original file; each later
generation is the one before it, imported into a fresh Blender and exported
with no changes at all. Every generation is then compared, field by field,
against both the original and its immediate predecessor.

The comparison walks the parsed document rather than a hand-written list of
things worth checking, so every coordinate, normal, UV, index, flag byte, name
and matrix element is covered, including any field added later.

Reading a chain
---------------
``vs previous`` is the column that matters. Once it reads ``identical`` the
model has settled and will never drift again however many times it is saved. A
difference against generation 0 that does not grow is the cost of the first
trip through Blender - split vertices, recomputed planes - not a leak.

Usage (plain Python, not Blender)::

    python io_mafia_toolkit/tools/chain_roundtrip.py
        --blender "C:/Program Files/Blender Foundation/Blender 5.1/blender.exe"
        --models  "D:/Hry/Mafia Editovani/models/frank.4ds" ...
        --sample --generations 5
"""

import argparse
import collections
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import types

# The addon package's __init__ imports bpy, which does not exist out here.
# Registering a bare package object with the right __path__ lets the format
# submodules load on their own.
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE_NAME = os.path.basename(_PACKAGE_DIR)
if _PACKAGE_NAME not in sys.modules:
    _stub = types.ModuleType(_PACKAGE_NAME)
    _stub.__path__ = [_PACKAGE_DIR]
    sys.modules[_PACKAGE_NAME] = _stub
sys.path.insert(0, os.path.dirname(_PACKAGE_DIR))

from io_mafia_toolkit.packages import module               # noqa: E402
read_document = module("4ds.codec").read_document
read_animation = module("5ds.codec").read_animation

HERE = os.path.dirname(os.path.abspath(__file__))
STEP = os.path.join(HERE, "chain_step.py")

#: A float pair closer than this counts as unchanged. Coordinates are stored as
#: 32-bit floats, so a value that survives a trip through Blender's own doubles
#: and back can land a bit or two away without anything having moved.
ABSOLUTE_TOLERANCE = 1e-6
RELATIVE_TOLERANCE = 1e-6

#: How many field paths a single comparison prints before it summarizes.
REPORTED_FIELDS = 12

#: Field paths that are meant to change and say nothing about correctness. The
#: file records when it was written, so every export differs here by design.
IGNORED_FIELDS = frozenset({"timestamp"})

#: Decimal places geometry is rounded to before the order-insensitive
#: comparison. Loose enough to absorb the last bit of a 32-bit float, tight
#: enough that anything a modeler could see still shows up.
DIGEST_PLACES = 5

#: One step of Blender's own storage for custom split normals, which is a pair
#: of 16-bit numbers. Setting a normal and reading it straight back does not
#: give the same value back, and repeating that shifts a minority of them one
#: step further every pass, always the same way and without ever settling -
#: 7.13e-5 per pass, still 7.13e-5 per pass three hundred passes later, on a
#: mesh no addon has touched. So normals moving by this much are the floor of
#: what Blender can store rather than anything the addon did, but they are a
#: slide and not a wobble: the report says so rather than calling them stable.
NORMAL_QUANTIZATION_STEP = 2.0e-4

#: Field paths holding a direction, for the test above.
_NORMAL_FIELD = re.compile(r"\.normals?\[")

#: Indices in a field path, replaced when differences are grouped so ten
#: thousand moved vertices read as one line rather than ten thousand.
_INDEX = re.compile(r"\[\d+\]")


def flatten(value, path="", out=None):
    """Every leaf of *value* as ``{"frames[3].position[0]": 1.25, ...}``."""
    if out is None:
        out = {}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            flatten(getattr(value, field.name),
                    f"{path}.{field.name}" if path else field.name, out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            flatten(item, f"{path}[{index}]", out)
    elif isinstance(value, (bytes, bytearray)):
        out[path] = bytes(value).hex()
    else:
        out[path] = value
    return out


def same(left, right):
    """True when two leaves agree, floats to within the tolerances above."""
    if isinstance(left, float) or isinstance(right, float):
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            return left == right
        if a != a or b != b:                     # NaN on either side
            return a != a and b != b
        span = max(abs(a), abs(b))
        return abs(a - b) <= ABSOLUTE_TOLERANCE + RELATIVE_TOLERANCE * span
    return left == right


@dataclasses.dataclass
class Difference:
    """What changed under one field path, with the indices collapsed."""

    count: int = 0
    worst: float = 0.0
    example: str = ""


def compare(before, after):
    """Group every disagreement between two documents by field path."""
    left = flatten(before)
    right = flatten(after)
    groups = collections.defaultdict(Difference)

    for key in left.keys() | right.keys():
        if key in IGNORED_FIELDS:
            continue
        a = left.get(key, "<missing>")
        b = right.get(key, "<missing>")
        if same(a, b):
            continue
        group = groups[_INDEX.sub("[]", key)]
        group.count += 1
        try:
            delta = abs(float(a) - float(b))
        except (TypeError, ValueError):
            delta = float("inf")
        if delta > group.worst or not group.example:
            group.worst = delta
            group.example = f"{key}: {a!r} -> {b!r}"
    return groups


def only_normals_moved(groups):
    """True when every field still moving holds a direction.

    Deliberately not thresholded. Where the movement comes from is settled -
    it is Blender's storage, which no addon can hold still - so what the report
    owes the reader is the size of it, printed alongside, rather than a cutoff
    that quietly reclassifies a mesh whose normals happen to step further.
    """
    return bool(groups) and all(_NORMAL_FIELD.search(path) for path in groups)


def describe(groups):
    """The lines to print for one comparison, headline first."""
    if not groups:
        return ["identical"]
    ranked = sorted(groups.items(), key=lambda item: -item[1].count)
    total = sum(group.count for _, group in ranked)
    lines = [f"{total} value(s) differ across {len(groups)} field(s)"]
    for path, group in ranked[:REPORTED_FIELDS]:
        worst = ("structural" if group.worst == float("inf")
                 else f"worst {group.worst:.6g}")
        lines.append(f"{path}  x{group.count}  {worst}   {group.example}")
    if len(groups) > REPORTED_FIELDS:
        lines.append(f"... and {len(groups) - REPORTED_FIELDS} more field(s)")
    return lines


def _round(values):
    return tuple(round(float(v), DIGEST_PLACES) for v in values)


def geometry_digest(document):
    """What each frame's geometry *is*, with the numbering taken away.

    The field walk above compares slot for slot, so renumbering a mesh - which
    the exporter is free to do, and does whenever a vertex has to be split -
    reads as thousands of changed values even though nothing moved. This turns
    each detail level into the multiset of its vertices and the multiset of its
    triangles named by the corners they actually join, so a mesh compares equal
    to itself however it was numbered.

    Winding is kept: a triangle is rotated to start at its lowest corner rather
    than sorted, so a face turned inside out still counts as a change.
    """
    digest = {}
    seen = collections.Counter()
    for frame in document.frames:
        if frame.geometry is None:
            continue
        levels = []
        for lod in frame.geometry.lods:
            corners = [_round(v.position) for v in lod.vertices]
            points = sorted(
                (_round(v.position), _round(v.normal), _round(v.uv))
                for v in lod.vertices)
            triangles = []
            for group in lod.face_groups:
                for face in group.faces:
                    if max(face) >= len(corners):
                        triangles.append((group.material_id, "out of range"))
                        continue
                    ring = [corners[i] for i in face]
                    start = ring.index(min(ring))
                    triangles.append((group.material_id,
                                      tuple(ring[start:] + ring[:start])))
            levels.append((round(lod.distance, DIGEST_PLACES),
                           tuple(points), tuple(sorted(triangles))))
        # Keyed by name, not by position in the file: frame order is worked out
        # from the scene rather than carried over, so a frame that moved down
        # the list has not lost anything and should not read as though it had.
        seen[frame.name] += 1
        key = (frame.name if seen[frame.name] == 1
               else f"{frame.name}#{seen[frame.name]}")
        digest[key] = tuple(levels)
    return digest


def compare_geometry(before, after):
    """Per-frame geometry differences that survive renumbering."""
    left = geometry_digest(before)
    right = geometry_digest(after)
    notes = []
    for key in sorted(left.keys() | right.keys()):
        a, b = left.get(key), right.get(key)
        if a == b:
            continue
        if a is None or b is None:
            notes.append(f"{key}: geometry {'appeared' if a is None else 'vanished'}")
            continue
        if len(a) != len(b):
            notes.append(f"{key}: {len(a)} detail level(s) -> {len(b)}")
            continue
        for level, (one, two) in enumerate(zip(a, b)):
            if one == two:
                continue
            _, points_a, faces_a = one
            _, points_b, faces_b = two
            lost = len(set(points_a) - set(points_b))
            gained = len(set(points_b) - set(points_a))
            notes.append(
                f"{key} lod{level}: {len(points_a)}->{len(points_b)} vertices "
                f"({lost} gone, {gained} new), "
                f"{len(faces_a)}->{len(faces_b)} triangles, "
                f"{len(set(faces_a) ^ set(faces_b))} not shared")
    return notes


def parse(raw, path):
    """The right reader for whichever format this chain is running."""
    if str(path).lower().endswith(".5ds"):
        return read_animation(raw, os.path.basename(str(path)))
    return read_document(raw)


def compare_tracks(before, after):
    """Per-track differences that survive the tracks being reordered.

    The field walk compares slot for slot, so a track that merely moved down
    the list reads as every one of its keys having changed. Nothing depends on
    that order - the game finds a track by name, comparing strings until one
    matches - so this keys by name and looks at the keys themselves.
    """
    left = {track.name: track for track in before.tracks}
    right = {track.name: track for track in after.tracks}
    notes = []
    for name in sorted(left.keys() | right.keys()):
        one, two = left.get(name), right.get(name)
        if one is None or two is None:
            notes.append(f"'{name}': track "
                         f"{'appeared' if one is None else 'vanished'}")
            continue
        if one.flags != two.flags:
            notes.append(f"'{name}': flags 0x{one.flags:X} -> 0x{two.flags:X}")
        for channel in ("rotations", "positions", "scales", "notes",
                        "named_notes"):
            a, b = getattr(one, channel), getattr(two, channel)
            if len(a) != len(b):
                notes.append(f"'{name}' {channel}: {len(a)} -> {len(b)} key(s)")
                continue
            frames_moved = sum(1 for (fa, _), (fb, _) in zip(a, b) if fa != fb)
            worst = 0.0
            for (_, va), (_, vb) in zip(a, b):
                if isinstance(va, (int, float)):
                    worst = max(worst, abs(float(va) - float(vb)))
                elif isinstance(va, str):
                    worst = max(worst, 0.0 if va == vb else float("inf"))
                else:
                    worst = max(worst,
                                max(abs(x - y) for x, y in zip(va, vb)))
            if frames_moved or worst > ABSOLUTE_TOLERANCE:
                notes.append(f"'{name}' {channel}: {frames_moved} frame(s) "
                             f"moved, worst value change {worst:.3e}")
    return notes


def run_step(blender, source, target, sample=False, model=None):
    """One import/export in its own Blender, returning True on success."""
    command = [blender, "--background", "--factory-startup", "--python", STEP,
               "--", "--output", target]
    if model:
        command += ["--model", model]
    command += ["--sample"] if sample else ["--input", source]
    finished = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    if "CHAIN_STEP_OK" in (finished.stdout or ""):
        return True
    tail = "\n".join((finished.stdout or "").strip().splitlines()[-8:])
    print(f"      step failed:\n{tail}")
    return False


def report(label, groups):
    lines = describe(groups)
    print(f"    {label}: {lines[0]}")
    for line in lines[1:]:
        print(f"        {line}")


def chain(blender, name, origin, out_dir, generations, sample=False,
          model=None):
    """Run one model or animation through the chain, reporting every step."""
    print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
    workspace = os.path.join(out_dir, re.sub(r"[^\w.-]", "_", name))
    os.makedirs(workspace, exist_ok=True)
    suffix = ".5ds" if model else ".4ds"

    files = [os.path.join(workspace, f"gen0{suffix}")]
    if sample:
        if not run_step(blender, None, files[0], sample=True):
            return False
    else:
        shutil.copyfile(origin, files[0])

    for generation in range(1, generations + 1):
        target = os.path.join(workspace, f"gen{generation}{suffix}")
        if not run_step(blender, files[-1], target, model=model):
            return False
        files.append(target)

    documents = []
    for path in files:
        with open(path, "rb") as handle:
            raw = handle.read()
        documents.append((raw, parse(raw, path)))

    settled = None
    drifting = 0.0
    ok = True
    for generation in range(1, len(documents)):
        raw, document = documents[generation]
        previous_raw, previous = documents[generation - 1]
        origin_raw, first = documents[0]

        against_previous = compare(previous, document)
        against_origin = compare(first, document)
        bytes_origin = "same bytes" if raw == origin_raw else "different bytes"
        bytes_previous = ("same bytes" if raw == previous_raw
                          else "different bytes")

        print(f"\n  generation {generation}  ({len(raw)} bytes; "
              f"{bytes_origin} as gen0, {bytes_previous} as "
              f"gen{generation - 1})")
        report(f"vs gen{generation - 1}", against_previous)
        report("vs gen0     ", against_origin)

        shifted = (compare_tracks(first, document) if model
                   else compare_geometry(first, document))
        if shifted:
            what = "tracks" if model else "geometry"
            unit = "track(s)" if model else "frame level(s)"
            print(f"    {what} vs gen0 (ignoring order): "
                  f"{len(shifted)} {unit} changed")
            for line in shifted[:REPORTED_FIELDS]:
                print(f"        {line}")
            if len(shifted) > REPORTED_FIELDS:
                print(f"        ... and {len(shifted) - REPORTED_FIELDS} more")
        else:
            what = "tracks" if model else "geometry"
            print(f"    {what} vs gen0 (ignoring order): identical")

        normals_only = only_normals_moved(against_previous)
        quiet = not against_previous or normals_only
        if normals_only:
            step = max(group.worst for group in against_previous.values())
            drifting = max(drifting, step)
            floor = ("at" if step <= NORMAL_QUANTIZATION_STEP else "above")
            print(f"    (nothing moved but normals, worst {step:.2e}, {floor} "
                  f"Blender's storage floor - which slides one step per save "
                  f"and never settles)")
        if quiet and settled is None:
            settled = generation
        elif not quiet and settled is not None:
            print(f"    ! something other than normals moved again after "
                  f"generation {settled}")
            ok = False

    if settled is None:
        print(f"\n  NOT SETTLED: still changing at generation {generations}")
        ok = False
    elif drifting:
        print(f"\n  settled at generation {settled} in everything but normals, "
              f"which keep sliding up to {drifting:.2e} per save on Blender's "
              f"own storage")
    else:
        print(f"\n  settled at generation {settled}, unchanged from there on")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Chain a model through "
                                                 "repeated import/export.")
    parser.add_argument("--blender", required=True)
    parser.add_argument("--models", nargs="*", default=[],
                        help="4DS files to run through the chain")
    parser.add_argument("--sample", action="store_true",
                        help="also chain a scene built from the Add > 4DS menu")
    parser.add_argument("--animations", nargs="*", default=[],
                        metavar="MODEL=ANIMATION",
                        help="animation chains, each a .4ds and the .5ds to "
                             "load onto it, joined by an equals sign")
    parser.add_argument("--generations", type=int, default=5)
    parser.add_argument("--out", default=os.path.join(HERE, "_chain_output"))
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    results = {}
    if args.sample:
        results["<Add menu sample>"] = chain(
            args.blender, "<Add menu sample>", None, args.out,
            args.generations, sample=True)
    for path in args.models:
        results[os.path.basename(path)] = chain(
            args.blender, os.path.basename(path), path, args.out,
            args.generations)
    for pair in args.animations:
        model, _, animation = pair.partition("=")
        label = f"{os.path.basename(animation)} on {os.path.basename(model)}"
        results[label] = chain(args.blender, label, animation, args.out,
                               args.generations, model=model)

    print(f"\n{'=' * 78}")
    for name, ok in results.items():
        print(f"  {'stable  ' if ok else 'UNSTABLE'}  {name}")
    bad = [name for name, ok in results.items() if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} model(s) settled")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
