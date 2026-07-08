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
  sounds    Decode all ESF sounds + music to 16-bit mono WAV.
  all       everything in one shot: extract, textures, world split (with
            _textures/ and _screens/), models, tracks, params, and sounds.

  Modding (see the "Modding" example below for the full workflow):
  retexture Re-encode an edited PNG back into a T*/M*/B*/EGF texture record.
  mod       ONE-SHOT repack: rebuild Hydro.fsd straight from a mods folder
            containing world-record and/or top-level file replacements.
  worldpack Rebuild just the world container from world-record mods
            (lower-level; `mod` calls this internally -- use `mod` instead
            unless you specifically need an intermediate container file).
  repack    Rebuild Hydro.fsd from top-level file mods only (lower-level;
            `mod` calls this internally -- use `mod` instead unless you're
            not touching anything inside the world container).

Every command takes -o/--outdir to choose the output directory. Defaults:
  extract/all -> <archive>_out/         world  -> <worldfile>_split/
  models      -> <splitdir>/_models/    params -> <splitdir>/_params/
  sounds      -> <extractdir>/sounds/wav/
  retexture   -> <original-folder>/_mods/<same-name>
  textures decodes in place, next to each .egf.

Examples (a full run from scratch):
  python3 hydrotool.py all Hydro.fsd -o out

Example (modding -- edit a boat texture and a model, then play it):
  # 1. edit out/bc0abcfa.bin_split/_textures/TBBBANS_A10.png in any editor
  #    (keep the same pixel dimensions), then re-encode it back:
  python3 hydrotool.py retexture out/bc0abcfa.bin_split/TBBBANS_A10.bin \\
      out/bc0abcfa.bin_split/_textures/TBBBANS_A10.png
  # -> out/bc0abcfa.bin_split/_mods/TBBBANS_A10.bin
  #
  # 2. (optional) export an edited mesh from Blender's Hydro Thunder addon
  #    into the SAME _mods/ folder (GBBBANSHUH0.bin + .trailer.bin)
  #
  # 3. one command rebuilds the whole FSD with every mod in _mods/ applied:
  python3 hydrotool.py mod Hydro.fsd out/bc0abcfa.bin_split/_mods -o Hydro_modded.fsd

Notes:
  * EGFs wider than 256 (the 640x480 loading screen) are stored as 256x256
    tiles and are de-tiled/re-tiled automatically.
  * ERM files are per-track radar maps, not 3D models.
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


def read_png(path):
    """Read a PNG (8-bit depth, non-interlaced; color types 0/2/3/4/6) into
    (w, h, rgba bytearray), no dependencies. Used by retexture() to load an
    edited image back in."""
    d = open(path, 'rb').read()
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f'{path}: not a PNG file')
    pos = 8
    w = h = bitdepth = colortype = interlace = None
    idat = bytearray()
    palette = None
    trns = None
    while pos < len(d):
        ln, typ = struct.unpack_from('>I4s', d, pos)
        body = d[pos+8:pos+8+ln]
        if typ == b'IHDR':
            w, h, bitdepth, colortype, comp, filt, interlace = \
                struct.unpack('>IIBBBBB', body)
        elif typ == b'IDAT':
            idat += body
        elif typ == b'PLTE':
            palette = [tuple(body[i:i+3]) for i in range(0, len(body), 3)]
        elif typ == b'tRNS':
            trns = body
        pos += 12 + ln
    if interlace:
        raise ValueError(f'{path}: interlaced PNGs are not supported -- '
                          f're-export without interlacing')
    if bitdepth != 8:
        raise ValueError(f'{path}: only 8-bit PNGs are supported '
                          f'(got {bitdepth}-bit)')
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colortype]
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        f = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos+stride]); pos += stride
        for i in range(stride):
            a = line[i-channels] if i >= channels else 0
            b = prev[i]
            c = prev[i-channels] if i >= channels else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a+b)//2) & 255
            elif f == 4:
                p = a+b-c; pa,pb,pc = abs(p-a),abs(p-b),abs(p-c)
                pr = a if pa<=pb and pa<=pc else (b if pb<=pc else c)
                line[i] = (line[i] + pr) & 255
        out[y*stride:(y+1)*stride] = line
        prev = line
    rgba = bytearray(w*h*4)
    for i in range(w*h):
        so = i*channels
        if colortype == 6:
            rgba[i*4:i*4+4] = out[so:so+4]
        elif colortype == 2:
            rgba[i*4:i*4+3] = out[so:so+3]; rgba[i*4+3] = 255
        elif colortype == 0:
            g = out[so]; rgba[i*4:i*4+4] = bytes((g, g, g, 255))
        elif colortype == 4:
            g, a = out[so], out[so+1]
            rgba[i*4:i*4+4] = bytes((g, g, g, a))
        else:  # 3: palette
            idx = out[so]
            r, g, b = palette[idx] if palette else (idx, idx, idx)
            a = trns[idx] if trns and idx < len(trns) else 255
            rgba[i*4:i*4+4] = bytes((r, g, b, a))
    return w, h, rgba


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
    """Convert one EGF file to PNG. Returns output path (or None).
    Images wider than 256 are stored as row-major 256x256 tiles (Glide's
    max texture size) and get de-tiled."""
    data = open(path, 'rb').read()
    if data[:4] != b'EGF\x04':
        return None
    u = struct.unpack_from('<I', data, 4)[0]
    h = u >> 11
    w = (u & 0x7FF) >> 1
    fmt4444 = u & 1
    if w > 256 and len(data) - 8 == w*h*2:
        px = array.array('H'); px.frombytes(data[8:])
        lin = array.array('H', bytes(w*h*2))
        pos = 0
        for ty in range(0, h, 256):
            th = min(256, h - ty)
            for tx in range(0, w, 256):
                tw = min(256, w - tx)
                for y in range(th):
                    lin[(ty+y)*w + tx:(ty+y)*w + tx + tw] = px[pos:pos+tw]
                    pos += tw
        px = lin
        stride = w
    else:
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
        slack = 4 if name[:1] in ('P', 'D', 'A') else 0
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
# texture re-encoding (PNG -> game format, for modding)
# ===========================================================================
#
# Inverts world_textures/world_mtextures/world_screens/egf_to_png: takes an
# edited PNG (same width/height as the decoded original -- dimensions can't
# change) and packs it back into the exact on-disk format, preserving the
# header (fmt/w/h) untouched so the output is always the same size as the
# input record. T*/M*/B* are stored bottom-up; EGF is stored top-down (and
# re-tiled into 256x256 blocks if wider than 256, mirroring egf_to_png).

def _q(v, bits):
    """Quantize an 8-bit channel down to `bits` bits, scaled back to the
    0..(2**bits - 1) integer range (matches the *17/*255 factors used when
    decoding, so re-encoding round-trips exactly for already-quantized
    colors)."""
    return round(v * ((1 << bits) - 1) / 255)


def _nearest_palette_index(rgba, palette, cache, use_alpha=True):
    key = (rgba, use_alpha)
    if key in cache:
        return cache[key]
    best, bestd = 0, None
    for i, (r, g, b, a) in enumerate(palette):
        dr, dg, db = rgba[0]-r, rgba[1]-g, rgba[2]-b
        dist = dr*dr + dg*dg + db*db
        if use_alpha:
            da = rgba[3]-a
            dist += da*da
        if bestd is None or dist < bestd:
            best, bestd = i, dist
            if dist == 0:
                break
    cache[key] = best
    return best


def encode_texture(orig, rgba, w, h, ext_palette=None):
    """Re-encode an RGBA image (top-down, w*h*4 bytes) into the same T*/M*/
    B*/EGF record format as `orig` (its original bytes). Returns new bytes,
    always the same length as `orig`. Raises ValueError on unsupported/
    mismatched input. `ext_palette` supplies a 256-entry [(r,g,b,a),...]
    palette for fmt-14 (AP_88) records, which reference a paired T* file's
    palette rather than carrying their own."""
    def get(x, y):                     # top-down PNG row order
        o = (y*w + x) * 4
        return (rgba[o], rgba[o+1], rgba[o+2], rgba[o+3])

    if orig[:4] == b'EGF\x04':
        info = struct.unpack_from('<I', orig, 4)[0]
        oh, ow = info >> 11, (info & 0x7FF) >> 1
        if (ow, oh) != (w, h):
            raise ValueError(f'PNG is {w}x{h}, resource needs {ow}x{oh}')
        fmt4444 = info & 1
        def pack(x, y):
            r, g, b, a = get(x, y)
            if fmt4444:
                return (_q(a,4)<<12)|(_q(r,4)<<8)|(_q(g,4)<<4)|_q(b,4)
            return ((1 if a >= 128 else 0)<<15)|(_q(r,5)<<10)|(_q(g,5)<<5)|_q(b,5)
        px = array.array('H', bytes(w*h*2))
        if w > 256:
            pos = 0
            for ty in range(0, h, 256):
                th = min(256, h-ty)
                for tx in range(0, w, 256):
                    tw = min(256, w-tx)
                    for y in range(th):
                        for x in range(tw):
                            px[pos] = pack(tx+x, ty+y); pos += 1
        else:
            for y in range(h):
                for x in range(w):
                    px[y*w+x] = pack(x, y)
        return bytes(orig[:8]) + px.tobytes()

    kind = orig[3:4]
    if kind == b'T' and len(orig) >= 36:
        fmt, ow, oh = struct.unpack_from('<3I', orig, 16)
    elif kind == b'M' and len(orig) >= 0x28:
        fmt, ow, oh = struct.unpack_from('<3I', orig, 0x10)
    elif kind == b'B' and len(orig) >= 16:
        fmt, ow, oh = 11, *struct.unpack_from('<2I', orig, 8)
    else:
        raise ValueError('not a recognized T*/M*/B*/EGF texture record')
    if (ow, oh) != (w, h):
        raise ValueError(f'PNG is {w}x{h}, resource needs {ow}x{oh}')

    def put16(x, y, v, base):
        o = base + ((h-1-y)*w + x)*2       # bottom-up
        struct.pack_into('<H', out, o, v)
    def put8(x, y, v, base):
        out[base + (h-1-y)*w + x] = v

    if kind == b'T':
        out = bytearray(orig)
        if fmt == 5:
            palette = _read_palette(orig)
            cache = {}
            idx_base = 36 + 1024
            for y in range(h):
                for x in range(w):
                    out[idx_base + (h-1-y)*w + x] = \
                        _nearest_palette_index(get(x, y), palette, cache)
        elif fmt in (8, 11, 12, 13, 14):
            pcache = {}
            for y in range(h):
                for x in range(w):
                    r, g, b, a = get(x, y)
                    if fmt == 11:
                        v = ((1 if a>=128 else 0)<<15)|(_q(r,5)<<10)|(_q(g,5)<<5)|_q(b,5)
                    elif fmt == 12:
                        v = (_q(a,4)<<12)|(_q(r,4)<<8)|(_q(g,4)<<4)|_q(b,4)
                    elif fmt == 13:
                        i8 = round(0.299*r+0.587*g+0.114*b)
                        v = (a<<8)|i8
                    elif fmt == 14:
                        if ext_palette:
                            idx = _nearest_palette_index(
                                (r, g, b, 0), ext_palette, pcache,
                                use_alpha=False)
                        else:
                            idx = round(0.299*r+0.587*g+0.114*b)
                        v = (a<<8)|idx
                    else:               # 8: ARGB_8332
                        v = (a<<8)|(_q(r,3)<<5)|(_q(g,3)<<2)|_q(b,2)
                    put16(x, y, v, 36)
        elif fmt in (0, 2, 3, 4):
            for y in range(h):
                for x in range(w):
                    r, g, b, a = get(x, y)
                    if fmt == 0:
                        v = (_q(r,3)<<5)|(_q(g,3)<<2)|_q(b,2)
                    elif fmt == 2:      # ALPHA_8: value lives in alpha
                        v = a
                    elif fmt == 3:      # INTENSITY_8: luma
                        v = round(0.299*r+0.587*g+0.114*b)
                    else:               # ALPHA_INTENSITY_44
                        i4 = _q(round(0.299*r+0.587*g+0.114*b), 4)
                        v = (_q(a,4)<<4)|i4
                    put8(x, y, v, 36)
        else:
            raise ValueError(f'unsupported T* fmt {fmt}')
        return bytes(out)

    if kind == b'M':
        out = bytearray(orig)
        for y in range(h):
            for x in range(w):
                r, g, b, a = get(x, y)
                if fmt == 11:
                    v = ((1 if a>=128 else 0)<<15)|(_q(r,5)<<10)|(_q(g,5)<<5)|_q(b,5)
                elif fmt == 12:
                    v = (_q(a,4)<<12)|(_q(r,4)<<8)|(_q(g,4)<<4)|_q(b,4)
                elif fmt == 13:
                    i8 = round(0.299*r+0.587*g+0.114*b)
                    v = (a<<8)|i8
                else:
                    raise ValueError(f'unsupported M* fmt {fmt}')
                put16(x, y, v, 0x28)
        return bytes(out)

    # B* loading screen: always ARGB1555, no alpha channel in-file
    out = bytearray(orig)
    for y in range(h):
        for x in range(w):
            r, g, b, _ = get(x, y)
            v = (1<<15)|(_q(r,5)<<10)|(_q(g,5)<<5)|_q(b,5)
            put16(x, y, v, 16)
    return bytes(out)


def cmd_retexture(args):
    orig = open(args.original, 'rb').read()
    w, h, rgba = read_png(args.png)
    ext_palette = None
    fmt = None
    if orig[3:4] == b'T' and len(orig) >= 36:
        fmt = struct.unpack_from('<I', orig, 16)[0]
    if fmt == 14:
        prefix = os.path.basename(args.original)[:6]
        for f in glob.glob(os.path.join(os.path.dirname(args.original) or
                                        '.', 'T*.bin')):
            if os.path.basename(f)[:6] == prefix:
                d = open(f, 'rb').read()
                if len(d) >= 36+1024 and struct.unpack_from('<I', d, 16)[0] == 5:
                    ext_palette = _read_palette(d)
                    break
    new_bytes = encode_texture(orig, rgba, w, h, ext_palette)
    outpath = args.output
    if not outpath:
        outdir = os.path.join(os.path.dirname(args.original) or '.', '_mods')
        os.makedirs(outdir, exist_ok=True)
        outpath = os.path.join(outdir, os.path.basename(args.original))
    elif os.path.isdir(outpath):
        outpath = os.path.join(outpath, os.path.basename(args.original))
    with open(outpath, 'wb') as f:
        f.write(new_bytes)
    print(f'{args.png} ({w}x{h}) -> {outpath} ({len(new_bytes)} bytes)')


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
        cmd_tracks(Namespace(splitdir=splitdir, outdir=None))
        cmd_params(Namespace(splitdir=splitdir, outdir=None))
        cmd_cameras(Namespace(splitdir=splitdir, outdir=None))
        cmd_anims(Namespace(splitdir=splitdir, outdir=None))
        cmd_sounds(Namespace(extractdir=outdir, outdir=None))
    else:
        print('world container not found in archive (skipped)')




# ===========================================================================
# repacking (modding support)
# ===========================================================================

def worldpack_bytes(data, moddir):
    """Rebuild the world container in memory, replacing any record whose
    <NAME>.bin exists in moddir. Record payloads may change size; each
    record's relocation trailer is preserved verbatim (or replaced from a
    <NAME>.trailer.bin if present), and the record table is rewritten.
    Byte-identical to the input when moddir is empty. Returns (nmod,
    new_bytes)."""
    table = struct.unpack_from('<I', data, 0x20)[0]
    payload_base = struct.unpack_from('<I', data, 0x24)[0]
    count = struct.unpack_from('<I', data, 0x2c)[0]
    recs = []
    for i in range(count):
        o = table + i*0x4c
        a, b, c = struct.unpack_from('<3I', data, o)
        name = data[o+12:o+24].split(b'\x00')[0].decode('latin1')
        fn = ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in name)
        recs.append([o, a, b, c, fn])
    order = sorted(range(count), key=lambda i: recs[i][1])
    # trailer of each record = bytes from payload end to next payload start
    ends = []
    for k, i in enumerate(order):
        a, b = recs[i][1], recs[i][2]
        nxt = recs[order[k+1]][1] if k+1 < len(order) else len(data)
        ends.append((i, a, b, nxt))
    out = bytearray(data[:payload_base])
    hdr = bytearray(data[:table + count*0x4c])
    nmod = 0
    pos = payload_base
    for i, a, b, nxt in ends:
        rep = os.path.join(moddir, recs[i][4] + '.bin')
        if os.path.isfile(rep):
            payload = open(rep, 'rb').read()
            nmod += 1
        else:
            payload = data[a:a+b]
        trep = os.path.join(moddir, recs[i][4] + '.trailer.bin')
        if os.path.isfile(trep):
            trailer = open(trep, 'rb').read()
        else:
            trailer = data[a+b:nxt]
        c = recs[i][3]
        if len(trailer) >= 8:
            tc = struct.unpack_from('<I', trailer, 4)[0]
            if tc != c and 8 + tc*16 <= len(trailer):
                c = tc
        struct.pack_into('<3I', hdr, recs[i][0], pos, len(payload), c)
        out += payload + trailer
        pos += len(payload) + len(trailer)
    out[:len(hdr)] = hdr
    return nmod, bytes(out)


def worldpack(container, moddir, outpath):
    """File-based wrapper around worldpack_bytes(). Returns (nmod, size)."""
    data = open(container, 'rb').read()
    nmod, out = worldpack_bytes(data, moddir)
    with open(outpath, 'wb') as f:
        f.write(out)
    return nmod, len(out)


def cmd_worldpack(args):
    nmod, size = worldpack(args.container, args.moddir, args.output)
    print(f'{nmod} records replaced -> {args.output} ({size} bytes)')


def fsd_repack(archive, moddir, outpath, names, overrides=None):
    """Rebuild an FSD archive. Files in moddir (matched by their extract
    path, e.g. data/textures/loading.egf or <hash>.bin) replace originals
    and are stored raw. `overrides` (optional {hash: bytes}) takes
    priority over moddir and is used by cmd_mod to splice in a rebuilt
    world container without touching disk. Unmodified files are copied
    verbatim (compressed blocks included). Byte-identical when moddir is
    empty and overrides is empty."""
    overrides = overrides or {}
    data = open(archive, 'rb').read()
    dirbytes = bytearray(data[:BLOCK_TABLE_OFFSET])
    blktab = list(struct.unpack_from('<%dI' % BLOCK_TABLE_SLOTS, data,
                                     BLOCK_TABLE_OFFSET))
    entries = []
    for i in range(DIR_SLOTS):
        h, off, size, blk = struct.unpack_from('<4I', data, DIR_OFFSET + i*16)
        if h or off or size or blk:
            entries.append([i, h, off, size, blk])
    def blocks_of(off, size, blk):
        n = (size + BLOCK_USIZE - 1) // BLOCK_USIZE
        return list(range(blk, blk + n))
    out = bytearray(0x28004)
    pos = len(out)
    nmod = 0
    for e in sorted(entries, key=lambda e: e[2]):
        i, h, off, size, blk = e
        if h in overrides:
            buf = overrides[h]
            struct.pack_into('<4I', dirbytes, DIR_OFFSET + i*16,
                             h, pos, len(buf), 0)
            out += buf
            pos += len(buf)
            nmod += 1
            continue
        rel = names.get(h)
        rep = None
        for cand in ([os.path.join(moddir, rel)] if rel else []) + [
                os.path.join(moddir, '%08x.bin' % h)]:
            if cand and os.path.isfile(cand):
                rep = cand
                break
        if rep:
            buf = open(rep, 'rb').read()
            struct.pack_into('<4I', dirbytes, DIR_OFFSET + i*16,
                             h, pos, len(buf), 0)
            out += buf
            pos += len(buf)
            nmod += 1
        elif blk == 0:
            struct.pack_into('<I', dirbytes, DIR_OFFSET + i*16 + 4, pos)
            out += data[off:off+size]
            pos += size
        else:
            struct.pack_into('<I', dirbytes, DIR_OFFSET + i*16 + 4, pos)
            for bi in blocks_of(off, size, blk):
                bo = blktab[bi]
                csize = struct.unpack_from('<I', data, bo + 4)[0]
                blktab[bi] = pos
                out += data[bo:bo+csize]
                pos += csize
    # EOF sentinel: first unused trailing entry points at end of data
    used = max(max(blocks_of(e[2], e[3], e[4])) for e in entries if e[4])
    blktab[used + 1] = pos
    out[:BLOCK_TABLE_OFFSET] = dirbytes
    struct.pack_into('<%dI' % BLOCK_TABLE_SLOTS, out, BLOCK_TABLE_OFFSET,
                     *blktab)
    with open(outpath, 'wb') as f:
        f.write(out)
    return nmod, len(out)


def cmd_repack(args):
    names = {}
    db = load_name_db(args.archive)
    for h, p in db.items():
        rel = p.split(':', 1)[-1].lstrip(chr(92)).replace(chr(92), os.sep)
        names[h] = rel.lower()
    nmod, size = fsd_repack(args.archive, args.moddir, args.output, names)
    print(f'{nmod} files replaced -> {args.output} ({size} bytes)')


WORLD_HASH = 0xbc0abcfa   # the world container's own id (it has no name)


def cmd_mod(args):
    """One-shot repack: a single mods folder can hold BOTH world-container
    resource replacements (11-char names like GBBBANSHUH0.bin, with an
    optional .trailer.bin) and top-level FSD file replacements (extract
    paths like data/textures/loading.egf, or <hash>.bin) side by side.
    This finds/decompresses the world container, applies any world-record
    mods in-memory (worldpack), then applies everything -- the rebuilt
    container plus any other file mods -- in a single repack pass.
    Supersedes running `worldpack` then `repack` by hand."""
    fsd = FSDArchive(args.archive)
    names = {}
    for h, p in load_name_db(args.archive).items():
        rel = p.split(':', 1)[-1].lstrip(chr(92)).replace(chr(92), os.sep)
        names[h] = rel.lower()
    overrides = {}
    nmod_world = 0
    world_entry = next((e for e in fsd.entries if e[0] == WORLD_HASH), None)
    if world_entry:
        world_bytes = fsd.read_file(world_entry)
        nmod_world, new_world = worldpack_bytes(world_bytes, args.moddir)
        if nmod_world:
            overrides[WORLD_HASH] = new_world
    nmod_top, size = fsd_repack(args.archive, args.moddir, args.output,
                                names, overrides)
    other = nmod_top - (1 if WORLD_HASH in overrides else 0)
    print(f'{nmod_world} world-container record(s) replaced'
          + (' (container rebuilt)' if nmod_world else '')
          + f'; {other} other top-level file(s) replaced'
          + f' -> {args.output} ({size} bytes)')


# ===========================================================================
# A* prop keyframe animations
# ===========================================================================
#
# A-record: u24 size+'A', u16 frame count, u16 bone count, f32 frame dt
# (1/30, 1/15, 0.2...), u32 ?, u32 ?, then keyframe records of 108 bytes
# (one per bone per frame, bone-major within frame): two {3x3 scaled
# rotation, f32x3 translation} blocks + {1.0, 1.0, 0.0} tail; some files
# carry one extra {u32, u32} pair after the first record. The first block
# is the bone's world transform for the frame; the second block's role
# (tangent/parent/bind?) is not yet pinned down -- both are preserved in
# the dump.

def cmd_anims(args):
    import json
    src = args.splitdir
    outdir = args.outdir or os.path.join(src, '_anims')
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for f in sorted(glob.glob(os.path.join(src, 'A*.bin'))):
        d = open(f, 'rb').read()
        if d[3:4] != b'A':
            continue
        name = os.path.splitext(os.path.basename(f))[0]
        nframes, nbones = struct.unpack_from('<2H', d, 4)
        dt = struct.unpack_from('<f', d, 8)[0]
        u1, u2 = struct.unpack_from('<2I', d, 0xc)
        recs = []
        pos = 0x14
        while pos + 108 <= len(d):
            v = struct.unpack_from('<27f', d, pos)
            if v[24] != 1.0 or v[25] != 1.0:      # lost sync (extra u32 pair)
                pos += 4
                continue
            recs.append({'m1': [round(x, 6) for x in v[:12]],
                         'm2': [round(x, 6) for x in v[12:24]]})
            pos += 108
        with open(os.path.join(outdir, name + '.json'), 'w') as out:
            json.dump({'frames': nframes, 'bones': nbones, 'dt': dt,
                       'u1': u1, 'u2': u2, 'keys': recs}, out, indent=0)
        n += 1
    print(f'{n} animations -> {outdir}/')


# ===========================================================================
# D* demo camera scripts
# ===========================================================================
#
# D-record: u24 size+'D', u32 record count, then records back to back:
#   {u32 time_s, f32 x, char camera[12], u32 nparams, f32 params[nparams]}
# A timed cut list for the attract-mode/credits director: at time_s switch
# to the named camera mode with the given parameters (last param is usually
# the FOV, 90). HIGH_SCORE_ {1,n}/{0,n} records toggle the high-score
# overlay. Param counts are fixed per camera type (verified all 7 files).
# NB: like P records, the declared size can undercut the final float (it
# spills into the trailer) -- world_split keeps 4 bytes of slack.

def cmd_cameras(args):
    src = args.splitdir
    outdir = args.outdir or os.path.join(src, '_cameras')
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for f in sorted(glob.glob(os.path.join(src, 'D*.bin'))):
        d = open(f, 'rb').read()
        if d[3:4] != b'D':
            continue
        name = os.path.splitext(os.path.basename(f))[0]
        nrec = struct.unpack_from('<I', d, 4)[0]
        pos = 8
        with open(os.path.join(outdir, name + '.txt'), 'w') as out:
            out.write('# time_s  camera        x       params\n')
            for i in range(nrec):
                if pos + 24 > len(d):
                    break
                t, x = struct.unpack_from('<If', d, pos); pos += 8
                cam = d[pos:pos+12].split(b'\x00')[0].decode('latin1')
                pos += 12
                np_ = struct.unpack_from('<I', d, pos)[0]; pos += 4
                if np_ > 32 or pos + np_*4 > len(d):
                    break
                params = struct.unpack_from('<%df' % np_, d, pos)
                pos += np_*4
                out.write('%6d  %-12s %-7g %s\n'
                          % (t, cam, x, ' '.join('%g' % p for p in params)))
        n += 1
    print(f'{n} camera scripts -> {outdir}/')


# ===========================================================================
# ESF sounds -> WAV
# ===========================================================================
#
# "ESF" + u8 version (8 here) + u32: low 24 bits = decoded PCM byte count,
# top byte flags: 0x80 = DVI IMA ADPCM (else raw PCM16), 0x40 = loop,
# 0x20 = 22050 Hz (else 11025), 0x10 = 16-bit. Mono. 4-bit IMA nibbles,
# high nibble first; standard step/index tables (exe decoder @0x46a6b0;
# format cross-checked against vgmstream's esf.c).

IMA_STEPS = [
    7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,
    73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,
    408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,
    1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,
    7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,
    22385,24623,27086,29794,32767]
IMA_INDEX = [-1,-1,-1,-1,2,4,6,8]


def esf_to_wav(path, out):
    """Decode one ESF (v8, mono IMA ADPCM) to a 16-bit WAV. Returns
    (samples, rate) or None."""
    import wave
    d = open(path, 'rb').read()
    if d[:4] != b'ESF' + bytes([8]):
        return None
    info = struct.unpack_from('<I', d, 4)[0]
    flags = info >> 24
    if not flags & 0x80:
        return None                          # raw PCM variant (unused here)
    rate = 22050 if flags & 0x20 else 11025
    sample = 0
    index = 0
    pcm = bytearray()
    for b in d[8:]:
        for nib in (b >> 4, b & 0xF):        # high nibble first
            step = IMA_STEPS[index]
            diff = step >> 3
            if nib & 4: diff += step
            if nib & 2: diff += step >> 1
            if nib & 1: diff += step >> 2
            if nib & 8: sample -= diff
            else:       sample += diff
            sample = max(-32768, min(32767, sample))
            index = max(0, min(88, index + IMA_INDEX[nib & 7]))
            pcm += struct.pack('<h', sample)
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return len(pcm) // 2, rate


def cmd_sounds(args):
    src = args.extractdir
    outdir = args.outdir or os.path.join(src, 'sounds', 'wav')
    n = 0
    for f in sorted(glob.glob(os.path.join(src, '**', '*.esf'),
                              recursive=True)):
        rel = os.path.relpath(f, src)
        dest = os.path.join(outdir, os.path.splitext(rel)[0] + '.wav')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if esf_to_wav(f, dest):
            n += 1
    print(f'{n} sounds -> {outdir}/')


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
    src = args.splitdir
    outdir = args.outdir or os.path.join(src, '_tracks')
    os.makedirs(outdir, exist_ok=True)
    relocs = load_relocs(src)
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


def load_relocs(splitdir):
    """Load relocs.json, canonicalizing import names to the actual record
    filenames (trailer imports are sometimes lowercase, records uppercase)."""
    import json
    canon = {}
    for f in glob.glob(os.path.join(splitdir, '*.bin')):
        n = os.path.splitext(os.path.basename(f))[0]
        canon[n.upper()] = n
    try:
        with open(os.path.join(splitdir, 'relocs.json')) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: {int(l): canon.get(n.upper(), n) for l, n in v.items()}
            for k, v in raw.items()}


def cmd_models(args):
    src = args.splitdir
    outdir = args.outdir or os.path.join(src, '_models')
    os.makedirs(outdir, exist_ok=True)
    relocs = load_relocs(src)
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

    p = sub.add_parser('worldpack', help='rebuild the world container with '
                       'replaced records (modding)')
    p.add_argument('container', help='original bc0abcfa.bin')
    p.add_argument('moddir', help='directory of replacement <NAME>.bin records')
    p.add_argument('-o', '--output', required=True, help='output container')
    p.set_defaults(func=cmd_worldpack)

    p = sub.add_parser('repack', help='rebuild Hydro.fsd with replaced files '
                       '(modding); replacements are stored uncompressed')
    p.add_argument('archive', help='original Hydro.fsd')
    p.add_argument('moddir', help='directory of replacement files (extract layout)')
    p.add_argument('-o', '--output', required=True, help='output .fsd')
    p.set_defaults(func=cmd_repack)

    p = sub.add_parser('mod', help='ONE-SHOT repack (recommended): rebuild '
                       'Hydro.fsd straight from a mods folder containing '
                       'both world-record mods (GBBBANSHUH0.bin, ...) and '
                       'top-level file mods (data/textures/loading.egf, '
                       '...) side by side -- no manual worldpack+repack '
                       'two-step needed')
    p.add_argument('archive', help='original Hydro.fsd')
    p.add_argument('moddir', help='mods folder (world-record .bin/.trailer.bin '
                   'files and/or top-level extract-path files, mixed)')
    p.add_argument('-o', '--output', required=True, help='output .fsd')
    p.set_defaults(func=cmd_mod)

    p = sub.add_parser('retexture', help='re-encode an edited PNG back into '
                       'a T*/M*/B*/EGF texture record, ready for `mod`')
    p.add_argument('original', help='the original texture .bin (from a world '
                   '_split dir) or .egf file being replaced')
    p.add_argument('png', help='edited PNG, same width/height as the decoded '
                   'original (dimensions cannot change)')
    p.add_argument('-o', '--output', help='output file or directory '
                   '(default: <original-folder>/_mods/<same-name>)')
    p.set_defaults(func=cmd_retexture)

    p = sub.add_parser('anims', help='dump A* prop keyframe animations to JSON')
    p.add_argument('splitdir', help='a world _split directory')
    p.add_argument('-o', '--outdir', help='output dir (default: <splitdir>/_anims)')
    p.set_defaults(func=cmd_anims)

    p = sub.add_parser('cameras', help='dump D* demo camera cut lists to text')
    p.add_argument('splitdir', help='a world _split directory')
    p.add_argument('-o', '--outdir', help='output dir (default: <splitdir>/_cameras)')
    p.set_defaults(func=cmd_cameras)

    p = sub.add_parser('sounds', help='decode all ESF sounds/music to WAV')
    p.add_argument('extractdir', help='an extract output dir (contains sound/, wavmusic/)')
    p.add_argument('-o', '--outdir', help='output dir (default: <extractdir>/sounds/wav)')
    p.set_defaults(func=cmd_sounds)

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
