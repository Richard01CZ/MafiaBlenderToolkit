"""Pure-Python reader and writer for the LS3D 4DS container format.

Nothing in this package imports ``bpy``. It converts between raw bytes and the
plain dataclasses in :mod:`.types`, which means it can be exercised against a
directory of real game models without launching Blender — see
``tools/verify_corpus.py``.

Typical use::

    from io_mafia_toolkit.packages import module
    codec = module("4ds.codec")
    doc = codec.read_file("Tommy.4ds")
    doc.frames[0].name = "renamed"
    codec.write_file(doc, "Tommy_out.4ds")

All coordinates in the returned structures are in the file's own axis order
(X right, Y up, Z forward). Converting to Blender's Z-up convention is the
adapter layer's job.
"""

from .document import (
    UnsupportedFeatureError, current_filetime, read_document, read_file,
    write_document, write_file,
)
from .types import (
    Billboard, BoneGroup, Document, Dummy, FaceGroup, Frame, Geometry, Glow,
    Joint, LensFlare, Light, LOD, Material, Mirror, Morph, MorphLOD,
    MorphRegion,
    Occluder, Portal, Projector, Sector, Skin, SkinLOD, Target, Vertex,
)

__all__ = [
    "read_file", "read_document", "write_file", "write_document",
    "current_filetime", "UnsupportedFeatureError",
    "Document", "Frame", "Material", "Geometry", "LOD", "Vertex", "FaceGroup",
    "Skin", "SkinLOD", "BoneGroup", "Morph", "MorphLOD", "MorphRegion",
    "Sector", "Portal", "Mirror", "LensFlare", "Glow", "Billboard",
    "Projector", "Occluder", "Dummy", "Target", "Joint", "Light",
]
