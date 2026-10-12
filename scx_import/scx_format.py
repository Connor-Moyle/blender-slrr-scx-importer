# SPDX-License-Identifier: MIT
"""
Pure-Python reader for Street Legal Racing: Redline (SLRR) ``.scx`` meshes.

This module has **no** dependency on ``bpy`` so it can be unit-tested outside of
Blender.  The Blender add-on (``__init__.py``) imports :func:`read_scx` and turns
the returned :class:`SCXModel` into real objects.

--------------------------------------------------------------------------------
SCX / "INVO" binary format (reverse engineered from the sample cars)
--------------------------------------------------------------------------------
Header::

    char[4]  magic       "INVO"  (Invictus Object)
    u32      version     always 4 in the samples
    u32      dir_count   number of directory records
    u32      _reserved   0
    dir_count * { u32 offset; u32 tag }     directory records

The directory records point at chunks elsewhere in the file.  ``tag`` is one of:

    1  MATERIAL   material description (name, colour, texture indices)
    4  LINK       tiny fixed record that ties a material to the next mesh (ignored)
    5  VERTICES   vertex buffer
    0  INDICES    u16 triangle-index buffer

A model is a sequence of sub-meshes; each sub-mesh contributes, in order, a
MATERIAL, a LINK, a VERTICES and an INDICES record.  We do not rely on the exact
grouping though -- we track the most recent material / vertex buffer and emit a
sub-mesh every time we hit an index buffer.

VERTICES chunk (tag 5)::

    u32 _f0        4
    u32 rec_size   total byte size of this record (incl. this 16-byte header)
    u32 v_count    number of vertices
    u32 flags      FVF-style flags (0x41 = pos+normal, 0x241 = pos+normal+uv)
    v_count * stride bytes,  stride = (rec_size - 16) // v_count
        float[3] position
        float[3] normal      (present when stride >= 24)
        float[2] uv          (present when stride >= 32)

INDICES chunk (tag 0)::

    u32 _f0        5
    u32 rec_size   total byte size (incl. this 12-byte header)
    u32 i_count    number of indices (multiple of 3 -> triangle list)
    i_count * u16  indices (0-based into this sub-mesh's own vertex buffer)

MATERIAL chunk (tag 1)::

    u32 _f0        0
    u32 rec_size
    u32 _f2
    u32 _f3        0
    u32 prop_count
    prop_count properties, each: u32 raw_tag  followed by a fixed payload.
        tag = raw_tag >> 24, or (raw_tag & 0xff) when the high byte is 0.
        tag 8  NAME      payload 40 bytes: char[36] name + u32 RGBA diffuse
        tag 6  TEXTURE   payload  8 bytes: u32 tex_index + u32 flags
                         the raw_tag low byte is the stage (0 = diffuse,
                         3 = reflection / environment map)
        tag 1  REFLECT   payload 12 bytes: shininess/colour (not used for import)
        tag 3  UV        payload 16 bytes: uv tiling (not used for import)

Texture indices refer to lines of the sibling ``<name>.tex`` file, which is a
plain-text list of image file names (one per line), resolved against a sibling
``textures/`` folder.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

MAGIC = b"INVO"

TAG_INDICES = 0
TAG_MATERIAL = 1
TAG_LINK = 4
TAG_VERTICES = 5

# Material property tags and the size (in bytes) of the payload that follows the
# 4-byte tag word.
_PROP_NAME = 8
_PROP_TEXTURE = 6
_PROP_REFLECT = 1
_PROP_UV = 3
_PROP_SIZES = {_PROP_NAME: 40, _PROP_TEXTURE: 8, _PROP_REFLECT: 12, _PROP_UV: 16}

# Texture stages (low byte of a TEXTURE property's raw tag).
STAGE_DIFFUSE = 0
STAGE_REFLECTION = 3


class SCXError(Exception):
    """Raised when a file is not a valid/parseable SCX."""


@dataclass
class TextureRef:
    index: int          # line number in the .tex file
    stage: int          # STAGE_DIFFUSE / STAGE_REFLECTION / ...
    name: str = ""      # resolved file name from the .tex list ("" if unknown)


@dataclass
class Material:
    name: str = ""
    diffuse: Tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)
    textures: List[TextureRef] = field(default_factory=list)

    def _stage(self, stage: int) -> Optional[TextureRef]:
        for t in self.textures:
            if t.stage == stage and t.name:
                return t
        return None

    @property
    def diffuse_texture(self) -> Optional[TextureRef]:
        return self._stage(STAGE_DIFFUSE)

    @property
    def reflection_texture(self) -> Optional[TextureRef]:
        return self._stage(STAGE_REFLECTION)


@dataclass
class SubMesh:
    material: Material
    positions: List[Tuple[float, float, float]] = field(default_factory=list)
    normals: List[Tuple[float, float, float]] = field(default_factory=list)
    uvs: List[Tuple[float, float]] = field(default_factory=list)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)
    has_uv: bool = False


@dataclass
class SCXModel:
    submeshes: List[SubMesh] = field(default_factory=list)
    tex_list: List[str] = field(default_factory=list)
    source_path: str = ""

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.source_path))[0]

    @property
    def vertex_count(self) -> int:
        return sum(len(s.positions) for s in self.submeshes)

    @property
    def triangle_count(self) -> int:
        return sum(len(s.faces) for s in self.submeshes)


# --------------------------------------------------------------------------- #
# low-level helpers
# --------------------------------------------------------------------------- #
def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _read_tex_list(scx_path: str) -> List[str]:
    """Read the sibling ``<name>.tex`` file (one texture file name per line)."""
    tex_path = os.path.splitext(scx_path)[0] + ".tex"
    if not os.path.isfile(tex_path):
        return []
    names: List[str] = []
    with open(tex_path, "r", encoding="latin-1", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                names.append(line)
    return names


def _parse_material(buf: bytes, off: int, size: int, tex_list: List[str]) -> Material:
    mat = Material()
    end = off + size
    try:
        prop_count = _u32(buf, off + 0x10)
    except struct.error:
        return mat
    p = off + 0x14
    for _ in range(prop_count):
        if p + 4 > end:
            break
        raw = _u32(buf, p)
        tag = raw >> 24
        if tag == 0:
            tag = raw & 0xFF
        payload_size = _PROP_SIZES.get(tag)
        if payload_size is None:
            break  # unknown property -> stop; we already have what we need
        payload = p + 4  # payload start
        if payload + payload_size > end:
            break
        if tag == _PROP_NAME:
            name = buf[payload:payload + 36].split(b"\x00", 1)[0]
            mat.name = name.decode("latin-1", "replace")
            rgba = _u32(buf, payload + 36)
            r = (rgba & 0xFF) / 255.0
            g = ((rgba >> 8) & 0xFF) / 255.0
            b = ((rgba >> 16) & 0xFF) / 255.0
            a = ((rgba >> 24) & 0xFF) / 255.0
            mat.diffuse = (r, g, b, a)
        elif tag == _PROP_TEXTURE:
            index = _u32(buf, payload)
            stage = raw & 0xFF
            name = tex_list[index] if 0 <= index < len(tex_list) else ""
            mat.textures.append(TextureRef(index=index, stage=stage, name=name))
        p += 4 + payload_size
    return mat


def _parse_vertices(buf: bytes, off: int) -> SubMesh:
    rec_size = _u32(buf, off + 4)
    v_count = _u32(buf, off + 8)
    sub = SubMesh(material=Material())
    if v_count <= 0:
        return sub
    stride = (rec_size - 16) // v_count
    if stride < 12:
        raise SCXError("implausible vertex stride %d" % stride)
    sub.has_uv = stride >= 32
    base = off + 16
    for i in range(v_count):
        vo = base + i * stride
        px, py, pz = struct.unpack_from("<3f", buf, vo)
        sub.positions.append((px, py, pz))
        if stride >= 24:
            nx, ny, nz = struct.unpack_from("<3f", buf, vo + 12)
            sub.normals.append((nx, ny, nz))
        else:
            sub.normals.append((0.0, 0.0, 1.0))
        if stride >= 32:
            u, v = struct.unpack_from("<2f", buf, vo + 24)
            sub.uvs.append((u, v))
        else:
            sub.uvs.append((0.0, 0.0))
    return sub


def _parse_indices(buf: bytes, off: int) -> List[Tuple[int, int, int]]:
    i_count = _u32(buf, off + 8)
    base = off + 12
    idx = struct.unpack_from("<%dH" % i_count, buf, base)
    faces = []
    for k in range(0, i_count - 2, 3):
        faces.append((idx[k], idx[k + 1], idx[k + 2]))
    return faces


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def read_scx(path: str) -> SCXModel:
    """Parse an ``.scx`` file into an :class:`SCXModel`.

    Handles both the modern directory-based version 4 files and the older
    inline version 3 files (used by SLRR for physics/collision hulls and a few
    light meshes).
    """
    with open(path, "rb") as fh:
        buf = fh.read()
    if buf[:4] != MAGIC:
        raise SCXError("not an SCX/INVO file: %r" % (buf[:4],))

    version = _u32(buf, 4)
    tex_list = _read_tex_list(path)
    # Directory formats: v4 and v6.  Inline formats: v3 and v5.  For anything
    # unknown, decide from the header: the directory formats carry a small
    # record count followed by a zero word; the inline ones a large block size.
    if version in (4, 6):
        return _read_v4(buf, path, tex_list)
    if version in (3, 5):
        return _read_v3(buf, path, tex_list)
    dir_count = _u32(buf, 8)
    reserved = _u32(buf, 0xC)
    if reserved == 0 and 0 < dir_count < 4096:
        return _read_v4(buf, path, tex_list)
    return _read_v3(buf, path, tex_list)


def _read_v4(buf: bytes, path: str, tex_list: List[str]) -> SCXModel:
    dir_count = _u32(buf, 8)
    entries = []
    for i in range(dir_count):
        eo = 0x10 + i * 8
        entries.append((_u32(buf, eo), _u32(buf, eo + 4)))

    # Record sizes come from the gap to the next-higher offset in the file.
    sorted_offsets = sorted({o for o, _ in entries})
    sorted_offsets.append(len(buf))
    next_off = {}
    for a, b in zip(sorted_offsets, sorted_offsets[1:]):
        next_off[a] = b

    model = SCXModel(tex_list=tex_list, source_path=path)

    cur_material = Material()
    cur_vertices: Optional[SubMesh] = None

    for off, tag in entries:
        size = next_off.get(off, len(buf)) - off
        if tag == TAG_MATERIAL:
            cur_material = _parse_material(buf, off, size, tex_list)
        elif tag == TAG_VERTICES:
            cur_vertices = _parse_vertices(buf, off)
        elif tag == TAG_INDICES:
            faces = _parse_indices(buf, off)
            if cur_vertices is None:
                continue
            cur_vertices.material = cur_material
            cur_vertices.faces = faces
            model.submeshes.append(cur_vertices)
            cur_vertices = None
        # TAG_LINK and anything else: ignored
    return model


# --------------------------------------------------------------------------- #
# inline reader (versions 3 and 5)
# --------------------------------------------------------------------------- #
# Inline layout, after the 8-byte magic+version:
#     u32 block_size              size of the material/header block (usually 136)
#   then, one or more sub-meshes back to back, each:
#     block_size bytes            material block (name + colours in a printable run)
#     u32 v_count
#     v_count * 64 bytes          float[3] pos, float[3] normal, float[2] uv, ...
#     u32 tri_count
#     tri_count * 3 * u32         triangle indices
# Version 3 files have a single sub-mesh; version 5 files (remake-pack cars) chain
# several.  block_size is taken from the header and used as the stride between
# sub-meshes, with a bounded scan as a fallback if that ever drifts.
_V3_STRIDE = 64


def _v3_material_name(block: bytes) -> str:
    """Extract a readable material name from an inline material block."""
    run = bytearray()
    best = b""
    for byte in block:
        if 32 <= byte < 127:
            run.append(byte)
        else:
            if len(run) > len(best):
                best = bytes(run)
            run = bytearray()
    if len(run) > len(best):
        best = bytes(run)
    return best.decode("latin-1", "replace").strip() if len(best) >= 3 else ""


def _v3_scan_vcount(buf: bytes, start: int, n: int) -> Optional[int]:
    """Locate the next plausible (v_count, verts, tri_count) start from *start*."""
    for off in range(start, min(start + 4096, n - 8), 4):
        vc = _u32(buf, off)
        if not (1 <= vc <= 5_000_000):
            continue
        vend = off + 4 + vc * _V3_STRIDE
        if vend + 4 > n:
            continue
        tc = _u32(buf, vend)
        if 1 <= tc <= 5_000_000 and vend + 4 + tc * 12 <= n + 16:
            return off
    return None


def _read_v3(buf: bytes, path: str, tex_list: List[str]) -> SCXModel:
    model = SCXModel(tex_list=tex_list, source_path=path)
    n = len(buf)
    block_size = _u32(buf, 8)
    if not (8 <= block_size <= 4096):
        block_size = 136

    off = 8
    sub_index = 0
    guard = 0
    while off + 4 < n and guard < 100000:
        guard += 1
        block_start = off
        vc_off = off + block_size
        vcount = _u32(buf, vc_off) if vc_off + 4 <= n else 0
        if not (1 <= vcount <= 5_000_000) or vc_off + 4 + vcount * _V3_STRIDE + 4 > n:
            # material block size drifted -- find the vertex count by scanning
            found = _v3_scan_vcount(buf, off, n)
            if found is None:
                break
            vc_off = found
            block_start = max(8, found - block_size)
            vcount = _u32(buf, vc_off)

        vbase = vc_off + 4
        v_end = vbase + vcount * _V3_STRIDE
        if v_end + 4 > n:
            break
        tri_count = _u32(buf, v_end)
        i_end = v_end + 4 + tri_count * 3 * 4
        if not (1 <= tri_count <= 5_000_000) or i_end > n + 16:
            break

        idx = struct.unpack_from("<%dI" % (tri_count * 3), buf, v_end + 4)
        if idx and max(idx) >= vcount:
            break  # misaligned -- stop rather than emit garbage

        mat = Material()
        mat.name = _v3_material_name(buf[block_start:vc_off])
        if sub_index < len(tex_list):
            mat.textures.append(TextureRef(
                index=sub_index, stage=STAGE_DIFFUSE, name=tex_list[sub_index]))
        elif tex_list:
            mat.textures.append(TextureRef(
                index=0, stage=STAGE_DIFFUSE, name=tex_list[0]))

        sub = SubMesh(material=mat, has_uv=True)
        for i in range(vcount):
            vo = vbase + i * _V3_STRIDE
            sub.positions.append(struct.unpack_from("<3f", buf, vo))
            sub.normals.append(struct.unpack_from("<3f", buf, vo + 12))
            sub.uvs.append(struct.unpack_from("<2f", buf, vo + 24))
        for k in range(0, tri_count * 3, 3):
            sub.faces.append((idx[k], idx[k + 1], idx[k + 2]))
        model.submeshes.append(sub)
        sub_index += 1

        off = i_end
        if n - off <= 16:
            break
    return model


_IMAGE_EXTS = ("png", "jpg", "jpeg", "dds", "tga", "bmp", "gif")
_TEX_INDEX_CACHE = {}


def _build_texture_index(scx_dir: str, extra_dirs):
    """Index (recursively) every image near *scx_dir*, keyed by lower-case file
    name and by stem, so subfolders and extension swaps still resolve."""
    key = (scx_dir, tuple(extra_dirs or []))
    cached = _TEX_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    by_name = {}
    by_stem = {}
    roots = [
        os.path.join(scx_dir, "textures"),
        os.path.join(os.path.dirname(scx_dir), "textures"),
        scx_dir,
        os.path.dirname(scx_dir),
    ]
    roots += list(extra_dirs or [])
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        real = os.path.normcase(os.path.abspath(root))
        if real in seen:
            continue
        seen.add(real)
        try:
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    lower = f.lower()
                    ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
                    if ext not in _IMAGE_EXTS:
                        continue
                    full = os.path.join(dirpath, f)
                    by_name.setdefault(lower, full)
                    by_stem.setdefault(lower.rsplit(".", 1)[0], full)
        except OSError:
            continue
    _TEX_INDEX_CACHE[key] = (by_name, by_stem)
    return by_name, by_stem


def resolve_texture_path(scx_path: str, tex_name: str, extra_dirs=None) -> Optional[str]:
    """Locate the image file *tex_name* on disk near *scx_path*.

    Searches the car's ``textures/`` folder (and siblings) **recursively**, plus
    any *extra_dirs*, matching first by exact file name and then by stem (so a
    ``.tex`` that asks for ``foo.tga`` still finds a converted ``foo.png``).
    """
    if not tex_name:
        return None
    scx_dir = os.path.dirname(os.path.abspath(scx_path))
    by_name, by_stem = _build_texture_index(scx_dir, extra_dirs)
    t = tex_name.lower()
    if t in by_name:
        return by_name[t]
    stem = t.rsplit(".", 1)[0]
    return by_stem.get(stem)
