# HydroThunderTool

Reverse-engineering toolkit for **Hydro Thunder (PC, Midway/Eurocom 2000)** — a
single pure-stdlib Python script that unpacks and decodes the game's `Hydro.fsd`
asset archive.

## What works

- **FSD container + EDL1 codec** — full extraction of all 542 files
  (Huffman+LZ decompressor, verified byte-identical on all 13,572 blocks)
- **Filenames** — 536/542 original paths recovered from `HYDRO.EXE` (`names.json`)
- **Textures** — all EGF UI textures and all 1,496 world textures → PNG
  (the `fmt` field is the 3dfx Glide `GrTextureFormat_t` enum)
- **Loading screens** — all 39 (`B*` records, ARGB1555)
- **3D models** — all 1,741 `G*` records → OBJ with UVs and surface groups
  (format reversed from the exe's draw code; boats, props, HUD elements)
- **Boat physics** — `P*` parameter records → readable text (mass, drag,
  buoyancy, handling for all 13 boats)

## Usage

Put `Hydro.fsd` from your game install next to the script, then:

```
python hydrotool.py all Hydro.fsd -o out          # extract + textures + world split
python hydrotool.py models out/bc0abcfa.bin_split # export all G* models to OBJ
python hydrotool.py params out/bc0abcfa.bin_split # dump boat physics to text
```

No dependencies. Game data is **not** included in this repo — bring your own copy.

## Docs

- [FSD_format.md](FSD_format.md) — complete file-format documentation
  (container, codec, hashing, every decoded record type, exe function addresses)
- [NEXT_STEPS.md](NEXT_STEPS.md) — project state, open problems
  (M* terrain patches, material→texture binding), and ruled-out dead ends

![models](models_showcase.png)
