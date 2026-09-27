"""The MBT CLI: driving the add-on from a command line, with or without a window.

A model, an animation, a movement track or a shadow can be opened, checked and
written back out without anybody clicking anything - for converting a folder
of files, for putting the add-on in a script, or just for looking at one model
quickly::

    mbt --textures "D:/Hry/Mafia Editovani/maps" --open Tommy.4ds --check
    mbt --open tommy.4ds --export out.4ds --option selection_only=True
    mbt --open tommy.4ds --open walk.5ds --save tommy.blend
    mbt --open tommy.4ds --gui

Nothing but Blender is needed: ``mbt.bat`` finds Blender itself and runs
this inside it, and Blender brings its own Python. The add-on can be installed
the same way, so a fresh machine needs no more than the zip and Blender::

    mbt --install
    mbt --install --textures "D:/Hry/Mafia Editovani/maps"

The same file is both the front door and the work. Run it with plain Python -
which only a workshop has to have - and it finds Blender and starts itself
again inside it; run it inside Blender and it does the work::

    python io_mafia_toolkit/cli.py --open Tommy.4ds
    blender --background --python io_mafia_toolkit/cli.py -- --open Tommy.4ds

Anything after ``--`` is what Blender hands the script, which is why the
arguments live there in the second form. Every path is made absolute before
Blender is started, because Blender is a program of its own and what counts as
"here" is not its business. The exit code is 0 when everything asked for was
done and 1 when something was refused, so a batch script can tell.
"""

import argparse
import contextlib
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_NAME = os.path.basename(HERE)
#: The folder the package sits in, which is what has to be on the import path
#: for the package to be importable by name.
PROJECT = os.path.dirname(HERE)

#: What each kind of file is opened and written with, by its own extension.
#: The names are the operators the menus use, so the command line and the
#: File menu go through exactly the same code.
FORMATS = {
    ".4ds": ("import_scene.4ds", "export_scene.4ds", "model"),
    ".5ds": ("import_scene.5ds", "export_scene.5ds", "animation"),
    ".tck": ("import_scene.tck", "export_scene.tck", "movement track"),
    ".6ds": ("import_scene.6ds", "export_scene.6ds", "shadow"),
}

#: Where Blender installs itself on Windows, newest last once sorted.
BLENDER_GLOBS = (
    r"C:\Program Files\Blender Foundation\Blender */blender.exe",
    r"C:\Program Files (x86)\Blender Foundation\Blender */blender.exe",
)

PREFIX = "[MBT/CLI]"


def say(message):
    # Flushed, because Blender's own output comes through a second pipe: an
    # unflushed line ends up printed after everything it was meant to head.
    print(f"{PREFIX} {message}", flush=True)


# ── finding Blender ──────────────────────────────────────────────────────────

def version_key(path):
    """Sort key from the folder name, so 5.10 lands above 5.2 rather than below."""
    found = re.search(r"Blender (\d+)\.(\d+)", path)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def newest_blender():
    """The newest Blender installed, or None when none can be found.

    Newest rather than first, so a version upgrade does not quietly leave
    everything pointing at the Blender that was there before it.
    """
    found = []
    for pattern in BLENDER_GLOBS:
        found.extend(glob.glob(pattern))
    return max(found, key=version_key) if found else None


# ── the arguments ────────────────────────────────────────────────────────────

def parser():
    made = argparse.ArgumentParser(
        prog="mbt",
        description="The MBT CLI. Open, check and write Mafia's models, "
                    "animations, movement tracks and shadows from a command "
                    "line.")
    made.add_argument("--open", metavar="FILE", action="append", default=[],
                      help="A .4ds, .5ds, .tck or .6ds to open. Repeat it to "
                           "open several - an animation onto the model it "
                           "belongs to, say - and they are opened in order.")
    made.add_argument("--export", metavar="FILE", action="append", default=[],
                      help="Where to write the scene, its kind read from the "
                           "name. Repeat it to write several.")
    made.add_argument("--save", metavar="FILE.blend",
                      help="Save the scene as a .blend as well.")
    made.add_argument("--textures", metavar="PATH",
                      help="The folder the textures are in, for this run "
                           "only. Without it the add-on's own setting stands.")
    made.add_argument("--option", metavar="NAME=VALUE", action="append",
                      default=[],
                      help="A setting for the import and export dialogs, "
                           "given as it is named there: selection_only, "
                           "write_animation, own_collection, load_animation "
                           "and the rest. Repeat it for more than one. A "
                           "setting the dialog does not have is refused.")
    made.add_argument("--install", metavar="ZIP OR FOLDER", nargs="?",
                      const="", default=None,
                      help="Install the add-on into Blender and switch it on. "
                           "With nothing after it, the copy this command "
                           "belongs to is the one installed. Given with "
                           "--textures, the texture folder is kept as well.")
    made.add_argument("--check", action="store_true",
                      help="Run every export check over the scene without "
                           "writing a model.")
    made.add_argument("--check-animation", action="store_true",
                      help="Run the animation checks over the scene.")
    made.add_argument("--gui", action="store_true",
                      help="Leave Blender open with the result on screen "
                           "instead of closing it. Only for a run started "
                           "from outside Blender.")
    made.add_argument("--blender", metavar="PATH",
                      help="Which Blender to run in. Without it the newest "
                           "one installed is used.")
    return made


def as_value(text):
    """``name=value`` the way the dialog means it: a switch, a number, a word."""
    if text.lower() in ("true", "yes", "on"):
        return True
    if text.lower() in ("false", "no", "off"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def options_from(given):
    """``["a=1", "b=off"]`` as ``{"a": 1, "b": False}``."""
    found = {}
    for pair in given:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--option wants name=value, not '{pair}'")
        found[name.strip()] = as_value(value.strip())
    return found


def format_for(path, doing):
    """The operators for a file's kind, or a complaint naming what is known."""
    kind = os.path.splitext(path)[1].lower()
    if kind not in FORMATS:
        known = ", ".join(sorted(FORMATS))
        raise ValueError(f"Cannot {doing} '{os.path.basename(path)}': the "
                         f"kinds are {known}")
    return FORMATS[kind]


# ── the work, inside Blender ─────────────────────────────────────────────────

def addon():
    """The add-on itself, registered and ready, however this run reached it."""
    import bpy
    if PACKAGE_NAME in bpy.context.preferences.addons:
        # Blender has it enabled already: that is the copy to drive, and
        # registering a second one would fight with it.
        return sys.modules.get(PACKAGE_NAME) or __import__(PACKAGE_NAME)
    if PROJECT not in sys.path:
        sys.path.insert(0, PROJECT)
    package = __import__(PACKAGE_NAME)
    package.register()
    return package


def preferences():
    """The add-on's own preferences, making a slot for them if there is none."""
    import bpy
    addons = bpy.context.preferences.addons
    entry = addons.get(PACKAGE_NAME)
    if entry is None:                       # registered by hand, from source
        entry = addons.new()
        entry.module = PACKAGE_NAME
    return entry.preferences


def call(idname, filepath, options):
    """Run one import or export operator, with the settings that apply to it.

    A setting the operator does not have is refused rather than dropped: a
    misspelled name that quietly does nothing is worse than a run that stops
    and says so.
    """
    import bpy
    area, _dot, name = idname.partition(".")
    operator = getattr(getattr(bpy.ops, area), name)
    known = set(operator.get_rna_type().properties.keys())
    unknown = [name for name in options if name not in known]
    if unknown:
        raise ValueError(f"{idname} has no setting called "
                         f"{', '.join(sorted(unknown))}")
    wanted = {name: value for name, value in options.items() if name in known}
    return operator(filepath=filepath, **wanted)


def attempt(doing, action):
    """Run one step, and say plainly when Blender refuses it.

    An operator whose poll says no raises rather than returning, and a run
    that dies on that says nothing useful and leaves an exit code that claims
    everything went fine.
    """
    try:
        return action() == {"FINISHED"}
    except RuntimeError as refusal:
        say(f"Refused - {doing}: {refusal}")
        return False


def packed(folder):
    """*folder* as a zip Blender can install, written where temporary files go.

    Blender installs an add-on from a zip or a single file, and a folder
    somebody unpacked is neither. Packing it again here is what lets the same
    command install the copy it was run from.
    """
    import tempfile
    import zipfile
    name = os.path.basename(folder.rstrip("\\/"))
    zip_path = os.path.join(tempfile.gettempdir(), f"{name}-to-install.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for base, dirs, names in os.walk(folder):
            dirs[:] = sorted(d for d in dirs
                             if d not in ("__pycache__", "_test_output"))
            for leaf in sorted(names):
                if leaf.endswith(".pyc"):
                    continue
                full = os.path.join(base, leaf)
                inside = os.path.join(name, os.path.relpath(full, folder))
                archive.write(full, inside.replace("\\", "/"))
    return zip_path


def install(source, textures=None):
    """Put the add-on into Blender, switch it on, and keep it that way.

    The preferences are saved here, unlike anywhere else in this command:
    installing is somebody setting their Blender up, and an add-on that is
    gone again the moment Blender closes is not installed.
    """
    import bpy
    # Run out of a zip that was never unpacked, installing with no name given
    # installs that zip itself rather than packing the copy unpacked from it.
    from_zip = os.environ.get("MAFIA_ZIP", "")
    if not source and from_zip and os.path.isfile(from_zip):
        source = from_zip
    source = os.path.abspath(source or HERE)
    if os.path.isdir(source):
        say(f"Packing {source}")
        source = packed(source)
    if not os.path.isfile(source):
        say(f"Nothing to install at {source}")
        return False
    say(f"Installing {source}")
    try:
        # Take the old copy out first. Installing over one writes the files
        # that are in the zip and leaves every file that is not, so a release
        # installed over a workshop build would keep the workshop's folders -
        # which is exactly what installing is meant to settle.
        if PACKAGE_NAME in bpy.context.preferences.addons:
            try:
                bpy.ops.preferences.addon_remove(module=PACKAGE_NAME)
                say("Took out the copy that was there")
            except RuntimeError:
                pass                    # never installed, or installed by hand
        bpy.ops.preferences.addon_install(overwrite=True, filepath=source)
        bpy.ops.preferences.addon_enable(module=PACKAGE_NAME)
    except RuntimeError as refusal:
        say(f"Refused - installing: {refusal}")
        return False
    if PACKAGE_NAME not in bpy.context.preferences.addons:
        say("Blender did not switch the add-on on")
        return False
    if textures:
        preferences().textures_path = textures
        say(f"Texture folder kept: {textures}")
    bpy.ops.wm.save_userpref()
    say("Installed and switched on")
    return True


def working_context():
    """A context the add-on's operators can work in, window or none.

    A script handed to a Blender that has a window runs *outside* that
    window's own context - `bpy.context.window` is empty even though a window
    is open - and an operator that polls for what is being looked at refuses.
    Switching an armature into Edit Mode is one, which is why importing a
    character through this command came out broken with a window open and
    perfect without one: in the background there is no window to be outside
    of, and the same operators are happy as they are.
    """
    import bpy

    manager = getattr(bpy.context, "window_manager", None)
    windows = list(getattr(manager, "windows", ()) or ())
    if not windows:
        return contextlib.nullcontext()
    window = windows[0]
    override = {"window": window, "screen": window.screen}
    area = next((a for a in window.screen.areas if a.type == "VIEW_3D"), None)
    if area is not None:
        override["area"] = area
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if region is not None:
            override["region"] = region
    return bpy.context.temp_override(**override)


def run(args):
    """Do what was asked, in order, and say how many steps were refused."""
    import bpy

    bpy.ops.wm.read_homefile(use_empty=True)
    with working_context():
        return _work(args)


def _work(args):
    """Everything asked for, inside a context its operators can work in."""
    import bpy

    options = options_from(args.option)
    refused_install = False
    if args.install is not None:
        folder = os.path.abspath(args.textures) if args.textures else None
        if folder and not os.path.isdir(folder):
            say(f"No such texture folder: {folder}")
            return 1
        refused_install = not install(args.install, folder)
    addon()

    if args.textures:
        folder = os.path.abspath(args.textures)
        if not os.path.isdir(folder):
            say(f"No such texture folder: {folder}")
            return 1
        preferences().textures_path = folder
        say(f"Textures: {folder}")

    refused = 1 if refused_install else 0
    for path in args.open:
        full = os.path.abspath(path)
        opener, _writer, kind = format_for(full, "open")
        if not os.path.isfile(full):
            say(f"No such {kind}: {full}")
            refused += 1
            continue
        say(f"Opening the {kind}: {full}")
        if not attempt(f"opening the {kind}",
                       lambda: call(opener, full, options)):
            refused += 1

    if args.check:
        say("Checking the scene")
        if not attempt("checking the scene", bpy.ops.ls3d.check_scene):
            refused += 1
    if args.check_animation:
        say("Checking the animation")
        if not attempt("checking the animation", bpy.ops.ls3d.check_animation):
            refused += 1

    for path in args.export:
        full = os.path.abspath(path)
        _opener, writer, kind = format_for(full, "write")
        folder = os.path.dirname(full)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        say(f"Writing the {kind}: {full}")
        if not attempt(f"writing the {kind}",
                       lambda: call(writer, full, options)):
            refused += 1

    if args.save:
        full = os.path.abspath(args.save)
        say(f"Saving the scene: {full}")
        if not attempt("saving the scene",
                       lambda: bpy.ops.wm.save_mainfile(filepath=full)):
            refused += 1

    say("Done" if not refused else f"Done, {refused} step(s) refused")
    return 1 if refused else 0


# ── starting Blender, from outside it ────────────────────────────────────────

def as_arguments(args):
    """The arguments written out again, every path of them made absolute.

    Blender is a program of its own, started from here: what counts as "here"
    is this command's business and not Blender's, so a name typed against the
    folder somebody is standing in is settled before it is handed over.
    """
    out = []
    if args.install is not None:
        out.append("--install")
        if args.install:
            out.append(os.path.abspath(args.install))
    if args.textures:
        out += ["--textures", os.path.abspath(args.textures)]
    for path in args.open:
        out += ["--open", os.path.abspath(path)]
    if args.check:
        out.append("--check")
    if args.check_animation:
        out.append("--check-animation")
    for path in args.export:
        out += ["--export", os.path.abspath(path)]
    if args.save:
        out += ["--save", os.path.abspath(args.save)]
    for pair in args.option:
        out += ["--option", pair]
    if args.gui:
        out.append("--gui")
    return out


def relaunch(args):
    """Start this same script inside Blender, and hand back its exit code."""
    blender = args.blender or newest_blender()
    if not blender or not os.path.isfile(blender):
        say("No Blender found. Give one with --blender.")
        return 2
    command = [blender]
    if not args.gui:
        command.append("--background")
    command += ["--python", os.path.abspath(__file__), "--"]
    command += as_arguments(args)
    say(f"Blender: {blender}")
    return subprocess.run(command).returncode


def inside_blender():
    try:
        import bpy                                              # noqa: F401
        return True
    except ImportError:
        return False


def main(argv=None):
    if argv is None:
        if inside_blender():
            # Blender hands the script everything after "--", and nothing at
            # all when there is no "--".
            argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
        else:
            argv = sys.argv[1:]
    args = parser().parse_args(argv)
    if not (args.open or args.export or args.save or args.check
            or args.check_animation or args.install is not None):
        parser().print_help()
        return 2
    try:
        if inside_blender():
            return run(args)
        return relaunch(args)
    except ValueError as complaint:           # a name or a kind nobody knows
        say(str(complaint))
        return 2


if __name__ == "__main__":
    code = main()
    if inside_blender() and "--gui" in sys.argv:
        # Leaving the window up is the whole point of --gui, and exiting a
        # startup script is how Blender is told to close.
        say("Blender stays open with this scene.")
    else:
        sys.exit(code)
