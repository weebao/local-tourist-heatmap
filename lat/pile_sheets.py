"""Group pile photos BY pile and build per-pile contact sheets.

Adjudication first: a coordinate shared by many photographers may be a genuine
landmark (Hoan Kiem Lake, the Temple of Literature - Flickr's place picker
legitimately snaps everyone to one pin) or a dumping ground (the Hanoi city
centroid). Only the second kind should be redistributed, so a vision pass needs
to see each pile as a group rather than shuffled together.
"""
import json, os
from collections import defaultdict
from PIL import Image, ImageDraw
from lat.relocate_fetch import load, pick_piles, fetch_all, OUT

MAX_PER_SHEET = 15


def main(top_n=14, sample=30, skip=0):
    rows = load()
    piles = pick_piles(rows)
    g = defaultdict(list)
    for r in piles:
        g[r["pile"]].append(r)
    order = sorted(g.items(), key=lambda kv: -len(kv[1]))[skip:skip + top_n]
    print(f"{len(g)} piles total; adjudicating the top {len(order)}")

    os.makedirs(f"{OUT}/piles", exist_ok=True)
    manifest = {}
    for pile, rs in order:
        rs_sorted = sorted(rs, key=lambda r: r["id"])
        step = max(1, len(rs_sorted) // sample)
        samp = rs_sorted[::step][:sample]
        ok = fetch_all(samp)
        if not ok:
            continue
        cols, cell = 5, 300
        rowsn = (len(ok) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell, rowsn * (cell + 24)), "white")
        d = ImageDraw.Draw(sheet)
        for i, (r, p) in enumerate(ok):
            cx, cy = (i % cols) * cell, (i // cols) * (cell + 24)
            try:
                im = Image.open(p).convert("RGB")
            except Exception:
                continue
            im.thumbnail((cell - 8, cell - 8))
            sheet.paste(im, (cx + 4, cy + 20))
            d.text((cx + 5, cy + 5), f"{i+1}. id={r['id']}", fill="black")
        fn = f"{OUT}/piles/pile_{pile.replace(',', '_')}.png"
        sheet.save(fn)
        manifest[pile] = {
            "sheet": fn, "n_total": len(rs),
            "n_users": len({r["user"] for r in rs}),
            "accs": sorted({r["acc"] for r in rs}),
            "sampled_ids": [r["id"] for r, _ in ok],
        }
        print(f"  {pile}  n={len(rs):4d}  users={manifest[pile]['n_users']:3d}  "
              f"acc={manifest[pile]['accs']}  -> {os.path.basename(fn)}")
    mpath = f"{OUT}/pile_manifest.json"
    if skip and os.path.exists(mpath):
        prev = json.load(open(mpath)); prev.update(manifest); manifest = prev
    json.dump(manifest, open(mpath, "w"), indent=1)
    return manifest


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=14)
    ap.add_argument("--skip", type=int, default=0)
    a = ap.parse_args()
    main(top_n=a.top, skip=a.skip)
