# SPDX-License-Identifier: MIT
"""
SLRR SCX Importer -- Blender add-on.

Imports Street Legal Racing: Redline ``.scx`` meshes (Invictus "INVO" format,
both version 3 and version 4) with model data, UVs, normals, materials and
textures.

Two modes:

* **Loose import** -- import whichever ``.scx`` files you pick (or a whole
  folder), each at its stored coordinates.
* **Assemble full car** -- when a car's ``scripts`` folder is present, read the
  ``.cfg`` / ``.java`` files and place every fitted body part (and the wheels)
  exactly where the game puts them, then write a ``<car>_info.txt`` report with
  weights, engine, gearing and the texture/material list.

The binary parsing lives in :mod:`scx_format`; the car assembly logic in
:mod:`slrr_cfg`.  Neither imports ``bpy``.
"""

import math
import os
import shutil

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    FloatProperty,
    StringProperty,
)
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector

from . import scx_format, slrr_cfg

bl_info = {
    "name": "SLRR SCX Importer (Street Legal Racing: Redline)",
    "author": "Generated for Connor",
    "version": (1, 3, 2),
    "blender": (4, 2, 0),
    "location": "File > Import > Street Legal Racing (.scx)",
    "description": "Import SLRR .scx cars: geometry, UVs, normals, materials, textures and in-game part placement.",
    "category": "Import-Export",
}

# SLRR is Y-up; Blender is Z-up.  Rotate +90 about X: (x, y, z) -> (x, -z, y).
_Y_UP_TO_Z_UP = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))

# material / texture names that mean "this is glass"
_GLASS_HINTS = ("okna", "window", "glass", "sklo", "sklic", "windshield")


def _finite(x):
    return not (math.isnan(x) or math.isinf(x))


def _root_matrix(convert_axis, scale):
    m = _Y_UP_TO_Z_UP if convert_axis else Matrix.Identity(4)
    return Matrix.Scale(scale, 4) @ m


# --------------------------------------------------------------------------- #
# textures & materials
# --------------------------------------------------------------------------- #
def _principled_input(bsdf, *names):
    for name in names:
        if name in bsdf.inputs:
            return bsdf.inputs[name]
    return None


def _load_image(filepath, cache):
    key = os.path.normcase(os.path.abspath(filepath))
    if key in cache:
        return cache[key]
    try:
        img = bpy.data.images.load(filepath, check_existing=True)
    except Exception as exc:  # noqa: BLE001
        print("[SCX] could not load image %r: %s" % (filepath, exc))
        img = None
    cache[key] = img
    return img


def _copy_texture(src, settings):
    texture_dir = settings["texture_dir"]
    try:
        os.makedirs(texture_dir, exist_ok=True)
        dst = os.path.join(texture_dir, os.path.basename(src))
        if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dst)):
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copy2(src, dst)
        return dst
    except OSError as exc:
        print("[SCX] could not copy texture %s: %s" % (src, exc))
        return src


def _get_image(scx_path, tex_name, settings, image_cache):
    """Return an image datablock for *tex_name*.

    If the file can't be found on disk we still return a named, broken-link
    placeholder so the material stays wired and Blender's *File > External Data
    > Find Missing Files* can relink it once the shared game textures are
    available.  Returns (image, found)."""
    if not tex_name:
        return None, False
    src = scx_format.resolve_texture_path(
        scx_path, tex_name, settings.get("extra_texture_dirs"))
    if src:
        if settings["copy_textures"] and settings["texture_dir"]:
            src = _copy_texture(src, settings)
        settings["_copied"].add(os.path.basename(src))
        img = _load_image(src, image_cache)
        if img:
            return img, True

    # not found -> broken-link placeholder, one per unique name
    settings["_missing"].add(tex_name)
    key = "missing::" + tex_name.lower()
    if key in image_cache:
        return image_cache[key], False
    img = bpy.data.images.get(tex_name)
    if img is None:
        img = bpy.data.images.new(tex_name, 8, 8, alpha=True)
        try:
            img.source = 'FILE'
            img.filepath = "//" + tex_name  # bare name -> Find Missing Files can locate
        except Exception:  # noqa: BLE001
            pass
    image_cache[key] = img
    return img, False


def _make_material(sub, scx_path, settings, image_cache, report, mat_log):
    mat_data = sub.material
    name = mat_data.name or "SCX_Material"
    diffuse = mat_data.diffuse_texture
    reflection = mat_data.reflection_texture

    # Deduplicate: two sub-meshes that describe the same material (same name,
    # textures and colour) share one Blender material datablock.
    key = (name,
           diffuse.name.lower() if diffuse else None,
           reflection.name.lower() if reflection else None,
           tuple(round(c, 3) for c in mat_data.diffuse))
    cache = settings["_mat_cache"]
    if key in cache:
        return cache[key]

    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    mat.use_backface_culling = False
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    out = nt.nodes.get("Material Output")
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        if out:
            nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    base_in = _principled_input(bsdf, "Base Color")
    if base_in is not None:
        base_in.default_value = tuple(mat_data.diffuse)
    if hasattr(mat, "diffuse_color"):
        mat.diffuse_color = tuple(mat_data.diffuse)

    name_l = name.lower()
    is_glass = any(h in name_l for h in _GLASS_HINTS)
    diff_name = ref_name = None

    if diffuse:
        img, found = _get_image(scx_path, diffuse.name, settings, image_cache)
        diff_name = diffuse.name + ("" if found else "  (missing)")
        if img:
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = img
            tex.location = (-600, 300)
            tex.label = "Diffuse"
            if base_in is not None:
                nt.links.new(tex.outputs["Color"], base_in)
            # Only wire alpha for genuine cut-out / glass textures.
            if "alpha" in diffuse.name.lower() or is_glass:
                alpha_in = _principled_input(bsdf, "Alpha")
                if alpha_in is not None:
                    nt.links.new(tex.outputs["Alpha"], alpha_in)
                _set_transparent(mat)

    if reflection:
        img, found = _get_image(scx_path, reflection.name, settings, image_cache)
        ref_name = reflection.name + ("" if found else "  (missing)")
        if img:
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = img
            tex.location = (-600, -100)
            tex.label = "Reflection"
            try:
                img.colorspace_settings.name = 'Non-Color'
            except Exception:  # noqa: BLE001
                pass
            spec = _principled_input(bsdf, "Specular Tint")
            if spec is not None and spec.type == 'RGBA':
                nt.links.new(tex.outputs["Color"], spec)
            rough = _principled_input(bsdf, "Roughness")
            if rough is not None:
                rough.default_value = 0.25

    if is_glass and not diffuse:
        if base_in is not None:
            base_in.default_value = (0.05, 0.06, 0.07, 1.0)
        alpha_in = _principled_input(bsdf, "Alpha")
        if alpha_in is not None:
            alpha_in.default_value = 0.25
        _set_transparent(mat)

    cache[key] = mat
    mat_log[name] = {"diffuse": diff_name, "reflection": ref_name, "glass": is_glass}
    return mat


def _set_transparent(mat):
    for attr, val in (("blend_method", 'BLEND'), ("shadow_method", 'HASHED')):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, val)
            except TypeError:
                pass
    if hasattr(mat, "show_transparent_back"):
        mat.show_transparent_back = False


# --------------------------------------------------------------------------- #
# mesh building
# --------------------------------------------------------------------------- #
def _build_submesh_object(sub, obj_name, scx_path, settings, image_cache, report, mat_log):
    """Build a mesh object with material; caller sets the transform/parent."""
    n_verts = len(sub.positions)
    faces = [list(f) for f in sub.faces
             if f[0] != f[1] and f[1] != f[2] and f[0] != f[2]
             and f[0] < n_verts and f[1] < n_verts and f[2] < n_verts]

    mesh = bpy.data.meshes.new(obj_name)
    mesh.from_pydata([tuple(v) for v in sub.positions], [], faces)
    mesh.update()
    mesh.validate(verbose=False, clean_customdata=False)

    if sub.has_uv and sub.uvs and len(sub.uvs) == n_verts:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            u, v = sub.uvs[loop.vertex_index]
            uv_layer.data[loop.index].uv = (u, 1.0 - v)

    if settings["import_normals"] and sub.normals and len(sub.normals) == len(mesh.vertices):
        safe = []
        for nrm in sub.normals:
            v = Vector(nrm)
            if not all(map(_finite, nrm)) or v.length < 1e-8:
                v = Vector((0.0, 0.0, 1.0))
            else:
                v.normalize()
            safe.append(v[:])
        if mesh.polygons:
            try:
                mesh.normals_split_custom_set_from_vertices(safe)
            except Exception as exc:  # noqa: BLE001
                print("[SCX] custom normals skipped for %s: %s" % (obj_name, exc))

    if hasattr(mesh, "use_auto_smooth"):
        mesh.use_auto_smooth = True

    obj = bpy.data.objects.new(obj_name, mesh)
    mat = _make_material(sub, scx_path, settings, image_cache, report, mat_log)
    mesh.materials.append(mat)
    return obj


def _import_scx_objects(filepath, base_name, settings, image_cache, report, mat_log):
    """Parse an .scx and return a list of (object, is_multi) with no transform."""
    model = scx_format.read_scx(filepath)
    objs = []
    multi = len(model.submeshes) > 1
    for i, sub in enumerate(model.submeshes):
        if not sub.positions:
            continue
        obj_name = base_name if not multi else "%s_%02d_%s" % (
            base_name, i, (sub.material.name or "mat"))
        objs.append(_build_submesh_object(
            sub, obj_name, filepath, settings, image_cache, report, mat_log))
    return objs, model


# --------------------------------------------------------------------------- #
# collection helpers
# --------------------------------------------------------------------------- #
def _new_collection(name, parent):
    """Create a uniquely-named child collection under *parent*."""
    coll = bpy.data.collections.new(name)
    parent.children.link(coll)
    return coll


def _link_part(objs, world_mtx, part_name, parent_coll):
    """Put *objs* in their own collection under *parent_coll* at *world_mtx*."""
    part_coll = _new_collection(part_name, parent_coll)
    for obj in objs:
        part_coll.objects.link(obj)
        obj.matrix_world = world_mtx
    return part_coll


# --------------------------------------------------------------------------- #
# loose import (one file at a time, at its own coordinates)
# --------------------------------------------------------------------------- #
def _import_loose(filepath, settings, image_cache, report, mat_log, parent_coll):
    base = os.path.splitext(os.path.basename(filepath))[0]
    try:
        objs, _ = _import_scx_objects(filepath, base, settings, image_cache, report, mat_log)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)
    if not objs:
        return 0, None
    # SLRR meshes are in centimetres -- bring them to metres like assembly does.
    transform = (_root_matrix(settings["convert_axis"], settings["scale"])
                 @ Matrix.Scale(slrr_cfg.MESH_SCALE, 4))
    _link_part(objs, transform, base, parent_coll)
    return len(objs), None


# --------------------------------------------------------------------------- #
# assembled car import (uses the scripts/ .cfg + .java scene graph)
# --------------------------------------------------------------------------- #
def _import_assembled(meshes_dir, scripts_dir, settings, image_cache, report, mat_log):
    asm = slrr_cfg.assemble(scripts_dir)
    car = asm.info.get("car_name", "SLRR Car")

    car_coll = _new_collection(car, settings["context"].scene.collection)
    root_mtx = _root_matrix(settings["convert_axis"], settings["scale"])
    scale_m = Matrix.Scale(slrr_cfg.MESH_SCALE, 4)  # centimetres -> metres

    # case-insensitive index of the .scx files actually on disk
    scx_index = {}
    try:
        for fn in os.listdir(meshes_dir):
            if fn.lower().endswith(".scx"):
                scx_index[os.path.splitext(fn)[0].lower()] = os.path.join(meshes_dir, fn)
    except OSError:
        pass

    count = 0
    missing = []
    failures = []

    def add(scx_name, world_slrr, coll_name=None, parent=None):
        """world_slrr is a mathutils.Matrix in SLRR (metres, Y-up) space."""
        nonlocal count
        f = scx_index.get(scx_name.lower())
        if not f:
            missing.append(scx_name)
            return 0
        try:
            objs, _ = _import_scx_objects(
                f, coll_name or scx_name, settings, image_cache, report, mat_log)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failures.append("%s: %s" % (scx_name, exc))
            return 0
        if not objs:
            return 0
        world_mtx = root_mtx @ world_slrr @ scale_m
        _link_part(objs, world_mtx, coll_name or scx_name, parent or car_coll)
        count += len(objs)
        return len(objs)

    # the chassis body mesh is the root (referenced only by id in the main cfg).
    chassis = slrr_cfg.pick_chassis_mesh(
        scx_index.keys(), asm.info.get("main_cfg", ""), asm.info.get("car_java", ""))
    if chassis:
        add(chassis, Matrix.Identity(4))

    for scx_name, world in asm.placements.items():
        add(scx_name, Matrix(world))

    if settings["import_wheels"] and asm.wheels:
        wheel_names = [n for n in scx_index
                       if n.startswith(("rim", "tire", "tyre", "wheel"))
                       and "_phys" not in n]
        rims = [n for n in wheel_names if n.startswith(("rim", "wheel"))]
        tyres = [n for n in wheel_names if n.startswith(("tire", "tyre"))]
        pick = []
        if rims:
            pick.append(sorted(rims, key=len)[0])
        if tyres:
            pick.append(sorted(tyres, key=len)[0])
        if pick:
            wheels_coll = _new_collection("wheels", car_coll)
            flip_y = Matrix.Rotation(math.pi, 4, 'Y')  # face the rim outward on the RH side
            for w in asm.wheels:
                base = Matrix.Translation(w["pos"])
                if w["right"]:
                    base = base @ flip_y
                for wn in pick:
                    add(wn, base, coll_name="%s_%s" % (wn, w["corner"]),
                        parent=wheels_coll)

    # report file
    if settings["write_report"]:
        out_dir = settings["texture_dir"] or os.path.dirname(meshes_dir)
        try:
            os.makedirs(out_dir, exist_ok=True)
            report_path = os.path.join(out_dir, _safe(car) + "_info.txt")
            _write_report(report_path, asm, mat_log, settings["_copied"],
                          settings["_missing"])
            settings["_report_path"] = report_path
        except OSError as exc:
            report({'WARNING'}, "Could not write car report: %s" % exc)

    return count, asm, missing, failures


def _safe(name):
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip() or "car"


def _write_report(path, asm, mat_log, copied, missing_tex=None):
    info = asm.info
    st = info.get("stats", {})
    L = []
    L.append("=" * 70)
    L.append("  %s" % info.get("car_name", "SLRR Car"))
    L.append("=" * 70)
    if info.get("maker"):
        L.append("Maker:        %s" % info["maker"])
    if info.get("description"):
        L.append("Description:  %s" % info["description"])
    L.append("")
    L.append("-- Mass / weight (kg, from physics bodies) --")
    L.append("  Chassis (incl. frame/engine bay):  %8.1f" % info.get("chassis_mass", 0.0))
    L.append("  Fitted body parts:                 %8.1f" % info.get("fitted_parts_mass", 0.0))
    L.append("  Approx. total (chassis+parts):     %8.1f"
             % (info.get("chassis_mass", 0.0) + info.get("fitted_parts_mass", 0.0)))
    L.append("")
    L.append("-- Drivetrain --")
    if "enginepower" in st:
        L.append("  enginepower:   " + " ".join("%g" % v for v in st["enginepower"]))
    if "brakepower" in st:
        L.append("  brakepower:    " + " ".join("%g" % v for v in st["brakepower"]))
    if "gear_ratios" in st:
        L.append("  gear ratios:   " + " ".join("%g" % v for v in st["gear_ratios"]))
    if "final_drive" in st:
        L.append("  final drive:   " + " ".join("%g" % v for v in st["final_drive"]))
    if "maxsteer" in st:
        L.append("  max steer:     " + " ".join("%g" % v for v in st["maxsteer"]))
    L.append("")
    wp = info.get("wheel_positions", [])
    if len(wp) >= 4:
        wb = abs(wp[0][2] - wp[2][2])
        tr = abs(wp[0][0] - wp[1][0])
        L.append("-- Wheels (metres, SLRR space) --")
        L.append("  wheelbase ~ %.3f m   track ~ %.3f m" % (wb, tr))
        for i, p in enumerate(wp):
            L.append("  wheel %d: (%.3f, %.3f, %.3f)" % (i, p[0], p[1], p[2]))
        L.append("")

    L.append("-- Fitted body parts (%d) --" % len(info.get("parts", {})))
    for nm, d in sorted(info.get("parts", {}).items()):
        rid = ("0x%X" % d["res_id"]) if d.get("res_id") is not None else "?"
        L.append("  %-22s id=%-8s mass=%6.2f kg  alias=%s"
                 % (nm, rid, d.get("mass", 0.0), d.get("alias", "")))
    L.append("")

    allp = info.get("all_parts", [])
    if allp:
        L.append("-- All fitted parts referenced by the car script --")
        for pkg, rid, label in allp:
            L.append("  %-40s  (%s 0x%X)" % (label, pkg, rid))
        L.append("")

    if mat_log:
        L.append("-- Materials (%d) --" % len(mat_log))
        for nm, d in sorted(mat_log.items()):
            bits = []
            if d.get("diffuse"):
                bits.append("diffuse=%s" % d["diffuse"])
            if d.get("reflection"):
                bits.append("reflection=%s" % d["reflection"])
            if d.get("glass"):
                bits.append("glass")
            L.append("  %-20s %s" % (nm, ", ".join(bits) if bits else "(colour only)"))
        L.append("")

    if copied:
        L.append("-- Textures found & applied (%d) --" % len(copied))
        for t in sorted(copied):
            L.append("  " + t)
        L.append("")

    if missing_tex:
        L.append("-- Textures MISSING from this car folder (%d) --" % len(missing_tex))
        L.append("  These are shared base-game textures not shipped with this mod.")
        L.append("  Point 'Extra Texture Folder' at your SLRR install, or use")
        L.append("  File > External Data > Find Missing Files after installing them.")
        for t in sorted(missing_tex):
            L.append("  " + t)
        L.append("")

    if asm.unresolved:
        L.append("-- Parts placed via wheel slots / not in attach graph --")
        L.append("  " + ", ".join(sorted(set(asm.unresolved))))
        L.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))


# --------------------------------------------------------------------------- #
# operator
# --------------------------------------------------------------------------- #
class IMPORT_OT_slrr_scx(Operator, ImportHelper):
    """Import Street Legal Racing: Redline .scx mesh files"""
    bl_idname = "import_scene.slrr_scx"
    bl_label = "Import SLRR SCX"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".scx"
    filter_glob: StringProperty(default="*.scx", options={'HIDDEN'})

    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    assemble_car: BoolProperty(
        name="Assemble Full Car (use scripts)",
        description="If a car 'scripts' folder is found, place fitted parts where the game puts them",
        default=True,
    )
    import_wheels: BoolProperty(
        name="Place Wheels",
        description="Place rim + tire meshes at the 4 wheel positions when assembling",
        default=True,
    )
    write_report: BoolProperty(
        name="Write Car Info File",
        description="Write a <car>_info.txt with weights, engine, gearing, materials and textures",
        default=True,
    )

    scan_folder: BoolProperty(
        name="Import Entire Folder",
        description="Import every .scx in the selected folder (loose mode)",
        default=False,
    )
    recursive: BoolProperty(name="Include Subfolders", default=True)
    include_physics: BoolProperty(
        name="Import Physics Meshes",
        description="Include _phys_* collision hulls",
        default=False,
    )

    convert_axis: BoolProperty(name="Convert Axes (Y-up -> Z-up)", default=True)
    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, max=1000.0)
    import_normals: BoolProperty(name="Import Custom Normals", default=True)
    group_empty: BoolProperty(name="Group Under Empty", default=True)
    per_file_collection: BoolProperty(name="Collection Per Folder", default=True)

    copy_textures: BoolProperty(name="Copy Textures To Folder", default=True)
    texture_dir: StringProperty(name="Texture Folder", subtype='DIR_PATH', default="")
    extra_texture_dir: StringProperty(
        name="Extra Texture Folder",
        description="Also search here (e.g. your SLRR install) for shared textures "
                    "not shipped with the car",
        subtype='DIR_PATH', default="")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Car Assembly", icon='AUTO')
        box.prop(self, "assemble_car")
        col = box.column()
        col.enabled = self.assemble_car
        col.prop(self, "import_wheels")
        col.prop(self, "write_report")

        box = layout.box()
        box.label(text="Loose / Bulk", icon='FILE_FOLDER')
        box.prop(self, "scan_folder")
        row = box.row()
        row.enabled = self.scan_folder
        row.prop(self, "recursive")
        box.prop(self, "include_physics")

        box = layout.box()
        box.label(text="Transform", icon='ORIENTATION_GLOBAL')
        box.prop(self, "convert_axis")
        box.prop(self, "scale")
        box.prop(self, "import_normals")

        box = layout.box()
        box.label(text="Textures", icon='TEXTURE')
        box.prop(self, "copy_textures")
        sub = box.column()
        sub.enabled = self.copy_textures
        sub.prop(self, "texture_dir")
        box.prop(self, "extra_texture_dir")

    # ------------------------------------------------------------------ #
    def _selected_dir(self):
        if self.directory:
            return self.directory
        if self.filepath:
            return os.path.dirname(self.filepath)
        return ""

    @staticmethod
    def _has(dirpath, ext):
        try:
            return any(f.lower().endswith(ext) for f in os.listdir(dirpath))
        except OSError:
            return False

    def _resolve_dirs(self):
        """Work out the meshes folder and the scripts folder from whatever the
        user picked -- a file, the ``meshes`` folder, or the ``<car>_data``
        folder that contains ``meshes`` and ``scripts``."""
        sel = self._selected_dir().rstrip("\\/")
        if not sel:
            return None, None

        # meshes folder: the selection, or its 'meshes' subfolder
        meshes = None
        if self._has(sel, ".scx"):
            meshes = sel
        else:
            sub = os.path.join(sel, "meshes")
            if os.path.isdir(sub) and self._has(sub, ".scx"):
                meshes = sub

        # the car's data folder = the one that holds meshes/ + scripts/
        if meshes and os.path.basename(meshes).lower() == "meshes":
            data = os.path.dirname(meshes)
        else:
            data = sel

        scripts = None
        for cand in (os.path.join(data, "scripts"),
                     os.path.join(sel, "scripts"),
                     os.path.join(os.path.dirname(sel), "scripts")):
            if os.path.isdir(cand) and self._has(cand, ".cfg"):
                scripts = cand
                break
        # if we found scripts but not meshes, look for meshes next to scripts
        if scripts and not meshes:
            cand = os.path.join(os.path.dirname(scripts), "meshes")
            if os.path.isdir(cand) and self._has(cand, ".scx"):
                meshes = cand
        return meshes, scripts

    def _explicit_files(self):
        """The specific .scx files the user actually picked (vs. a folder)."""
        picked = []
        if self.files:
            for f in self.files:
                if f.name and f.name.lower().endswith(".scx"):
                    picked.append(os.path.join(self.directory, f.name))
        if not picked and self.filepath and os.path.isfile(self.filepath) \
                and self.filepath.lower().endswith(".scx"):
            picked.append(self.filepath)
        return sorted(set(picked))

    def _gather_paths(self):
        paths = []
        if self.scan_folder and self.directory:
            walker = os.walk(self.directory) if self.recursive else [
                (self.directory, [], os.listdir(self.directory))]
            for root, _d, names in walker:
                for n in names:
                    if n.lower().endswith(".scx"):
                        paths.append(os.path.join(root, n))
        elif self.files:
            for f in self.files:
                if f.name:
                    paths.append(os.path.join(self.directory, f.name))
        if not paths and self.filepath:
            paths.append(self.filepath)
        return sorted(set(paths))

    def _base_settings(self, context):
        extra = []
        if self.extra_texture_dir:
            extra.append(bpy.path.abspath(self.extra_texture_dir))
        return {
            "context": context,
            "convert_axis": self.convert_axis,
            "scale": self.scale,
            "import_normals": self.import_normals,
            "import_wheels": self.import_wheels,
            "write_report": self.write_report,
            "texture_dir": bpy.path.abspath(self.texture_dir) if self.texture_dir else "",
            "copy_textures": self.copy_textures,
            "extra_texture_dirs": extra,
            "_copied": set(),
            "_missing": set(),
            "_mat_cache": {},
        }

    def execute(self, context):
        image_cache = {}
        mat_log = {}
        meshes_dir, scripts_dir = self._resolve_dirs()

        # If the user picked specific file(s) (not a whole folder), import just
        # those -- this is how you grab a single mesh or a couple of variants.
        explicit = self._explicit_files()
        assemble = (self.assemble_car and scripts_dir and meshes_dir
                    and not (explicit and not self.scan_folder))

        # ---- assembled car mode ----
        if assemble:
            settings = self._base_settings(context)
            try:
                n, asm, missing, failures = _import_assembled(
                    meshes_dir, scripts_dir, settings, image_cache, self.report, mat_log)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self.report({'ERROR'}, "Assembly failed: %s (see System Console)" % exc)
                return {'CANCELLED'}
            if n == 0:
                detail = (failures[0] if failures
                          else "no meshes found in %s" % meshes_dir)
                self.report({'ERROR'},
                            "Assembled 0 objects (%d parts resolved). First issue: %s"
                            % (len(asm.placements), detail))
                print("[SCX] assembled 0 objects. placements=%d missing=%d failures=%d"
                      % (len(asm.placements), len(missing), len(failures)))
                for f in failures[:10]:
                    print("[SCX]   FAIL", f)
                return {'FINISHED'}
            msg = "Assembled '%s': %d objects, %d parts" % (
                asm.info.get("car_name", "car"), n, len(asm.placements))
            if settings.get("_report_path"):
                msg += "  |  info: " + os.path.basename(settings["_report_path"])
            if missing:
                msg += "  |  %d meshes missing" % len(missing)
            if settings["_missing"]:
                msg += "  |  %d textures not on disk" % len(settings["_missing"])
            if failures:
                msg += "  |  %d failed (see console)" % len(failures)
                for f in failures[:10]:
                    print("[SCX] FAIL", f)
            self.report({'INFO'}, msg)
            return {'FINISHED'}

        # ---- loose / bulk mode ----
        paths = self._gather_paths()
        if not paths:
            self.report({'ERROR'}, "No .scx files selected")
            return {'CANCELLED'}

        # one shared settings so material de-dup & texture sets span all files
        settings = self._base_settings(context)
        # a top-level collection to hold each file's own collection
        folder = (os.path.basename(os.path.dirname(os.path.dirname(paths[0])))
                  or os.path.basename(os.path.dirname(paths[0])) or "SCX Import")
        parent_coll = bpy.data.collections.new(folder)
        context.scene.collection.children.link(parent_coll)

        total = skipped = 0
        errors = []
        for path in paths:
            base = os.path.basename(path)
            if not self.include_physics and base.lower().startswith("_phys"):
                skipped += 1
                continue
            n, err = _import_loose(path, settings, image_cache, self.report,
                                   mat_log, parent_coll)
            total += n
            if err:
                errors.append("%s: %s" % (base, err))

        msg = "Imported %d object(s) from %d file(s)" % (total, len(paths))
        if skipped:
            msg += ", skipped %d physics mesh(es)" % skipped
        if settings["_missing"]:
            msg += ", %d textures not on disk" % len(settings["_missing"])
        if self.assemble_car and not scripts_dir and len(paths) > 1:
            msg += "  |  no 'scripts' folder -> loose import (parts NOT positioned)"
        if errors:
            for e in errors[:5]:
                print("[SCX] " + e)
            self.report({'WARNING'}, msg + ", %d failed (see console)" % len(errors))
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


def _menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_slrr_scx.bl_idname, text="Street Legal Racing (.scx)")


_classes = (IMPORT_OT_slrr_scx,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func_import)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
