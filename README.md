# SLRR SCX Importer for Blender

A Blender add-on that imports **Street Legal Racing: Redline** (`.scx`) car models —
the Invictus *INVO* mesh format — with geometry, UVs, normals, materials and
textures, and can **rebuild a whole car in-place** from its `scripts` folder.

Built to recover old SLRR models when the 3ds Max sources are long gone.

![Blender](https://img.shields.io/badge/Blender-4.2%E2%80%935.x-orange)
![License](https://img.shields.io/badge/license-MIT-blue)
![Format](https://img.shields.io/badge/SCX-v3%20%7C%20v4%20%7C%20v5%20%7C%20v6-green)

---

## Features

- **Geometry, UVs and per-vertex normals** (imported as custom split normals so
  shading matches the game). Handles all four SCX versions seen in the wild —
  `v3`/`v5` (inline) and `v4`/`v6` (directory), including multi-block v5 meshes.
- **Assemble Full Car** — when a car's `scripts/` folder is present, the add-on
  reads the `.cfg` `slot`/`attach` hierarchy and the fitted-parts list from
  `src/<Car>.java` and places every body part + wheels exactly where the game
  puts them (correct variant, correct chassis, mirrored wheels).
- **Materials, SLRR-style** — diffuse colour + diffuse texture, reflection /
  environment map wired to specular, real transparency only for glass/alpha,
  and **material de-duplication** (identical materials are shared, not copied).
- **Textures** — resolved recursively from the car's `textures/` tree (incl.
  subfolders), matched by name or stem. Missing shared base-game textures are
  linked as named placeholders (so *Find Missing Files* works), and an **Extra
  Texture Folder** option lets you point at your SLRR install.
- **Per-part collections** — every imported mesh gets its own collection, so
  parts are easy to toggle and edit.
- **Car info report** — writes `<Car>_info.txt` with per-part and total weights,
  engine / gearing / brakes, wheelbase & track, the full fitted-parts list, and
  every material with its textures.

## Install

Download [`slrr_scx_importer.zip`](slrr_scx_importer.zip) from this repo.

- **Blender 4.2 – 5.x (extension):** `Edit ▸ Preferences ▸ Get Extensions ▸ ▾ ▸
  Install from Disk…` → pick the zip.
- **Blender 3.x / 4.0–4.1 (legacy add-on):** `Edit ▸ Preferences ▸ Add-ons ▸
  Install…` → pick the zip → enable **SLRR SCX Importer**.

Then: `File ▸ Import ▸ Street Legal Racing (.scx)`.

## Usage

A SLRR car unpacks to a `<Car>_data` folder (`meshes/` + `textures/` +
`scripts/`). Point the importer at it:

| You want | Do this |
|---|---|
| One / a few meshes | Select the `.scx` file(s) and Import |
| The complete built car | Select the `<Car>_data` folder, **Assemble Full Car** ✓ |
| Every variant of every part | **Assemble** ✗, **Import Entire Folder** ✓, select `meshes/` |

Full option reference: [`scx_import/README.md`](scx_import/README.md).

## How it works

- [`scx_import/scx_format.py`](scx_import/scx_format.py) — a dependency-free
  (`bpy`-less) parser for the SCX binary format. The format is documented in its
  header comment.
- [`scx_import/slrr_cfg.py`](scx_import/slrr_cfg.py) — the `.cfg` / `.java`
  scene-graph assembler.
- [`scx_import/__init__.py`](scx_import/__init__.py) — the Blender integration.

## Notes

- Textures come from the car's unpacked `textures/` folder. The packed `.rpk`
  (`RPAK`) archives are **not** unpacked — they only reference the loose files.
- This repo ships **no game assets**. Street Legal Racing: Redline and its file
  formats are the property of Invictus Games. Use the add-on only with content
  you are entitled to.

## License

[MIT](LICENSE) © Connor Moyle. Not affiliated with or endorsed by Invictus Games.
