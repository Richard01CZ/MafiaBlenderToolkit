"""Build the installable zip, and put it into Blender.

Run with plain Python, not Blender::

    python io_mafia_toolkit/tools/build_addon.py
    python io_mafia_toolkit/tools/build_addon.py --release
    python io_mafia_toolkit/tools/build_addon.py --no-install
    python io_mafia_toolkit/tools/build_addon.py --blender "C:/.../blender.exe"

There are two builds, and ``build-dev.bat`` and ``build-release.bat`` beside
the package are the short way to each:

**The workshop build**, which is the default, holds the whole tree - the
proving suite, the corpus verifiers, this script - so a Blender it is
installed into can run the tests against exactly what is installed. Its zip is
named ``-dev`` so it is never mistaken for the other one.

**The release build**, ``--release``, holds the add-on and nothing else: no
``tools`` folder, nothing that writes to ``_test_output``. What a user
installs should be what a user needs, and a test suite inside somebody else's
Blender is neither wanted nor safe to run there.

With no ``--blender`` it finds the newest Blender installed, so a version
upgrade does not quietly leave the build going into the old one - which is
exactly what happened when 5.1 became 5.2 and the addon stayed behind in the
folder of a Blender that no longer existed.
"""

import argparse
import os
import re
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(HERE)
PACKAGE_NAME = os.path.basename(PACKAGE)
PROJECT = os.path.dirname(PACKAGE)

#: Folders that hold generated output rather than addon source.
SKIP_DIRS = {"__pycache__", "_test_output"}

#: Folders the release build leaves behind: the workshop, and everything in
#: it. Nothing the add-on runs imports any of this - it is the test suite, the
#: corpus verifiers and this script.
WORKSHOP_DIRS = {"tools"}

def _command_line():
    """The add-on's own command line, loaded as a plain file.

    It carries the search for an installed Blender, and this script needs the
    same one. Loading it by path rather than importing the package keeps bpy
    out of it - the package's own __init__ imports bpy, which is not here.
    """
    import importlib.util
    path = os.path.join(PACKAGE, "cli.py")
    spec = importlib.util.spec_from_file_location("ls3d_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


newest_blender = _command_line().newest_blender


def addon_version():
    """The version out of ``bl_info``, as the zip name spells it.

    Numbers join with dots and a label, where there is one, with a dash: so
    ``(1, 0, 0)`` gives ``1.0.0`` and ``(1, 0, 0, "rc1")`` would give
    ``1.0.0-rc1``. Blender's own version string joins the lot with dots
    instead, which is why this cannot simply reuse it.
    """
    source = open(os.path.join(PACKAGE, "__init__.py"), encoding="utf-8").read()
    found = re.search(r'"version"\s*:\s*\(([^)]*)\)', source)
    if not found:
        return "unknown"
    parts = [part.strip().strip("'\"") for part in found.group(1).split(",")
             if part.strip()]
    numbers = [part for part in parts if part.isdigit()]
    labels = [part for part in parts if not part.isdigit()]
    return ".".join(numbers) + ("".join(f"-{label}" for label in labels))


def source_files(release=False):
    """Every file that goes into the zip, as ``(on disk, in the zip)``."""
    leave_out = SKIP_DIRS | (WORKSHOP_DIRS if release else set())
    files = []
    for base, dirs, names in os.walk(PACKAGE):
        dirs[:] = sorted(d for d in dirs if d not in leave_out)
        for name in sorted(names):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(base, name)
            files.append((full, os.path.relpath(full, PROJECT).replace("\\", "/")))
    return files


def build(zip_path, release=False):
    files = source_files(release)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for full, arcname in files:
            archive.write(full, arcname)
    return len(files), os.path.getsize(zip_path)


#: Installing needs Blender's own preferences, so this runs without
#: --factory-startup and saves them afterwards - otherwise the enable is
#: forgotten the moment the process exits.
INSTALL = """
import bpy, sys
bpy.ops.preferences.addon_install(overwrite=True, filepath=r"{zip_path}")
bpy.ops.preferences.addon_enable(module="{package}")
bpy.ops.wm.save_userpref()
enabled = "{package}" in bpy.context.preferences.addons
print("LS3D_INSTALL_OK" if enabled else "LS3D_INSTALL_FAILED")
"""


def install(blender, zip_path):
    """Install and enable the zip. Returns True when Blender confirms it."""
    finished = subprocess.run(
        [blender, "--background", "--python-expr",
         INSTALL.format(zip_path=zip_path, package=PACKAGE_NAME)],
        capture_output=True, text=True, errors="replace")
    if "LS3D_INSTALL_OK" in finished.stdout:
        return True
    sys.stderr.write(finished.stdout[-2000:] + finished.stderr[-2000:])
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default="",
                        help="Blender to install into; default is the newest found")
    parser.add_argument("--no-install", action="store_true",
                        help="build the zip only")
    parser.add_argument("--out", default="",
                        help="where to write the zip; default is beside the package")
    parser.add_argument("--release", action="store_true",
                        help="build what ships: the add-on without the "
                             "workshop folder")
    args = parser.parse_args()

    label = "" if args.release else "-dev"
    name = f"mafia-toolkit-{addon_version()}{label}.zip"
    zip_path = args.out or os.path.join(PROJECT, name)
    count, size = build(zip_path, args.release)
    flavor = "release" if args.release else "workshop"
    left_out = len(source_files()) - count
    print(f"built {zip_path}: {flavor}, {count} files, {size} bytes"
          + (f", {left_out} workshop file(s) left out" if args.release else ""))

    if args.no_install:
        return 0

    blender = args.blender or newest_blender()
    if not blender:
        print("no Blender found to install into; built the zip only")
        return 0
    if install(blender, zip_path):
        print(f"installed and enabled in {blender}")
        return 0
    print(f"INSTALL FAILED into {blender}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
