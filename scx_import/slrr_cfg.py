# SPDX-License-Identifier: MIT
"""
Parser + scene-graph assembler for SLRR car ``scripts`` folders.

A Street Legal Racing: Redline car ships its meshes in local space; the parts
are positioned by a hierarchy of ``slot`` / ``attach`` records in the per-part
``.cfg`` files, rooted at the chassis (defined in ``_main_<car>.cfg``).  The set
of parts that are actually fitted is listed in the car's ``src/<Car>.java``
(``stock_parts_list_*``).

This module is dependency-free (no ``bpy``) so it can be tested standalone.  It
returns, per fitted part, a 4x4 world matrix in SLRR space (Y-up, **metres** --
meshes are in centimetres, so multiply mesh coords by :data:`MESH_SCALE`).

Everything here is best-effort text parsing; unknown parts are skipped rather
than raising.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 1 mesh unit == 1 cm; SLRR .cfg transforms are in metres.
MESH_SCALE = 0.01

# Resource IDs used by parts to mean "the chassis / body root".
ROOT_IDS = {0x06, 0xCC6}

Mat = List[List[float]]


# --------------------------------------------------------------------------- #
# tiny 4x4 matrix maths (row-major, points are column vectors: p' = M @ p)
# --------------------------------------------------------------------------- #
def mat_identity() -> Mat:
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def mat_mul(a: Mat, b: Mat) -> Mat:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def mat_translate(x: float, y: float, z: float) -> Mat:
    m = mat_identity()
    m[0][3], m[1][3], m[2][3] = x, y, z
    return m


def _rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]]


def _rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]]


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def mat_euler(rx: float, ry: float, rz: float) -> Mat:
    # SLRR applies the rotations in Z, Y, X order (matches in-game assembly of
    # the sample cars); i.e. R = Rx @ Ry @ Rz.
    return mat_mul(_rot_x(rx), mat_mul(_rot_y(ry), _rot_z(rz)))


def slot_matrix(pos, rot) -> Mat:
    return mat_mul(mat_translate(*pos), mat_euler(*rot))


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
@dataclass
class Part:
    name: str                                   # base file name (== .scx name)
    res_id: Optional[int] = None
    alias: str = ""
    slots: Dict[int, Tuple[tuple, tuple]] = field(default_factory=dict)
    attach: List[Tuple[int, int]] = field(default_factory=list)  # (parentRes, slotId)
    mass: float = 0.0
    category: Optional[int] = None
    wings: int = 0


@dataclass
class Assembly:
    placements: Dict[str, Mat] = field(default_factory=dict)   # scx name -> world matrix (SLRR metres)
    wheels: List[Mat] = field(default_factory=list)            # world matrices for wheel positions
    info: dict = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)


_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _floats(line: str, count: int) -> List[float]:
    vals = re.findall(_NUM, line)
    return [float(v) for v in vals[:count]]


def parse_part_cfg(path: str) -> Part:
    name = os.path.splitext(os.path.basename(path))[0]
    part = Part(name=name)
    with open(path, "r", encoding="latin-1", errors="replace") as fh:
        text = fh.read()

    m = re.search(r"resource ID of this part:\s*0x([0-9A-Fa-f]+)", text)
    if not m:
        m = re.search(r"its ID is\s*0x([0-9A-Fa-f]+)", text)
    if m:
        part.res_id = int(m.group(1), 16)
    m = re.search(r'resource alias of this part:\s*"([^"]*)"', text)
    if m:
        part.alias = m.group(1)

    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()   # strip trailing ; comments
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        low = line.lower()
        if low.startswith("slot"):
            nums = _floats(line, 7)
            if len(nums) >= 7:
                pos = (nums[0], nums[1], nums[2])
                rot = (nums[3], nums[4], nums[5])
                sid = int(round(nums[6]))
                part.slots[sid] = (pos, rot)
        elif low.startswith("attach"):
            hexid = re.search(r"0x([0-9A-Fa-f]+)", line)
            tail = re.findall(r"\b(\d+)\b", line.split("x", 1)[-1])
            if hexid and tail:
                part.attach.append((int(hexid.group(1), 16), int(tail[-1])))
        elif low.startswith("body"):
            nums = _floats(line, 7)
            if len(nums) >= 7:
                part.mass += nums[6]
        elif low.startswith("category"):
            n = _floats(line, 1)
            if n:
                part.category = int(n[0])
        elif low.startswith("wing"):
            part.wings += 1
    return part


def parse_main_cfg(path: str):
    """Return (root_slots, wheel_positions, stats) for the chassis."""
    slots: Dict[int, Tuple[tuple, tuple]] = {}
    wheels: List[tuple] = []
    stats: dict = {"chassis_mass": 0.0}
    with open(path, "r", encoding="latin-1", errors="replace") as fh:
        text = fh.read()

    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        low = line.lower()
        if low.startswith("slot"):
            nums = _floats(line, 7)
            if len(nums) >= 7:
                slots[int(round(nums[6]))] = ((nums[0], nums[1], nums[2]),
                                              (nums[3], nums[4], nums[5]))
        elif low.startswith("wheel"):
            nums = _floats(line, 6)
            if len(nums) >= 6:
                wheels.append(((nums[0], nums[1], nums[2]),
                               (nums[3], nums[4], nums[5])))
        elif low.startswith("body"):
            nums = _floats(line, 7)
            if len(nums) >= 7:
                stats["chassis_mass"] += nums[6]
        elif low.startswith("enginepower"):
            stats["enginepower"] = _floats(line, 6)
        elif low.startswith("brakepower"):
            stats["brakepower"] = _floats(line, 2)
        elif low.startswith("ratios"):
            stats["gear_ratios"] = _floats(line, 8)
        elif low.startswith("rearendratio"):
            stats["final_drive"] = _floats(line, 1)
        elif low.startswith("maxsteer"):
            stats["maxsteer"] = _floats(line, 2)
    return slots, wheels, stats


_JAVA_PART = re.compile(
    r"stock_parts_list_(\w+?)\[\s*\d+\]\s*=\s*cars\.racers\.\w+:0x([0-9A-Fa-f]+)r;\s*//\s*\"([^\"]*)\"")
_JAVA_ANY_PART = re.compile(
    r"=\s*([\w.]+):0x([0-9A-Fa-f]+)r;\s*//\s*\"([^\"]*)\"")


def parse_equipped(java_path: str):
    """Return (body_res_ids:set, groups:dict, meta:dict) from a car's .java."""
    with open(java_path, "r", encoding="latin-1", errors="replace") as fh:
        text = fh.read()
    body_ids = set()
    groups: Dict[str, List[Tuple[int, str]]] = {}
    for grp, hx, label in _JAVA_PART.findall(text):
        rid = int(hx, 16)
        body_ids.add(rid)
        groups.setdefault(grp, []).append((rid, label.strip()))

    meta = {}
    for key in ("makerName", "vehicleName", "name", "description"):
        m = re.search(key + r'\s*=\s*"([^"]*)"', text)
        if m:
            meta[key] = m.group(1)
    # everything the car fits, for the report (engine/suspension/etc.)
    all_parts = [(pkg, int(hx, 16), lbl.strip())
                 for pkg, hx, lbl in _JAVA_ANY_PART.findall(text)]
    meta["all_parts"] = all_parts
    return body_ids, groups, meta


def _find_main_cfg(scripts_dir: str, car_name: Optional[str]) -> Optional[str]:
    mains = [f for f in os.listdir(scripts_dir)
             if f.lower().startswith("_main_") and f.lower().endswith(".cfg")]
    if not mains:
        return None
    if not car_name:
        return os.path.join(scripts_dir, sorted(mains)[0])

    car_tokens = set(t for t in re.split(r"[^a-z0-9]+", car_name.lower()) if t)

    def score(f):
        toks = set(t for t in re.split(r"[^a-z0-9]+", f.lower()) if t
                   and t not in ("main", "cfg"))
        return (len(toks & car_tokens), -len(toks ^ car_tokens), f)

    return os.path.join(scripts_dir, max(sorted(mains), key=score))


def _find_car_java(scripts_dir: str):
    """Pick the car's main .java deterministically.

    A car folder can hold several sellable variants (e.g. GTR / GTS / Nismo /
    LHD).  We sort for stability and prefer a plain GT-R / base variant over the
    special editions so repeated imports are consistent.
    """
    src = os.path.join(scripts_dir, "src")
    if not os.path.isdir(src):
        return None, None
    candidates = []
    for f in sorted(os.listdir(src)):
        if not f.endswith(".java"):
            continue
        p = os.path.join(src, f)
        try:
            with open(p, "r", encoding="latin-1", errors="replace") as fh:
                if "addStockParts" in fh.read():
                    candidates.append((p, os.path.splitext(f)[0]))
        except OSError:
            continue
    if not candidates:
        return None, None

    def score(item):
        n = item[1].lower()
        s = 0
        if "_lhd" in n:
            s += 4          # deprioritise left-hand-drive conversions
        if "gts" in n or "ecr" in n:
            s += 2          # deprioritise non-GT-R trims
        if "nismo" in n or "400r" in n or "_vt" in n:
            s += 1          # deprioritise special editions
        return (s, len(n), n)

    candidates.sort(key=score)
    return candidates[0]


def assemble(scripts_dir: str) -> Assembly:
    """Build the fitted-car scene graph from a car's ``scripts`` folder."""
    asm = Assembly()

    java_path, car_name = _find_car_java(scripts_dir)
    body_ids: set = set()
    groups: dict = {}
    meta: dict = {}
    if java_path:
        body_ids, groups, meta = parse_equipped(java_path)

    main_cfg = _find_main_cfg(scripts_dir, car_name)
    root_slots, wheels, stats = ({}, [], {})
    if main_cfg:
        root_slots, wheels, stats = parse_main_cfg(main_cfg)

    # index every part cfg by resource id and by name
    parts_by_id: Dict[int, Part] = {}
    parts_by_name: Dict[str, Part] = {}
    for f in os.listdir(scripts_dir):
        if not f.endswith(".cfg") or f.lower().startswith("_main_"):
            continue
        try:
            part = parse_part_cfg(os.path.join(scripts_dir, f))
        except OSError:
            continue
        parts_by_name[part.name] = part
        if part.res_id is not None:
            parts_by_id[part.res_id] = part

    # which parts to place: the fitted body parts if known, else all of them
    if body_ids:
        wanted = [parts_by_id[r] for r in body_ids if r in parts_by_id]
    else:
        wanted = list(parts_by_name.values())

    # resolve world matrices; root ids resolve to identity + root_slots
    world: Dict[int, Mat] = {}
    for rid in ROOT_IDS:
        world[rid] = mat_identity()

    def slot_of(parent_res: int, slot_id: int):
        if parent_res in ROOT_IDS:
            return root_slots.get(slot_id)
        p = parts_by_id.get(parent_res)
        return p.slots.get(slot_id) if p else None

    pending = list(wanted)
    guard = 0
    while pending and guard < len(pending) * 4 + 8:
        guard += 1
        still: List[Part] = []
        for part in pending:
            placed = False
            for parent_res, slot_id in part.attach:
                if parent_res in world:
                    st = slot_of(parent_res, slot_id)
                    if st is None:
                        continue
                    m = mat_mul(world[parent_res], slot_matrix(*st))
                    if part.res_id is not None:
                        world[part.res_id] = m
                    asm.placements[part.name] = m
                    placed = True
                    break
            if not placed:
                still.append(part)
        if len(still) == len(pending):
            break
        pending = still
    for part in pending:
        asm.unresolved.append(part.name)

    # wheels: one per chassis wheel position.  We hand the raw position, the
    # base rotation and a left/right/front/rear label to the importer, which
    # mirrors the right-hand wheels so the rim faces outwards on both sides.
    corners = ["FL", "FR", "RL", "RR"]
    for i, (pos, rot) in enumerate(wheels):
        asm.wheels.append({
            "pos": pos,
            "rot": rot,
            "right": pos[0] > 0,
            "corner": corners[i] if i < len(corners) else "W%d" % i,
        })

    # collect the report
    total_body = sum(p.mass for p in parts_by_name.values())
    asm.info = {
        "car_name": meta.get("vehicleName") or car_name or "SLRR car",
        "maker": meta.get("makerName", ""),
        "description": meta.get("description", ""),
        "chassis_mass": stats.get("chassis_mass", 0.0),
        "fitted_parts_mass": sum(p.mass for p in wanted),
        "all_parts_mass": total_body,
        "stats": stats,
        "groups": groups,
        "all_parts": meta.get("all_parts", []),
        "parts": {p.name: {"res_id": p.res_id, "alias": p.alias,
                           "mass": p.mass, "category": p.category,
                           "wings": p.wings} for p in wanted},
        "wheel_positions": [w for w, _ in wheels],
        "n_wheels": len(wheels),
        "main_cfg": os.path.basename(main_cfg) if main_cfg else "",
        "car_java": car_name or "",
    }
    return asm


def pick_chassis_mesh(scx_names, main_cfg_name, car_java=""):
    """Choose the chassis body mesh for the selected variant from the available
    ``chassis*`` meshes (there is one per trim: gtr / gts / lhd_gtr ...).

    *scx_names* is an iterable of mesh base names (lower-case, no extension).
    Returns the best base name, or None.
    """
    cands = [n for n in scx_names
             if n.startswith("chassis")
             and not n.endswith(("_city", "_lod"))
             and "_phys" not in n and "_city" not in n]
    if not cands:
        return None
    ref = (main_cfg_name + " " + car_java).lower()
    ref_tokens = set(re.split(r"[^a-z0-9]+", ref))

    def score(name):
        toks = [t for t in name.split("_")[1:] if t]  # tokens after "chassis"
        good = sum(1 for t in toks if t in ref_tokens)
        bad = sum(1 for t in toks if t and t not in ref_tokens)
        return (good - bad, -len(name))

    cands.sort(key=score, reverse=True)
    return cands[0]
