"""Faithful renderer for Eric Fischer's 2010 "Locals and Tourists" series.

The published JPEGs are 6137x6137 and the small versions everyone sees are
downscales of them. That matters: at full size the point marks are **opaque**,
and the soft blended colours of the small version are largely what
area-averaging those opaque pixels over white produces. For a downscaled block, let f_b, f_r, f_y be the fractions covered by opaque
blue, red and yellow POINTS, f_m the basemap (#AAAA00, which removes a third of
R and G and all of B), and g_b, g_r, g_y the fractions covered by LINES
weighted by their effective alpha:

    R/255 = 1 - f_b - g_b - f_m/3
    G/255 = 1 - f_b - f_r - g_b - g_r - f_m/3
    B/255 = 1 - f_r - f_y - g_r - g_y - f_m

Both extra terms matter. Dropping the basemap term makes the relation wrong by
up to 210/255 (it covers ~7% of the canvas, more than all the data ink
together); dropping the alpha weighting on the line terms costs up to 113/255
on ~1% of the published downscale. So we draw at full size and let a *box*
downscale do the averaging. (Lanczos would be wrong here for the same reason:
its ringing sprays grey haloes across pixels that should stay pure white.)

Spec, from the author's own descriptions plus measurement of the original:
  canvas      6137 x 6137, background #FFFFFF
  projection  cylindrical equirectangular, scaled so that a degree of latitude
              and a degree of longitude cover equal ground at the box centre
              ("Maybe I should have used Mercator instead")
  bounds      his own precomputed 15-mile city box, dlat = 0.217705 exactly
  basemap     1 px strokes, #AAAA00, uniform - no road hierarchy, no fills,
              water bodies appear as bare outlines
  points      3 x 3 px opaque squares
              #0000FF locals   #FF0000 tourists   #FFFF00 unknown
  lines       1 px segments at alpha 0.55, joining consecutive photos by the
              same photographer taken <= 10 minutes apart. Points are opaque;
              lines are not. Coincident segments ACCUMULATE: n overlapping
              same-colour segments give 1 - (1 - a)^n.

              Measured with no estimator at all. For blue ink over white the
              red channel is exactly 255(1-a), so an isolated single segment
              on a white background reads its own alpha off one pixel. Taking
              London blue pixels that survive a 15 px directional opening, sit
              >= 3 px from any 3x3 square, have no basemap or red ink within
              2 px, and are locally sparse (blue coverage < 3% in a 31x31
              window):

                  n = 981 clean isolated line pixels, median R = 115 -> 0.549
                  red lines: n = 187, median B = 118              -> 0.537
                  (255 * 0.45 = 114.75)

              Accumulation is visible in the same data: stratifying by local
              blue density, the median core red channel steps 114 -> 59 -> 50
              -> 47, against 115 for one 0.55 line and 52 for two.

              Two earlier answers were wrong and are recorded so they are not
              repeated. A line-vs-point coverage ratio (0.893) cannot measure
              per-pixel alpha at all. A luma estimator read 0.896 because 98.5%
              of its mask was overlapping bundles rather than isolated
              segments, and because it was "validated" on this renderer's own
              output at a time when that output was constant-alpha by
              construction - a circular check that could not detect the very
              mixture it was being applied to.

  order       points interleaved across the three classes and drawn last;
              lines composited per class, so a crossing between two different
              colours does have an order within any one render
"""
import math
import random
from PIL import Image, ImageDraw

DLAT = 0.217705                     # his constant, ~15 miles
SIZE = 6137                         # measured size of the originals
BG = (255, 255, 255)
BASEMAP = (170, 170, 0)             # #AAAA00
COLORS = {"local": (0, 0, 255), "tourist": (255, 0, 0), "unknown": (255, 255, 0)}
# "two pictures within a reasonably short time and distance of each other".
# The 2010 renderer was never published; his later code used the caps below.
# Some cap is necessary: without one, a photographer whose consecutive shots
# carry a city-centroid geotag and then a real one gets joined by a
# kilometres-long chord across the city.
LINE_ALPHA = 0.55                   # measured on the original; see module docstring
LINE_MAX_SECONDS = 600
# 15000 ft, the cap in his own later code; undocumented for the 2010 series.
# Kept because it is his own published number and inventing a tighter one would
# be inventing a parameter. It is not free: it admits 101 segments over 1 km,
# which read as a faint radial starburst around the centre. The longest thin
# straight line feature measurable in the London original is ~1.07 km, so this
# may well be looser than the 2010 renderer's. Disclosed in the README.
LINE_MAX_METRES = 4572.0
LINE_MAX_SPEED_MPS = 38.0           # 85 mph

# Fischer's own Hanoi city box (bounds/flickr-picasa cluster 124), which he
# computed but never published a map for.
HANOI_BOUNDS = (20.926386, 105.728233, 21.144091, 105.961466)   # s, w, n, e


class EquirectFrame:
    """Cylindrical projection with equal ground scale in x and y at centre."""

    def __init__(self, bounds, size=SIZE):
        self.s, self.w, self.n, self.e = bounds
        self.size = int(size)
        self.dlat = self.n - self.s
        self.dlon = self.e - self.w

    def to_px(self, lon, lat):
        x = (lon - self.w) / self.dlon * self.size
        y = (self.n - lat) / self.dlat * self.size
        return x, y

    def contains(self, lon, lat):
        return self.w <= lon <= self.e and self.s <= lat <= self.n

    def metres_per_px(self):
        return self.dlat * 111320.0 / self.size

    def __repr__(self):
        return (f"EquirectFrame({self.size}px, bounds S{self.s:.6f} W{self.w:.6f} "
                f"N{self.n:.6f} E{self.e:.6f}, {self.metres_per_px():.2f} m/px)")


def box_for(center_lat, center_lon, dlat=DLAT):
    """Build one of his 15-mile boxes around a point."""
    dlon = dlat / math.cos(math.radians(center_lat))
    return (center_lat - dlat / 2, center_lon - dlon / 2,
            center_lat + dlat / 2, center_lon + dlon / 2)


def draw_basemap(frame, nodes, ways, color=BASEMAP, include=None):
    """1 px uniform strokes for every feature. Returns an RGB image."""
    img = Image.new("RGB", (frame.size, frame.size), BG)
    d = ImageDraw.Draw(img)
    n_drawn = 0
    for w in ways:
        tags = w.get("tags") or {}
        if include is not None and not include(tags):
            continue
        if not ({"highway", "waterway", "natural", "landuse"} & tags.keys()):
            continue
        pts = []
        for r in (w.get("nodes") or []):
            nd = nodes.get(r)
            if nd is not None:
                pts.append(frame.to_px(nd[0], nd[1]))
        if len(pts) >= 2:
            d.line(pts, fill=color, width=1)
            n_drawn += 1
    return img, n_drawn


def _ground_metres(lon0, lat0, lon1, lat1):
    mlat = math.radians((lat0 + lat1) / 2)
    dx = (lon1 - lon0) * 111320.0 * math.cos(mlat)
    dy = (lat1 - lat0) * 111320.0
    return math.hypot(dx, dy)


# A connecting segment asserts that you know where BOTH photographs were
# taken. Flickr geo accuracy 16 is street level, 11 is city level; joining two
# city-level pins draws a line between two places nobody stood. Fischer worked
# from the full Flickr firehose where precise geotags dominate; the
# Creative-Commons slice carries a much larger share of coarse ones.
LINE_MIN_ACCURACY = 12


PIN_TOLERANCE_M = 5.0
# A place pin is a coordinate VALUE that repeats. Only repeated values are
# allowed to merge with each other; letting one-off GPS coordinates take part
# lets dense city-centre positions chain into huge blobs (15 m single-link over
# every coordinate finds 75 "pins" covering 6,041 photos, which is nonsense).
PIN_MIN_REPEAT = 5


def _coord_clusters(coords, tol_m=PIN_TOLERANCE_M):
    """Single-link cluster distinct coordinates at a few metres.

    Neither exact equality nor 4-decimal rounding is the right identity for a
    shared place pin. Exact equality splits one real pin apart: the Old Quarter
    pin appears as 105.85/105.849998/105.849997 crossed with
    21.033333/21.0333, five stored values within 4 m of each other, and
    treating them as five pins drops 91 photos out of detection. Rounding to
    4 dp is arbitrary and the pin count is 5x sensitive to the number of
    decimals. What actually identifies a pin is "the same position up to float
    representation", so we cluster at a metre-scale tolerance and use the same
    identity everywhere a pin is referred to.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    if not coords:
        return {}
    arr = np.array(coords, dtype=float)
    lat0 = float(np.median(arr[:, 1]))
    mx = arr[:, 0] * 111320.0 * math.cos(math.radians(lat0))
    my = arr[:, 1] * 111320.0
    xy = np.stack([mx, my], axis=1)

    parent = list(range(len(coords)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in cKDTree(xy).query_pairs(tol_m):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri
    return {coords[i]: find(i) for i in range(len(coords))}


def find_pin_clusters(photos, min_users=3, min_n=12, tol_m=PIN_TOLERANCE_M,
                      exclude_ids=()):
    """-> (coord_to_cluster, pin_cluster_ids, cluster_centroid)

    `exclude_ids` are photo ids to ignore when deciding what is a pin. The
    relocation pass deliberately places many photographers' photos on one
    landmark coordinate; counting those as "a shared pin, therefore not a
    measured position" would let the pipeline discredit its own output.
    """
    from collections import defaultdict

    exclude_ids = set(exclude_ids)
    considered = [p for p in photos if p.get("id") not in exclude_ids]
    raw = defaultdict(int)
    for p in considered:
        raw[(p["lon"], p["lat"])] += 1
    coords = sorted(c for c, k in raw.items() if k >= PIN_MIN_REPEAT)
    c2c = _coord_clusters(coords, tol_m)

    users = defaultdict(set)
    n = defaultdict(int)
    sums = defaultdict(lambda: [0.0, 0.0])
    for p in considered:
        cid = c2c.get((p["lon"], p["lat"]))
        if cid is None:
            continue
        users[cid].add(p["user"])
        n[cid] += 1
        sums[cid][0] += p["lon"]
        sums[cid][1] += p["lat"]
    pin_ids = {cid for cid in n if n[cid] >= min_n and len(users[cid]) >= min_users}
    centroid = {cid: (sums[cid][0] / n[cid], sums[cid][1] / n[cid]) for cid in n}
    return c2c, pin_ids, centroid


def find_pins(photos, min_users=3, min_n=12, tol_m=PIN_TOLERANCE_M,
              exclude_ids=()):
    """Set of coordinates belonging to a shared place pin."""
    c2c, pin_ids, _ = find_pin_clusters(photos, min_users, min_n, tol_m,
                                        exclude_ids)
    return {coord for coord, cid in c2c.items() if cid in pin_ids}


def _pin_key(p):
    return (p["lon"], p["lat"])


def build_marks(photos, labels, frame, line_max_seconds=LINE_MAX_SECONDS,
                line_max_metres=LINE_MAX_METRES,
                line_max_speed=LINE_MAX_SPEED_MPS,
                line_min_accuracy=LINE_MIN_ACCURACY,
                pins=None):
    """-> list of drawing ops: ("pt", x, y, colour) and ("ln", x0,y0,x1,y1, colour).

    photos: dicts with user, date (datetime), lon, lat.
    Lines join a photographer's consecutive shots taken within the time limit;
    both endpoints must be on canvas.
    """
    ops = []
    by_user = {}
    for p in photos:
        by_user.setdefault(p["user"], []).append(p)

    for user, ps in by_user.items():
        col = COLORS[labels[user]]
        # tie-break on id so the output does not depend on input row order
        ps.sort(key=lambda r: (r["date"], r.get("id", "")))
        for p in ps:
            if frame.contains(p["lon"], p["lat"]):
                x, y = frame.to_px(p["lon"], p["lat"])
                ops.append(("pt", x, y, col))
        for a, b in zip(ps, ps[1:]):
            dt = (b["date"] - a["date"]).total_seconds()
            if 0 <= dt <= line_max_seconds:
                dist = _ground_metres(a["lon"], a["lat"], b["lon"], b["lat"])
                if dist > line_max_metres:
                    continue
                # Two photos at the identical coordinate are not a segment.
                # 8,199 of 10,639 "lines" were exactly zero length, inflating
                # the count while drawing nothing the point had not drawn.
                if dist <= 0.0:
                    continue
                # dt == 0 must not bypass the speed cap: identical timestamps
                # with a real separation imply infinite speed, and 508 such
                # lines were being drawn, one of them 4.5 km long.
                if dist / max(dt, 1.0) > line_max_speed:
                    continue
                if min(a.get("acc", 16), b.get("acc", 16)) < line_min_accuracy:
                    continue
                if pins and (_pin_key(a) in pins or _pin_key(b) in pins):
                    continue
                if frame.contains(a["lon"], a["lat"]) and frame.contains(b["lon"], b["lat"]):
                    x0, y0 = frame.to_px(a["lon"], a["lat"])
                    x1, y1 = frame.to_px(b["lon"], b["lat"])
                    ops.append(("ln", x0, y0, x1, y1, col))
    return ops


def draw_marks(img, ops, seed=20100615, line_alpha=LINE_ALPHA):
    """Lines first at `line_alpha`, then opaque 3x3 points on top.

    Coincident same-colour segments accumulate as 1 - (1 - alpha)^n, which is
    what the original does. Classes are composited in a shuffled order, so
    cross-class line overlaps do have a draw order within any one render (the
    affected area is 3,636 px). Points are drawn last, opaque, with the three
    classes interleaved (his `intersperse2` step), so individual photographs
    are never buried under the trails.
    """
    import numpy as np

    rng = random.Random(seed)
    base = np.asarray(img).astype(np.float32)

    lines = [o for o in ops if o[0] == "ln"]
    by_class = {}
    for _t, x0, y0, x1, y1, col in lines:
        by_class.setdefault(col, []).append((x0, y0, x1, y1))
    order = sorted(by_class)
    rng.shuffle(order)
    S = img.size[0]
    for col in order:
        # Per-pixel overlap COUNT, not a flat mask: the original accumulates
        # coincident segments, and its line cores step 114 -> 59 -> 50 with
        # overlap depth, matching 1 - (1 - a)^n.
        counts = np.zeros((S, S), dtype=np.int32)
        for x0, y0, x1, y1 in sorted(by_class[col]):
            # Unit-step DDA: n+1 samples where n = round(max(|dx|,|dy|)), so
            # the dominant axis advances by exactly 1 per step. This is what
            # ImageDraw's Bresenham did, and both alternatives are wrong:
            # taking int(max)+1 samples makes the step land in [1, 2) and
            # leaves holes (471 of 2,560 segments had one), while oversampling
            # 2x emits two pixels in the same dominant-axis column and turns
            # the trail into a 4-connected staircase carrying ~22% more ink
            # than a 1 px line. The original's isolated diagonals satisfy
            # area == span exactly, i.e. 8-connected, so unit-step it is.
            # Step count from the ROUNDED endpoints, not the float span:
            # deriving it from the span can come out one short (leaving a hole)
            # or one long (painting a pixel twice, which then renders at the
            # n=2 accumulation rung from a single segment).
            n = max(abs(int(round(x1)) - int(round(x0))),
                    abs(int(round(y1)) - int(round(y0))))
            if n == 0:
                xs = np.array([int(round(x0))], dtype=np.int64)
                ys = np.array([int(round(y0))], dtype=np.int64)
            else:
                t = np.arange(n + 1, dtype=float) / n
                xs = np.round(x0 + (x1 - x0) * t).astype(np.int64)
                ys = np.round(y0 + (y1 - y0) * t).astype(np.int64)
            ok = (xs >= 0) & (xs < S) & (ys >= 0) & (ys < S)
            if ok.any():
                np.add.at(counts, (ys[ok], xs[ok]), 1)
        hit = counts > 0
        if not hit.any():
            continue
        a_eff = 1.0 - np.power(1.0 - line_alpha, counts[hit].astype(np.float32))
        c = np.array(col, dtype=np.float32)
        base[hit] = (base[hit] * (1.0 - a_eff)[:, None]
                     + c[None, :] * a_eff[:, None])

    out = Image.fromarray(np.clip(np.round(base), 0, 255).astype(np.uint8), "RGB")
    # Sort canonically before shuffling: a fixed seed permuting a list whose
    # order came from input row order still yields a varying raster.
    pts = sorted((o for o in ops if o[0] == "pt"),
                 key=lambda o: (round(o[1], 3), round(o[2], 3), o[3]))
    rng.shuffle(pts)
    d = ImageDraw.Draw(out)
    for _t, x, y, col in pts:
        xi, yi = int(round(x)), int(round(y))
        d.rectangle([xi - 1, yi - 1, xi + 1, yi + 1], fill=col)
    return out
