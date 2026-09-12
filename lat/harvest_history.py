"""Fill in the per-photographer WORLDWIDE geotagged history the merge needs.

The locals/tourists colours are a residency test, so every photographer who
appears in Hanoi needs their geotagged photos *everywhere*, with dates. The
Hanoi harvests landed without it: Commons had no history file at all and
iNaturalist covered 166 of 2,106 observers, which would have left almost every
non-Flickr photographer classified "unknown" purely for lack of evidence.

Deliberately sequential and single-process. Running five harvesters and a
26 GB SQL join concurrently exhausted this machine's 6.7 GB earlier; one user
is held in memory at a time and each is appended and flushed as it completes,
so the run is resumable and its footprint is flat.

    PYTHONPATH=. .venv/bin/python lat/harvest_history.py commons [--limit N]
    PYTHONPATH=. .venv/bin/python lat/harvest_history.py inat    [--limit N]
"""
import argparse
import csv
import os
import sys
import time
from collections import Counter

import requests

MULTI = "data/multi"
UA = "hanoi-locals-tourists/1.0 (contact: baochidangg@gmail.com)"
PER_USER_CAP = 2000


def _read_col(path, col_names, want="user"):
    """Column-name tolerant single-column reader."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        r = csv.reader(f, delimiter="\t")
        hdr = next(r, None) or []
        idx = None
        for name in col_names:
            if name in hdr:
                idx = hdr.index(name)
                break
        if idx is None:
            return []
        return [row[idx] for row in r if len(row) > idx and row[idx].strip()]


def done_users(path):
    return set(_read_col(path, ["user"]))


def open_appender(path):
    """Append, writing a header only if the file is new."""
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", newline="")
    w = csv.writer(f, delimiter="\t", lineterminator="\n")
    if new:
        w.writerow(["user", "date", "lon", "lat"])
        f.flush()
    return f, w


# ----------------------------------------------------------------- Commons
def commons_history(user, session, cap=PER_USER_CAP, pause=1.0,
                    max_pages=20):
    """Every geotagged upload by `user`, as (date, lon, lat).

    `generator=allimages&gaiuser=` lists the user's own uploads, and
    `prop=coordinates` only returns a coordinates array for the geotagged
    ones, so the filter is free. DateTimeOriginal is preferred over the upload
    timestamp because the Hanoi side of this source is 97% capture dates and a
    30-day span rule behaves differently on upload times.
    """
    out, cont, kinds, pages = [], None, Counter(), 0
    while True:
        # Bound the work per photographer. `allimages` pages through ALL of a
        # user's uploads, not just the geotagged ones, so a prolific uploader
        # with sparse geotags can otherwise paginate for minutes on their own.
        if pages >= max_pages:
            return out, kinds, "page_limit"
        pages += 1
        p = {"action": "query", "format": "json", "formatversion": "2",
             "generator": "allimages", "gaiuser": user, "gaisort": "timestamp",
             "gailimit": "500", "prop": "coordinates|imageinfo",
             "colimit": "max", "iiprop": "timestamp|extmetadata",
             "iiextmetadatafilter": "DateTimeOriginal", "maxlag": "5"}
        if cont:
            p.update(cont)
        try:
            r = session.get("https://commons.wikimedia.org/w/api.php",
                            params=p, timeout=60)
            if r.status_code != 200:
                time.sleep(5)
                return out, kinds, f"http {r.status_code}"
            d = r.json()
        except Exception as ex:
            time.sleep(5)
            return out, kinds, f"{type(ex).__name__}"
        if "error" in d:
            return out, kinds, str(d["error"].get("code"))
        for page in d.get("query", {}).get("pages", []) or []:
            co = page.get("coordinates")
            if not co:
                continue
            ii = (page.get("imageinfo") or [{}])[0]
            taken = ((ii.get("extmetadata") or {})
                     .get("DateTimeOriginal", {}) or {}).get("value")
            date = taken or ii.get("timestamp")
            kinds["taken" if taken else "uploaded"] += 1
            if date:
                out.append((str(date), co[0]["lon"], co[0]["lat"]))
        if len(out) >= cap:
            return out[:cap], kinds, "capped"
        cont = d.get("continue")
        if not cont:
            return out, kinds, None
        time.sleep(pause)


# -------------------------------------------------------------- iNaturalist
def inat_history(user, session, cap=PER_USER_CAP, pause=1.05, max_pages=2):
    """Geotagged observations at the temporal EXTREMES of `user`'s record.

    Paging a prolific observer's whole history costs five or more requests
    each, and at the 60 requests/minute iNaturalist asks for, 1,900 observers
    would take five hours. Two requests suffice for what the residency test
    needs: the earliest 200 observations and the latest 200. A span test asks
    how far apart a photographer's dates are inside one 15-mile box, so the
    ends of their record are the informative part. The first 400 rows in id
    order are merely their oldest and say nothing about whether they are still
    there.

    Obscured and private coordinates are refused: iNaturalist randomises those
    inside roughly 0.2 degrees, about 22 km, which is noise at any scale this
    project renders, and letting them into a residency test would invent
    presence in a city the observer may never have visited.
    """
    out, seen, note = [], set(), None
    orders = ("asc", "desc")[:max(1, min(2, max_pages))]
    for order in orders:
        params = {"user_login": user, "per_page": "200",
                  "order_by": "observed_on", "order": order, "geo": "true"}
        try:
            r = session.get("https://api.inaturalist.org/v1/observations",
                            params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(30)
                r = session.get("https://api.inaturalist.org/v1/observations",
                                params=params, timeout=60)
            if r.status_code != 200:
                return out, Counter(), f"http {r.status_code}"
            res = r.json().get("results", []) or []
        except Exception as ex:
            return out, Counter(), type(ex).__name__
        if len(res) >= 200:
            note = "truncated"
        for o in res:
            oid = o.get("id")
            if oid in seen:
                continue
            seen.add(oid)
            if (o.get("geoprivacy") in ("obscured", "private")
                    or o.get("taxon_geoprivacy") in ("obscured", "private")
                    or o.get("obscured")):
                continue
            lat, lon = o.get("latitude"), o.get("longitude")
            if lat is None or lon is None:
                g = (o.get("geojson") or {}).get("coordinates")
                if not g:
                    continue
                lon, lat = g[0], g[1]
            date = (o.get("observed_on") or o.get("time_observed_at")
                    or o.get("created_at"))
            if date:
                out.append((str(date), lon, lat))
        time.sleep(pause)
    return out[:cap], Counter(), note


SPECS = {
    "commons": dict(hanoi="commons_hanoi.tsv", out="commons_history.tsv",
                    cols=["user"], fn=commons_history, pause=0.15,
                    max_pages=6),
    "inat": dict(hanoi="inat_hanoi.tsv", out="inat_history.tsv",
                 cols=["user"], fn=inat_history, pause=1.05,
                 max_pages=2),
}


def main(source, limit=None, max_pages=None, workers=1, pause=None):
    spec = SPECS[source]
    hanoi = os.path.join(MULTI, spec["hanoi"])
    outp = os.path.join(MULTI, spec["out"])
    counts = Counter(_read_col(hanoi, spec["cols"]))
    already = done_users(outp)
    # Busiest photographers first: they carry the most points, so a run cut
    # short still covers the users who matter most to the picture.
    todo = [u for u, _ in counts.most_common() if u not in already]
    if limit:
        todo = todo[:limit]
    print(f"{source}: {len(counts)} photographers in Hanoi, "
          f"{len(already)} already have history, {len(todo)} to fetch",
          flush=True)

    mp = max_pages if max_pages else spec.get("max_pages", 20)
    pz = pause if pause else spec["pause"]
    session = requests.Session()
    session.headers["User-Agent"] = UA
    f, w = open_appender(outp)
    tot, kinds, fails = 0, Counter(), Counter()

    def fetch(u):
        # One session per worker: requests.Session is not documented
        # thread-safe, and sharing one produced no speedup worth the risk.
        s = requests.Session()
        s.headers["User-Agent"] = UA
        return (u,) + spec["fn"](u, s, pause=pz, max_pages=mp)

    try:
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            it = ThreadPoolExecutor(workers).map(fetch, todo)
        else:
            it = (fetch(u) for u in todo)
        for i, (u, rows, k, note) in enumerate(it, 1):
            kinds.update(k)
            if note:
                fails[note] += 1
            for date, lon, lat in rows:
                w.writerow([u, date, lon, lat])
            f.flush()
            tot += len(rows)
            if i % 25 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] {u[:28]:28s} +{len(rows):5d} rows "
                      f"total {tot:,}" + (f"  notes={dict(fails)}" if fails else ""),
                      flush=True)
    finally:
        f.close()
    print(f"DONE {source}: {tot:,} history rows for {len(todo)} photographers")
    if kinds:
        print(f"  date kinds: {dict(kinds)}")
    if fails:
        print(f"  notes: {dict(fails)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=sorted(SPECS))
    ap.add_argument("--limit", type=int, default=None)
    # Truncating a photographer's history can only ever LOSE evidence of
    # residency elsewhere, so it biases toward "unknown" - the same direction
    # as having no history at all, never toward a false "tourist".
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap requests per photographer (default per source)")
    ap.add_argument("--pause", type=float, default=None,
                    help="per-request sleep inside a worker; with N workers "
                         "the aggregate rate is roughly N/pause per second, "
                         "so raise it when raising --workers")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent photographers; keep at 1 for providers "
                         "that ask for a request rate (iNaturalist)")
    a = ap.parse_args()
    main(a.source, a.limit, a.max_pages, a.workers, a.pause)
