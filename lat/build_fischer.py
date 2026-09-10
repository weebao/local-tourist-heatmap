"""Build the Hanoi map to Fischer's actual 2010 specification."""
import json, os, sys
from collections import defaultdict
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lat.build import load_hanoi, load_user_history, HANOI_LON, HANOI_LAT
from lat.classify import classify_users, LOCAL, TOURIST, UNKNOWN
from lat.basemap import load_overpass
from lat.fischer import (EquirectFrame, HANOI_BOUNDS, SIZE, draw_basemap,
                         build_marks, draw_marks, find_pins, find_pin_clusters)


def main(size=SIZE, out="out/hanoi_locals_tourists",
         overrides_path="data/reloc/overrides.json", exclude_paths=True,
         downscales=(1600, 800)):
    overrides, exclude = {}, []
    if os.path.exists(overrides_path):
        overrides = {k: tuple(v) for k, v in json.load(open(overrides_path)).items()}
        print(f"loaded {len(overrides)} visual relocations")
    ex_path = "data/reloc/exclude.json"
    if os.path.exists(ex_path):
        exclude = json.load(open(ex_path))
        print(f"loaded {len(exclude)} photos to drop as outside Hanoi")

    print("loading photos")
    hanoi = load_hanoi(overrides=overrides, exclude=exclude)
    users = {r["user"] for r in hanoi}
    hist = load_user_history(users)

    # Residency is judged inside Fischer's own Hanoi city box, the same box the
    # map is drawn in - that is what his cluster-based rule does. A generous
    # radius around the centre would count someone shooting 30 km outside the
    # city as a Hanoi local.
    s_, w_, n_, e_ = HANOI_BOUNDS

    def in_city(lon, lat):
        return w_ <= lon <= e_ and s_ <= lat <= n_

    print("classifying photographers")
    # Every Hanoi photo also appears in the worldwide history, at its ORIGINAL
    # coordinate and before any visual relocation. Left in, the stale copy is
    # re-injected and the relocations/exclusions are void for classification.
    # (Measured label-neutral today, but the guarantee should be real.)
    # Key on the PRE-relocation coordinate: that is the copy the worldwide
    # history holds, and keying on the post-override value cannot match it.
    stale = {(r["user"], r["date"].date(), round(r["orig_lon"], 3),
              round(r["orig_lat"], 3)) for r in hanoi}
    hist_kept = [h for h in hist
                 if (h["user"], h["date"].date(), round(h["lon"], 3),
                     round(h["lat"], 3)) not in stale]
    print(f"  dropped {len(hist) - len(hist_kept):,} history rows that duplicate "
          f"a Hanoi row (pre-relocation copies)")
    recs = hist_kept + [{"user": r["user"], "date": r["date"], "lon": r["lon"],
                         "lat": r["lat"]} for r in hanoi]
    labels_full = classify_users(recs, in_city, city_bounds=HANOI_BOUNDS)
    labels = {u: v[0] for u, v in labels_full.items()}
    cu = defaultdict(int)
    for lab in labels.values():
        cu[lab] += 1
    print(f"  photographers: local {cu[LOCAL]}  tourist {cu[TOURIST]}  unknown {cu[UNKNOWN]}")

    frame = EquirectFrame(HANOI_BOUNDS, size)
    print(f"  {frame}")

    print("drawing basemap")
    nodes, ways = load_overpass("data/hanoi_osm.json.gz")
    include = None
    if exclude_paths:
        skip = {"footway", "path", "steps", "cycleway", "bridleway", "corridor"}
        include = lambda t: t.get("highway") not in skip
    img, nw = draw_basemap(frame, nodes, ways, include=include)
    print(f"  {nw:,} ways stroked")

    # Mask the photos the vision pass adjudicated as wrongly placed. This is
    # keyed on explicit photo ids emitted by lat/apply_vision.py, not on a
    # coordinate format - see the note there.
    masked_ids = set()
    mi_path = "data/reloc/masked_ids.json"
    if os.path.exists(mi_path):
        masked_ids = set(json.load(open(mi_path)))
    masked = 0
    if masked_ids:
        keep = []
        for r in hanoi:
            if r["id"] in masked_ids and r["id"] not in overrides:
                masked += 1
                continue
            keep.append(r)
        print(f"  masked {masked} photos from the map: on a pile the vision "
              f"pass adjudicated a dumping ground, and not visually "
              f"identifiable (kept for classification)")
        hanoi_draw = keep
    else:
        hanoi_draw = hanoi

    print("building marks")
    pins = find_pins(hanoi_draw, exclude_ids=overrides)
    from lat.fischer import _pin_key
    n_pin = sum(1 for p in hanoi_draw if _pin_key(p) in pins)
    print(f"  {len(pins)} shared place-pin coordinates identified ({n_pin} photos)")
    ops = build_marks(hanoi_draw, labels, frame, pins=pins)
    npt = sum(1 for o in ops if o[0] == "pt")
    nln = sum(1 for o in ops if o[0] == "ln")
    cp = defaultdict(int)
    for o in ops:
        if o[0] == "pt":
            cp[o[3]] += 1
    from lat.fischer import COLORS
    inv = {v: k for k, v in COLORS.items()}
    print(f"  {npt:,} points + {nln:,} connecting lines")
    print("  points by class: " + "  ".join(f"{inv[c]} {n:,}" for c, n in cp.items()))

    # Photographers who actually put a mark on the map, so the README can quote
    # an artifact rather than a separately computed number.
    on_map = defaultdict(set)
    for r in hanoi_draw:
        if frame.contains(r["lon"], r["lat"]):
            on_map[labels[r["user"]]].add(r["user"])
    on_map_counts = {k: len(v) for k, v in on_map.items()}
    n_on_map = len(set().union(*on_map.values())) if on_map else 0
    print(f"  photographers with a mark on the map: {n_on_map} "
          + "  ".join(f"{k} {v}" for k, v in sorted(on_map_counts.items())))

    # Cross-class line overlap, measured from the ops rather than guessed from
    # colours (a colour test cannot see a blue/red crossing at all: the two
    # masks are mutually exclusive).
    import numpy as _np
    _cls = {}
    for o in ops:
        if o[0] != "ln":
            continue
        n = int(round(max(abs(o[3] - o[1]), abs(o[4] - o[2]))))
        if n == 0:
            xs, ys = [int(round(o[1]))], [int(round(o[2]))]
        else:
            t = _np.arange(n + 1, dtype=float) / n
            xs = _np.round(o[1] + (o[3] - o[1]) * t).astype(int)
            ys = _np.round(o[2] + (o[4] - o[2]) * t).astype(int)
        _cls.setdefault(o[5], set()).update(zip(xs, ys))
    _keys = list(_cls)
    _multi = set()
    for i in range(len(_keys)):
        for j in range(i + 1, len(_keys)):
            _multi |= _cls[_keys[i]] & _cls[_keys[j]]
    cross_class_px = len(_multi)
    print(f"  pixels carrying more than one line class: {cross_class_px}")

    print("compositing")
    img = draw_marks(img, ops)
    os.makedirs("out", exist_ok=True)
    full = f"{out}_{size}.png"
    img.save(full)
    print(f"wrote {full}")
    for d in downscales:
        # BOX = true area averaging, which is the relation the colour model
        # describes. Lanczos ringing cut pure white from 47% to 30%.
        img.resize((d, d), Image.BOX).save(f"{out}_{d}.png")
        print(f"wrote {out}_{d}.png")

    json.dump({"photographers": {k: cu[k] for k in (LOCAL, TOURIST, UNKNOWN)},
               "photographers_on_map": {k: on_map_counts.get(k, 0)
                                        for k in (LOCAL, TOURIST, UNKNOWN)},
               "photographers_on_map_total": n_on_map,
               "masked_on_discredited_pins": masked,
               "points": {inv[c]: n for c, n in cp.items()},
               "connecting_lines": nln,
               "cross_class_line_px": cross_class_px, "frame": repr(frame),
               "bounds_source": "Fischer bounds/flickr-picasa cluster 124",
               "relocated": len(overrides)},
              open("out/stats.json", "w"), indent=1)
    return img


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=SIZE)
    ap.add_argument("--out", default="out/hanoi_locals_tourists")
    # Excluding footways/paths is the default because modern OSM maps them far
    # more exhaustively than the 2010 basemap this style was drawn against.
    ap.add_argument("--include-paths", action="store_true",
                    help="also stroke footway/path/steps/cycleway/bridleway")
    a = ap.parse_args()
    main(size=a.size, out=a.out, exclude_paths=not a.include_paths)
