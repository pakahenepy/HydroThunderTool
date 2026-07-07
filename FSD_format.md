# Hydro Thunder (PC) `Hydro.fsd` format

Everything below verified against the retail file (67,234,920 bytes). All integers little-endian. TODO update this

## Container

| offset | contents |
|---|---|
| 0x00000 | `"FSD"` + u8 version (0x02) |
| 0x00004 | directory: 2048 slots × 16 bytes |
| 0x08004 | block table: 32768 × u32 |
| 0x28004 | data region |

**Directory entry** (sorted ascending by hash; empty slots are zero):

```
u32 hash    Encode_String() of uppercase full path (see Hashing)
u32 offset  physical offset of asset data
u32 size    decompressed size
u32 block   0 = stored raw at offset
            N = EDL1-compressed; blocks N, N+1, ... in the block table,
                one per 0x2000 bytes of output. offset == block_table[N].
```

542 entries used: 459 raw ESF sounds + 83 compressed (69 EGF, 13 ERM, 1 world container).

**Block table**: physical offsets of compressed blocks (index 0 unused). Entries 1..13573 used; the final entry is an EOF sentinel. Duplicate entries appear at file boundaries — harmless fenceposts. Byte-coverage audit: the data region has zero gaps and no orphan data.

## Compression (EDL type 1)

Block: `"EDL\x01"` + u32 csize (total incl. 12-byte header) + u32 0x2000, then bitstream.
Each block is an **independent** LSB-first bitstream decoding to exactly 0x2000 bytes (a file's final block: less). No LZ window crosses block boundaries (verified: 0 out-of-window refs archive-wide).

Deflate-like stream grammar, repeated until an EOF bit:

- read 1 bit **mode**
- **mode 1** (Huffman segment):
  - 9-bit count → literal/length code lengths, then 9-bit count → distance code lengths. Code lengths are nibbles read as: 1 flag bit; if set, read a new 4-bit value into `stack`, else reuse `stack` (which persists across both tables and across segments). A zero count keeps the previous table.
  - Tables built canonically into two-level lookups (10-bit primary for literal/length, 8-bit for distance; long codes chain via a secondary table).
  - Symbols: <0x100 = literal byte; 0x100 = end of segment; >0x100 = match: length from base/extra tables (+3 min), then distance symbol (base/extra, +1 min), copy.
- **mode 0**: 15-bit count, then that many raw bytes (8 bits each).
- read 1 bit: 1 = end of block, 0 = another segment.

Bit reservoir refills 4 bytes (LE) at a time; `csize` is always a multiple of 4.
Reference implementation: `fsd_extract.py` (pure Python, ported from Noah "Zoinkity" Granath's decoder and verified byte-identical on all 13,572 blocks).

## Hashing / filenames

Filenames are not stored; the directory keys on a hash of the uppercase full path:

```python
def encode_string(s):
    ret = shift = 0
    for ch in s:
        ret = (ret + (ord(ch) << shift)) & 0xFFFFFFFF
        shift = 0 if shift >= 0x18 else shift + 8
    return (ret + len(s)) & 0xFFFFFFFF
```

(= sum of the string's LE dwords + length; recovered from HYDRO.EXE via the RazorBack project.) Known paths: `H:\SOUND\<n>.ESF` (all 459 sounds), `H:\WAVMUSIC\TRACK<n>.ESF`. The hash is weak enough to dictionary-attack the remaining 83 names if candidate strings turn up (e.g. in HYDRO.EXE).

## Asset types

Every asset begins `4CC type + u32 info`, data at +8.

**ESF** (`"ESF"` + u8 version, 8 here) - mono sound, stored raw. info low 24 bits = decoded PCM byte count (= (filesize-8)*4), top-byte flags: **0x80 = DVI IMA ADPCM** (else raw PCM16), **0x40 = loop**, **0x20 = 22050 Hz (else 11025)**, **0x10 = 16-bit**. All 459 retail files are 0x90/0xD0 -> IMA, 11025 Hz, 16-bit. Decode: standard IMA step/index tables, high nibble first, state {sample=0, index=0} (exe decoder at `0x46a6b0`, tables `0x4f4790`/`0x4f47d0`; matches vgmstream esf.c). `hydrotool.py sounds` -> `sounds/wav/`.

**EGF** (`"EGF\x04"`) — 16bpp texture. info: bits 11..31 = height, bits 1..10 = width (NPOT widths padded — stride = payload/(h·2)), bit 0 = format: **0 = ARGB1555 (bit15 = 1-bit alpha), 1 = ARGB4444**. Pixels row-major at +8. Converter: `egf2png.py`. EGFs wider than 256 (the single 640x480 `loading.egf`) are stored as row-major 256x256 tiles (Glide's max texture size); `egf_to_png` de-tiles them. Decoded 2026-07-06 — renders perfectly.

**ERM** (`"ERM!"`) — model (13 files = the 13 boats, ids sequential). Header u32s look like counts (e.g. 0x2A0/0x1F0/0x99) followed by zeroed tables and u16 index lists. Internal structure not yet mapped.

**World container** (id `bc0abcfa`, 104 MB) — starts `"ABCDEFGHIJKLMNOPQRS\0"`, then an offset table, small floats, and repeating `DATA` chunk sections. Streamed track/world data; extracts fine, internals unexplored.

## Filename recovery from HYDRO.EXE

The exe embeds literal asset paths (`h:\data\textures\*.egf`, `h:\data\radar\radmap_%c.erm`, `h:\sound\%d.esf`, `h:\wavmusic\track%02d.esf`). Mining these + the numeric/`%c` templates + a hash dictionary attack recovers **537/542** names (`e2c5a941` = `H:\DATA\TEXTURES\TEST.EGF`). `names.json` (id→path) ships alongside; `fsd_extract.py` loads it automatically and lays files out under their real `data/...` tree. The 5 leftovers are four tiny 8x8 EGFs (0x88 bytes) and the world container `bc0abcfa`, none referenced by a literal string (dictionary attacks over all exe strings x path patterns found nothing — cosmetic).

The EGF loader in the exe (at .text 0x46e94a) confirms the texture header decode exactly: `height = info >> 11`, `width = (info >> 1) & 0x3ff`, `format = info & 1`, buffer = `w*h*2`.

## ERM = radar maps (not boat models)

The 13 ERM files are the **radar/minimap overlays**, one per track — path `h:\data\radar\radmap_<A..Z>.erm` (letters A,C,D,G,J,M,N,P,R,V,X,Y,Z). There are no standalone 3D boat meshes in Hydro.fsd; boat and world geometry live inside the 104MB `bc0abcfa` container (see below).

ERM header: `"ERM!"` + u32 width + u32 height + u32 record_count (e.g. radmap_D = 208×128, 47 records). The body is a stream of **32-byte-aligned records**. From the parser (0x43e5b0): each record starts with a tag byte; tags 0–9 are fixed-form with size `u16@+2 * 4 + 0x20`, other tags dispatch through a jump table at 0x44bd10 keyed on `tag - 'M'` (0x4D) — i.e. the record tags are ASCII letters, the same family (`M`,`G`,`T`,...) seen as resource-name prefixes in the world container. This is a small display-list/scene format, not a raw vertex buffer; fully decoding it means walking that jump table. Not yet done.

## World container `bc0abcfa` (104 MB)

Resource database. Header (u32 @0x20 = table offset 0x2c00, @0x24 = payload base 0x66c00, @0x2c = count 4588). Record table: 0x4c bytes each — u32 payload_offset, u32 size, u32 count, char name[12], `"DATA"`, 0xCD debug-fill, two f32, u32 checksum. Payloads tile the file with no overlaps. `hydrotool.py world` dumps all 4,588 named sub-resources + index.csv. Names look like `<type><track><id><variant>` (e.g. `GWWHEADEBH1`, `TPTSKY__B11`, `MZTBLDG__16`). Prefix histogram: G=1741, T=1496, M=1155, A=84, P=42, B=39, H=23, D=7, I=1.

### T* textures = Glide formats (all 1,496 decoded)

T-record: 36-byte header — u24 size + `'T'`, then u32s; fmt/w/h at +16/+20/+24. **`fmt` is literally the 3dfx Glide `GrTextureFormat_t` value** (the game is a Glide title and downloads these buffers straight to the rasterizer):

| fmt | Glide format | decode |
|---|---|---|
| 0 | RGB_332 | 8bpp direct color |
| 2 | ALPHA_8 | alpha only; RGB comes from iterated vertex color |
| 3 | INTENSITY_8 | greyscale |
| 4 | ALPHA_INTENSITY_44 | hi nibble alpha, lo intensity |
| 5 | P_8 | **256×u32 ARGB8888 palette first (at +36), then w·h indices** |
| 8 | ARGB_8332 | hi byte alpha, lo byte RGB332 |
| 11 | ARGB_1555 | |
| 12 | ARGB_4444 | |
| 13 | ALPHA_INTENSITY_88 | hi byte alpha, lo byte intensity |
| 14 | AP_88 | hi byte alpha, lo byte index into a paired P_8's palette |

Verified: `filesize == 36 + w·h·bpp (+1024 for the fmt-5 palette)` holds for **all 1,494 sized textures, zero exceptions**. Pixels are stored bottom-up. The container even ships a validation suite: `T_XTESTBB{31,41,51,61,71,81,91}` is the same 128×128 test scene in fmt 8/13/4/14/0/3/5 respectively.

Notes:
- There are **no missing palettes**. The 8bpp textures that decode greyscale (glows, shadows `TBB*SH00`, exhaust, lights, steam) are ALPHA_8 / INTENSITY_8 / AI_44 by design — in-game color comes from vertex colors.
- The 519 fmt-5 files `T?TWAT1{00..31}90` are **32 palette-animated water frames per track** (32×32 P_8, self-contained). The fmt-2 `T?TWATR_W03/W04` are the water alpha masks.

### Other world-container types (surveyed 2026-07-02)

- **B\*** = loading screens, fully decoded: 16-byte header `u24 size+'B', u32 2, u32 w, u32 h, u32 2` then w·h ARGB1555 bottom-up. 13 track banners (640×132/162/186) + 3 full 640×480 (Eurocom logo etc). `hydrotool.py world` exports them.
- **A\*** = keyframe animations for ambient props (PENGuin, BEAR, HELIcopter, KAYAk, ORCA…): header has f32 1/30 (frame time) + count dwords, body = 4×4 float matrices. Not decoded further.
- **D\*** = demo/credits camera scripts, DECODED (2026-07-07): u32 record count @+4, then `{u32 time_s, f32 x, char camera[12], u32 nparams, f32 params[n]}` — a timed cut list for the attract-mode director. Camera modes: `DOLLY_TARGET`(5 params), `ESPN_CAMERA`(3), `GAMECAMERA1-3`(1), `TARGET_SWIVEL`(6), `CIRCLING_CAM`(7: orbit radius/height/speed/dir…), `STATIONARY_`(2), `MOUNTED_CAM`/`MOUNTED_SWIVEL`(8), `CHASING_CAM`(6), `DOLLY_CAMERA`(4), `SCROLL_CREDITS`(2); last param is usually FOV (90). `HIGH_SCORE_ {1,n}/{0,n}` toggles the high-score overlay. Same declared-size undercut as P records (final float spills into the trailer; split keeps slack). `hydrotool.py cameras` dumps them to `_cameras/*.txt`.
- **H\*** = the per-track SCENE files (`H<track>T<name>TRH0`, one per track + menu scenes `HWTBTS_` boat-select / `HWTCRED` credits / `HWTHISC` high-score / `H_TMASS`). Partially mapped (2026-07-06):
  - Header: u32 sector count @+4 (8/16), then sectors ×20B `{u32 node-list ptr*, f32 x, f32 y(=water level, e.g. 211), f32 z, u32 flags}` — positions trace the course sequentially = **checkpoint/progress waypoints**. Then a master header (@0x148 for ARCT): ~13 u32 counts + ~20 relocated section pointers.
  - **Embedded track-surface geometry** using the same building blocks as G records: 44-byte materials (the track's 54 M-texture imports patch material+0x14 exactly like G models), sub-part/surface/triangle records.
  - **Scene-node arrays**: 0x40-byte-ish nodes with a relocated model pointer, an 8-char type tag (e.g. `ANIMPENG` = animated penguin), f32 x/z position, scale, and params — the G-model **instance placements** (Arctic imports 166 G models). Node size varies by type (deltas 0x40/0x50/0x44 observed).
  - A **6,136-entry relocated pointer table** (master drawable list) targeting 308 chunk descriptors (212B: two pointers, cos/sin heading, bbox, params), plus float **spline tables** ({x, z, dirx, dirz, dist, …} waypoint streams — racing line / camera paths).
  - Trailer relocation counts are huge (11,057 for ARCT) — internal pointers outline the whole structure. NOTE: the trailer's first dword is NOT always `FDFDFDFD` (often 0) — match the count field instead.
  - **Embedded world mesh SOLVED (2026-07-06)**: master header at `8 + nsec*20`: counts `{surfaces, tris, materials, verts, uvs}` at +0x90, and a 9-slot pointer table at +0xb0 (file coords; values rel record+4, same coordinate rule as G): [1]=surface pool `{u32 tri_count, tri_ptr, mat_ptr}`, [2]=triangle pool (0x30, same records as G), [3]=materials (0x2c, texture via reloc trailer at +0x14), [4]=vertices (24B), [5]=uvs, [7]=normal vectors, [0]/[8]=spatial chunk data. Triangle indices are global into the pools. `hydrotool.py tracks` exports all 17 tracks-with-geometry to OBJ+MTL (menu scenes and the `H1W/H3W` bonus layouts have zero mesh counts — they only place models). Verified: Arctic Circle (21,726 faces) and Venice Canals render as their recognizable courses.
  - **Scene-node placements SOLVED (2026-07-06)**: each named G-model import in the reloc trailer patches a node's pointer slot p. Node record: p+0x10 char tag[8] (node type), p+0x1c f32 x,y,z (verified: overlay lands on the course; fliers like `HANGGLID` have high y), p+0x2a u16 yaw (0..0xffff = 360deg), p+0x34 f32 scale, p+0x3c f32 50.0 (radius?). 91 distinct tags across the game: `NONE` (static, 901x), `SIGNARRO` arrow signs, `HYDROREG`/`HYDROHOV` boost pickups, `BUOYCHEC`/`BUOYFINI` checkpoint/finish buoys, `ANIMPENG`/`ANIMBEAR`/`ANIMFISH`/`ANIMCROC`/`ANIMSEAF`/`ANIMBATS` wildlife, `FIRESMAL`/`FIRELARG`, `JETSKI__`, `TRAIN___`, `BALLOON_`, `HANGGLID`, `CARPOLIC`, `BRKGLASS`, `LAVAFLOW`, `LITEPOL*` lights, `PILINGW*` pilings, etc. `hydrotool.py tracks` writes `<scene>_nodes.csv` (model, tag, x, y, z, yaw_deg, scale) — 3,278 placements game-wide, including the mesh-less menu/bonus scenes.
  - Still to do (niche): nodes with no model reference (triggers/lights-only), chunk descriptors/spline semantics (racing line, cameras). Entry point: the track-descriptor table in .data (`0x4ef8b8`+, referenced via `0x4eef48`/`0x4ef054`).
- **I_SGAME_AA0**: `ISND` magic — sound-parameter table {u16 0x17, u16 sound_id, f32 value}.

### G* mesh format (draw path fully reverse-engineered from Hydro.exe, 2026-07-02)

Key exe functions: `0x43ddd0` get-resource-by-name → raw record payload; `0x4378a0` draw model (ecx=**record+4**, edx=flags); `0x437970` per-sub-part draw — THE reference for the format; `0x4371f0` sphere-vs-frustum cull; `0x44bdd0` world-container directory init (compressed 6-bit names, `push 0x2c00`).

The model pointer the engine uses = **record base + 4**, so file offsets below = draw-code struct offsets + 4. Record header:

| file off | meaning |
|---|---|
| +0x00 | u24 size, u8 'G' |
| +0x08, +0x0c | sub-part count (duck=5, HUD marker=1) |
| +0x10 | triangle count |
| +0x14 | surface count |
| +0x18 | vertex count |
| +0x1c | UV count |
| +0x20 | normal-record count (lighting cache entries) |
| +0x24 | normal-vector count |
| +0x28..+0x48 | 9 section offsets, **base = file+0x4c** (mostly verified) |
| +0x4c | stale tool-machine pointer (garbage, e.g. 0xa51160) |
| +0x50 | bounding sphere f32 x,y,z,radius |

Section slots (file slot = in-memory slot + 4; in-memory pointers are fixed up at load — fixup routine not yet located, but section identities are verified by exact size math on G_FDUCKHIH0 and GHWPLAYIDH1):

| slot | contents |
|---|---|
| +0x28 | model-level bbox/culling block (0xcc bytes seen) |
| +0x2c | sub-parts, **0xc4 bytes each**: bbox corners, 12×255.0 lighting colors, +0x80 surface count, +0x84 surface offset |
| +0x30 | surfaces, **12 bytes each** (in memory: {u32 tri_count, tri*, material*}) |
| +0x34 | triangles, **0x30 bytes each**: f32×3 centroid, f32 plane-d, f32×3 face normal, then 3 × {u16 vert_idx, u16 normal_rec_idx, u16 uv_idx}, u16 pad |
| +0x38 | fixed 44-byte block (both models) — unknown |
| +0x3c | vertices, **24 bytes each**: f32 x,y,z + 12 bytes runtime scratch (cache frame, transformed-slot idx, 1.0f) |
| +0x40 | UVs, **8 bytes each**: f32 u, f32 v |
| +0x44 | runtime lighting-cache section (zeros in file), 24B records indexed by normal_rec_idx |
| +0x48 | normal vectors, **12 bytes each**: f32 x,y,z (unit length, verified) |

Materials: surface+8 → material block: +0 u16 render flags (bit0 gouraud?, bit2, 0x100 = skip in mirror pass, 0x4000/0x8000 blend modes), +0xc/+0x10 f32 (texture scale), +0x14 → texture resource, +0x28 color-table index. Render state keyed partly on the texture NAME's 10th char ('0'/'1' — the LOD digit).

**SOLVED (same day): the base for every offset in the record is `record+4`** — identical to the in-memory model pointer, so file slot k = draw-code slot k with +4 on the stored offset. Earlier "base 0x4c" readings were aliasing artifacts of self-similar 24-byte arrays. Corrected layout: slots[0]=0 (self), [1]=sub-parts (0xc4; +4=nsurf, +8=surface-list offset), [2]=surface pool ({u32 tri_count, tri_off, mat_off} ×12B), [3]=triangle pool, [4]=materials, [5]=vertices (24B), [6]=UVs, [7]=runtime normal-record cache (zeros, count@+0x20 × 24B), [8]=normal vectors. **Unused slots hold stale tool-machine pointers — validate against counts before dereferencing.** `hydrotool.py models` exports all 1,741 G records to OBJ (verified: boat hulls, Tinytanic, props all render correctly; 144k verts total).

### M* records = mipmapped world-surface textures (fully decoded)

Not heightfields. Header: u24 size+`'M'`, u32 0, u32 0 (runtime slots), u32 2, **u32 fmt (Glide enum: 11=ARGB_1555 for 1,119 files, 12=ARGB_4444, 13=AI_88)**, u32 w, u32 h, 3 u32s of LOD/mip info; pixels at +0x28, top mip first, full chain down to 2×2. Size formula `0x28 + 2·(w·h + w/2·h/2 + … + 2·2)` is exact on all 1,155 files. These are the track-surface textures (CLIF, SAND, BLDG, ROOF, WALL…) that G-model materials bind to; `hydrotool.py world` decodes them into `_textures/` beside the T* set.

### Record relocation trailers (the "0xCD fill" between records)

Every world-container record is followed by: `FD FD FD FD` marker, u32 entry count (**= the record table's `count` field**), then count × 16-byte entries `{char name[12], u32 location}`. Locations are in the record's rel-to-+4 coordinate space:

- **Zero-name entries** = internal fixups — locations of offset fields the loader converts to pointers (the 9 header slots, each sub-part's surface-list field, …).
- **Named entries** = resource imports — the loader resolves `name` and stores the pointer at `location`. For G models these are the **texture bindings**: each entry's location = a material's `+0x14` field, name = the M\*/T\* texture resource.

`world_split` now saves the named entries to `relocs.json`, and `models` uses them to emit `.mtl` files (`map_Kd ../_textures/<name>.png`) with `usemtl` per surface. Material record (44 bytes, rel +4): u16 flags, u16 tri_start, u32 tri_count, u32 0, f32 uv-scale?, f32 uv-scale?, u32 texture-pointer slot (+0x14, patched via reloc), f32×4 RGBA color (+0x18), u32 color-table idx (+0x28).

Not palettes. Format: u24 size + `'P'`, u32 1, u32 param_count, then params back to back: `name\0` + u8 type + value, where type 0 = `\0`-terminated string, type 1 = float32. E.g. `PBBBANSHUP0` → `SELECT_BOAT = Banshee`, `MASS = 14854.9`, `GRAVITY_MIN = 200`, drag/buoyancy/handling tables (129 params). 42 files = 13 boats × 2 tuning variants + per-track boat variants (`P?X*HUH0/1`). Caveat: the declared record size systematically undercuts the last float by ~2 bytes (it spills into the 0xCD inter-record fill) — parse by count, not size. `hydrotool.py params <splitdir>` dumps them all to text.
