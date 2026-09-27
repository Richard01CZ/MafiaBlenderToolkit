"""Validate the pure-Python format layer against a directory of real .4ds files.

Runs outside Blender. For every file it checks that:
  1. the file parses without error, and
  2. re-serializing the parsed document reproduces the original bytes exactly.

Byte-exact round-tripping is a strong check: it proves the reader consumed every
field (nothing skipped or double-counted) and that the writer emits them in the
same order and size.

Usage:
    python -m io_mafia_toolkit.tools.verify_corpus <directory> [--verbose]
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

_format = __import__(f"{_PACKAGE_NAME}.4ds.codec", fromlist=["*"])
_binary = __import__(f"{_PACKAGE_NAME}.common.binary", fromlist=["*"])
_document = __import__(f"{_PACKAGE_NAME}.4ds.codec.document", fromlist=["*"])

FormatError = _binary.FormatError
read_document = _format.read_document
write_document = _format.write_document
UnsupportedFeatureError = _document.UnsupportedFeatureError


def first_difference(a, b):
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    return limit if len(a) != len(b) else -1


def check(path):
    """Return (status, detail). status is 'ok', 'unsupported', or 'fail'."""
    with open(path, "rb") as handle:
        original = handle.read()
    try:
        doc = read_document(original, os.path.basename(path))
    except UnsupportedFeatureError as exc:
        return "unsupported", str(exc)
    except (FormatError, Exception) as exc:
        return "fail", f"{type(exc).__name__}: {exc}"

    try:
        rebuilt = write_document(doc).getvalue()
    except Exception as exc:
        return "fail", f"write {type(exc).__name__}: {exc}"

    if rebuilt != original:
        offset = first_difference(original, rebuilt)
        return "fail", (f"round-trip differs at byte {offset} "
                        f"(original {len(original)} bytes, rebuilt {len(rebuilt)})")
    return "ok", ""



def report_folders(paths, directory):
    """Say which folders the files came from, when it is more than one.

    These verifiers walk a directory tree, so a stray folder sitting under the
    one being scanned - a backup of a few models, say - is silently folded
    into the total and quietly moves the numbers when someone shifts it. Naming
    the folders makes that visible instead.
    """
    import collections
    counts = collections.Counter(
        os.path.relpath(os.path.dirname(path), directory) for path in paths)
    if len(counts) <= 1:
        return
    print("  from:")
    for where, count in counts.most_common():
        print(f"    {count:5d}  {where if where != '.' else '(this folder)'}")

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    files = []
    for root, _dirs, names in os.walk(args.directory):
        for name in names:
            if name.lower().endswith(".4ds"):
                files.append(os.path.join(root, name))
    files.sort()
    if args.limit:
        files = files[:args.limit]

    counts = collections.Counter()
    reasons = collections.Counter()
    examples = {}
    for path in files:
        status, detail = check(path)
        counts[status] += 1
        if status != "ok":
            key = detail.split(":")[0][:70] if status == "fail" else detail[:70]
            reasons[key] += 1
            examples.setdefault(key, path)
            if args.verbose:
                print(f"{status.upper():12} {os.path.basename(path):32} {detail}")

    total = len(files)
    print(f"\n{total} file(s) under {args.directory}")
    report_folders(files, args.directory)
    print(f"  round-trip exact : {counts['ok']}")
    print(f"  unsupported      : {counts['unsupported']}")
    print(f"  failed           : {counts['fail']}")
    if reasons:
        print("\n  breakdown:")
        for key, count in reasons.most_common(20):
            print(f"    {count:5}  {key}")
            print(f"           e.g. {os.path.basename(examples[key])}")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
