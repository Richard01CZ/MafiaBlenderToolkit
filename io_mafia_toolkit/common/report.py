"""Collecting and presenting the result of an import or export.

The original implementation kept six module-level globals and only ever showed
counts in the popup, so the actual messages were console-only. Here a single
:class:`Report` object accumulates structured entries, the popup lists them, and
the operator decides success or failure from ``report.failed`` rather than from
whether an exception happened to escape.
"""

import bpy

#: What every console line starts with. One stem so the addon's output can be
#: picked out of a shared console at a glance, and the format after it so a
#: message that does not name a file still says which side it came from.
CONSOLE_STEM = "MBT"
CONSOLE_PREFIX = f"[{CONSOLE_STEM}]"


def console_prefix(fmt=None):
    """``[MBT]``, or ``[MBT/5DS]`` when the format is known."""
    return f"[{CONSOLE_STEM}/{fmt}]" if fmt else CONSOLE_PREFIX

INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"

#: The report the popup operator renders. Set by :meth:`Report.begin`.
_active = None


class Report:
    """Messages gathered during one import or export run."""

    def __init__(self, title="", fmt=None):
        self.title = title
        #: Which format this run is about - "4DS", "5DS", "TCK" - or None for
        #: anything that spans them.
        self.fmt = fmt
        self.entries = []       # list of (level, message)
        self.fixes = []         # de-duplicated suggested remedies
        self.error_count = 0
        self.warning_count = 0
        #: Called with 0-100 as work proceeds. The operators point this at
        #: Blender's progress bar; outside Blender it stays a no-op, which is
        #: what lets the importer and exporter run headless in the test suite.
        self.progress_fn = None
        self._span = (0.0, 100.0)

    @property
    def prefix(self):
        """What this run's console lines start with."""
        return console_prefix(self.fmt)

    def as_format(self, fmt):
        """Tag this run's lines with another format for a stretch.

        A model export writes the animation beside it on the same report, and
        those lines belong to the animation rather than to the model.
        """
        report = self

        class _Switched:
            def __enter__(self):
                self.was = report.fmt
                report.fmt = fmt
                return report

            def __exit__(self, *_exception):
                report.fmt = self.was
                return False

        return _Switched()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def begin(self, title, fmt=None):
        global _active
        self.title = title
        if fmt is not None:
            self.fmt = fmt
        self.entries.clear()
        self.fixes.clear()
        self.error_count = 0
        self.warning_count = 0
        _active = self
        print()
        print("=" * 62)
        print(f"{self.prefix} {title}")
        print("=" * 62)
        return self

    def finish(self, title):
        self.title = title
        self.progress(100.0)
        print(f"{self.prefix} {title}")

    # ── progress ──────────────────────────────────────────────────────────────
    def progress(self, percent):
        """Report absolute progress, 0-100."""
        if self.progress_fn is None:
            return
        try:
            self.progress_fn(max(0.0, min(100.0, float(percent))))
        except Exception:
            self.progress_fn = None     # a broken sink must not stop the import

    def span(self, start, end):
        """Claim a slice of the bar for the phase that follows.

        Lets each phase count its own items from 0 to 1 through :meth:`step`
        without knowing where it sits in the run as a whole.
        """
        self._span = (float(start), float(end))
        self.progress(start)

    def step(self, index, total):
        """Report *index* of *total* items done inside the current span."""
        if not total:
            return
        start, end = self._span
        self.progress(start + (end - start) * (index / float(total)))

    def item(self, kind, index, total, description=""):
        """Name the one thing being worked on, and move the bar with it.

        ``[Mat 003/011] 'WOOD.BMP' flags 0x40808001``. The count is padded to
        the width of the total so the names line up in a column, which is what
        makes a long list readable when something goes wrong partway down it -
        the line before the traceback says which one it was on.
        """
        self.step(index, total)
        width = max(len(str(total)), 2)
        head = f"  [{kind} {index:0{width}d}/{total:0{width}d}]"
        print(f"{head} {description}" if description else head)

    @property
    def failed(self):
        return self.error_count > 0

    # ── message intake ────────────────────────────────────────────────────────
    def info(self, message):
        print(f"{self.prefix} {message}")

    def stage(self, message):
        print(f"--- {message} ---")

    def substage(self, message):
        print(f"  > {message}")

    def warn(self, message, fix=None):
        self.warning_count += 1
        self.entries.append((WARNING, message))
        print(f"{self.prefix} WARNING: {message}")
        if fix:
            self.suggest(fix)

    def error(self, message, fix=None):
        self.error_count += 1
        self.entries.append((ERROR, message))
        print(f"{self.prefix} ERROR: {message}")
        if fix:
            self.suggest(fix)

    def suggest(self, fix):
        if fix and fix not in self.fixes:
            self.fixes.append(fix)

    # ── presentation ──────────────────────────────────────────────────────────
    @property
    def icon(self):
        if self.error_count:
            return "CANCEL"
        if self.warning_count:
            return "ERROR"
        return "CHECKMARK"

    def show(self):
        """Open the summary popup, if a window manager is available."""
        try:
            bpy.ops.ls3d.result_popup("INVOKE_DEFAULT")
        except RuntimeError:
            pass    # headless / background run: console output is enough


class LS3D_OT_ResultPopup(bpy.types.Operator):
    """Summary of the last 4DS import or export"""

    bl_idname = "ls3d.result_popup"
    bl_label = "4DS Result"
    bl_options = {"INTERNAL"}

    #: Longer runs can produce hundreds of entries; the dialog shows this many.
    MAX_ENTRIES_SHOWN = 12

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        report = _active
        if report is None:
            layout.label(text="Nothing to report.")
            return

        layout.label(text=report.title, icon=report.icon)
        layout.separator()

        if report.error_count:
            layout.label(text=f"{report.error_count} error(s)", icon="CANCEL")
        if report.warning_count:
            layout.label(text=f"{report.warning_count} warning(s)", icon="ERROR")
        if not report.error_count and not report.warning_count:
            layout.label(text="No issues found.", icon="CHECKMARK")

        problems = [e for e in report.entries if e[0] in (ERROR, WARNING)]
        if problems:
            layout.separator()
            box = layout.column(align=True)
            for level, message in problems[:self.MAX_ENTRIES_SHOWN]:
                icon = "CANCEL" if level == ERROR else "ERROR"
                for line in _wrap(message, 78):
                    box.label(text=line, icon=icon)
                    icon = "BLANK1"
            hidden = len(problems) - self.MAX_ENTRIES_SHOWN
            if hidden > 0:
                box.label(text=f"... and {hidden} more (see the system console)",
                          icon="BLANK1")

        if report.fixes:
            layout.separator()
            layout.label(text="Suggested fixes:", icon="TOOL_SETTINGS")
            box = layout.box().column(align=True)
            for fix in report.fixes:
                for line in _wrap(fix, 78):
                    box.label(text=line)

        if report.error_count or report.warning_count:
            layout.separator()
            layout.label(text="Full details: Window > Toggle System Console",
                         icon="INFO")


def _wrap(text, width):
    """Split *text* into lines no wider than *width*, since Blender labels
    do not wrap on their own."""
    words = str(text).split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = "    " + word
    lines.append(current)
    return lines


CLASSES = (LS3D_OT_ResultPopup,)
