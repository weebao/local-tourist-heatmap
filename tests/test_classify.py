"""Tests for the locals/tourists rule and the map projection.

Run: PYTHONPATH=. .venv/bin/python tests/test_classify.py
"""
import math
import sys
from datetime import datetime, timedelta

from lat.classify import classify_users, LOCAL, TOURIST, UNKNOWN
from lat.fischer import (EquirectFrame, HANOI_BOUNDS, box_for, DLAT,
                         _ground_metres, find_pin_clusters)

FAIL = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name:34s} got={got!r}")


def close(name, got, want, tol):
    ok = abs(got - want) <= tol
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r} +-{tol}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name:34s} got={got:.6f} want={want:.6f}")


# ---------------------------------------------------------------- classification
S, W, N, E = HANOI_BOUNDS


def in_city(lon, lat):
    return W <= lon <= E and S <= lat <= N


D0 = datetime(2010, 3, 1)


def rec(u, lon, lat, day):
    return {"user": u, "lon": lon, "lat": lat, "date": D0 + timedelta(days=day)}


def labels_for(recs):
    return {u: v[0] for u, v in
            classify_users(recs, in_city, city_bounds=HANOI_BOUNDS).items()}


print("classification rule")
r = []
# a Hanoi resident: photographs here across 200 days
r += [rec("hanoi_local", 105.85, 21.03, d) for d in (0, 50, 200)]
# a Paris resident on a 2-day visit
r += [rec("paris", 2.35, 48.85, d) for d in (0, 150, 300)]
r += [rec("paris", 105.85, 21.03, d) for d in (400, 402)]
# someone with under a month of photos anywhere
r += [rec("newbie", 105.86, 21.02, d) for d in (0, 4)]
r += [rec("newbie", 100.5, 13.75, 5)]
# resident of Hanoi AND Tokyo: local of *this* city wins
r += [rec("both", 105.84, 21.04, d) for d in (0, 90)]
r += [rec("both", 139.7, 35.68, d) for d in (200, 300)]
# the month boundary
r += [rec("edge_29", 105.83, 21.05, d) for d in (0, 29)]
r += [rec("edge_30", 105.83, 21.05, d) for d in (0, 30)]
# a Haiphong resident (~100 km away) visiting for a day: a tourist here
r += [rec("haiphong", 106.68, 20.86, d) for d in (0, 60)]
r += [rec("haiphong", 105.85, 21.02, 61)]
# a single photograph is not a month
r += [rec("single", 105.85, 21.03, 0)]
# two photos at the same instant are not a month
r += [rec("sametime", 105.85, 21.03, 0), rec("sametime", 105.85, 21.03, 0)]

g = labels_for(r)
for user, want in {"hanoi_local": LOCAL, "paris": TOURIST, "newbie": UNKNOWN,
                   "both": LOCAL, "edge_29": UNKNOWN, "edge_30": LOCAL,
                   "haiphong": TOURIST, "single": UNKNOWN,
                   "sametime": UNKNOWN}.items():
    check(user, g[user], want)

print("\nhome city is a 15-mile box, exhaustively positioned")
# Haiphong resident visiting for a day -> tourist here.
r2 = [rec("hp_only", 106.68, 20.86, d) for d in (0, 90)]
r2 += [rec("hp_only", 105.85, 21.03, 91)]
check("haiphong resident, 1 day here", labels_for(r2)["hp_only"], TOURIST)

# A strung-out corridor is not a city: nobody has a month inside any single
# 15-mile box away from Hanoi.
r3 = [rec("corridor", 105.85 + 0.15 * i, 21.03 - 0.03 * i, i * 20) for i in range(6)]
check("strung-out corridor is not a city", labels_for(r3)["corridor"], UNKNOWN)

# The box search must cover every position that can matter, not just boxes
# centred on the photographer's own photos. These two points are ~10 miles
# apart on BOTH axes, so no point-centred box holds both, but one does exist.
r4 = [rec("between", 100.0, 10.0, 0), rec("between", 100.145, 10.145, 300)]
check("box between two points is found", labels_for(r4)["between"], TOURIST)

# Edge alignment alone is not enough under the disjointness constraint: the only
# box holding both of these, while staying clear of Hanoi's box, sits flush
# against Hanoi's western edge with its own edges touching neither point.
r4b = [rec("flush_west", 105.7182, 21.03, 0), rec("flush_west", 105.6782, 21.03, 100)]
check("flush-against-city box is found", labels_for(r4b)["flush_west"], TOURIST)

# A resident whose home box straddles the antimeridian is still a resident.
r5 = [rec("dateline", 179.99, 0.0, 0), rec("dateline", -179.99, 0.0, 200)]
r5 += [rec("dateline", 105.85, 21.03, 201)]
check("antimeridian home box works", labels_for(r5)["dateline"], TOURIST)

# A degree-of-longitude box is meaningless at the poles, so those candidate
# centres are refused rather than silently mismeasured.
r6 = [rec("polar", 10.0, 89.9, 0), rec("polar", 10.5, 89.9, 400)]
check("polar centres refused", labels_for(r6)["polar"], UNKNOWN)

# Someone resident just beyond the drawn box IS a local of a neighbouring box
# under this scheme. Documented edge effect: every real tourist's maximal-span
# home box centre is at least 29.8 km from the city centre, and only 1 of 613
# is within 60 km, so it is not driving the red layer.
r7 = [rec("just_outside", 105.85, 21.16, d) for d in (0, 120)]
r7 += [rec("just_outside", 105.85, 21.03, 121)]
check("just outside the box is foreign", labels_for(r7)["just_outside"], TOURIST)

print("\nclassification must not depend on the machine's timezone")
import os
import time as _time
_before = labels_for(r2)["hp_only"]
os.environ["TZ"] = "Pacific/Chatham"
try:
    _time.tzset()
except AttributeError:
    pass
check("timezone does not change a label", labels_for(r2)["hp_only"], _before)
os.environ.pop("TZ", None)
try:
    _time.tzset()
except AttributeError:
    pass

print("\nprojection")
f = EquirectFrame(HANOI_BOUNDS, 6137)
close("dlat is his 15-mile constant", f.dlat, DLAT, 1e-9)
close("equal ground scale x vs y",
      (f.dlon * math.cos(math.radians((S + N) / 2))) / f.dlat, 1.0, 1e-3)
close("metres per pixel", f.metres_per_px(), 3.95, 0.02)
# corners land on the corners
x, y = f.to_px(W, N)
close("NW corner x", x, 0.0, 1e-6)
close("NW corner y", y, 0.0, 1e-6)
x, y = f.to_px(E, S)
close("SE corner x", x, 6137.0, 1e-6)
close("SE corner y", y, 6137.0, 1e-6)
# north is up, east is right
x0, y0 = f.to_px(105.85, 21.00)
x1, y1 = f.to_px(105.90, 21.10)
check("east is right", x1 > x0, True)
check("north is up", y1 < y0, True)
# Hoan Kiem Lake sits near the middle of his box
hx, hy = f.to_px(105.8524, 21.0288)
check("Hoan Kiem inside frame", 0 < hx < 6137 and 0 < hy < 6137, True)

print("\nbox_for reproduces his box shape")
b = box_for(21.035238, 105.844850)
close("box_for dlat", b[2] - b[0], DLAT, 1e-9)
close("box_for dlon", b[3] - b[1], f.dlon, 1e-4)

print("\nplace-pin identity: repeated values, merged at a few metres")
# One real pin recorded with float noise must count as ONE pin, and one-off
# coordinates must not chain into it.
pin_rows = []
for i, (lo, la) in enumerate([(105.85, 21.033333), (105.849998, 21.0333),
                              (105.849997, 21.0333)]):
    for k in range(6):
        pin_rows.append({"id": f"p{i}_{k}", "user": f"u{i}{k % 3}",
                         "lon": lo, "lat": la})
c2c, pins, cent = find_pin_clusters(pin_rows)
check("float-noise variants are one pin", len(pins), 1)
check("all its photos are on the pin",
      sum(1 for r in pin_rows if c2c.get((r["lon"], r["lat"])) in pins), 18)
solo = pin_rows + [{"id": "s", "user": "z", "lon": 105.8501, "lat": 21.0334}]
c2c2, pins2, _ = find_pin_clusters(solo)
check("a one-off coordinate is not a pin",
      c2c2.get((105.8501, 21.0334)) in pins2, False)

print("\nground distance helper")
close("1 degree of latitude", _ground_metres(105.85, 21.0, 105.85, 22.0), 111320, 5)
close("zero distance", _ground_metres(105.85, 21.0, 105.85, 21.0), 0.0, 1e-9)

print()
if FAIL:
    print(f"{len(FAIL)} FAILURE(S):")
    for f_ in FAIL:
        print("  -", f_)
    sys.exit(1)
print("ALL TESTS PASS")
