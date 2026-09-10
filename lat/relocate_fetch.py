"""Select photos whose geotag is a shared 'pile' coordinate, fetch their
images, and lay them out as labelled contact sheets for visual geolocation.

Why: 18.4% of the Hanoi rows carry city-level-or-worse geo accuracy, and several
hundred of them sit on *identical* coordinates shared by many photographers -
Flickr place-centroid pins. Rendered as-is they become bright fake hotspots
that no photograph was actually taken at. For those, the pixels are better
evidence than the metadata.
"""
import csv, io, os, sys, json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import requests
from PIL import Image, ImageDraw

TSV = "data/yfcc/hanoi_wide.tsv"
OUT = "data/reloc"
COLS = ["id","user","nick","date","lon","lat","acc","page","dl","server","farm","secret","ext"]


def load(tsv=TSV):
    rows = []
    with open(tsv, newline="") as f:
        for r in csv.reader(f, delimiter="\t"):
            if len(r) < 13:
                continue
            d = dict(zip(COLS, r))
            try:
                d["lon"] = float(d["lon"]); d["lat"] = float(d["lat"])
                d["acc"] = int(d["acc"] or 0)
            except ValueError:
                continue
            rows.append(d)
    return rows


def pick_piles(rows, min_users=3, min_n=12):
    """Coordinates shared by several distinct photographers = a place pin,
    not a place. Single-user piles are left alone: one person geotagging their
    own photos at one point is at least self-consistent."""
    g = defaultdict(list)
    for r in rows:
        g[(round(r["lon"], 4), round(r["lat"], 4))].append(r)
    out = []
    for key, rs in g.items():
        if len(rs) >= min_n and len({r["user"] for r in rs}) >= min_users:
            for r in rs:
                r["pile"] = f"{key[0]:.4f},{key[1]:.4f}"
            out += rs
    return out


def url_for(r, size="z"):
    return f"https://live.staticflickr.com/{r['server']}/{r['id']}_{r['secret']}_{size}.jpg"


def fetch_one(r):
    path = os.path.join(OUT, "img", f"{r['id']}.jpg")
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        return path
    for size in ("z", "c", ""):
        try:
            resp = requests.get(url_for(r, size), timeout=25)
            if resp.status_code == 200 and len(resp.content) > 2000:
                with open(path, "wb") as f:
                    f.write(resp.content)
                return path
        except Exception:
            pass
    return None


def fetch_all(rows, workers=16):
    os.makedirs(os.path.join(OUT, "img"), exist_ok=True)
    with ThreadPoolExecutor(workers) as ex:
        paths = list(ex.map(fetch_one, rows))
    ok = [(r, p) for r, p in zip(rows, paths) if p]
    return ok


def sheets(ok, per=12, cols=4, cell=330, tag="sheet"):
    """Contact sheets, each tile captioned with the photo id so a vision pass
    can report per-photo answers unambiguously."""
    os.makedirs(os.path.join(OUT, "sheets"), exist_ok=True)
    made = []
    for si in range(0, len(ok), per):
        chunk = ok[si:si + per]
        rowsn = (len(chunk) + cols - 1) // cols
        W, H = cols * cell, rowsn * (cell + 26)
        sheet = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(sheet)
        for i, (r, p) in enumerate(chunk):
            cx, cy = (i % cols) * cell, (i // cols) * (cell + 26)
            try:
                im = Image.open(p).convert("RGB")
            except Exception:
                continue
            im.thumbnail((cell - 8, cell - 8))
            sheet.paste(im, (cx + 4, cy + 22))
            d.text((cx + 5, cy + 6), f"{i+1}. id={r['id']}", fill="black")
        name = os.path.join(OUT, "sheets", f"{tag}_{si//per:03d}.png")
        sheet.save(name)
        made.append((name, [r["id"] for r, _ in chunk]))
    return made


if __name__ == "__main__":
    rows = load()
    piles = pick_piles(rows)
    print(f"total rows            {len(rows)}")
    print(f"pile rows to relocate {len(piles)}")
    byp = defaultdict(int)
    for r in piles: byp[r["pile"]] += 1
    for k, v in sorted(byp.items(), key=lambda x: -x[1]):
        print(f"   {k}  n={v}")
    ok = fetch_all(piles)
    print(f"images fetched        {len(ok)} / {len(piles)}")
    made = sheets(ok)
    print(f"contact sheets        {len(made)}")
    json.dump({n: ids for n, ids in made}, open(os.path.join(OUT, "sheets.json"), "w"), indent=1)
    json.dump([{k: r[k] for k in ("id","user","lon","lat","acc","pile","page")} for r, _ in ok],
              open(os.path.join(OUT, "pile_rows.json"), "w"), indent=1)
