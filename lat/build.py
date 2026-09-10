"""Assemble the Hanoi locals-and-tourists map end to end."""
import csv, gzip, json, os, sys
from collections import defaultdict
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lat.proj import SquareFrame
from lat.classify import classify_users, LOCAL, TOURIST, UNKNOWN
from lat.render import render
from lat.basemap import load_overpass, build_layers, combine

HANOI_LON, HANOI_LAT = 105.8542, 21.0285
# Flickr opened in Feb 2004 and YFCC100M stops in 2014. Dates outside this
# window are broken EXIF (the raw data contains 1950 and 1951 stamps); left in,
# a single bogus 1950 date inflates a photographer's span past a month and
# mislabels them a local.
DATE_MIN, DATE_MAX = datetime(2003, 1, 1), datetime(2015, 6, 1)


def parse_date(s):
    try:
        d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return d if DATE_MIN <= d <= DATE_MAX else None


def load_hanoi(path="data/yfcc/hanoi_wide.tsv", overrides=None, exclude=None):
    """overrides: {photo_id: (lon, lat)} from the visual relocation pass.
    exclude: photo ids the visual pass placed outside Hanoi entirely."""
    overrides = overrides or {}
    exclude = set(exclude or ())
    rows, bad_date, relocated, dropped = [], 0, 0, 0
    with open(path, newline="") as f:
        for r in csv.reader(f, delimiter="\t"):
            if len(r) < 7:
                continue
            if r[0] in exclude:
                dropped += 1
                continue
            d = parse_date(r[3])
            if d is None:
                bad_date += 1
                continue
            try:
                lon, lat, acc = float(r[4]), float(r[5]), int(r[6] or 0)
            except ValueError:
                continue
            olon, olat = lon, lat
            if r[0] in overrides:
                lon, lat = overrides[r[0]]
                relocated += 1
            rows.append({"id": r[0], "user": r[1], "date": d,
                         "lon": lon, "lat": lat, "acc": acc,
                         # the coordinate as harvested, which is what the
                         # worldwide-history file also holds
                         "orig_lon": olon, "orig_lat": olat})
    print(f"  hanoi rows kept {len(rows)}  (dropped {bad_date} bad/out-of-range dates, "
          f"{dropped} placed outside Hanoi by sight, {relocated} visually relocated)")
    return rows


def load_user_history(users, path="data/yfcc/allgeo.tsv.gz",
                      cache="data/yfcc/hist_hanoi_users.tsv"):
    """Worldwide photo history for the given photographers.

    48M rows is slow to filter in Python, so the row selection is pushed down
    to awk and the result cached.
    """
    want = set(users)
    # Key the cache on the photographer set as well as the source file. Keying
    # on mtime alone would silently reuse a stale join if the set of Hanoi
    # photographers changed (which it does whenever relocations or exclusions
    # change) while allgeo.tsv.gz stayed put.
    import hashlib
    sig = hashlib.sha1(("\n".join(sorted(want))).encode()).hexdigest()[:16]
    sig_path = cache + ".sig"
    stale = (not os.path.exists(cache)
             or os.path.getmtime(cache) < os.path.getmtime(path)
             or not os.path.exists(sig_path)
             or open(sig_path).read().strip() != sig)
    if stale:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write("\n".join(sorted(want)) + "\n")
            ulist = tf.name
        print(f"  filtering {path} for {len(want)} photographers (awk)...")
        # pipefail, and write to a temp path then rename: without both, a
        # truncated allgeo.tsv.gz yields a partial cache stamped with a valid
        # signature, and every later run silently trusts it.
        tmp = cache + ".part"
        cmd = (f"zcat {path} | awk -F'\\t' 'NR==FNR{{u[$1];next}} $1 in u' "
               f"{ulist} - > {tmp}")
        try:
            subprocess.run(["bash", "-o", "pipefail", "-c", cmd], check=True)
            os.replace(tmp, cache)
        finally:
            for junk in (ulist, tmp):
                try:
                    os.unlink(junk)
                except OSError:
                    pass
        with open(sig_path, "w") as f:
            f.write(sig)
    out = []
    n = 0
    with open(cache) as f:
        for line in f:
            n += 1
            p = line.rstrip("\n").split("\t")
            if len(p) != 4 or p[0] not in want:
                continue
            try:
                d = datetime.strptime(p[1], "%Y-%m-%d")
            except ValueError:
                continue
            if not (DATE_MIN <= d <= DATE_MAX):
                continue
            out.append({"user": p[0], "date": d,
                        "lon": float(p[2]), "lat": float(p[3])})
    print(f"  scanned {n:,} worldwide rows -> {len(out):,} rows for "
          f"{len({r['user'] for r in out})} of {len(want)} Hanoi photographers")
    return out


def main(span_m=32000, size=2000, out="out/hanoi_locals_tourists.png",
         city_radius_deg=0.28, k=0.55, gamma=0.52, dot_radius=0.0,
         point_sigma=0.6,
         overrides_path="data/reloc/overrides.json"):
    overrides = {}
    if os.path.exists(overrides_path):
        overrides = {k2: tuple(v) for k2, v in json.load(open(overrides_path)).items()}
        print(f"loaded {len(overrides)} visual relocations")

    print("loading Hanoi photos")
    hanoi = load_hanoi(overrides=overrides)
    users = {r["user"] for r in hanoi}

    print("loading worldwide history for those photographers")
    hist = load_user_history(users)

    # The city test: a generous disc around Hanoi, so a photographer shooting
    # anywhere in the metro counts as being "in this city".
    def in_city(lon, lat):
        return ((lon - HANOI_LON) ** 2 + (lat - HANOI_LAT) ** 2) <= city_radius_deg ** 2

    print("classifying photographers")
    recs = hist + [{"user": r["user"], "date": r["date"],
                    "lon": r["lon"], "lat": r["lat"]} for r in hanoi]
    labels = classify_users(recs, in_city, city_bounds=(20.85, 105.60, 21.20, 106.05))
    counts = defaultdict(int)
    for _u, (lab, _i) in labels.items():
        counts[lab] += 1
    print(f"  photographers: local {counts[LOCAL]}  tourist {counts[TOURIST]}  "
          f"unknown {counts[UNKNOWN]}")

    frame = SquareFrame(HANOI_LON, HANOI_LAT, span_m, size)
    print(f"  {frame}")

    pts = {LOCAL: [[], []], TOURIST: [[], []], UNKNOWN: [[], []]}
    pc = defaultdict(int)
    for r in hanoi:
        lab = labels[r["user"]][0]
        x, y = frame.to_px(r["lon"], r["lat"])
        if -2 <= x < size + 2 and -2 <= y < size + 2:
            pts[lab][0].append(x); pts[lab][1].append(y)
            pc[lab] += 1
    print(f"  photos in frame: local {pc[LOCAL]}  tourist {pc[TOURIST]}  "
          f"unknown {pc[UNKNOWN]}  total {sum(pc.values())}")

    print("rasterising basemap")
    nodes, ways = load_overpass("data/hanoi_osm.json.gz")
    roads = combine(build_layers(nodes, ways, frame, ss=4), sigma=0.35)

    print("rendering")
    im = render(frame, {k2: (np.array(v[0]), np.array(v[1])) for k2, v in pts.items()},
                roads=roads, dot_radius=dot_radius, point_sigma=point_sigma,
                k=k, gamma=gamma)
    os.makedirs("out", exist_ok=True)
    im.save(out)
    print(f"wrote {out}")
    json.dump({"counts_photographers": dict(counts), "counts_photos": dict(pc),
               "frame": repr(frame), "n_relocated": len(overrides)},
              open("out/stats.json", "w"), indent=1)
    return im


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--span", type=float, default=32000)
    ap.add_argument("--size", type=int, default=2000)
    ap.add_argument("--out", default="out/hanoi_locals_tourists.png")
    ap.add_argument("--k", type=float, default=0.55)
    ap.add_argument("--gamma", type=float, default=0.52)
    ap.add_argument("--dot", type=float, default=0.0)
    ap.add_argument("--psig", type=float, default=0.6)
    a = ap.parse_args()
    main(span_m=a.span, size=a.size, out=a.out, k=a.k, gamma=a.gamma,
         dot_radius=a.dot, point_sigma=a.psig)
