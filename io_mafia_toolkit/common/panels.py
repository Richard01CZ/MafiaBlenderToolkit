"""Writing a sentence into a panel, broken to the width it is drawn at.

Blender draws a label as one line and cuts short whatever does not fit, with
no way to read the rest: a note written as one long string loses its end, and
a note broken by hand loses it as soon as somebody drags the sidebar narrower
than the person who wrote it. So the breaking is done here, against the width
the panel is actually being drawn at and the font it is actually drawn with.
"""

import blf
import bpy

#: What a note loses before its text starts: the panel's own padding, the box
#: it usually sits in, and the column an icon takes. Measured against the
#: panels this add-on draws, at the width Blender opens them.
PANEL_MARGIN = 75
ICON_COLUMN = 20
#: Never break below this: a sidebar dragged down to nothing would turn a
#: sentence into a column of single words.
MIN_ROOM = 110
#: What to assume when there is no region to measure against.
FALLBACK_ROOM = 240


def room(icon="NONE"):
    """How many pixels of a panel a label's text has to itself."""
    region = getattr(bpy.context, "region", None)
    width = getattr(region, "width", 0) or 0
    if width <= 0:
        return FALLBACK_ROOM
    taken = PANEL_MARGIN + (ICON_COLUMN if icon and icon != "NONE" else 0)
    return max(MIN_ROOM, width - taken)


def _font_ready():
    """Set the measuring font to the one panels are drawn with."""
    prefs = bpy.context.preferences
    try:
        points = prefs.ui_styles[0].widget.points
        scale = prefs.system.ui_scale
    except (AttributeError, IndexError):
        points, scale = 11.0, 1.0
    blf.size(0, points * scale)


#: About how many pixels a character takes, for when the font cannot be
#: measured - which is every run without a window, where nothing is drawn
#: anyway and only the breaking is worth keeping sane.
PIXELS_PER_LETTER = 7.0
#: Narrower than this a character cannot really be, so a font measuring below
#: it is not loaded and is not worth asking. Without a window Blender answers
#: with something far too small rather than with nothing at all, which is why
#: the test is on the width and not on whether there is an answer.
MIN_LETTER_PIXELS = 2.0
#: The word the font is tried on before it is trusted.
SAMPLE = "measuring"


def lines_for(text, width):
    """*text* broken into lines, none wider than *width* pixels.

    A word too wide to fit on a line of its own is left whole rather than cut:
    a name is worth showing even where it runs past the edge.
    """
    _font_ready()
    measured = (blf.dimensions(0, SAMPLE)[0]
                >= MIN_LETTER_PIXELS * len(SAMPLE))
    letters = max(12, int(width / PIXELS_PER_LETTER))
    lines = []
    line = ""
    for word in text.split():
        tried = f"{line} {word}" if line else word
        over = (blf.dimensions(0, tried)[0] > width if measured
                else len(tried) > letters)
        if line and over:
            lines.append(line)
            line = word
        else:
            line = tried
    if line:
        lines.append(line)
    return lines or [""]


def say(layout, text, icon="NONE"):
    """Write *text* down *layout*, broken where the panel runs out of room.

    The icon goes on the first line and the rest are held out level with it,
    the way a note written by hand would be.
    """
    for index, line in enumerate(lines_for(text, room(icon))):
        layout.label(text=line,
                     icon=icon if index == 0 else
                     ("BLANK1" if icon and icon != "NONE" else "NONE"))
