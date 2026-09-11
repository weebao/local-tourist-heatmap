"""Render the multi-source Hanoi map with the existing renderer.

Everything about the picture - projection, bounds, colours, 3x3 opaque points,
the 0.55 line alpha, the accuracy >= 12 line gate - comes from lat.fischer
unchanged. This file only decides which rows reach it, and writes into
out/multi/ so the single-source deliverables in out/ are never touched.

Three things it produces that the single-source build does not need:
  --per-source renders, one map per source plus the combined one, because a
      merged map with no way to see which source drew what is not checkable;
  a cap sweep, printed and written to stats.json, because the right cap is a
      trade-off between "one uploader's album" and "throwing away real data"
      and this build refuses to pick one silently;
  per-source and per-class counts in out/multi/stats.json.
"""
import json
import os
import sys
from collections import Counter, defaultdict

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lat.classify import LOCAL, TOURIST, UNKNOWN
from lat.basemap import load_overpass
from lat.fischer import (EquirectFrame, HANOI_BOUNDS, SIZE, COLORS,
                         draw_basemap, build_marks, draw_marks, find_pins,
                         _pin_key)
from lat import multi

OUT_DIR = "out/multi"
CAP_SWEEP = (None, 2000, 1000, 500, 200, 100, 50)


def _basemap(frame, exclude_paths=True, osm="data/hanoi_osm.json.gz"):
    """The same basemap the single-source build draws, or bare white if the
    Overpass extract is not present (it is gitignored, ~18 MB)."""
    if not os.path.exists(osm):
        print(f"  {osm} absent: rendering on bare white")
        return Image.new("RGB", (frame.size, frame.size), (255, 255, 255)), 0
    nodes, ways = load_overpass(osm)
    include = None
    if exclude_paths:
        skip = {"footway", "path", "steps", "cycleway", "bridleway", "corridor"}
        include = lambda t: t.get("highway") not in skip
    return draw_basemap(frame, nodes, ways, include=include)


def _render(base_img, rows, labels, frame, pins):
    ops = build_marks(rows, labels, frame, pins=pins)
    npt = sum(1 for o in ops if o[0] == "pt")
    nln = sum(1 for o in ops if o[0] == "ln")
    return draw_marks(base_img.copy(), ops), ops, npt, nln


def main(size=2000, directory=multi.MULTI_DIR, cap=None, out_dir=OUT_DIR,
         per_source=False, downscales=(), exclude_paths=True,
         date_kinds=multi.RESIDENCY_DATE_KINDS, include_flickr=True,
         sources=None, window=None, all_sources=False, tag=""):
    os.makedirs(out_dir, exist_ok=True)
    frame = EquirectFrame(HANOI_BOUNDS, size)

    print(f"merging sources from {directory}/")
    m = multi.merge(directory, cap=cap, include_flickr=include_flickr,
                    sources=sources, date_kinds=date_kinds, window=window,
                    all_sources=all_sources)
    rows, labels = m["rows"], m["labels"]
    ci = m["classify"]
    print(f"  photographers: local {ci['counts'].get(LOCAL, 0)}  "
          f"tourist {ci['counts'].get(TOURIST, 0)}  "
          f"unknown {ci['counts'].get(UNKNOWN, 0)}  "
          f"({ci['users_untested_no_eligible_date']} untestable: no "
          f"{'/'.join(ci['residency_date_kinds']) if isinstance(ci['residency_date_kinds'], list) else 'eligible'} date)")
    if not rows:
        print("no rows to draw: no source produced any usable row")

    # The harvesters' own rejects, re-run through our filter. Free adversarial
    # test: a row somebody else judged unmappable must be unmappable here too.
    excl = multi.verify_excluded(directory)
    for src, v in excl.items():
        verdict = "agrees" if v["agrees"] else f"DISAGREES on {v['would_still_map']}"
        print(f"  {src}_hanoi_excluded.tsv: {v['rows_readable']:,} rows, "
              f"our filter {verdict}  their reasons {v['their_reasons']}")

    print("cap sweep (points landing on the canvas)")
    sweep = multi.cap_sweep(m["map_rows_uncapped"], CAP_SWEEP, frame=frame)
    for sw in sweep:
        print(f"  cap {str(sw['cap'] or 'none'):>4s}: {sw['points']:>7,} points  "
              f"{sw['photographers']:>4} photographers  "
              f"top-3 {sw['top3_share']*100:5.1f}%  "
              f"top-5 {sw['top5_share']*100:5.1f}%  "
              f"{sw['by_source']}")

    print(f"  {frame}")
    print("drawing basemap")
    base_img, nw = _basemap(frame, exclude_paths)
    print(f"  {nw:,} ways stroked")

    # Shared place pins are a Flickr place-picker artefact, but the same shape
    # of artefact exists on every source (a Commons uploader geotagging a batch
    # from a map click), so the detector runs over the merged set.
    pins = find_pins(rows)
    n_pin = sum(1 for r in rows if _pin_key(r) in pins)
    print(f"  {len(pins)} shared place-pin coordinates ({n_pin} photos)")

    print("rendering combined")
    img, ops, npt, nln = _render(base_img, rows, labels, frame, pins)
    cp = Counter()
    inv = {v: k for k, v in COLORS.items()}
    for o in ops:
        if o[0] == "pt":
            cp[inv[o[3]]] += 1
    print(f"  {npt:,} points + {nln:,} connecting lines")
    print("  points by class: " + "  ".join(f"{k} {v:,}" for k, v in sorted(cp.items())))
    combined = os.path.join(out_dir, f"hanoi_multi{tag}_{size}.png")
    img.save(combined)
    print(f"wrote {combined}")
    for d in downscales:
        img.resize((d, d), Image.BOX).save(
            os.path.join(out_dir, f"hanoi_multi{tag}_{d}.png"))
        print(f"wrote {out_dir}/hanoi_multi{tag}_{d}.png")

    per_src_render = {}
    if per_source:
        for src in sorted({r["src"] for r in rows}):
            srows = [r for r in rows if r["src"] == src]
            simg, _o, sn, sl = _render(base_img, srows, labels, frame, pins)
            path = os.path.join(out_dir, f"hanoi_multi{tag}_{src}_{size}.png")
            simg.save(path)
            per_src_render[src] = {"png": os.path.basename(path),
                                   "points": sn, "lines": sl}
            print(f"wrote {path}  ({sn:,} points, {sl:,} lines)")

    summ = multi.source_summary(rows, labels)
    on_map = defaultdict(set)
    for r in rows:
        if frame.contains(r["lon"], r["lat"]):
            on_map[labels.get(r["user"], UNKNOWN)].add(r["user"])

    stats = {
        "generated": __doc__.splitlines()[0],
        "frame": repr(frame),
        "bounds_source": "Fischer bounds/flickr-picasa cluster 124 (as out/stats.json)",
        "sources_loaded": [
            {"source": r["source"], "present": bool(r.get("exists")),
             "rows": r.get("kept", 0), "history_rows": r.get("history_rows", 0),
             "date_kind": r.get("date_kind"),
             "columns": r.get("columns"), "error": r.get("error")}
            for r in m["load"]["sources"]],
        "source_docs": m["load"]["report_docs"],
        "rows_loaded": m["rows_loaded"],
        "window": m["window"],
        "window_dropped": m["window_dropped"],
        "sources_present_but_excluded": {
            r["source"]: r.get("excluded_because")
            for r in m["load"]["sources"] if r.get("skipped")},
        "harvester_exclusions_recheck": excl,
        "obscured_dropped": m["obscured_dropped"],
        "precision_dropped": m["precision_dropped"],
        "precision_drop_metres": multi.PRECISION_DROP_M,
        "precision_coarse_metres": multi.PRECISION_COARSE_M,
        "dedup_removed_by_pair": m["dedup_pairs"],
        "dedup_by_key": m["dedup_by_key"],
        "residency_date_kinds": ci["residency_date_kinds"],
        "photographers": ci["counts"],
        "photographers_untestable": ci["users_untested_no_eligible_date"],
        "photographers_on_map": {k: len(v) for k, v in on_map.items()},
        "cap": cap,
        "cap_removed": m["cap_removed"],
        "cap_sweep": sweep,
        "points_total": npt,
        "points_by_class": dict(cp),
        "connecting_lines": nln,
        "per_source": summ,
        "per_source_renders": per_src_render,
        "shared_pins": {"coords": len(pins), "photos": n_pin},
    }
    stats_name = f"stats{tag}.json"
    with open(os.path.join(out_dir, stats_name), "w") as f:
        json.dump(stats, f, indent=1, default=str)
    print(f"wrote {out_dir}/{stats_name}")
    return img, stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=2000,
                    help=f"canvas px; the published single-source map is {SIZE}")
    ap.add_argument("--dir", default=multi.MULTI_DIR)
    ap.add_argument("--cap", type=int, default=None,
                    help="max photos per photographer on the map; "
                         "no default, see the sweep this prints")
    ap.add_argument("--per-source", action="store_true",
                    help="also render one map per source")
    ap.add_argument("--downscale", type=int, nargs="*", default=[])
    ap.add_argument("--include-paths", action="store_true")
    ap.add_argument("--sources", nargs="*", default=None,
                    help="restrict to these source keys")
    ap.add_argument("--no-flickr", action="store_true",
                    help="leave YFCC out, to see the new sources alone")
    ap.add_argument("--residency-dates", nargs="*",
                    default=list(multi.RESIDENCY_DATE_KINDS),
                    choices=[multi.TAKEN, multi.OBSERVED, multi.UPLOAD,
                             multi.UNSTATED],
                    help="date kinds allowed into the residency span test; "
                         "default excludes upload dates")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--all-sources", action="store_true",
                    help="also merge sources switched off by default "
                         "(gbif, panoramax); see MERGE.md for why they are off")
    # The sources do not cover the same years: YFCC stops in 2014 and
    # iNaturalist is overwhelmingly 2020s, so the all-time merge is a
    # 2004-2026 composite. --yfcc-window renders the like-for-like period.
    ap.add_argument("--from", dest="dfrom", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--to", dest="dto", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--yfcc-window", action="store_true",
                    help="restrict every source to 2004-01-01..2015-06-01, "
                         "the period YFCC100M actually covers")
    ap.add_argument("--tag", default="",
                    help="suffix for the output filenames, so two runs can "
                         "coexist in out/multi/")
    a = ap.parse_args()
    win = None
    if a.yfcc_window:
        win, tag = multi.YFCC_WINDOW, a.tag or "_2004_2014"
    else:
        tag = a.tag
        if a.dfrom or a.dto:
            win = (multi.parse_date(a.dfrom) if a.dfrom else None,
                   multi.parse_date(a.dto) if a.dto else None)
    main(size=a.size, directory=a.dir, cap=a.cap, out_dir=a.out,
         per_source=a.per_source, downscales=a.downscale,
         exclude_paths=not a.include_paths,
         date_kinds=tuple(a.residency_dates),
         include_flickr=not a.no_flickr, sources=a.sources,
         window=win, all_sources=a.all_sources, tag=tag)
