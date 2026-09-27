"""Panel pieces both formats' sidebars lean on.

Only what more than one format needs lives here - reading the addon
preferences, mainly. The panels themselves sit with the format they belong
to, in ``4ds/ui.py`` and ``5ds/ui.py``.
"""

def _show_raw_flags():
    """Whether the numeric flag fields are shown (addon preference)."""
    from . import get_preferences
    prefs = get_preferences()
    return getattr(prefs, "show_raw_flags", True) if prefs is not None else True


def _show_reserved_flags():
    """Whether the engine-owned bits get checkboxes (addon preference)."""
    from . import get_preferences
    prefs = get_preferences()
    return bool(getattr(prefs, "show_reserved_flags", False)) if prefs else False
