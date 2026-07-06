#!/usr/bin/env python3
"""
hydrotool.py -- all-in-one extractor/decoder for Midway "FSD\\x02" archives
(Hydro Thunder, PC/arcade). Pure stdlib, no dependencies.

Works straight from Hydro.fsd. Subcommands:

  extract   Unpack every file to a folder, using original paths where known.
            Names come from an optional names.json (mine of HYDRO.EXE) merged
            with built-in pattern cracking; 536/542 files get real paths.
  textures  Decode EGF UI textures to PNG (ARGB1555 / ARGB4444, flipped).
  world     Split the 104MB world container into its ~4588 named resources
            and decode all T* textures (fmt = Glide GrTextureFormat_t) and
            B* loading screens to PNG, into _textures/ and _screens/.
  models    Export all G* geometry records to OBJ (verts + UVs + surface
            groups). Run on a world _split directory.
  params    Dump P* boat physics parameter records to readable text.
  all       everything in one shot: extract, textures, world split (with
            _textures/ and _screens/), models, and params.

Every command takes -o/--outdir to choose the output directory. Defaults:
  extract/all -> <archive>_out/         world  -> <worldfile>_split/
  models      -> <splitdir>/_models/    params -> <splitdir>/_params/
  textures decodes in place, next to each .egf.

Examples (a full run from scratch):
  python3 hydrotool.py all Hydro.fsd -o out
  python3 hydrotool.py models out/bc0abcfa.bin_split
  python3 hydrotool.py params out/bc0abcfa.bin_split

Notes:
  * The single 640x480 title EGF (id 1720011c) is stored tiled and comes out
    scrambled; every other EGF is fine.
  * ERM files are per-track radar maps, not 3D models.
  * M* world records (terrain heightfield patches) are not decoded yet.
  * Format documentation lives in FSD_format.md alongside this script.
"""

import argparse
import array
import csv
import glob
import os
import struct
import sys
import zlib

DIR_OFFSET = 0x4
DIR_SLOTS = 2048
BLOCK_TABLE_OFFSET = 0x8004
BLOCK_TABLE_SLOTS = 32768
BLOCK_USIZE = 0x2000


# ===========================================================================
# shared: PNG writer
# ===========================================================================

def write_png(path, w, h, rgba):
    """Write RGBA8888 bytes as a PNG (no dependencies)."""
    def ck(t, d):
        c = t + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c))
    raw = b''.join(b'\x00' + rgba[y*w*4:(y+1)*w*4] for y in range(h))
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n'
                + ck(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                + ck(b'IDAT', zlib.compress(raw, 9)) + ck(b'IEND', b''))


TABLE1 = [0,1,2,3,4,5,6,7,8,0xA,0xC,0xE,0x10,0x14,0x18,0x1C,0x20,0x28,0x30,0x38,
          0x40,0x50,0x60,0x70,0x80,0xA0,0xC0,0xE0,0xFF,0,0,0]
TABLE2 = [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0,0,0,0]
TABLE3 = [0,1,2,3,4,6,8,0xC,0x10,0x18,0x20,0x30,0x40,0x60,0x80,0xC0,0x100,0x180,
          0x200,0x300,0x400,0x600,0x800,0xC00,0x1000,0x1800,0x2000,0x3000,0x4000,0x6000]
TABLE4 = [0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,0xA,0xA,0xB,0xB,0xC,0xC,0xD,0xD,0,0]


def _fill_buffer(large, what, total, num, bufsize):
    """Faithful port of EDL_FillBuffer: builds two-level Huffman lookup."""
    when = [0]*num
    number = [0]*16
    back = 0
    for y in range(1, 16):
        for x in range(total):
            if what[x] == y:
                when[back] = x
                back += 1
                number[y] += 1
    x = 0
    lens = list(what)
    for y in range(1, 16):
        for _ in range(number[y]):
            lens[x] = y
            x += 1

    samp = [0]*num
    if num:
        z = lens[0]
        back = 0
        for x in range(num):
            y = lens[x]
            if y != z:
                back <<= (y - z)
                z = y
            y = (1 << y) | back
            back += 1
            s = 0
            while y != 1:
                s = (s << 1) | (y & 1)
                y >>= 1
            samp[x] = s

    for i in range(0x600):
        large[i] = 0
    buf = [0]*(1 << bufsize)
    for x in range(num):
        bk = lens[x]
        if bk < bufsize:
            y = 1 << bk
            z = samp[x]
            while True:
                large[z] = (when[x] << 7) + bk
                z += y
                if z >> bufsize:
                    break
        else:
            buf[samp[x] & ((1 << bufsize) - 1)] = bk

    z = 0
    for x in range(1 << bufsize):
        y = buf[x]
        if y:
            y -= bufsize
            if y > 8:
                return -8
            large[x] = (z << 7) + (y << 4)
            z += 1 << y
    if z > 0x1FF:
        return -9

    base = 1 << bufsize
    for x in range(num):
        if lens[x] < bufsize:
            continue
        z = large[samp[x] & (base - 1)]
        y = samp[x] >> bufsize
        while True:
            large[y + (z >> 7) + base] = (when[x] << 7) + lens[x]
            y += 1 << (lens[x] - bufsize)
            if (y >> ((z >> 4) & 7)) != 0:
                break
    return 0


def edl1_decompress_block(src: bytes, max_out: int = 0x2000) -> bytes:
    """Decompress one EDL1 block bitstream (12-byte header already stripped)."""
    out = bytearray()
    pos = 0
    size = len(src)
    data = 0
    count = 0
    large = [0]*0x600
    small = [0]*0x600
    stack = 0   # last code-length nibble persists across tables and segments

    def fill(d, c):
        nonlocal pos
        if c > 32 or c < 0:
            return d, c
        t = min(4, size - pos)
        y = int.from_bytes(src[pos:pos+t], 'little')
        pos += t
        return (y << c) | d, c + t*8

    def read_lens(n):
        nonlocal data, count, stack
        what = [0]*0x400
        nz = 0
        for y in range(n):
            data, count = fill(data, count)
            flag = data & 1
            data >>= 1
            count -= 1
            if flag:
                data, count = fill(data, count)
                stack = data & 0xF
                data >>= 4
                count -= 4
            what[y] = stack
            if stack:
                nz += 1
        return what, nz

    while pos <= size:
        data, count = fill(data, count)
        mode = data & 1
        data >>= 1
        count -= 1

        if mode:
            data, count = fill(data, count)
            n = data & 0x1FF
            data >>= 9
            count -= 9
            if n:
                what, nz = read_lens(n)
                r = _fill_buffer(large, what, n, nz, 10)
                if r < 0:
                    raise ValueError('bad Huffman table (literal/length)')
            data, count = fill(data, count)
            n = data & 0x1FF
            data >>= 9
            count -= 9
            if n:
                what, nz = read_lens(n)
                r = _fill_buffer(small, what, n, nz, 8)
                if r < 0:
                    raise ValueError('bad Huffman table (distance)')

            while True:
                data, count = fill(data, count)
                x = large[data & 0x3FF]
                y = x & 0xF
                z = (x >> 4) & 7
                if y == 0:
                    x >>= 7
                    data, count = fill(data, count)
                    x += (data >> 10) & ((1 << z) - 1)
                    x = large[x + 0x400]
                    y = x & 0xF
                data >>= y
                count -= y
                x >>= 7
                if x < 0x100:
                    out.append(x)
                    if len(out) > max_out:
                        return bytes(out)
                elif x > 0x100:
                    z = TABLE2[x - 0x101]
                    y = 0
                    if z:
                        data, count = fill(data, count)
                        y = data & ((1 << z) - 1)
                        data >>= z
                        count -= z
                    num = TABLE1[x - 0x101] + y + 3

                    data, count = fill(data, count)
                    x = small[data & 0xFF]
                    y = x & 0xF
                    z = (x & 0x70) >> 4
                    if y == 0:
                        x >>= 7
                        data, count = fill(data, count)
                        x += (data >> 8) & ((1 << z) - 1)
                        x = small[x + 0x100]
                        y = x & 0xF
                    data >>= y
                    count -= y
                    x >>= 7
                    z = TABLE4[x]
                    y = 0
                    if z:
                        data, count = fill(data, count)
                        y = data & ((1 << z) - 1)
                        data >>= z
                        count -= z
                    back = TABLE3[x] + y + 1
                    for _ in range(num):
                        p = len(out) - back
                        out.append(out[p] if p >= 0 else 0)
                        if len(out) > max_out:
                            return bytes(out)
                else:
                    break  # 0x100 = end of symbol segment
        else:
            data, count = fill(data, count)
            num = data & 0x7FFF
            data >>= 15
            count -= 15
            for _ in range(num):
                data, count = fill(data, count)
                out.append(data & 0xFF)
                data >>= 8
                count -= 8

        data, count = fill(data, count)
        eof = data & 1
        data >>= 1
        count -= 1
        if eof:
            return bytes(out)
    return bytes(out)


# ---------------------------------------------------------------------------
# filename hash (reversed from HYDRO.EXE)
# ---------------------------------------------------------------------------

def encode_string(s: str) -> int:
    """The game's filename hash: sum of chars shifted by 8*(pos mod 4),
    plus the string length. Paths are uppercase, e.g. H:\\SOUND\\100.ESF"""
    ret = 0
    shift = 0
    for ch in s:
        ret = (ret + (ord(ch) << shift)) & 0xFFFFFFFF
        shift = 0 if shift >= 0x18 else shift + 8
    return (ret + len(s)) & 0xFFFFFFFF



def load_name_db(archive_path):
    """Optional: merge names from a names.json sitting next to the archive
    or this script (maps 8-hex-digit id -> original path). Produced by
    mining HYDRO.EXE for h:\\... path strings."""
    import json
    names = {}
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.path.dirname(os.path.abspath(archive_path)), 'names.json'),
                 os.path.join(here, 'names.json')):
        try:
            with open(cand) as f:
                for k, v in json.load(f).items():
                    names[int(k, 16)] = v
            break
        except (OSError, ValueError):
            continue
    return names

def crack_names(ids):
    """Recover original paths for hashes in `ids` using known patterns."""
    names = {}
    for i in range(4000):
        for pat in (f"H:\\SOUND\\{i}.ESF",
                    f"H:\\WAVMUSIC\\TRACK{i}.ESF",
                    f"H:\\WAVMUSIC\\TRACK{i:02d}.ESF"):
            h = encode_string(pat)
            if h in ids and h not in names:
                names[h] = pat
    return names


# ---------------------------------------------------------------------------
# FSD container
# ---------------------------------------------------------------------------

class FSDArchive:
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.data = f.read()
        if self.data[:3] != b'FSD':
            raise ValueError('not an FSD archive (bad magic)')
        self.version = self.data[3]
        self.entries = []
        for i in range(DIR_SLOTS):
            e = struct.unpack_from('<4I', self.data, DIR_OFFSET + i * 16)
            if e != (0, 0, 0, 0):
                self.entries.append(e)
        self.blocks = list(struct.unpack_from(
            '<%dI' % BLOCK_TABLE_SLOTS, self.data, BLOCK_TABLE_OFFSET))

    def read_file(self, entry):
        _id, off, size, blk = entry
        if blk == 0:
            return self.data[off:off + size]
        out = bytearray()
        nblocks = (size + BLOCK_USIZE - 1) // BLOCK_USIZE
        for k in range(nblocks):
            a = self.blocks[blk + k]
            magic, csize, usize = struct.unpack_from('<4sII', self.data, a)
            if magic != b'EDL\x01':
                raise ValueError(f'block {blk + k} @ {a:#x}: bad magic {magic!r}')
            piece = edl1_decompress_block(self.data[a + 12:a + csize], usize)
            want = min(size - k * BLOCK_USIZE, BLOCK_USIZE)
            if len(piece) < want:
                raise ValueError(
                    f'block {blk + k}: short decode {len(piece):#x} < {want:#x}')
            out += piece[:want]
        return bytes(out)


EXT = {b'ESF\x08': '.esf', b'EGF\x04': '.egf', b'ERM!': '.erm'}


# ===========================================================================
# EGF textures (UI / menu / HUD)
# ===========================================================================

def egf_to_png(path, out=None):
    """Convert one EGF file to PNG. Returns output path (or None)."""
    data = open(path, 'rb').read()
    if data[:4] != b'EGF\x04':
        return None
    u = struct.unpack_from('<I', data, 4)[0]
    h = u >> 11
    w = (u & 0x7FF) >> 1
    fmt4444 = u & 1
    stride = (len(data) - 8) // (h * 2)
    px = array.array('H'); px.frombytes(data[8:8 + stride*h*2])
    rgba = bytearray(stride*h*4)
    i = 0
    if fmt4444:
        for v in px:
            rgba[i]=(v>>8&15)*17; rgba[i+1]=(v>>4&15)*17
            rgba[i+2]=(v&15)*17; rgba[i+3]=(v>>12)*17; i+=4
    else:
        for v in px:
            rgba[i]=(v>>10&31)*255//31; rgba[i+1]=(v>>5&31)*255//31
            rgba[i+2]=(v&31)*255//31; rgba[i+3]=255 if v>>15 else 0; i+=4
    out = out or os.path.splitext(path)[0] + '.png'
    write_png(out, stride, h, bytes(rgba))
    return out


def cmd_textures(args):
    src = args.dir
    targets = ([src] if src.endswith('.egf')
               else sorted(glob.glob(os.path.join(src, '**', '*.egf'),
                                     recursive=True)))
    ok = 0
    for f in targets:
        if egf_to_png(f):
            ok += 1
    print(f'{ok} EGF textures -> PNG')



# ===========================================================================
# world container (the 104MB resource database)
# ===========================================================================

def world_split(data, outdir):
    """Split the world container into its named sub-resources. Returns count.

    Each record is followed by a relocation table: u32 (varies -- 0 or
    0xfdfdfdfd fill), u32 entry count (= the record-table count field),
    then count x 16 bytes of {char name[12], u32 location}. Zero-name
    entries are internal offset->pointer fixups; named entries are resource
    imports (e.g. a G model's texture bindings: location =
    material_offset+0x14). Named entries are collected into relocs.json
    alongside index.csv."""
    import json
    count = struct.unpack_from('<I', data, 0x2c)[0]
    table = struct.unpack_from('<I', data, 0x20)[0]
    os.makedirs(outdir, exist_ok=True)
    rows = []
    relocs = {}
    for i in range(count):
        o = table + i*0x4c
        a, b, c = struct.unpack_from('<3I', data, o)
        name = data[o+12:o+24].split(b'\0')[0].decode('latin1')
        f1, f2, x = struct.unpack_from('<ffI', data, o+64)
        fn = ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in name)
        # P records' declared size can undercut the last float32 by a couple
        # of bytes (spills into the trailer); keep 4 bytes of slack for those.
        slack = 4 if name.startswith('P') else 0
        with open(os.path.join(outdir, fn + '.bin'), 'wb') as f:
            f.write(data[a:a+b+slack])
        if c and struct.unpack_from('<I', data, a+b+4)[0] == c:
            named = {}
            for k in range(c):
                e = a + b + 8 + k*16
                rn = data[e:e+12].split(b'\0')[0].decode('latin1')
                if rn:
                    loc = struct.unpack_from('<I', data, e+12)[0]
                    named[loc] = rn
            if named:
                relocs[fn] = named
        rows.append((name, f'{a:#x}', b, c, f1, f2, f'{x:08x}'))
    with open(os.path.join(outdir, 'index.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['name','offset','size','count','float1','float2','checksum'])
        w.writerows(rows)
    with open(os.path.join(outdir, 'relocs.json'), 'w') as f:
        json.dump(relocs, f, indent=0)
    return count


# T* texture record: 36-byte header (fmt/w/h at +16, all little-endian) then
# pixel data. `fmt` is the 3dfx Glide GrTextureFormat_t value:
#   0 RGB_332   2 ALPHA_8   3 INTENSITY_8   4 ALPHA_INTENSITY_44
#   5 P_8 (256xARGB8888 palette FIRST, then w*h indices)
#   8 ARGB_8332   11 ARGB_1555   12 ARGB_4444
#   13 ALPHA_INTENSITY_88   14 AP_88 (alpha + index into a paired P_8 palette)
# Verified: size == 36 + w*h*bpp (+1024 for the fmt-5 palette) for all 1494
# textures in the retail world container, zero exceptions.

def _read_palette(d):
    """256-entry ARGB8888 palette at +36 of a fmt-5 record -> [(r,g,b,a)]."""
    ent = struct.unpack_from('<256I', d, 36)
    return [((v>>16)&0xFF, (v>>8)&0xFF, v&0xFF, (v>>24)&0xFF) for v in ent]


def world_textures(splitdir):
    """Decode T* textures in a split dir to PNG (into _textures/).
    Returns (converted, skipped)."""
    pngdir = os.path.join(splitdir, '_textures')
    os.makedirs(pngdir, exist_ok=True)
    pals = {}                                  # 6-char prefix -> palette (fmt 14)
    for f in glob.glob(os.path.join(splitdir, 'T*.bin')):
        d = open(f, 'rb').read()
        if len(d) >= 36+1024 and struct.unpack_from('<I', d, 16)[0] == 5:
            pals.setdefault(os.path.basename(f)[:6], _read_palette(d))
    ok = skip = 0
    for f in sorted(glob.glob(os.path.join(splitdir, 'T*.bin'))):
        d = open(f, 'rb').read()
        if len(d) < 36 or d[3:4] != b'T':
            skip += 1; continue
        fmt, w, h, n = struct.unpack_from('<4I', d, 16)
        if not (w and h):
            skip += 1; continue
        rgba = bytearray(w*h*4)
        def put(x, y, r, g, b, a):
            o = ((h-1-y)*w + x) * 4            # textures are bottom-up
            rgba[o:o+4] = bytes((r, g, b, a))
        if fmt in (8, 11, 12, 13, 14) and len(d)-36 >= w*h*2:
            a = array.array('H'); a.frombytes(d[36:36+w*h*2])
            pal = pals.get(os.path.basename(f)[:6]) if fmt == 14 else None
            for y in range(h):
                for x in range(w):
                    v = a[y*w+x]
                    if fmt == 11:      # ARGB_1555
                        put(x,y,(v>>10&31)*255//31,(v>>5&31)*255//31,
                            (v&31)*255//31,255 if v>>15 else 0)
                    elif fmt == 12:    # ARGB_4444
                        put(x,y,(v>>8&15)*17,(v>>4&15)*17,(v&15)*17,(v>>12)*17)
                    elif fmt == 13:    # ALPHA_INTENSITY_88
                        i8 = v & 0xFF
                        put(x,y,i8,i8,i8,v>>8)
                    elif fmt == 14:    # AP_88: alpha + palette index
                        if pal:
                            r,g,b,_ = pal[v & 0xFF]
                            put(x,y,r,g,b,v>>8)
                        else:
                            i8 = v & 0xFF
                            put(x,y,i8,i8,i8,v>>8)
                    else:              # 8: ARGB_8332
                        put(x,y,(v>>5&7)*255//7,(v>>2&7)*255//7,
                            (v&3)*255//3,v>>8)
        elif fmt == 5 and len(d)-36 >= 1024 + w*h:
            pal = _read_palette(d)
            idx = d[36+1024:36+1024+w*h]
            for y in range(h):
                for x in range(w):
                    put(x, y, *pal[idx[y*w+x]])
        elif fmt in (0, 2, 3, 4) and len(d)-36 >= w*h:
            idx = d[36:36+w*h]
            for y in range(h):
                for x in range(w):
                    v = idx[y*w+x]
                    if fmt == 0:       # RGB_332
                        put(x,y,(v>>5&7)*255//7,(v>>2&7)*255//7,(v&3)*255//3,255)
                    elif fmt == 2:     # ALPHA_8 (color comes from the mesh)
                        put(x,y,255,255,255,v)
                    elif fmt == 3:     # INTENSITY_8
                        put(x,y,v,v,v,255)
                    else:              # ALPHA_INTENSITY_44
                        i8 = (v&15)*17
                        put(x,y,i8,i8,i8,(v>>4)*17)
        else:
            skip += 1; continue
        write_png(os.path.join(pngdir,
                  os.path.splitext(os.path.basename(f))[0] + '.png'),
                  w, h, bytes(rgba))
        ok += 1
    return ok, skip


def world_mtextures(splitdir):
    """Decode M* mipmapped world-surface textures into _textures/.
    Header: u24 size+'M', u32 0, u32 0, u32 2, u32 fmt (Glide enum: 11 =
    ARGB_1555, 12 = ARGB_4444, 13 = AI_88), u32 w, u32 h, 3 u32s LOD info;
    pixels at +0x28 top mip first, chain continues down to 2x2 (only the
    top mip is exported). Returns count."""
    pngdir = os.path.join(splitdir, '_textures')
    os.makedirs(pngdir, exist_ok=True)
    ok = 0
    for f in sorted(glob.glob(os.path.join(splitdir, 'M*.bin'))):
        d = open(f, 'rb').read()
        if len(d) < 0x28 or d[3:4] != b'M':
            continue
        fmt, w, h = struct.unpack_from('<3I', d, 0x10)
        if fmt not in (11, 12, 13) or not (w and h) or 0x28 + w*h*2 > len(d):
            continue
        px = array.array('H'); px.frombytes(d[0x28:0x28 + w*h*2])
        rgba = bytearray(w*h*4)
        for y in range(h):
            for x in range(w):
                v = px[y*w+x]
                o = ((h-1-y)*w + x) * 4            # bottom-up like T*
                if fmt == 11:      # ARGB_1555
                    rgba[o:o+4] = bytes(((v>>10&31)*255//31,
                                         (v>>5&31)*255//31,
                                         (v&31)*255//31, 255 if v>>15 else 0))
                elif fmt == 12:    # ARGB_4444
                    rgba[o:o+4] = bytes(((v>>8&15)*17, (v>>4&15)*17,
                                         (v&15)*17, (v>>12)*17))
                else:              # AI_88
                    i8 = v & 0xFF
                    rgba[o:o+4] = bytes((i8, i8, i8, v>>8))
        write_png(os.path.join(pngdir,
                  os.path.splitext(os.path.basename(f))[0] + '.png'),
                  w, h, bytes(rgba))
        ok += 1
    return ok


def world_screens(splitdir):
    """Decode B* loading screens (16-byte header: u24 size+'B', u32 2, u32 w,
    u32 h, u32 2; then w*h ARGB1555 pixels, bottom-up) into _screens/.
    Returns count."""
    pngdir = os.path.join(splitdir, '_screens')
    os.makedirs(pngdir, exist_ok=True)
    ok = 0
    for f in sorted(glob.glob(os.path.join(splitdir, 'B*.bin'))):
        d = open(f, 'rb').read()
        if len(d) < 16 or d[3:4] != b'B':
            continue
        w, h = struct.unpack_from('<2I', d, 8)
        if len(d) < 16 + w*h*2:
            continue
        px = array.array('H'); px.frombytes(d[16:16+w*h*2])
        rgba = bytearray(w*h*4)
        for y in range(h):
            for x in range(w):
                v = px[y*w+x]
                o = ((h-1-y)*w + x) * 4
                rgba[o:o+4] = bytes(((v>>10&31)*255//31, (v>>5&31)*255//31,
                                     (v&31)*255//31, 255))
        write_png(os.path.join(pngdir,
                  os.path.splitext(os.path.basename(f))[0] + '.png'),
                  w, h, bytes(rgba))
        ok += 1
    return ok


def cmd_world(args):
    """Split a world container file and decode its textures.
    The world file is the large resource extracted as data/... .bin with the
    'ABCDEFG...' magic (id bc0abcfa)."""
    data = open(args.worldfile, 'rb').read()
    outdir = args.outdir or args.worldfile + '_split'
    n = world_split(data, outdir)
    ok, skip = world_textures(outdir)
    mok = world_mtextures(outdir)
    scr = world_screens(outdir)
    print(f'{n} resources -> {outdir}/  ({ok} T + {mok} M textures decoded, '
          f'{skip} skipped, {scr} loading screens)')



# ===========================================================================
# extract everything from the FSD
# ===========================================================================

EXT = {b'ESF\x08': '.esf', b'EGF\x04': '.egf', b'ERM!': '.erm'}
WORLD_MAGIC = b'ABCD'   # world container starts "ABCDEFGH..."


def cmd_extract(args):
    fsd = FSDArchive(args.archive)
    names = crack_names({e[0] for e in fsd.entries})
    names.update(load_name_db(args.archive))
    print(f'FSD v{fsd.version}: {len(fsd.entries)} files '
          f'({len(names)} original names recovered)')
    outdir = args.outdir or os.path.splitext(args.archive)[0] + '_out'
    os.makedirs(outdir, exist_ok=True)
    manifest = []
    world_path = None
    for n, e in enumerate(fsd.entries, 1):
        h, off, size, blk = e
        buf = fsd.read_file(e)
        magic = bytes(buf[:4])
        if h in names:
            rel = names[h].split(':', 1)[-1].lstrip('\\').replace('\\', os.sep)
            name = rel.lower()
        else:
            name = f'{h:08x}{EXT.get(magic, ".bin")}'
        path = os.path.join(outdir, name)
        os.makedirs(os.path.dirname(path) or outdir, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(buf)
        if magic == WORLD_MAGIC and size > 1_000_000:
            world_path = path
        manifest.append((f'{h:08x}', names.get(h, ''), name, size, blk,
                         magic.decode('latin1')))
        if n % 50 == 0 or n == len(fsd.entries):
            print(f'  {n}/{len(fsd.entries)}', file=sys.stderr)
    with open(os.path.join(outdir, 'manifest.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['hash','original_path','output','size','start_block','type'])
        w.writerows(manifest)
    print(f'extracted to {outdir}/ (+ manifest.csv)')
    return outdir, world_path


def cmd_all(args):
    outdir, world_path = cmd_extract(args)
    # decode all EGF textures in place
    ok = 0
    for f in glob.glob(os.path.join(outdir, '**', '*.egf'), recursive=True):
        if egf_to_png(f):
            ok += 1
    print(f'{ok} EGF textures -> PNG')
    # split + decode the world container if we found it
    if world_path:
        from argparse import Namespace
        data = open(world_path, 'rb').read()
        splitdir = world_path + '_split'
        nrec = world_split(data, splitdir)
        tok, tskip = world_textures(splitdir)
        mok = world_mtextures(splitdir)
        scr = world_screens(splitdir)
        print(f'world: {nrec} resources -> {splitdir}/ '
              f'({tok} T + {mok} M textures, {scr} screens, {tskip} skipped)')
        cmd_models(Namespace(splitdir=splitdir, outdir=None))
        cmd_params(Namespace(splitdir=splitdir, outdir=None))
    else:
        print('world container not found in archive (skipped)')




# ===========================================================================
# H* track scenes -- embedded world mesh export
# ===========================================================================
#
# H-record: u24 size+'H', u32 sector count, sectors x 20B {ptr, x, y, z,
# flags} (checkpoint waypoints), then a master header at 8+nsec*20:
#   +0x90: u32 counts {surfaces, tris, materials, verts, uvs}
#   +0xb0 (file coords): 9 slots (all offsets rel record+4, like G):
#          [1]=surface pool {u32 tri_count, tri_ptr, mat_ptr}
#          [2]=triangle pool (0x30, same layout as G)
#          [3]=materials (0x2c, +0x14 patched via reloc trailer)
#          [4]=vertices (24B)  [5]=uvs (8B)  [7]=normals (12B)
# The rest of the file (chunk descriptors, scene-node instance arrays,
# splines) is not needed for the mesh and is not parsed here.

def h_to_obj(d, out, texrefs=None):
    """Export an H track scene's embedded world mesh to OBJ (+MTL).
    Returns (verts, faces) or None."""
    if len(d) < 0x100 or d[3:4] != b'H':
        return None
    B = 4
    nsec = struct.unpack_from('<I', d, 4)[0]
    base = 8 + nsec*20
    nsurf, ntri, nmat, nvert, nuv = struct.unpack_from('<5I', d, base+0x90)
    slots = struct.unpack_from('<9I', d, base+0xb0)
    surf_o, mat_s, vert_o, uv_o = slots[1]+B, slots[3], slots[4]+B, slots[5]+B
    if not (nvert and nsurf) or vert_o + nvert*24 > len(d):
        return None
    verts = [struct.unpack_from('<3f', d, vert_o+i*24) for i in range(nvert)]
    uvs = [struct.unpack_from('<2f', d, uv_o+i*8) for i in range(nuv)]
    mats = {}
    faces = []
    for j in range(nsurf):
        cnt, tptr, mptr = struct.unpack_from('<3I', d, surf_o+j*12)
        if mptr not in mats:
            tex = (texrefs or {}).get(mptr + 0x14)
            mats[mptr] = tex or 'mat_%x' % mptr
        for t in range(cnt):
            to = tptr + B + t*0x30
            if to + 0x30 > len(d):
                return None
            e = struct.unpack_from('<9H', d, to+0x1c)
            vi, ui = e[0::3], e[2::3]
            if any(v >= nvert for v in vi):
                return None
            faces.append((vi, ui, j, mptr))
    textured = texrefs and any((m + 0x14) in texrefs for m in mats)
    if textured:
        with open(os.path.splitext(out)[0] + '.mtl', 'w') as f:
            for mptr, mtl in sorted(mats.items()):
                f.write('newmtl %s\n' % mtl)
                if (mptr + 0x14) in texrefs:
                    f.write('map_Kd ../_textures/%s.png\n' % texrefs[mptr+0x14])
                f.write('\n')
    with open(out, 'w') as f:
        if textured:
            f.write('mtllib %s.mtl\n' %
                    os.path.splitext(os.path.basename(out))[0])
        for v in verts:
            f.write('v %.4f %.4f %.4f\n' % v)
        for u in uvs:
            f.write('vt %.4f %.4f\n' % u)
        last = None
        for vi, ui, j, mptr in faces:
            if j != last:
                f.write('g surf%d\n' % j)
                if textured:
                    f.write('usemtl %s\n' % mats[mptr])
                last = j
            if uvs and all(u < len(uvs) for u in ui):
                f.write('f %d/%d %d/%d %d/%d\n' % (vi[0]+1, ui[0]+1,
                        vi[1]+1, ui[1]+1, vi[2]+1, ui[2]+1))
            else:
                f.write('f %d %d %d\n' % (vi[0]+1, vi[1]+1, vi[2]+1))
    return len(verts), len(faces)


def h_nodes(d, texrefs):
    """Extract model-placement scene nodes from an H record via its named
    G-model imports. Node record (from the patched pointer slot p):
    +0x10 char tag[8], +0x1c f32 x,y,z, +0x2a u16 yaw (0..0xffff = 360deg),
    +0x34 f32 scale. Returns [(model, tag, x, y, z, yaw_deg, scale)]."""
    out = []
    for loc, res in sorted(texrefs.items()):
        if not res.startswith(('G', 'g')):
            continue
        p = loc + 4
        if p + 0x40 > len(d):
            continue
        tag = d[p+0x10:p+0x18].rstrip(b'\x00').decode('latin1', 'replace')
        x, y, z = struct.unpack_from('<3f', d, p + 0x1c)
        yaw = struct.unpack_from('<H', d, p + 0x2a)[0] * 360.0 / 65536.0
        scale = struct.unpack_from('<f', d, p + 0x34)[0]
        out.append((res, tag, x, y, z, yaw, scale))
    return out


def cmd_tracks(args):
    import json
    src = args.splitdir
    outdir = args.outdir or os.path.join(src, '_tracks')
    os.makedirs(outdir, exist_ok=True)
    relocs = {}
    try:
        with open(os.path.join(src, 'relocs.json')) as f:
            relocs = {k: {int(l): n for l, n in v.items()}
                      for k, v in json.load(f).items()}
    except (OSError, ValueError):
        pass
    nobj = nskip = nnodes = 0
    for f in sorted(glob.glob(os.path.join(src, 'H*.bin'))):
        d = open(f, 'rb').read()
        name = os.path.splitext(os.path.basename(f))[0]
        refs = relocs.get(name, {})
        try:
            r = h_to_obj(d, os.path.join(outdir, name + '.obj'), refs)
        except (struct.error, IndexError):
            r = None
        nodes = h_nodes(d, refs)
        if nodes:
            with open(os.path.join(outdir, name + '_nodes.csv'), 'w',
                      newline='') as nf:
                w = csv.writer(nf)
                w.writerow(['model', 'tag', 'x', 'y', 'z', 'yaw_deg', 'scale'])
                for n in nodes:
                    w.writerow([n[0], n[1], '%.3f' % n[2], '%.3f' % n[3],
                                '%.3f' % n[4], '%.1f' % n[5], '%.3f' % n[6]])
            nnodes += len(nodes)
        if r:
            nobj += 1
            print('  %s: %d verts, %d faces, %d placements'
                  % (name, r[0], r[1], len(nodes)))
        else:
            nskip += 1
            if nodes:
                print('  %s: no mesh, %d placements' % (name, len(nodes)))
    print(f'{nobj} track meshes + {nnodes} object placements -> {outdir}/  '
          f'({nskip} without mesh)')


# ===========================================================================
# P* boat physics parameters
# ===========================================================================
#
# P-record: u24 size + 'P', u32 1, u32 param_count, then params back to back:
#   name\0  u8 type  value      type 0 = \0-terminated string, 1 = float32
# e.g. PBBBANSHUP0 = "SELECT_BOAT"="Banshee", MASS=14854.9, GRAVITY_MIN=...
# NB: some records' declared size undercounts by a couple of bytes (the last
# float spills into the 0xCD inter-record fill); parse by count, not size.

def parse_params(d):
    """Parse a P* resource -> list of (name, value). Truncated tail -> None."""
    n = struct.unpack_from('<I', d, 8)[0]
    pos = 12
    out = []
    for _ in range(n):
        e = d.find(b'\0', pos)
        if e < 0:
            break
        name = d[pos:e].decode('latin1'); pos = e + 1
        if pos >= len(d):
            out.append((name, None)); break
        t = d[pos]; pos += 1
        if t == 0:
            e = d.find(b'\0', pos)
            out.append((name, d[pos:e].decode('latin1'))); pos = e + 1
        elif pos + 4 <= len(d):
            out.append((name, struct.unpack_from('<f', d, pos)[0])); pos += 4
        else:
            out.append((name, None)); break
    return out


def cmd_params(args):
    outdir = args.outdir or os.path.join(args.splitdir, '_params')
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for f in sorted(glob.glob(os.path.join(args.splitdir, 'P*.bin'))):
        d = open(f, 'rb').read()
        if d[3:4] != b'P':
            continue
        name = os.path.splitext(os.path.basename(f))[0]
        with open(os.path.join(outdir, name + '.txt'), 'w') as out:
            for k, v in parse_params(d):
                if isinstance(v, float):
                    v = '%g' % round(v, 6)
                out.write('%s = %s\n' % (k, '<truncated>' if v is None else v))
        n += 1
    print(f'{n} parameter files -> {outdir}/')


# ===========================================================================
# G* geometry -- SOLVED (format reversed from Hydro.exe draw code @0x437970)
# ===========================================================================
#
# All offsets in a G record are relative to record+4 (the engine's in-memory
# model pointer). Layout (file offsets):
#   +0x00 u24 size, u8 'G'
#   +0x08 sub-part count
#   +0x10 total tri count   +0x14 ?        +0x18 vertex count
#   +0x1c uv count          +0x20 normal-record count  +0x24 normal count
#   +0x28 9 section offsets (add 4):
#         [0] 0/self  [1] sub-parts (0xc4 each)  [2] surface pool
#         [3] triangle pool  [4] materials  [5] vertices (24B stride)
#         [6] uvs (f32 pairs)  [7] runtime cache  [8] normals (f32 triples)
#   +0x50 bounding sphere f32 x,y,z,r
# sub-part: +0 bbox data, +0x4c 12 x 255.0 colors; +4 = nsurf, +8 = surface
#           list offset.
# surface:  {u32 tri_count, u32 tri_off, u32 mat_off}  (12 bytes)
# triangle: 0x30 bytes: f32x3 centroid, f32 d, f32x3 face normal,
#           3 x {u16 vert_idx, u16 normal_rec_idx, u16 uv_idx}, u16 pad
#
# M* records are NOT this format (terrain heightfield patches, 128x128
# byte grids) -- not handled here.

def g_to_obj(d, out, texrefs=None):
    """Export one G record to OBJ (+ MTL when texture bindings are known).
    texrefs: {location: resource_name} from relocs.json -- a material at
    offset m uses texture texrefs[m + 0x14]. Returns (verts, faces) or None."""
    if len(d) < 0x54 or d[3:4] != b'G':
        return None
    B = 4
    nsub = struct.unpack_from('<I', d, 0x08)[0]
    nvert = struct.unpack_from('<I', d, 0x18)[0]
    nuv = struct.unpack_from('<I', d, 0x1c)[0]
    slots = struct.unpack_from('<9I', d, 0x28)
    subs_o, vert_o, uv_o = slots[1]+B, slots[5]+B, slots[6]+B
    if nuv and uv_o + nuv*8 > len(d):
        nuv = 0                     # unused slot keeps a stale tool pointer
    if not nvert or vert_o + nvert*24 > len(d):
        return None
    verts = [struct.unpack_from('<3f', d, vert_o+i*24) for i in range(nvert)]
    uvs = [struct.unpack_from('<2f', d, uv_o+i*8) for i in range(nuv)]
    faces = []
    mats = {}                       # material offset -> mtl name
    for s in range(nsub):
        so = subs_o + s*0xc4
        if so + 0xc4 > len(d):
            return None
        nsurf, surfoff = struct.unpack_from('<2I', d, so+4)
        for j in range(nsurf):
            po = surfoff + B + j*12
            if po + 12 > len(d):
                return None
            cnt, toff, moff = struct.unpack_from('<3I', d, po)
            if moff not in mats:
                tex = (texrefs or {}).get(moff + 0x14)
                mats[moff] = tex or 'mat_%x' % moff
            for t in range(cnt):
                to = toff + B + t*0x30
                if to + 0x30 > len(d):
                    return None
                e = struct.unpack_from('<9H', d, to+0x1c)
                vi, ui = e[0::3], e[2::3]
                if any(v >= nvert for v in vi):
                    return None
                faces.append((vi, ui, s, j, moff))
    textured = texrefs and any((m + 0x14) in texrefs for m in mats)
    if textured:
        with open(os.path.splitext(out)[0] + '.mtl', 'w') as f:
            for moff, mtl in sorted(mats.items()):
                r, g, b, a = struct.unpack_from('<4f', d, moff + B + 0x18)
                f.write('newmtl %s\nKd %.4f %.4f %.4f\n' % (mtl, r, g, b))
                if (moff + 0x14) in texrefs:
                    f.write('map_Kd ../_textures/%s.png\n' % texrefs[moff+0x14])
                f.write('\n')
    with open(out, 'w') as f:
        if textured:
            f.write('mtllib %s.mtl\n' %
                    os.path.splitext(os.path.basename(out))[0])
        for v in verts:
            f.write('v %.5f %.5f %.5f\n' % v)
        for u in uvs:
            f.write('vt %.5f %.5f\n' % u)
        last = None
        for vi, ui, s, j, moff in faces:
            if (s, j) != last:
                f.write('g part%d_surf%d\n' % (s, j))
                if textured:
                    f.write('usemtl %s\n' % mats[moff])
                last = (s, j)
            if uvs and all(u < len(uvs) for u in ui):
                f.write('f %d/%d %d/%d %d/%d\n' % (vi[0]+1, ui[0]+1,
                        vi[1]+1, ui[1]+1, vi[2]+1, ui[2]+1))
            else:
                f.write('f %d %d %d\n' % (vi[0]+1, vi[1]+1, vi[2]+1))
    return len(verts), len(faces)


def cmd_models(args):
    import json
    src = args.splitdir
    outdir = args.outdir or os.path.join(src, '_models')
    os.makedirs(outdir, exist_ok=True)
    relocs = {}
    try:
        with open(os.path.join(src, 'relocs.json')) as f:
            relocs = {k: {int(l): n for l, n in v.items()}
                      for k, v in json.load(f).items()}
    except (OSError, ValueError):
        pass
    nobj = nvert = nface = nskip = 0
    for f in sorted(glob.glob(os.path.join(src, 'G*.bin'))):
        d = open(f, 'rb').read()
        name = os.path.splitext(os.path.basename(f))[0]
        try:
            r = g_to_obj(d, os.path.join(outdir, name + '.obj'),
                         relocs.get(name))
        except (struct.error, IndexError):
            r = None
        if r:
            nobj += 1; nvert += r[0]; nface += r[1]
        else:
            nskip += 1
            p = os.path.join(outdir, name + '.obj')
            if os.path.exists(p):
                os.remove(p)
    print(f'{nobj} OBJ models ({nvert} verts, {nface} faces) -> {outdir}/  '
          f'({nskip} skipped)')


# ===========================================================================
# CLI
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(
        description='Hydro Thunder FSD archive tool (extract + decode)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('extract', help='unpack all files from the FSD')
    p.add_argument('archive', help='path to Hydro.fsd')
    p.add_argument('-o', '--outdir', help='output dir (default: <archive>_out)')
    p.set_defaults(func=lambda a: cmd_extract(a))

    p = sub.add_parser('all', help='extract + decode textures + split world')
    p.add_argument('archive', help='path to Hydro.fsd')
    p.add_argument('-o', '--outdir', help='output dir (default: <archive>_out)')
    p.set_defaults(func=cmd_all)

    p = sub.add_parser('textures', help='decode EGF file(s) or a folder to PNG')
    p.add_argument('dir', help='an .egf file or a directory to search')
    p.set_defaults(func=cmd_textures)

    p = sub.add_parser('world', help='split world container + decode T* '
                       'textures and B* loading screens')
    p.add_argument('worldfile', help='the large ABCD... resource from extract '
                   '(out/bc0abcfa.bin)')
    p.add_argument('-o', '--outdir', help='output dir (default: <worldfile>_split)')
    p.set_defaults(func=cmd_world)

    p = sub.add_parser('tracks', help='export H* track scenes: embedded world '
                       'mesh to OBJ+MTL')
    p.add_argument('splitdir', help='a world _split directory')
    p.add_argument('-o', '--outdir', help='output dir (default: <splitdir>/_tracks)')
    p.set_defaults(func=cmd_tracks)

    p = sub.add_parser('params', help='dump P* boat physics parameters to text')
    p.add_argument('splitdir', help='a world _split directory')
    p.add_argument('-o', '--outdir', help='output dir (default: <splitdir>/_params)')
    p.set_defaults(func=cmd_params)

    p = sub.add_parser('models', help='export all G* geometry records to OBJ '
                       '(verts + UVs + surface groups)')
    p.add_argument('splitdir', help='a world _split directory')
    p.add_argument('-o', '--outdir', help='output dir (default: <splitdir>/_models)')
    p.set_defaults(func=cmd_models)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
