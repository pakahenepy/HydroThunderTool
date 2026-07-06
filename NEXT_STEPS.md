# Hydro Thunder RE — state & path forward

## Done (all in `hydrotool.py`, pure stdlib)
- **Container / decompression**: FSD format + EDL1 codec fully reversed, verified.
- **Names**: 536/542 recovered from HYDRO.EXE (`names.json`). Files extract to real paths.
- **Textures**: all EGF UI textures → PNG (1555/4444, flipped). Title screen (id 1720011c) is tiled → scrambled, documented.
- **World container** (`bc0abcfa`, 104MB): split into 4,588 named resources + `index.csv`.
- **World textures — SOLVED, all 1,496 → PNG**: `fmt` is the Glide `GrTextureFormat_t` enum (0=RGB332, 2=ALPHA_8, 3=I8, 4=AI44, 5=P_8 with embedded 256×ARGB8888 palette *first* then indices, 8=ARGB8332, 11=1555, 12=4444, 13=AI88, 14=AP88). Size formula `36+w·h·bpp(+1024 pal)` verified on every file. Greyscale 8bpp textures are alpha/intensity formats by design — **no palettes are missing**. The `T_XTESTBB*` series is the same test image in 7 formats (built-in validation).
- **P\* resources = boat physics parameters** (NOT palettes): `name\0 + type + value` lists — MASS, BUOYANCY, DRAG_*, handling for all 13 boats × variants. `hydrotool.py params` dumps them to text. (Known quirk: declared size undercuts the last float; parse by count.)
- **ERM**: identified as per-track **radar maps**, not 3D models.

Run everything: `python hydrotool.py all Hydro.fsd -o out`

## Boat/world geometry — G* SOLVED, exporter working (2026-07-02)
**All 1,741 G models export to OBJ** (144,211 verts / 158,105 faces, 0 skipped): `python hydrotool.py models <splitdir>` → `_models/*.obj` with positions, UVs, and per-surface groups. Validated visually: Banshee/Razorback hulls, Tinytanic, trees, HUD elements all render correctly. Full format spec in FSD_format.md — the critical key was that **every offset is relative to record+4** (the engine's model pointer), and unused section slots contain stale tool-machine pointers (garbage — check counts before trusting offsets).

## M* textures + material binding — SOLVED (2026-07-02, same day)
- **M\*** (1,155) are NOT heightfields: they're the **mipmapped track-surface textures** (Glide fmt 11/12/13, full mip chain to 2×2). All decode into `_textures/`.
- **Materials → textures SOLVED** via the record **relocation trailers** (`FDFDFDFD` + `{name[12], u32 location}` entries between records — what we thought was 0xCD fill). Named entries bind texture resources to each material's +0x14 slot. `world_split` saves them to `relocs.json`; `models` emits `.mtl` + `usemtl`, so OBJs open textured in Blender. Details in FSD_format.md.

## H* = the track scene files — partially mapped (2026-07-06)
Confirmed: H files ARE the object-placement/track-assembly data (one per track + 3D menu scenes). Mapped so far: checkpoint/waypoint sector table, embedded track-surface geometry reusing G-format materials (M-texture imports patch mat+0x14), scene-node instance arrays (8-char type tags like `ANIMPENG`, model ptr + x/z + scale), 6,136-entry drawable pointer table → 308 chunk descriptors, spline waypoint streams. Full details + future entry points in FSD_format.md. **Track meshes EXPORT (2026-07-06)**: `hydrotool.py tracks` writes all 17 tracks' drivable world meshes to `_tracks/*.obj+mtl` (textured; verified Arctic + Venice). Remaining: node-type catalog (full object placement), splines/chunk semantics. Also fixed: reloc-trailer marker isn't always FDFDFDFD (match count field instead) — relocs.json now covers 1,635 records.

Still open (smaller, well-scoped):
- **A\*** prop animations, **D\*** camera scripts — surveyed, undecoded (see FSD_format.md).
- Glide capture remains unnecessary.

## Recommended path forward: Glide capture (deterministic)
Hydro Thunder is a 3dfx Glide game — no D3D/OpenGL imports; it `LoadLibrary`s `glide2x.dll` at runtime. That's the shortcut: capture one frame of real vertex data, diff against file bytes, and the format falls out in minutes instead of hours of static disassembly.

You don't need to "download Glide" as a thing to install per se — you need a **Glide wrapper that logs draw calls**. Options, easiest first:

1. **dgVoodoo2** (free, actively maintained). Drop its `glide2x.dll` next to Hydro.exe so the game runs on a modern GPU. It doesn't log geometry by itself, but it makes the game *run*, which is prerequisite for #3.
2. **nGlide** — same idea, simpler, but less debug-friendly.
3. **The actual capture** — two sub-options:
   - **x64dbg** (free): run the game, set a breakpoint on `grDrawVertexArray` / `grDrawVertexArrayContiguous` / `grDrawTriangle` inside the loaded `glide2x.dll`. When it hits, dump the vertex buffer pointer + count from the args. One hit = the exact vertex struct (stride, field order, UV/color layout).
   - **A logging Glide wrapper**: build/patch a `glide2x.dll` shim that logs every `grDrawVertexArray` call's vertices to a file. More setup, but captures a whole track at once. (openglide / psVoodoo sources are a starting point if you want to go this route.)

### What to capture
For each draw call, log: `mode`, `count`, and the raw bytes of each vertex (Glide `GrVertex` is typically 0x40 bytes: x,y,z, oow, r,g,b,a, then s/t per TMU). Save ~100 vertices from one boat.

### Then bring back
Upload the capture (or just paste 20-30 vertices as hex/floats + their count). With ground-truth verts I can:
1. Find those exact XYZ values inside the corresponding G/M file → locks the vertex section offset & stride.
2. Map the surrounding bytes to UV/normal/color → full vertex format.
3. Decode the index/section table by matching draw order → faces.
Result: a real OBJ/glTF exporter for all 1,741 G-files.

## If you'd rather stay static (no game running)
Next concrete task: disassemble the G/M section-parser (start from the resource that reads the 0x10-header counts) and map each count-dword to a section type. I can do this in a focused session; it's just slower and validated by eye rather than by ground truth.

## Already ruled out (don't re-test these)
Negative results from the sessions so far, so a fresh start doesn't waste effort:

- **EDL2 / LZ-only codec** (Zoinkity EDLdec2, QuickBMS): produces plausible-looking output but a consumption audit showed ~half the bitstream unread per block. Wrong. The correct codec is EDL1 (Huffman+LZ), already in `hydrotool.py`.
- **Cross-block LZ window** (both continuous-window and concatenated-payload models): wrong. Each EDL1 block is an independent bitstream decoding to exactly 0x2000 bytes; verified 0 out-of-window refs archive-wide.
- **The `0x44bd10` jump table** in HYDRO.EXE: looked like a record-type dispatcher but is just a tag *validity check* (M/T/m/t → valid, else reject). NOT the geometry parser. Don't chase it again.
- **`0x451950`**: it's a resource hashmap/manager (0x55c600 table, 0x20-byte entries), not a mesh reader.
- **Graphics-import xrefs**: there are none. No d3d/ddraw/opengl/glide in the import table — the game `LoadLibrary`s `glide2x.dll` at runtime, so static xrefs to draw calls don't exist. Must go dynamic (breakpoint the loaded DLL) — this is *why* the Glide-capture route is recommended.
- **Blind float32 sniffing of G/M files**: over-captures. Normals, UVs, and matrices are also float32, so a "find runs of plausible floats" scan pulls in non-vertex data. The decoded cube looked like 56 verts instead of 8. A point cloud from this is contaminated — not a usable model.
- **Assuming a fixed vertex-section slot** (e.g. always offs[1]): the section *count varies per file* (a detail LOD had 8 sections, a coarser LOD had 2). Section identity is keyed by the 0x10-header count dwords, not a fixed index. Any parser must read those counts, not hardcode positions.
- **`ERM!` as boat models**: they're per-track radar/minimap bitmaps (`radmap_A..Z`). There are no standalone 3D boat meshes in Hydro.fsd; geometry is inside the world container.

## The 6 still-unnamed FSD files
Not referenced by any literal path in the exe: five tiny EGFs (four 0x88, one 0x2008) and the world container `bc0abcfa`. Cosmetic — everything meaningful is named.

## ~~Loose texture thread~~ — CLOSED (2026-07-02)
The "missing palette" theory was wrong twice over: `P*` files are boat *physics parameter* lists (see FSD_format.md), and the greyscale 8bpp textures are Glide ALPHA_8/INTENSITY_8/AI_44 formats that have no palette by design (`TBB*SH00` are 8×8 boat shadows, not skins). fmt-5 files are self-contained P_8 textures with the palette embedded before the pixels — including the 32-frame animated water palettes per track. All 1,496 T* textures now decode; nothing is left greyscale that shouldn't be.
