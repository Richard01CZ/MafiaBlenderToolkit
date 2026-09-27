"""Rewriting BMP and TGA files into the form the game's image loader reads.

Nothing here imports ``bpy``: it is bytes in, bytes out, so every case can be
checked against real files without Blender in the way. The Blender side - the
buttons, the backups, reloading the images - is :mod:`.ops_texfix`.

The loader is narrow. For a BMP it reads the 14-byte file header and exactly 40
bytes of info header, then the palette straight after, then the pixel rows
straight after that, bottom row first, uncompressed, at 8, 16, 24 or 32 bits.
For a TGA it reads the 18-byte header, skips the id field and then reads the
pixels as they lie, left to right. A file that departs from any of that is read
out of step or refused, however well every other program shows it.

Every repair keeps what the image is:

- **Paletted images stay paletted.** An 8-bit texture's color key is palette
  entry 0, so turning one into 24-bit would lose its transparent color. 1-, 2-
  and 4-bit images and run-length packed ones become plain 8-bit with the same
  palette and the same index in every pixel.
- **Truecolor images keep their depth** where the loader can read it. Only
  bit-field layouts it cannot read are unpacked - to 32 bits when they carry
  alpha, 24 when they do not, and a 16-bit X1R5G5B5 layout is left as it is.
- **A header rewrite does not touch a pixel.** The commonest failure, a BMP
  saved with a 108- or 124-byte header, is fixed by replacing the header and
  copying the pixel rows across unchanged.
"""

import os
import struct
from dataclasses import dataclass

from ..common import constants as C

#: The BMP file header, in bytes.
BMP_FILE_HEADER_BYTES = 14
#: The OS/2 header: 16-bit sizes and three-byte palette entries.
BMP_CORE_HEADER_BYTES = 12
#: The OS/2 2.x header, whose compression numbers mean something else.
BMP_OS2_HEADER_BYTES = 64

BMP_RGB = 0
BMP_RLE8 = 1
BMP_RLE4 = 2
BMP_BITFIELDS = 3
BMP_ALPHABITFIELDS = 6
#: Compression names, for saying what a file uses.
BMP_COMPRESSION_NAMES = {
    BMP_RLE8: "RLE8", BMP_RLE4: "RLE4", BMP_BITFIELDS: "bit fields",
    4: "JPEG", 5: "PNG", BMP_ALPHABITFIELDS: "alpha bit fields",
}

#: The 16-bit layout the loader assumes for every 16-bit image.
BMP_X1R5G5B5_MASKS = (0x7C00, 0x03E0, 0x001F)
#: The 32-bit layout it assumes: blue, green, red, alpha bytes.
BMP_X8R8G8B8_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF)

#: What the repairs write into the resolution fields when the source has none.
#: The loader never reads them.
BMP_DEFAULT_RESOLUTION = 2835

TGA_HEADER_BYTES = 18
#: Run-length packed Targa types, and the plain type each unpacks to.
TGA_UNPACKED_TYPE = {packed: packed - 8 for packed in C.TGA_COMPRESSED_TYPES}


class RepairError(Exception):
    """The file cannot be rewritten into a form the game reads."""


@dataclass
class Plan:
    """What is wrong with one texture file that a repair can put right."""

    path: str
    #: Short, for the material panel: what is wrong, not how it is fixed.
    summary: str


# ── Finding out what needs doing ──────────────────────────────────────────────
def plan_repair(path):
    """A :class:`Plan` for the file at *path*, or ``None`` if it needs none.

    Only a BMP or TGA the repair can actually rewrite gets a plan. Anything
    else - a file that is fine, one that cannot be read, one using a packing the
    repair does not unpack - gets ``None``, and the export's own checks go on
    naming the problem.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension not in C.TEXTURE_EXTENSIONS:
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(BMP_FILE_HEADER_BYTES + 124 + 16)
    except OSError:
        return None
    try:
        issues = (_bmp_issues(head) if extension == ".bmp"
                  else _tga_issues(head))
    except RepairError:
        return None
    return Plan(path, "; ".join(issues)) if issues else None


def _bmp_issues(head):
    header = _bmp_header(head)
    issues = []
    if header["size"] != C.BMP_INFO_HEADER_BYTES:
        issues.append(f"{header['size']}-byte BMP header, the game reads "
                      f"{C.BMP_INFO_HEADER_BYTES}")
    elif (header["compression"] == BMP_RGB
          and header["offset"] != header["expected_offset"]):
        # Only worth saying on its own: a longer header or bit masks move the
        # pixels too, and those are already named.
        issues.append("BMP pixels are not where the game reads them")
    if header["compression"] != BMP_RGB:
        issues.append(f"{BMP_COMPRESSION_NAMES[header['compression']]} "
                      f"compressed BMP")
    if header["depth"] not in C.BMP_BIT_DEPTHS:
        issues.append(f"{header['depth']}-bit BMP")
    if header["height"] < 0:
        issues.append("BMP rows stored top to bottom")
    return issues


def _tga_issues(head):
    if len(head) < TGA_HEADER_BYTES:
        raise RepairError("too short to be a Targa")
    image_type = head[2]
    descriptor = head[17]
    if image_type not in (1, 2, 3) and image_type not in TGA_UNPACKED_TYPE:
        raise RepairError(f"Targa type {image_type}")
    issues = []
    if image_type in TGA_UNPACKED_TYPE:
        issues.append("run-length packed TGA")
    if descriptor & C.TGA_ORIGIN_RIGHT:
        issues.append("right-to-left TGA")
    return issues


# ── BMP ───────────────────────────────────────────────────────────────────────
def _bmp_header(data):
    """Every header field a repair needs, from however the file lays it out."""
    if len(data) < BMP_FILE_HEADER_BYTES + BMP_CORE_HEADER_BYTES \
            or data[:2] != b"BM":
        raise RepairError("not a BMP")
    offset = struct.unpack_from("<I", data, 10)[0]
    size = struct.unpack_from("<I", data, 14)[0]
    info = BMP_FILE_HEADER_BYTES
    header = {"offset": offset, "size": size, "masks": None,
              "resolution": (BMP_DEFAULT_RESOLUTION, BMP_DEFAULT_RESOLUTION)}

    if size == BMP_CORE_HEADER_BYTES:
        width, height, _planes, depth = struct.unpack_from("<HHHH", data,
                                                           info + 4)
        header.update(width=width, height=height, depth=depth,
                      compression=BMP_RGB, colors=0, entry_bytes=3)
    elif 16 <= size <= 124:
        if len(data) < info + min(size, 40):
            raise RepairError("the header runs off the end of the file")
        width, height = struct.unpack_from("<ii", data, info + 4)
        depth = struct.unpack_from("<H", data, info + 14)[0]
        compression = (struct.unpack_from("<I", data, info + 16)[0]
                       if size >= 20 else BMP_RGB)
        if size >= 32:
            header["resolution"] = struct.unpack_from("<ii", data, info + 24)
        colors = struct.unpack_from("<I", data, info + 32)[0] if size >= 36 else 0
        if size == BMP_OS2_HEADER_BYTES and compression in (3, 4):
            raise RepairError("an OS/2 packing the repair does not unpack")
        if compression not in (BMP_RGB, BMP_RLE8, BMP_RLE4, BMP_BITFIELDS,
                               BMP_ALPHABITFIELDS):
            name = BMP_COMPRESSION_NAMES.get(compression, str(compression))
            raise RepairError(f"{name} compression")
        header.update(width=width, height=height, depth=depth,
                      compression=compression, colors=colors, entry_bytes=4)
    else:
        raise RepairError(f"a {size}-byte info header")

    width = header["width"]
    depth = header["depth"]
    compression = header["compression"]
    if width <= 0 or header["height"] == 0:
        raise RepairError("no pixels")
    if depth not in (1, 2, 4, 8, 16, 24, 32):
        raise RepairError(f"{depth} bits per pixel")
    if compression == BMP_RLE8 and depth != 8 \
            or compression == BMP_RLE4 and depth != 4 \
            or compression in (BMP_BITFIELDS, BMP_ALPHABITFIELDS) \
            and depth not in (16, 32):
        raise RepairError("a packing that does not match its depth")
    if compression in (BMP_RLE8, BMP_RLE4) and header["height"] < 0:
        raise RepairError("packed rows stored top to bottom")

    # Bit masks sit inside the longer headers, and straight after a 40-byte
    # one - which also moves where the palette starts.
    mask_bytes = 0
    if compression in (BMP_BITFIELDS, BMP_ALPHABITFIELDS):
        count = 4 if compression == BMP_ALPHABITFIELDS or size >= 56 else 3
        if size >= 52:
            at = info + 40
        else:
            at = info + size
            mask_bytes = 4 * count
        if len(data) < at + 4 * count:
            raise RepairError("the bit masks run off the end of the file")
        masks = struct.unpack_from(f"<{count}I", data, at)
        header["masks"] = masks if count == 4 else masks + (0,)

    # The palette the loader reads: the header's color count, or a full one for
    # a paletted image that gives none - and it reads it even for truecolor.
    entries = header["colors"]
    if entries == 0 and depth <= 8:
        entries = 1 << depth
    header["palette_at"] = info + size + mask_bytes
    header["entries"] = entries
    header["expected_offset"] = (BMP_FILE_HEADER_BYTES + C.BMP_INFO_HEADER_BYTES
                                 + 4 * header["colors"]
                                 + (4 * (1 << depth)
                                    if header["colors"] == 0 and depth <= 8
                                    else 0))
    return header


def repair_bmp(data):
    """*data*, a BMP, rewritten as one the game reads. Raises :class:`RepairError`."""
    header = _bmp_header(data)
    width = header["width"]
    height = abs(header["height"])
    depth = header["depth"]
    compression = header["compression"]

    palette = b""
    if depth <= 8:
        count = min(header["entries"], 256)
        at = header["palette_at"]
        step = header["entry_bytes"]
        raw = data[at:at + count * step]
        if len(raw) < count * step:
            raise RepairError("the palette runs off the end of the file")
        palette = raw if step == 4 else b"".join(
            raw[i:i + 3] + b"\x00" for i in range(0, len(raw), 3))

    pixels = data[header["offset"]:]
    if compression == BMP_RLE8:
        rows = _unpack_rle(pixels, width, height, nibbles=False)
        out_depth = 8
    elif compression == BMP_RLE4:
        rows = _unpack_rle(pixels, width, height, nibbles=True)
        out_depth = 8
    else:
        rows = _plain_rows(pixels, width, height, depth)
        if header["height"] < 0:
            rows.reverse()                      # the loader wants bottom first
        out_depth = depth
        if depth < 8:
            rows = [_expand_indices(row, width, depth) for row in rows]
            out_depth = 8
        elif compression in (BMP_BITFIELDS, BMP_ALPHABITFIELDS):
            rows, out_depth = _unpack_bitfields(rows, width, depth,
                                                header["masks"])
    if out_depth > 8:
        palette = b""
    return write_bmp(width, height, out_depth, rows, palette,
                     header["resolution"])


def _plain_rows(pixels, width, height, depth):
    stride = ((width * depth + 31) // 32) * 4
    used = (width * depth + 7) // 8
    if len(pixels) < stride * (height - 1) + used:
        raise RepairError("the pixels run off the end of the file")
    return [pixels[y * stride:y * stride + used] for y in range(height)]


def _expand_indices(row, width, depth):
    """1-, 2- or 4-bit indices, first pixel in the high bits, as one byte each."""
    mask = (1 << depth) - 1
    out = bytearray(width)
    for x in range(width):
        bit = x * depth
        byte = row[bit // 8]
        out[x] = (byte >> (8 - depth - bit % 8)) & mask
    return bytes(out)


def _unpack_rle(stream, width, height, nibbles):
    """Run-length packed indices as plain rows, bottom row first.

    Pixels a packed image skips over are left at index 0.
    """
    grid = [bytearray(width) for _ in range(height)]
    x = y = 0
    at = 0
    end = len(stream)

    def put(value):
        nonlocal x
        if y < height and x < width:
            grid[y][x] = value
        x += 1

    while at + 1 < end:
        count, value = stream[at], stream[at + 1]
        at += 2
        if count:
            if nibbles:
                pair = (value >> 4, value & 0x0F)
                for index in range(count):
                    put(pair[index % 2])
            else:
                for _ in range(count):
                    put(value)
            continue
        if value == 0:                                  # end of line
            x, y = 0, y + 1
        elif value == 1:                                # end of image
            break
        elif value == 2:                                # skip ahead
            if at + 1 >= end:
                break
            x += stream[at]
            y += stream[at + 1]
            at += 2
        else:                                           # literal run
            if nibbles:
                size = (value + 1) // 2
                literal = stream[at:at + size]
                for index in range(value):
                    byte = literal[index // 2] if index // 2 < len(literal) else 0
                    put(byte >> 4 if index % 2 == 0 else byte & 0x0F)
            else:
                size = value
                for byte in stream[at:at + size]:
                    put(byte)
            at += size + (size & 1)                     # padded to a word
    return [bytes(row) for row in grid]


def _mask_reader(mask):
    """``value -> 0..255`` for one channel's mask, or ``None`` for no channel."""
    if not mask:
        return None
    shift = (mask & -mask).bit_length() - 1
    top = (mask >> shift)
    return lambda value: ((value & mask) >> shift) * 255 // top


def _unpack_bitfields(rows, width, depth, masks):
    """Bit-field pixels in a layout the loader reads, and the depth they took."""
    red, green, blue, alpha = masks
    if depth == 16 and (red, green, blue) == BMP_X1R5G5B5_MASKS \
            and alpha in (0, 0x8000):
        return rows, 16                     # already the loader's own layout
    if depth == 32 and (red, green, blue) == BMP_X8R8G8B8_MASKS \
            and alpha in (0, 0xFF000000):
        return rows, 32                     # likewise, byte for byte

    channels = [_mask_reader(mask) for mask in (blue, green, red)]
    alpha_of = _mask_reader(alpha)
    code = "<H" if depth == 16 else "<I"
    step = depth // 8
    out_depth = 32 if alpha_of else 24
    converted = []
    for row in rows:
        out = bytearray()
        for (value,) in struct.iter_unpack(code, row[:width * step]):
            for read in channels:
                out.append(read(value) if read else 0)
            if alpha_of:
                out.append(alpha_of(value))
        converted.append(bytes(out))
    return converted, out_depth


def write_bmp(width, height, depth, rows, palette=b"",
              resolution=(BMP_DEFAULT_RESOLUTION, BMP_DEFAULT_RESOLUTION)):
    """A BMP exactly as the game reads one.

    *rows* are unpadded, bottom row first; *palette* is four bytes an entry.
    """
    stride = ((width * depth + 31) // 32) * 4
    body = bytearray()
    for row in rows:
        body += row
        body += b"\x00" * (stride - len(row))
    offset = BMP_FILE_HEADER_BYTES + C.BMP_INFO_HEADER_BYTES + len(palette)
    file_header = b"BM" + struct.pack("<IHHI", offset + len(body), 0, 0, offset)
    info = struct.pack("<IiiHHIIiiII", C.BMP_INFO_HEADER_BYTES, width, height, 1,
                       depth, BMP_RGB, len(body), resolution[0], resolution[1],
                       len(palette) // 4, 0)
    return file_header + info + palette + bytes(body)


# ── TGA ───────────────────────────────────────────────────────────────────────
def repair_tga(data):
    """*data*, a TGA, unpacked and turned left to right. Raises :class:`RepairError`."""
    if len(data) < TGA_HEADER_BYTES:
        raise RepairError("too short to be a Targa")
    id_length, has_map, image_type = data[0], data[1], data[2]
    map_length, map_bits = struct.unpack_from("<HB", data, 5)
    width, height, bits, descriptor = struct.unpack_from("<HHBB", data, 12)
    if image_type not in (1, 2, 3) and image_type not in TGA_UNPACKED_TYPE:
        raise RepairError(f"Targa type {image_type}")
    step = (bits + 7) // 8
    if width == 0 or height == 0 or step == 0:
        raise RepairError("no pixels")

    at = TGA_HEADER_BYTES + id_length
    if has_map:
        at += map_length * ((map_bits + 7) // 8)
    lead = data[TGA_HEADER_BYTES:at]
    count = width * height * step

    if image_type in TGA_UNPACKED_TYPE:
        pixels = _unpack_tga(data, at, count, step)
        image_type = TGA_UNPACKED_TYPE[image_type]
    else:
        pixels = data[at:at + count]
        if len(pixels) < count:
            raise RepairError("the pixels run off the end of the file")

    if descriptor & C.TGA_ORIGIN_RIGHT:
        row_bytes = width * step
        turned = bytearray()
        for y in range(height):
            row = pixels[y * row_bytes:(y + 1) * row_bytes]
            for x in range(width - 1, -1, -1):
                turned += row[x * step:(x + 1) * step]
        pixels = bytes(turned)
        descriptor &= ~C.TGA_ORIGIN_RIGHT

    header = bytearray(data[:TGA_HEADER_BYTES])
    header[2] = image_type
    header[17] = descriptor
    # Any footer is left behind: its offsets point into the packed layout, and
    # the loader never reads one.
    return bytes(header) + lead + pixels


def _unpack_tga(data, at, count, step):
    out = bytearray()
    end = len(data)
    while len(out) < count:
        if at >= end:
            raise RepairError("the packed pixels run off the end of the file")
        packet = data[at]
        at += 1
        run = (packet & 0x7F) + 1
        if packet & 0x80:
            pixel = data[at:at + step]
            at += step
            out += pixel * run
        else:
            out += data[at:at + run * step]
            at += run * step
    return bytes(out[:count])


def repair(data, extension):
    """*data* rewritten for the game, by the file's *extension*."""
    extension = extension.lower()
    if extension == ".bmp":
        return repair_bmp(data)
    if extension == ".tga":
        return repair_tga(data)
    raise RepairError(f"{extension or 'no extension'} is not a BMP or TGA")
