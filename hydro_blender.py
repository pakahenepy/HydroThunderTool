# Hydro Thunder (PC) Blender add-on: import/export for HydroThunderTool.
#
# Install: Edit > Preferences > Add-ons > Install... > pick this file.
# A "Hydro Thunder" tab appears in the 3D View sidebar (N-panel).
#
# Works directly on a world _split directory made by `hydrotool.py world`
# (needs relocs.json + _textures/ for materials, i.e. run `hydrotool.py all`).
#
#   Import Model  - any G*.bin: one mesh object per sub-part under a root
#                   empty, textured materials, per-corner normals.
#   Import Track  - any H*.bin: world mesh + every prop placement instanced
#                   (collection instances), one click per track.
#   Import Anim   - any A*.bin onto the selected model root: keyframes each
#                   sub-part (bones == sub-parts, rigid hierarchy animation).
#   Export Model  - selected mesh objects -> a game-ready G record
#                   (<NAME>.bin + <NAME>.trailer.bin) for `hydrotool.py
#                   worldpack`. Materials named after a texture resource
#                   (e.g. MNTTORC_112) become texture bindings.
#
# Format docs: FSD_format.md in the HydroThunderTool repo.

bl_info = {
    "name": "Hydro Thunder (PC) formats",
    "author": "HydroThunderTool",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "3D View > Sidebar > Hydro Thunder",
    "description": "Import/export Hydro Thunder G models, H tracks, A anims",
    "category": "Import-Export",
}

import json
import math
import os
import struct

try:
    import bpy
    import bmesh
    from bpy_extras.io_utils import ImportHelper, ExportHelper
    from mathutils import Matrix, Vector
    IN_BLENDER = True
except ImportError:          # allows headless parsing tests outside Blender
    IN_BLENDER = False

B = 4  # every offset in a record is relative to record+4

# game is Y-up, Blender is Z-up:  (x, y, z) -> (x, -z, y)
GAME_TO_BLENDER = ((1, 0, 0), (0, 0, -1), (0, 1, 0))


# ===========================================================================
# pure parsing (no bpy) -- testable outside Blender
# ===========================================================================

def parse_g(d):
    """Parse a G record -> dict with verts/uvs/normals/subparts/surfaces."""
    if len(d) < 0x54 or d[3:4] != b'G':
        raise ValueError('not a G record')
    nsub = struct.unpack_from('<I', d, 0x08)[0]
    ntri, _x14, nvert, nuv, nrec, nnorm = struct.unpack_from('<6I', d, 0x10)
    slots = struct.unpack_from('<9I', d, 0x28)
    subs_o, vert_o, uv_o, norm_o = (slots[1]+B, slots[5]+B, slots[6]+B,
                                    slots[8]+B)
    verts = [struct.unpack_from('<3f', d, vert_o+i*24) for i in range(nvert)]
    uvs = ([struct.unpack_from('<2f', d, uv_o+i*8) for i in range(nuv)]
           if nuv and uv_o + nuv*8 <= len(d) else [])
    norms = ([struct.unpack_from('<3f', d, norm_o+i*12) for i in range(nnorm)]
             if nnorm and norm_o + nnorm*12 <= len(d) else [])
    subparts = []
    for s in range(nsub):
        so = subs_o + s*0xc4
        nsurf, surfoff = struct.unpack_from('<2I', d, so+4)
        surfaces = []
        for j in range(nsurf):
            cnt, toff, moff = struct.unpack_from('<3I', d, surfoff+B+j*12)
            tris = []
            for t in range(cnt):
                to = toff + B + t*0x30
                e = struct.unpack_from('<9H', d, to+0x1c)
                fnorm = struct.unpack_from('<3f', d, to+0x10)
                tris.append((e[0::3], e[1::3], e[2::3], fnorm))
            surfaces.append({'mat_off': moff, 'tris': tris})
        subparts.append(surfaces)
    return {'verts': verts, 'uvs': uvs, 'norms': norms,
            'subparts': subparts, 'nrec': nrec}


def parse_h(d):
    """Parse an H track scene -> world mesh dict (or None) like parse_g."""
    if len(d) < 0x100 or d[3:4] != b'H':
        raise ValueError('not an H record')
    nsec = struct.unpack_from('<I', d, 4)[0]
    base = 8 + nsec*20
    nsurf, ntri, nmat, nvert, nuv = struct.unpack_from('<5I', d, base+0x90)
    slots = struct.unpack_from('<9I', d, base+0xb0)
    if not (nvert and nsurf):
        return None
    surf_o, vert_o, uv_o = slots[1]+B, slots[4]+B, slots[5]+B
    verts = [struct.unpack_from('<3f', d, vert_o+i*24) for i in range(nvert)]
    uvs = [struct.unpack_from('<2f', d, uv_o+i*8) for i in range(nuv)]
    surfaces = []
    for j in range(nsurf):
        cnt, tptr, mptr = struct.unpack_from('<3I', d, surf_o+j*12)
        tris = []
        for t in range(cnt):
            to = tptr + B + t*0x30
            e = struct.unpack_from('<9H', d, to+0x1c)
            tris.append((e[0::3], e[1::3], e[2::3], None))
        surfaces.append({'mat_off': mptr, 'tris': tris})
    return {'verts': verts, 'uvs': uvs, 'norms': [],
            'subparts': [surfaces], 'nrec': 0}


def parse_h_nodes(d, texrefs):
    """Model placements from an H record (needs its relocs)."""
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
        scale = struct.unpack_from('<f', d, p + 0x34)[0] or 1.0
        out.append((res, tag, x, y, z, yaw, scale))
    return out


def parse_anim(d):
    """Parse an A record -> (nframes, nbones, dt, binds, keys).

    Layout: header, then one 108-byte bind record per bone (records are
    {12f local matrix+trans, 12f root matrix+trans, 1.0, 1.0, 0.0} and the
    binds are separated by {u32,u32} pairs), then nframes*nbones key
    records, frame-major. Playback transform for bone b at frame f:
    Root(f,b) @ Local(f,b) @ inverse(RootBind(b) @ LocalBind(b)) -- the
    identity at the bind pose, so assembled model-space parts animate as
    deltas."""
    if d[3:4] != b'A':
        raise ValueError('not an A record')
    nframes, nbones = struct.unpack_from('<2H', d, 4)
    dt = struct.unpack_from('<f', d, 8)[0]
    recs = []
    pos = 0x14
    while pos + 12 <= len(d):
        if pos + 108 <= len(d):
            v = struct.unpack_from('<27f', d, pos)
            if v[24] == 1.0 and v[25] == 1.0 and v[26] == 0.0:
                loc = ((v[0], v[1], v[2]), (v[3], v[4], v[5]),
                       (v[6], v[7], v[8]), (v[9], v[10], v[11]))
                root = ((v[12], v[13], v[14]), (v[15], v[16], v[17]),
                        (v[18], v[19], v[20]), (v[21], v[22], v[23]))
                recs.append((loc, root))
                pos += 108
                continue
        pos += 8                            # bone-track separator
    binds, keys = recs[:nbones], recs[nbones:]
    return nframes, nbones, dt, binds, keys


def find_anims(splitdir, model_name):
    """A records matching a G model's name stem (same track/category/name,
    any variant suffix), e.g. GAXBEAR_AH0 -> AAXBEAR_*."""
    stem = model_name[1:9]
    out = []
    for f in sorted(os.listdir(splitdir)):
        if f.endswith('.bin') and f[:1] in 'Aa' and f[1:9].upper() == stem.upper():
            out.append(os.path.join(splitdir, f))
    return out


def load_relocs(splitdir):
    try:
        with open(os.path.join(splitdir, 'relocs.json')) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: {int(l): n for l, n in v.items()} for k, v in raw.items()}


# ===========================================================================
# G record builder (no bpy) -- for export
# ===========================================================================

def build_g(subparts, verts, uvs, norms):
    """Build a game-ready G record + relocation trailer.
    subparts: [ [ {texture: name-or-None, tris: [(vi3, ni3, ui3), ...]} ] ]
    Returns (record_bytes, trailer_bytes)."""
    ntri = sum(len(s['tris']) for sp in subparts for s in sp)
    nsurf = sum(len(sp) for sp in subparts)
    nsub = len(subparts)
    if not norms:
        norms = [(0.0, 0.0, 1.0)]

    def vsub(a, b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
    def cross(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2],
                             a[0]*b[1]-a[1]*b[0])
    def norm3(a):
        l = math.sqrt(a[0]*a[0]+a[1]*a[1]+a[2]*a[2]) or 1.0
        return (a[0]/l, a[1]/l, a[2]/l)

    xs = [v[0] for v in verts]; ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    lo = (min(xs), min(ys), min(zs)); hi = (max(xs), max(ys), max(zs))
    ctr = tuple((lo[i]+hi[i])/2 for i in range(3))
    rad = max(math.dist(ctr, v) for v in verts)

    fixups = []          # internal reloc locations (rel record+4 coords)
    imports = []         # (texture_name, location)

    # section order mirrors retail files: fixed bbox/culling block right
    # after the 0x60 header, then subparts, surfaces, tris, materials,
    # verts, uvs, runtime cache, normals.
    out = bytearray(0x60)                      # header, filled at the end
    def rel(): return len(out) - B

    corners = [(X[0], Y[1], Z[2]) for X in (lo, hi) for Y in (lo, hi)
               for Z in (lo, hi)]
    bb = struct.pack('<I4fI', 0, ctr[0], ctr[1], ctr[2], rad, 8)
    bb += b''.join(struct.pack('<3f', *c) for c in corners)
    out += bb + bytes(0xcc - len(bb))

    sub_off = rel()
    sub_pos = len(out)
    out += bytes(0xc4 * nsub)                  # placeholder sub-parts

    surf_off = rel()
    surf_pos = len(out)
    out += bytes(12 * nsurf)

    tri_off = rel()
    tri_pos = len(out)
    out += bytes(0x30 * ntri)

    mats = []                                  # unique texture -> mat index
    texmap = {}
    for sp in subparts:
        for s in sp:
            key = s.get('texture')
            if key not in texmap:
                texmap[key] = len(mats)
                mats.append(key)
    mat_off = rel()
    mat_pos = len(out)
    for k, tex in enumerate(mats):
        m = bytearray(0x2c)
        struct.pack_into('<HHI', m, 0, 1, 0, 0)
        struct.pack_into('<4f', m, 0x18, 1.0, 1.0, 1.0, 1.0)
        out += m
        if tex:
            imports.append((tex, mat_off + k*0x2c + 0x14))

    vert_off = rel()
    for v in verts:
        out += struct.pack('<3f3f', v[0], v[1], v[2], 0.0, 0.0, 1.0)

    uv_off = rel()
    for u in uvs:
        out += struct.pack('<2f', u[0], u[1])

    nrec = max(1, len(norms))                  # runtime lighting cache
    cache_off = rel()
    out += bytes(24 * nrec)

    norm_off = rel()
    for n in norms:
        out += struct.pack('<3f', *n)

    # fill sub-parts + surfaces + tris
    si = 0; ti = 0
    for spi, sp in enumerate(subparts):
        so = sub_pos + spi*0xc4
        pv = [verts[t[0][k]] for s in sp for t in s['tris'] for k in range(3)]
        if not pv:
            pv = [ctr]
        pl = (min(v[0] for v in pv), min(v[1] for v in pv),
              min(v[2] for v in pv))
        ph = (max(v[0] for v in pv), max(v[1] for v in pv),
              max(v[2] for v in pv))
        struct.pack_into('<12f', out, so, ph[0], pl[1], pl[2], pl[0],
                         ph[1], ph[2], pl[0], ph[1], pl[2], ph[0], pl[1],
                         ph[0])
        for k in range(12):
            struct.pack_into('<f', out, so + 0x4c + k*4, 255.0)
        struct.pack_into('<2I', out, so + 4, len(sp), surf_off + si*12)
        fixups.append(sub_off + spi*0xc4 + 8)
        for s in sp:
            po = surf_pos + si*12
            struct.pack_into('<3I', out, po, len(s['tris']),
                             tri_off + ti*0x30,
                             mat_off + texmap[s.get('texture')]*0x2c)
            fixups.append(surf_off + si*12 + 4)
            fixups.append(surf_off + si*12 + 8)
            for (vi, ni, ui) in s['tris']:
                to = tri_pos + ti*0x30
                a, b_, c = (verts[vi[0]], verts[vi[1]], verts[vi[2]])
                cen = tuple((a[k]+b_[k]+c[k])/3 for k in range(3))
                fn = norm3(cross(vsub(b_, a), vsub(c, a)))
                r = max(math.dist(cen, p) for p in (a, b_, c))
                struct.pack_into('<4f3f', out, to, cen[0], cen[1], cen[2],
                                 r, fn[0], fn[1], fn[2])
                struct.pack_into('<9H', out, to + 0x1c,
                                 vi[0], ni[0], ui[0], vi[1], ni[1], ui[1],
                                 vi[2], ni[2], ui[2])
                ti += 1
            si += 1

    # header
    struct.pack_into('<I', out, 0, (len(out) & 0xffffff) | (ord('G') << 24))
    struct.pack_into('<2I', out, 8, nsub, nsub)
    struct.pack_into('<6I', out, 0x10, ntri, 1, len(verts), len(uvs),
                     nrec, len(norms))
    struct.pack_into('<9I', out, 0x28, 0, sub_off, surf_off, tri_off,
                     mat_off, vert_off, uv_off, cache_off, norm_off)
    struct.pack_into('<4f', out, 0x50, ctr[0], ctr[1], ctr[2], rad)
    for k in range(1, 9):                       # slots 1..8 are relocated
        fixups.append(0x24 + k*4)

    entries = [(b'', loc) for loc in sorted(fixups)] + \
              [(nm.encode('latin1'), loc) for nm, loc in imports]
    trailer = bytearray(struct.pack('<2I', 0, len(entries)))
    for nm, loc in entries:
        trailer += nm.ljust(12, b'\x00')[:12] + struct.pack('<I', loc)
    return bytes(out), bytes(trailer)


# ===========================================================================
# Blender glue
# ===========================================================================

if IN_BLENDER:

    CONV = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))

    def _material(name, splitdir):
        mat = bpy.data.materials.get(name)
        if mat:
            return mat
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        png = os.path.join(splitdir, '_textures', name + '.png')
        if os.path.isfile(png):
            bsdf = mat.node_tree.nodes.get('Principled BSDF')
            tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
            tex.image = bpy.data.images.load(png, check_existing=True)
            tex.interpolation = 'Closest'
            mat.node_tree.links.new(bsdf.inputs['Base Color'],
                                    tex.outputs['Color'])
        return mat

    def _mesh_from(name, g, surfaces, splitdir, texrefs, coll):
        verts, uvs, norms = g['verts'], g['uvs'], g['norms']
        me = bpy.data.meshes.new(name)
        faces = []
        face_mat = []
        face_uv = []
        face_no = []
        mats = []
        mat_idx = {}
        for s in surfaces:
            tex = (texrefs or {}).get(s['mat_off'] + 0x14)
            key = tex or 'hydro_untextured'
            if key not in mat_idx:
                mat_idx[key] = len(mats)
                mats.append(key)
            for (vi, ni, ui, fn) in s['tris']:
                faces.append(tuple(vi))
                face_mat.append(mat_idx[key])
                face_uv.append(ui)
                face_no.append(ni)
        me.from_pydata([tuple(v) for v in verts], [], faces)
        for key in mats:
            me.materials.append(_material(key, splitdir))
        for p, mi in zip(me.polygons, face_mat):
            p.material_index = mi
        if uvs:
            uvl = me.uv_layers.new(name='UVMap')
            for p, ui in zip(me.polygons, face_uv):
                for li, u in zip(p.loop_indices, ui):
                    if u < len(uvs):
                        # game v maps to Blender Y directly: the PNGs are
                        # written bottom-up, which already matches Blender's
                        # bottom-left UV origin
                        uvl.data[li].uv = (uvs[u][0], uvs[u][1])
        if norms:
            loops = []
            for p, ni in zip(me.polygons, face_no):
                for k in range(3):
                    n = norms[ni[k]] if ni[k] < len(norms) else (0, 0, 1)
                    loops.append(n)
            try:
                me.normals_split_custom_set(loops)
            except Exception:
                pass
        me.update()
        ob = bpy.data.objects.new(name, me)
        coll.objects.link(ob)
        return ob

    def import_model(binpath, coll=None, with_anims=True):
        splitdir = os.path.dirname(binpath)
        name = os.path.splitext(os.path.basename(binpath))[0]
        g = parse_g(open(binpath, 'rb').read())
        texrefs = load_relocs(splitdir).get(name, {})
        coll = coll or bpy.context.scene.collection
        root = bpy.data.objects.new(name, None)
        root.matrix_world = CONV
        coll.objects.link(root)
        for i, surfaces in enumerate(g['subparts']):
            ob = _mesh_from('%s.part%d' % (name, i), g, surfaces,
                            splitdir, texrefs, coll)
            ob.parent = root
        if with_anims:
            for ap in find_anims(splitdir, name):
                try:
                    import_anim(ap, root)
                except Exception:
                    pass
        return root

    def import_track(binpath):
        splitdir = os.path.dirname(binpath)
        name = os.path.splitext(os.path.basename(binpath))[0]
        d = open(binpath, 'rb').read()
        relocs = load_relocs(splitdir)
        texrefs = relocs.get(name, {})
        scn = bpy.context.scene
        world = parse_h(d)
        if world:
            root = bpy.data.objects.new(name + '_world', None)
            root.matrix_world = CONV
            scn.collection.objects.link(root)
            ob = _mesh_from(name + '_mesh', world, world['subparts'][0],
                            splitdir, texrefs, scn.collection)
            ob.parent = root
        # placements
        cache = {}
        canon = {n.upper(): n for n in
                 (os.path.splitext(f)[0]
                  for f in os.listdir(splitdir) if f.endswith('.bin'))}
        for (model, tag, x, y, z, yaw, scale) in parse_h_nodes(d, texrefs):
            model = canon.get(model.upper(), model)
            if model not in cache:
                srcs = bpy.data.collections.get('Hydro Sources')
                if not srcs:
                    srcs = bpy.data.collections.new('Hydro Sources')
                    scn.collection.children.link(srcs)
                mcoll = bpy.data.collections.new(model)
                srcs.children.link(mcoll)
                path = os.path.join(splitdir, model + '.bin')
                if os.path.isfile(path):
                    import_model(path, mcoll, with_anims=True)
                lc = bpy.context.view_layer.layer_collection.children.get(
                    'Hydro Sources')
                if lc:
                    lc.exclude = True
                cache[model] = mcoll
            inst = bpy.data.objects.new('%s_%s' % (tag or 'node', model),
                                        None)
            inst.instance_type = 'COLLECTION'
            inst.instance_collection = cache[model]
            inst.location = CONV @ Vector((x, y, z))
            inst.rotation_euler = (0, 0, -math.radians(yaw))
            inst.scale = (scale,) * 3
            scn.collection.objects.link(inst)

    def _key_object(ob, anim_name, frames):
        """Bake (frame, Matrix) list into a muted NLA track on ob."""
        rest_basis = ob.matrix_basis.copy()
        rest_pinv = ob.matrix_parent_inverse.copy()
        ad = ob.animation_data_create()
        act = bpy.data.actions.new('%s.%s' % (anim_name, ob.name))
        ad.action = act
        for f, mat in frames:
            ob.matrix_parent_inverse.identity()
            ob.matrix_basis = mat
            ob.keyframe_insert('location', frame=f)
            ob.keyframe_insert('rotation_euler', frame=f)
            ob.keyframe_insert('scale', frame=f)
        try:
            ad.action = None
        except Exception:
            pass
        track = ad.nla_tracks.new()
        track.name = anim_name
        track.mute = True
        strip = track.strips.new(anim_name, 1, act)
        if hasattr(strip, 'action_slot') and getattr(act, 'slots', None):
            try:
                strip.action_slot = act.slots[0]
            except Exception:
                pass
        ob.matrix_basis = rest_basis                # restore rest pose
        ob.matrix_parent_inverse = rest_pinv

    def _mat4(blk):
        m, t = blk[:3], blk[3]
        return Matrix(((m[0][0], m[0][1], m[0][2], t[0]),
                       (m[1][0], m[1][1], m[1][2], t[1]),
                       (m[2][0], m[2][1], m[2][2], t[2]),
                       (0, 0, 0, 1)))

    def import_anim(binpath, root, as_nla=True):
        anim_name = os.path.splitext(os.path.basename(binpath))[0]
        nframes, nbones, dt, binds, keys = parse_anim(
            open(binpath, 'rb').read())
        parts = sorted((o for o in root.children), key=lambda o: o.name)
        scn = bpy.context.scene
        scn.render.fps = max(1, round(1.0 / dt)) if dt > 0 else 30
        scn.frame_start = 1
        scn.frame_end = max(scn.frame_end, nframes)
        targets = parts if (nbones > 1 and parts) else [root]
        inv_bind = []
        for b in range(nbones):
            if b < len(binds):
                loc, rt = binds[b]
                inv_bind.append((_mat4(rt) @ _mat4(loc)).inverted_safe())
            else:
                inv_bind.append(Matrix.Identity(4))
        per_ob = {ob: [] for ob in targets}
        for f in range(nframes):
            for bidx in range(min(nbones, len(targets))):
                k = f * nbones + bidx
                if k >= len(keys):
                    break
                loc, rt = keys[k]
                mat = _mat4(rt) @ _mat4(loc) @ inv_bind[bidx]
                per_ob[targets[bidx]].append((f + 1, mat))
        for ob, frames in per_ob.items():
            if frames:
                _key_object(ob, anim_name, frames)
        if not as_nla:
            set_animation(root, anim_name)

    def _anim_objects(root):
        if root.instance_type == 'COLLECTION' and root.instance_collection:
            for ob in root.instance_collection.all_objects:
                yield ob
            return
        yield root
        for ob in root.children_recursive:
            yield ob

    def set_animation(root, anim_name):
        """Unmute NLA tracks named anim_name under root; mute the rest.
        anim_name None/'' = static pose."""
        for ob in _anim_objects(root):
            ad = ob.animation_data
            if not ad:
                continue
            for tr in ad.nla_tracks:
                tr.mute = (tr.name != anim_name)
            if anim_name:
                for tr in ad.nla_tracks:
                    if tr.name == anim_name:
                        ob.matrix_parent_inverse.identity()

    def anim_names(root):
        names = []
        for ob in _anim_objects(root):
            if ob.animation_data:
                for tr in ob.animation_data.nla_tracks:
                    if tr.name not in names:
                        names.append(tr.name)
        return names

    def export_model(objs, outpath):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        inv = CONV.inverted()
        verts = []
        uvs = []
        norms = []
        vmap = {}
        umap = {}
        nmap = {}
        def vid(co):
            key = (round(co[0], 5), round(co[1], 5), round(co[2], 5))
            if key not in vmap:
                vmap[key] = len(verts)
                verts.append(key)
            return vmap[key]
        def uid(uv):
            key = (round(uv[0], 5), round(uv[1], 5))
            if key not in umap:
                umap[key] = len(uvs)
                uvs.append(key)
            return umap[key]
        def nid(n):
            key = (round(n[0], 4), round(n[1], 4), round(n[2], 4))
            if key not in nmap:
                nmap[key] = len(norms)
                norms.append(key)
            return nmap[key]
        subparts = []
        for ob in objs:
            if ob.type != 'MESH':
                continue
            me = ob.evaluated_get(depsgraph).to_mesh()
            me.calc_loop_triangles()
            try:
                me.calc_normals_split()
            except AttributeError:
                pass
            xf = inv @ ob.matrix_world
            nxf = xf.to_3x3()
            uvl = me.uv_layers.active
            per_tex = {}
            for lt in me.loop_triangles:
                mat = (ob.material_slots[lt.material_index].material
                       if ob.material_slots else None)
                tex = mat.name.split('.')[0] if mat else None
                if tex == 'hydro_untextured':
                    tex = None
                vi = []
                ni = []
                ui = []
                for li, v in zip(lt.loops, lt.vertices):
                    co = xf @ me.vertices[v].co
                    vi.append(vid(co))
                    n = (nxf @ Vector(me.loops[li].normal)).normalized()
                    ni.append(nid(n))
                    ui.append(uid(uvl.data[li].uv) if uvl else 0)
                per_tex.setdefault(tex, []).append(
                    (tuple(vi), tuple(ni), tuple(ui)))
            subparts.append([{'texture': t, 'tris': tr}
                             for t, tr in per_tex.items()])
        if not uvs:
            uvs = [(0.0, 0.0)]
        rec, trailer = build_g(subparts, verts, uvs, norms)
        open(outpath, 'wb').write(rec)
        open(os.path.splitext(outpath)[0] + '.trailer.bin', 'wb'
             ).write(trailer)
        return len(verts), sum(len(s['tris']) for sp in subparts for s in sp)

    # ---- operators / UI -------------------------------------------------

    class HYDRO_OT_import_model(bpy.types.Operator, ImportHelper):
        bl_idname = 'hydro.import_model'
        bl_label = 'Import Hydro Model (G*.bin)'
        filename_ext = '.bin'
        filter_glob: bpy.props.StringProperty(default='*.bin',
                                              options={'HIDDEN'})
        files: bpy.props.CollectionProperty(
            type=bpy.types.OperatorFileListElement, options={'HIDDEN'})
        directory: bpy.props.StringProperty(subtype='DIR_PATH',
                                            options={'HIDDEN'})
        import_anims: bpy.props.BoolProperty(
            name='Import animations', default=True,
            description='Also import matching A* animations as toggleable '
                        'NLA tracks (use Play Animation in the panel)')
        def execute(self, context):
            paths = [os.path.join(self.directory, f.name)
                     for f in self.files if f.name] or [self.filepath]
            done = 0
            for p in paths:
                base = os.path.basename(p)
                if not base[:1].upper() == 'G':
                    self.report({'WARNING'}, base + ' is not a G model')
                    continue
                try:
                    import_model(p, with_anims=self.import_anims)
                    done += 1
                except Exception as e:
                    self.report({'WARNING'}, '%s: %s' % (base, e))
            if not done:
                return {'CANCELLED'}
            self.report({'INFO'}, 'imported %d model(s)' % done)
            return {'FINISHED'}

    class HYDRO_OT_import_track(bpy.types.Operator, ImportHelper):
        bl_idname = 'hydro.import_track'
        bl_label = 'Import Hydro Track (H*.bin)'
        filename_ext = '.bin'
        filter_glob: bpy.props.StringProperty(default='*.bin',
                                              options={'HIDDEN'})
        def execute(self, context):
            base = os.path.basename(self.filepath)
            if not base[:1].upper() == 'H':
                self.report({'ERROR'}, base + ' is not an H track scene')
                return {'CANCELLED'}
            import_track(self.filepath)
            return {'FINISHED'}

    def _root_of(ob):
        if ob.instance_type == 'COLLECTION' and ob.instance_collection:
            return ob                       # resolved by _anim_objects
        while ob and ob.parent:
            ob = ob.parent
        return ob

    def _anim_items(self, context):
        items = [('__none__', '(static)', 'mute all animations')]
        ob = context.active_object
        if ob:
            for n in anim_names(_root_of(ob)):
                items.append((n, n, ''))
        return items

    class HYDRO_OT_set_anim(bpy.types.Operator):
        bl_idname = 'hydro.set_anim'
        bl_label = 'Play Animation'
        bl_property = 'anim'
        anim: bpy.props.EnumProperty(items=_anim_items)
        def execute(self, context):
            ob = context.active_object
            if not ob:
                self.report({'ERROR'}, 'select a model first')
                return {'CANCELLED'}
            name = None if self.anim == '__none__' else self.anim
            set_animation(_root_of(ob), name)
            return {'FINISHED'}
        def invoke(self, context, event):
            context.window_manager.invoke_search_popup(self)
            return {'FINISHED'}

    class HYDRO_OT_import_anim(bpy.types.Operator, ImportHelper):
        bl_idname = 'hydro.import_anim'
        bl_label = 'Import Hydro Anim (A*.bin) onto selected model root'
        filename_ext = '.bin'
        filter_glob: bpy.props.StringProperty(default='*.bin',
                                              options={'HIDDEN'})
        def execute(self, context):
            base = os.path.basename(self.filepath)
            if not base[:1].upper() == 'A':
                self.report({'ERROR'}, base + ' is not an A animation')
                return {'CANCELLED'}
            root = context.active_object
            if root is None:
                self.report({'ERROR'}, 'select the model root empty first')
                return {'CANCELLED'}
            while root.parent:
                root = root.parent
            import_anim(self.filepath, root, as_nla=False)
            return {'FINISHED'}

    class HYDRO_OT_export_model(bpy.types.Operator, ExportHelper):
        bl_idname = 'hydro.export_model'
        bl_label = 'Export selection as Hydro G record'
        filename_ext = '.bin'
        def execute(self, context):
            nv, nt = export_model(context.selected_objects, self.filepath)
            self.report({'INFO'}, 'wrote %d verts / %d tris (+.trailer.bin)'
                        % (nv, nt))
            return {'FINISHED'}

    class HYDRO_PT_panel(bpy.types.Panel):
        bl_label = 'Hydro Thunder'
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = 'Hydro Thunder'
        def draw(self, context):
            c = self.layout.column()
            c.operator('hydro.import_model', text='Import Model (G)')
            c.operator('hydro.import_track', text='Import Track (H)')
            c.operator('hydro.import_anim', text='Import Anim (A)')
            c.separator()
            c.operator('hydro.set_anim', text='Play Animation...')
            c.separator()
            c.operator('hydro.export_model', text='Export Model (G)')

    CLASSES = (HYDRO_OT_import_model, HYDRO_OT_import_track,
               HYDRO_OT_import_anim, HYDRO_OT_set_anim,
               HYDRO_OT_export_model, HYDRO_PT_panel)

    def register():
        for cls in CLASSES:
            bpy.utils.register_class(cls)

    def unregister():
        for cls in reversed(CLASSES):
            bpy.utils.unregister_class(cls)

    if __name__ == '__main__':
        register()
