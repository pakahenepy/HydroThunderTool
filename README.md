# HydroThunderTool

Reverse-engineering toolkit for **Hydro Thunder (PC, Midway/Eurocom 2000)** — a
single pure-stdlib Python script that unpacks and decodes the game's `Hydro.fsd`
asset archive.

## What works

- **FSD container + EDL1 codec** — full extraction of all 542 files
  (Huffman+LZ decompressor, verified byte-identical on all 13,572 blocks)
- **Filenames** — 536/542 original paths recovered from `HYDRO.EXE` (`names.json`)
- **Textures** — all EGF UI textures, all 1,496 `T*` world textures, and all
  1,155 `M*` mipmapped track-surface textures → PNG (the `fmt` field is the
  3dfx Glide `GrTextureFormat_t` enum)
- **Loading screens** — all 39 (`B*` records, ARGB1555)
- **3D models** — all 1,741 `G*` records → OBJ **with materials**: UVs,
  surface groups, and `.mtl` texture bindings recovered from the container's
  relocation trailers (format reversed from the exe's draw code)
- **Boat physics** — `P*` parameter records → readable text (mass, drag,
  buoyancy, handling for all 13 boats)

## Usage

Put `Hydro.fsd` from your game install next to the script, then:

```
python hydrotool.py all Hydro.fsd -o out   # everything: extract, textures,
                                           # world split, models, params
```

Outputs land in `out/bc0abcfa.bin_split/`: `_textures/`, `_screens/`,
`_models/` (OBJ+MTL, open in Blender), `_params/`. Individual steps are also
available as subcommands (`extract`, `textures`, `world`, `models`, `params`) —
see `python hydrotool.py --help`.

No dependencies. Game data is **not** included in this repo — bring your own copy.

## Docs

- [FSD_format.md](FSD_format.md) — complete file-format documentation
  (container, codec, hashing, every decoded record type, exe function addresses)
- [NEXT_STEPS.md](NEXT_STEPS.md) — project state, remaining problems
  (H* collision data, A* animations, D* camera scripts, track assembly),
  and ruled-out dead ends

![models](models_showcase.png)
