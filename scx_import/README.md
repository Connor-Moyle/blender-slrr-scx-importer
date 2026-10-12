# SLRR SCX Importer

A Blender add-on that imports **Street Legal Racing: Redline** (`.scx`) models —
the Invictus *INVO* mesh format — with geometry, UVs, normals, materials and
textures. Built to recover old car models when the 3ds Max sources are gone.

Works with both SCX versions found in SLRR cars:

* **Version 4** – the modern body / panel / interior / light / wheel meshes.
* **Version 3** – older inline meshes, used for `_phys_*` collision hulls and a
  few light meshes.

## Features

* **Assemble Full Car** — when the car's `scripts/` folder is present, the parts
  are placed exactly where the game puts them by reading the `.cfg` `slot`/
  `attach` hierarchy and the fitted-parts list from `src/<Car>.java`. Wheels are
  placed at the four suspension positions. (Meshes are modelled in local space,
  so this is what makes the parts *fit together* instead of piling at the origin.)
* **Car info report** — writes `<Car>_info.txt` next to the textures with the
  car name, per-part and total **weights**, engine / gearing / brakes, wheelbase
  & track, the full fitted-parts list (engine, suspension, brakes…), every
  material and which textures it uses, and the texture list.
* Model data, **UVs**, and per-vertex **normals** (custom split normals so
  shading matches the game).
* **Materials** configured SLRR-style: diffuse colour + diffuse texture, the
  **reflection / environment map** (`odlesk*`) wired to specular, and real
  **transparency only for glass / alpha** materials (not every PNG).
* **Textures**: resolved from the car's `textures/` folder, optionally **copied
  into a folder of your choice**, with Blender linked to the copies.
* **Loose / bulk import**: pick individual files or a whole folder when you just
  want raw meshes without assembly.
* Physics collision hulls (`_phys_*`) are skipped by default (toggle to include).

## Install

### Blender 4.2 – 5.x (extension)
1. `Edit ▸ Preferences ▸ Get Extensions ▸ ▾ ▸ Install from Disk…`
2. Choose `slrr_scx_importer.zip`.

### Blender 3.x / 4.0–4.1 (legacy add-on)
1. `Edit ▸ Preferences ▸ Add-ons ▸ Install…`
2. Choose `slrr_scx_importer.zip`, then tick **SLRR SCX Importer**.

## Use

`File ▸ Import ▸ Street Legal Racing (.scx)`

Options in the import panel:

| Option | What it does |
|---|---|
| **Assemble Full Car (use scripts)** | If a sibling `scripts/` folder is found, place fitted parts where the game puts them. Pick any `.scx` inside `meshes/` (or the folder) to trigger it. |
| **Place Wheels** | Put rim + tire at the 4 wheel positions when assembling. |
| **Write Car Info File** | Write `<Car>_info.txt` (weights, engine, materials, textures). |
| **Import Entire Folder** | Loose mode: import every `.scx` in the folder as-is. |
| **Include Subfolders** | Recurse when importing a folder. |
| **Import Physics Meshes** | Also import `_phys_*` collision hulls. |
| **Convert Axes (Y-up → Z-up)** | Rotate from SLRR's Y-up to Blender's Z-up. |
| **Scale** | Uniform import scale (meshes assemble at ~real-world metres). |
| **Import Custom Normals** | Apply the file's per-vertex normals. |
| **Copy Textures To Folder** / **Texture Folder** | Copy referenced textures to a destination you choose. The info file is written here too. |

### Three ways to use it

* **One / a few meshes** — select the specific `.scx` file(s) in the browser and
  Import. Only those are imported, at their own coordinates. (Picking specific
  files always wins over assembly, so you can grab a single part.)
* **Rebuild the whole car** — select the `<Car>_data` folder (or its `meshes/`
  folder, or any single file in it) with **Assemble Full Car** ticked. It finds
  `meshes/` + `scripts/` itself, loads the chassis + the fitted parts + wheels,
  and writes the info file. Only the *stock/fitted* variant of each part is used.
* **Every variant of every part** — turn **Assemble Full Car** off, tick
  **Import Entire Folder**, and point it at the `meshes/` (or `<Car>_data`)
  folder. You get all alternates (every bumper, hood, wing, chassis trim…),
  which will overlap — handy for picking parts by hand.

A car folder can hold several trims (GT-R / GTS / Nismo / LHD…); assembly picks
the base right-hand-drive GT-R-style variant and matches its chassis mesh.

### Where the files live
SLRR cars unpack to a `<Car>_data` folder:

```
<Car>_data/
  meshes/     *.scx  +  *.tex   (mesh + its texture list)
  textures/   *.png *.jpg *.dds  (the images)
  scripts/    *.class
```

Point the importer at `meshes/` and it finds the sibling `textures/` folder
automatically.

## Notes / limits

* Textures come from the already-unpacked `textures/` folder. The packed
  `.rpk` (`RPAK`) archives are **not** unpacked by this add-on.
* Reflection maps are hooked to the Principled *Specular Tint* input on Blender
  versions that expose it as a colour; otherwise they are still imported and
  labelled so nothing is lost.
* `.tex` files with more entries than a mesh has sub-meshes is normal — the
  extras are reflection/alpha maps referenced by individual materials.

## How it works

`scx_format.py` is a dependency-free parser (no `bpy`) that turns an `.scx`
into plain Python data; `__init__.py` is the Blender layer that builds meshes,
materials and textures from it. The format documentation is in the header
comment of `scx_format.py`.
