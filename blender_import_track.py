# Blender importer for HydroThunderTool exports: loads a track mesh and
# instances every prop from its placements CSV.
#
# Usage (Blender 3.x/4.x):
#   1. Run `hydrotool.py all` + `hydrotool.py tracks` first.
#   2. Open Blender -> Scripting tab -> open this file.
#   3. Set SPLIT_DIR and TRACK below, hit Run Script.
#
# One linked-duplicate collection instance is created per placement, so 600
# arrow signs cost one mesh. Props keep their MTL textures (they resolve via
# ../_textures/ relative to _models/).

import bpy
import csv
import math
import os

# ---- configure -------------------------------------------------------------
SPLIT_DIR = r"C:\path\to\out\bc0abcfa.bin_split"   # <-- edit me
TRACK = "HATARCTTRH0"                              # scene to build (see NAMING.md)
IMPORT_TRACK_MESH = True
# ----------------------------------------------------------------------------

TRACKS_DIR = os.path.join(SPLIT_DIR, "_tracks")
MODELS_DIR = os.path.join(SPLIT_DIR, "_models")


def import_obj(path):
    """Import an OBJ (Blender 4.x or legacy operator), return new objects."""
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=path)
    else:
        bpy.ops.import_scene.obj(filepath=path)
    return [o for o in bpy.data.objects if o not in before]


def game_to_blender(x, y, z):
    """Game space is Y-up; Blender is Z-up. Matches the OBJ importer's
    default axis mapping (forward -Z / up Y)."""
    return (x, -z, y)


def main():
    if IMPORT_TRACK_MESH:
        track_obj = os.path.join(TRACKS_DIR, TRACK + ".obj")
        if os.path.exists(track_obj):
            for o in import_obj(track_obj):
                o.name = TRACK + "_world"
        else:
            print("no world mesh for", TRACK, "(menu/bonus scene)")

    csv_path = os.path.join(TRACKS_DIR, TRACK + "_nodes.csv")
    rows = list(csv.DictReader(open(csv_path)))
    print(len(rows), "placements")

    cache = {}   # model name -> collection to instance
    missing = set()
    for i, r in enumerate(rows):
        model = r["model"]
        if model not in cache:
            path = os.path.join(MODELS_DIR, model + ".obj")
            if not os.path.exists(path):
                missing.add(model)
                cache[model] = None
                continue
            objs = import_obj(path)
            coll = bpy.data.collections.new(model)
            bpy.context.scene.collection.children.link(coll)
            for o in objs:
                for c in o.users_collection:
                    c.objects.unlink(o)
                coll.objects.link(o)
                o.hide_render = o.hide_viewport = False
            # hide the source collection; we only want the instances
            bpy.context.view_layer.layer_collection.children[
                coll.name].exclude = True
            cache[model] = coll
        coll = cache[model]
        if coll is None:
            continue
        inst = bpy.data.objects.new(
            "%s_%s_%03d" % (r["tag"] or "node", model, i), None)
        inst.instance_type = "COLLECTION"
        inst.instance_collection = coll
        inst.location = game_to_blender(
            float(r["x"]), float(r["y"]), float(r["z"]))
        # yaw: game heading about the up axis; flip sign if props face backwards
        inst.rotation_euler = (0.0, 0.0, -math.radians(float(r["yaw_deg"])))
        s = float(r["scale"]) or 1.0
        inst.scale = (s, s, s)
        bpy.context.scene.collection.objects.link(inst)

    if missing:
        print("models not found:", sorted(missing))
    print("done:", TRACK)


main()
