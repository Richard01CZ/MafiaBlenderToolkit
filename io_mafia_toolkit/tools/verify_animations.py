"""Validate the animation codec against a directory of real .5ds files.

Runs outside Blender. For every file it checks that:
  1. the file parses without error, and
  2. re-serializing the parsed animation reproduces the original bytes exactly.

Byte-exact round-tripping is a strong check: it proves the reader consumed
every field - nothing skipped, nothing double-counted - and that the writer
puts them back in the same order, at the same offsets, with the same padding.

Usage:
    python io_mafia_toolkit/tools/verify_animations.py <directory> [--verbose]
"""

import argparse
import collections
import os
import sys
import types

# The addon package's __init__ imports bpy, which does not exist outside
# Blender. Registering a bare package object with the right __path__ lets the
# submodules (and their relative imports) load on their own.
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE_NAME = os.path.basename(_PACKAGE_DIR)
if _PACKAGE_NAME not in sys.modules:
    _stub = types.ModuleType(_PACKAGE_NAME)
    _stub.__path__ = [_PACKAGE_DIR]
    sys.modules[_PACKAGE_NAME] = _stub
sys.path.insert(0, os.path.dirname(_PACKAGE_DIR))

from io_mafia_toolkit.packages import module           # noqa: E402
_codec = module("5ds.codec")
AnimationError = _codec.AnimationError
read_animation = _codec.read_animation
write_animation = _codec.write_animation


def animations_under(directory):
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if name.lower().endswith(".5ds"):
                yield os.path.join(root, name)


def report_folders(paths, directory):
    """Say which folders the files came from, when it is more than one.

    These verifiers walk a directory tree, so a stray folder sitting under the
    one being scanned - a backup of a few files, say - is silently folded into
    the total and quietly moves the numbers when someone shifts it. Naming the
    folders makes that visible instead.
    """
    import collections
    counts = collections.Counter(
        os.path.relpath(os.path.dirname(path), directory) for path in paths)
    if len(counts) <= 1:
        return
    print("  from:")
    for where, count in counts.most_common():
        print(f"    {count:5d}  {where if where != '.' else '(this folder)'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--verbose", action="store_true",
                        help="name every file as it is checked")
    args = parser.parse_args()

    exact = 0
    tracks = 0
    keys = 0
    unsupported = collections.Counter()
    examples = {}
    failures = []
    total = 0

    seen = []
    for path in animations_under(args.directory):
        seen.append(path)
        total += 1
        with open(path, "rb") as handle:
            raw = handle.read()
        try:
            animation = read_animation(raw, os.path.basename(path))
        except AnimationError as problem:
            reason = str(problem).split(": ", 1)[-1]
            unsupported[reason] += 1
            examples.setdefault(reason, os.path.basename(path))
            continue

        tracks += len(animation.tracks)
        keys += sum(len(track.rotations) + len(track.positions)
                    + len(track.scales) + len(track.notes)
                    for track in animation.tracks)
        if write_animation(animation) == raw:
            exact += 1
            if args.verbose:
                print(f"  ok       {path}")
        else:
            failures.append(path)
            print(f"  REWROTE DIFFERENTLY  {path}")

    print(f"\n{total} file(s) under {args.directory}")
    report_folders(seen, args.directory)
    print(f"  round-trip exact : {exact}")
    print(f"  unsupported      : {sum(unsupported.values())}")
    print(f"  failed           : {len(failures)}")
    print(f"  {tracks} track(s), {keys} key(s)")
    if unsupported:
        print("\n  breakdown:")
        for reason, count in unsupported.most_common():
            print(f"    {count:5d}  {reason[:70]}")
            print(f"           e.g. {examples[reason]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
