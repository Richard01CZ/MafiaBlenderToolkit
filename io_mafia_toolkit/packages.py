"""Reaching the format packages, whose names begin with a digit.

Python's ``import`` statement needs a name it can spell as an identifier, and
``4ds`` is not one - nor are ``5ds`` and ``6ds``. Inside a format package
ordinary relative imports work as usual; only crossing from outside needs to
come through here. ``tck`` is spellable and needs none of this, but goes the
same way when a caller happens to be reaching for several at once.
"""

import importlib


def module(path):
    """One module out of a format package.

    ``module("6ds.codec")`` is the shadow codec, ``module("5ds.io")`` the
    animation's Blender side, and so on. The path is relative to this package,
    so it reads the same as the import statement would if it could be written.
    """
    return importlib.import_module("." + path, __package__)
