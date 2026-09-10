"""Eric Fischer's locals/tourists classification.

From the author's own description of the series:

  blue   locals   - people who have taken pictures in this city dated over a
                    range of a month or more
  red    tourists - people who seem to be a local of a different city and who
                    took pictures in this city for less than a month
  yellow unknown  - people who haven't taken pictures anywhere for over a month

So the decision needs each photographer's *global* history, not just their
photos inside the target city: "local of a different city" is only answerable
by looking at where else they shoot and over what span.
"""
from collections import defaultdict
from datetime import datetime, timedelta
import math

_EPOCH = datetime(1970, 1, 1)

LOCAL = "local"
TOURIST = "tourist"
UNKNOWN = "unknown"

# One month, as a day count. Fischer says "a month or more"; 30 days is the
# reading that does not depend on which calendar month a trip lands in.
MONTH_DAYS = 30

# Fischer judged residency inside a fixed global set of ~3015 "city" boxes,
# each 15 miles on a side (dlat = 0.217705 deg, dlon scaled by cos(lat) so the
# ground extent is square). We reproduce that shape rather than clustering,
# because a cluster is not a city: single-link clustering at any workable link
# distance chains along dense corridors, and a chained blob 300 km across
# cannot support the claim "a local of a *different city*".
CITY_BOX_DLAT = 0.217705

# A candidate home box only counts as "a different city" if it does not overlap
# the target city's box at all.



def _haversine_km(lon1, lat1, lon2, lat2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(max(0.0, a)))


def _span_days(dates):
    if not dates:
        return 0.0
    return (max(dates) - min(dates)) / timedelta(days=1)


def _best_foreign_box_span(pts, city_bounds, dlat=CITY_BOX_DLAT,
                           threshold_days=None, max_abs_lat=85.0):
    """Longest date span this photographer has inside any 15-mile box that does
    not overlap the target city's box.

    pts: [(lon, lat, date), ...] -> (best_span_days, centre or None)

    The search enumerates every box position that can matter. For a fixed-size
    box the unconstrained optimum can always be slid until its bottom edge
    touches some point's latitude and its left edge touches some point's
    longitude, so those two edge alignments suffice - but only unconstrained.
    The disjointness requirement breaks that argument: sliding a box until its
    edges touch points can push it INTO the target city's box, so the
    constrained optimum may instead sit flush against that box with its edges
    touching no point at all. The four flush positions are therefore added to
    the candidate sets explicitly.

    Two earlier versions were wrong here. Using the photographer's own photo
    positions as box *centres* understated 8 photographers into "unknown"; then
    point-edge alignment alone still missed one, whose only qualifying box lies
    flush against Hanoi's western edge.

    `threshold_days`, when given, allows an early return as soon as any
    qualifying box is found; the caller only needs the yes/no.
    """
    import numpy as np

    if not pts:
        return 0.0, None
    s_, w_, n_, e_ = city_bounds

    lon = np.array([p[0] for p in pts], dtype=float)
    lat = np.array([p[1] for p in pts], dtype=float)
    # Naive datetimes must not be resolved through the machine's local zone, or
    # the published counts become machine-dependent (one boundary photographer
    # flips under a half-hour-DST zone).
    t = np.array([(p[2] - _EPOCH).total_seconds() for p in pts], dtype=float)

    # A box measured in degrees of longitude is meaningless near the poles.
    usable = np.abs(lat) <= max_abs_lat
    if not usable.any():
        return 0.0, None
    lon, lat, t = lon[usable], lat[usable], t[usable]

    hlat = dlat / 2.0
    best, best_c = 0.0, None
    # bottom edges: each point's latitude, plus flush below/above the city box
    lat_edges = np.unique(np.concatenate([
        np.round(lat, 5),
        np.array([s_ - dlat - 1e-9, n_ + 1e-9]),
    ]))
    lon_base = np.unique(np.round(lon, 5))

    for bs in lat_edges:                     # bottom edge on a point's latitude
        bn = bs + dlat
        band = (lat >= bs - 1e-12) & (lat <= bn + 1e-12)
        if not band.any():
            continue
        clat = bs + hlat
        coslat = max(1e-6, math.cos(math.radians(clat)))
        hlon = (dlat / coslat) / 2.0
        if hlon >= 180.0:                    # box wraps the whole globe
            continue
        blon, bt = lon[band], t[band]
        # left edges: each point's longitude, plus flush west/east of the city
        lon_edges = np.unique(np.concatenate([
            lon_base,
            np.array([w_ - 2.0 * hlon - 1e-9, e_ + 1e-9]),
        ]))
        for bw in lon_edges:
            clon = bw + hlon
            # unwrap longitudes relative to the candidate centre so a box
            # straddling +-180 still selects the right points
            rel = ((blon - clon + 180.0) % 360.0) - 180.0
            m = np.abs(rel) <= hlon + 1e-12
            if not m.any():
                continue
            be = bw + 2.0 * hlon
            # reject boxes overlapping the target city's box (also unwrapped)
            dlon_c = abs(((clon - (w_ + e_) / 2.0 + 180.0) % 360.0) - 180.0)
            lat_disjoint = (bn < s_) or (bs > n_)
            lon_disjoint = dlon_c > (hlon + (e_ - w_) / 2.0)
            if not (lat_disjoint or lon_disjoint):
                continue
            span = (bt[m].max() - bt[m].min()) / 86400.0
            if span > best:
                cl = ((clon + 180.0) % 360.0) - 180.0
                best, best_c = span, (float(cl), float(clat))
                if threshold_days is not None and best >= threshold_days:
                    return best, best_c
    return best, best_c


def classify_users(records, in_city, month_days=MONTH_DAYS,
                   city_bounds=None, dlat=CITY_BOX_DLAT):
    """records: iterable of dicts with keys user, lon, lat, date (datetime).

    `in_city(lon, lat) -> bool` decides membership of the target city.
    `city_bounds` is that city's (s, w, n, e) box, needed to tell whether a
    candidate home box elsewhere is genuinely a *different* city.

    Returns {user: (label, reason_dict)}.
    """
    by_user = defaultdict(list)
    for r in records:
        by_user[r["user"]].append((r["lon"], r["lat"], r["date"]))

    labels = {}
    for user, pts in by_user.items():
        city_dates = [d for lon, lat, d in pts if in_city(lon, lat)]
        city_span = _span_days(city_dates)

        info = {"n_total": len(pts), "n_city": len(city_dates),
                "city_span_days": round(city_span, 1)}

        if city_span >= month_days:
            labels[user] = (LOCAL, info)
            continue

        # Not a local here. Are they a local of a different city?
        if city_bounds is None:
            raise ValueError("city_bounds is required to identify a home city "
                             "elsewhere")
        best_other, centre = _best_foreign_box_span(
            pts, city_bounds, dlat, threshold_days=month_days)
        info["best_other_span_days"] = round(best_other, 1)
        info["home_box_centre"] = centre
        if best_other >= month_days:
            labels[user] = (TOURIST, info)
        else:
            labels[user] = (UNKNOWN, info)

    return labels


def label_points(records, in_city, **kw):
    """-> list of (lon, lat, label) for records inside the target city only."""
    labels = classify_users(records, in_city, **kw)
    return [(r["lon"], r["lat"], labels[r["user"]][0])
            for r in records if in_city(r["lon"], r["lat"])]
