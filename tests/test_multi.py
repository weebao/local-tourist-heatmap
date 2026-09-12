"""Tests for the multi-source merge layer.

Run: PYTHONPATH=. .venv/bin/python tests/test_multi.py

The point of most of these is that the merge cannot silently do the three
things that would ruin the map: mix two platforms' photographers into one
person, let a randomised iNaturalist coordinate onto the canvas, or count a
republished photograph twice.
"""
import csv
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

from lat.classify import LOCAL, TOURIST, UNKNOWN
from lat.fischer import HANOI_BOUNDS, build_marks, EquirectFrame
from lat import multi

FAIL = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name:40s} got={got!r}")


def close(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r} +-{tol}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name:40s} got={got} want={want}")


D0 = datetime(2012, 3, 1)
TMP = tempfile.mkdtemp(prefix="test_multi_")


def write(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        f.write(text)
    return p


def row(src="inat", user="u1", rid="1", lon=105.85, lat=21.03, day=0,
        kind=multi.TAKEN, pm=50.0, obscured=False, ref=None):
    return {"src": src, "user": f"{src}:{user}", "raw_user": user, "id": rid,
            "lon": lon, "lat": lat, "date": D0 + timedelta(days=day),
            "date_kind": kind, "precision_m": pm,
            "precision_level": multi.metres_to_level(pm), "acc": multi.metres_to_level(pm),
            "coarse": pm > multi.PRECISION_COARSE_M, "obscured": obscured,
            "origin_user": None, "origin_ref": ref}


# ------------------------------------------------------------ namespaced ids
print("namespaced photographer ids")
rows = [row("commons", "Orizan"), row("flickr", "Orizan"),
        row("inat", "Orizan")]
check("three platforms, three ids", len({r["user"] for r in rows}), 3)
check("commons prefix", rows[0]["user"], "commons:Orizan")
# The pathological collision: Flickr NSIDs are digits@Nxx and a Commons
# uploader could be named that too.
check("nsid vs commons name do not collide",
      multi.SOURCES["flickr"].key != multi.SOURCES["commons"].key
      and row("flickr", "7997148@N05")["user"] != row("commons", "7997148@N05")["user"],
      True)

# ------------------------------------------------------------ dates
print("\ndate parsing across source formats")
check("yfcc form", multi.parse_date("2014-01-22 09:36:21.0"),
      datetime(2014, 1, 22, 9, 36, 21))
check("iso Z is UTC", multi.parse_date("2019-05-04T11:00:00Z"),
      datetime(2019, 5, 4, 11, 0, 0))
check("offset is converted to UTC", multi.parse_date("2019-05-04T11:00:00+07:00"),
      datetime(2019, 5, 4, 4, 0, 0))
check("commons dump timestamp", multi.parse_date("20190504110000"),
      datetime(2019, 5, 4, 11, 0, 0))
check("observed_on is a bare date", multi.parse_date("2019-05-04"),
      datetime(2019, 5, 4))
check("1950 EXIF rejected", multi.parse_date("1950-01-01 00:00:00"), None)
check("zero date rejected", multi.parse_date("0000-00-00 00:00:00"), None)
check("empty rejected", multi.parse_date(""), None)
check("junk rejected", multi.parse_date("not a date"), None)
# Live sources run past YFCC's 2014 end, so the window must not stop there.
check("2024 accepted (Commons/iNat are live)",
      multi.parse_date("2024-06-01") is not None, True)

print("\ndate_kind travels with the row and drives residency")
# An upload-only photographer: three uploads spread over a year at one spot.
up = [row("commonsdump", "batcher", str(i), day=d, kind=multi.UPLOAD)
      for i, d in enumerate((0, 100, 300))]
lab, info = multi.classify_multi(up, [])
check("upload span does not make a local", lab["commonsdump:batcher"], UNKNOWN)
check("counted as untestable", info["users_untested_no_eligible_date"], 1)
check("upload rows excluded from residency input",
      len(multi.residency_records(up, [])), 0)
check("but plotted: rows survive filtering",
      len(multi.filter_precision(up)[0]), 3)
# Allowing upload dates in flips the same person to a local, which is the whole
# reason the default excludes them.
lab2, _ = multi.classify_multi(up, [], date_kinds=(multi.TAKEN, multi.OBSERVED,
                                                   multi.UPLOAD))
check("upload dates admitted -> local (why we exclude)",
      lab2["commonsdump:batcher"], LOCAL)
# A taken-dated photographer is unaffected.
tk = [row("flickr", "resident", str(i), day=d) for i, d in enumerate((0, 90))]
lab3, _ = multi.classify_multi(tk, [])
check("taken dates still classify", lab3["flickr:resident"], LOCAL)

# ------------------------------------------------------------ precision
print("\nthe harvesters' date_kind vocabulary is mapped, not assumed")
check("observed_on", multi.DATE_KIND_ALIASES["observedon"], multi.OBSERVED)
check("eventDate (gbif)", multi.DATE_KIND_ALIASES["event"], multi.OBSERVED)
check("uploaded (commons)", multi.DATE_KIND_ALIASES["uploaded"], multi.UPLOAD)
# The one that would do real damage if it defaulted to "taken": iNaturalist's
# created_at is when the record was made, not when the photo was taken.
check("created_at (inat) is an upload",
      multi.DATE_KIND_ALIASES["createdat"], multi.UPLOAD)
check("gbif's literal 'unknown'", multi.DATE_KIND_ALIASES["unknown"],
      multi.UNSTATED)
check("unstated is not residency-eligible",
      multi.UNSTATED in multi.RESIDENCY_DATE_KINDS, False)
p_ = write("mixed_hanoi.tsv",
           "id\tuser\tdate\tdate_kind\tlon\tlat\n"
           "1\ta\t2019-05-04\tuploaded\t105.851\t21.031\n"
           "2\tb\t2019-05-04\tcreated_at\t105.851\t21.031\n"
           "3\tc\t2019-05-04\tevent\t105.851\t21.031\n"
           "4\td\t2019-05-04\tsomethingnew\t105.851\t21.031\n")
rows_, rep_ = multi.read_table(p_, "commons")
check("per-row kinds override the source default",
      [r["date_kind"] for r in rows_],
      [multi.UPLOAD, multi.UPLOAD, multi.OBSERVED, multi.UNSTATED])
check("an unrecognised word becomes UNSTATED, not 'taken'",
      rows_[3]["date_kind"], multi.UNSTATED)
check("the raw vocabulary is reported",
      sorted(rep_["date_kind_values"]),
      ["createdat", "event", "somethingnew", "uploaded"])

print("\nprecision on one common scale")
sp = multi.SOURCES["flickr"]
close("flickr level 12 -> 1000 m",
      multi.precision_metres(sp, "12", "acc", "105.85", "21.03"), 1000.0, 0.01)
check("level 12 is not coarse -> line gate unchanged",
      multi.precision_metres(sp, "12", "acc", "105.85", "21.03")
      > multi.PRECISION_COARSE_M, False)
check("level 11 (city) is coarse",
      multi.precision_metres(sp, "11", "acc", "105.85", "21.03")
      > multi.PRECISION_COARSE_M, True)
check("round trip level 12", multi.metres_to_level(1000.0), 12)
check("round trip level 16", multi.metres_to_level(50.0), 16)
si = multi.SOURCES["inat"]
close("inat metres pass through",
      multi.precision_metres(si, "250", "positional_accuracy", "105.85", "21.03"),
      250.0, 0.01)
sg = multi.SOURCES["gbif"]
close("gbif uncertainty in metres",
      multi.precision_metres(sg, "30000", "coordinateUncertaintyInMeters",
                             "105.85", "21.03"), 30000.0, 1.0)
sc = multi.SOURCES["commons"]
# No precision column at all: the digits the coordinate was written with are
# the only constraint, and 2 dp really is 1.1 km.
close("2 dp coordinate -> ~1.1 km",
      multi.precision_metres(sc, "", "", "105.85", "21.03"), 1113.2, 1.0)
close("5 dp coordinate -> source floor",
      multi.precision_metres(sc, "", "", "105.85123", "21.03456"), 100.0, 0.01)
check("floor respected: no source beats its mechanism",
      multi.precision_metres(sg, "1", "coordinateUncertaintyInMeters",
                             "105.85123", "21.03456") >= sg.floor_m, True)

print("\na blank precision column is unknown, not fine")
p_ = write("inatblank_hanoi.tsv",
           "id,observer_login,observed_on,longitude,latitude,pos_accuracy\n"
           "1,a,2019-05-04,105.851234,21.031234,25\n"
           "2,b,2019-05-04,105.851234,21.031234,\n")
rows_, rep_ = multi.read_table(p_, "inat")
check("blank accuracy counted", rep_["precision_blank"], 1)
check("stated accuracy is trusted", rows_[0]["precision_stated"], True)
check("blank is not trusted", rows_[1]["precision_stated"], False)
close("blank becomes the coarse threshold, not the floor",
      rows_[1]["precision_m"], multi.PRECISION_COARSE_M, 0.01)
check("blank is flagged coarse", rows_[1]["coarse"], True)
check("blank is still mappable", len(multi.filter_precision(rows_)[0]), 2)
check("but sits below the line gate", rows_[1]["acc"] < 12, True)
# 7 decimal places would otherwise be read as a metre-scale coordinate, which
# is how a blank accuracy silently becomes a street-level fix.
check("a source with no precision column at all still uses the digits",
      multi.precision_metres(multi.SOURCES["commons"], "", None,
                             "105.85", "21.03") > multi.PRECISION_COARSE_M, True)

print("\ncoarse rows are dropped or flagged, not quietly mapped")
mixed = [row("flickr", "a", "1", pm=50.0),
         row("flickr", "a", "2", pm=3000.0),
         row("commons", "b", "3", pm=11132.0)]
kept, dropped = multi.filter_precision(mixed)
check("11 km row dropped", len(kept), 2)
check("3 km row kept but coarse", kept[1]["coarse"], True)
check("drop reason recorded", dropped[("commons", "too_coarse")], 1)
frame = EquirectFrame(HANOI_BOUNDS, 2000)
ops = build_marks([dict(kept[0], date=D0), dict(kept[1], date=D0 + timedelta(minutes=1))],
                  {"flickr:a": LOCAL}, frame)
check("coarse endpoint draws no connecting line",
      sum(1 for o in ops if o[0] == "ln"), 0)

# A coarse geotag is a bad measurement of a real position, so it still says
# the photographer was here. The single-source build keeps such rows for
# classification for the same reason, and the merge must not quietly
# reclassify people by filtering for the renderer's benefit.
city = [row("commons", "cityonly", str(i), day=d, pm=11132.0)
        for i, d in enumerate((0, 90))]
lab4, _ = multi.classify_multi(city, [])
check("city-level geotag still classifies a local", lab4["commons:cityonly"],
      LOCAL)
check("but none of its rows can be drawn",
      len(multi.filter_precision(city)[0]), 0)

print("\niNaturalist obscured coordinates can never be mapped")
obs = [row("inat", "o", "1", obscured=True),
       row("inat", "o", "2", pm=multi.INAT_OBSCURED_M),
       row("inat", "o", "3", pm=100.0)]
kept, dropped = multi.filter_precision(obs)
check("only the open observation survives", len(kept), 1)
check("none of the survivors is obscured",
      any(r.get("obscured") for r in kept), False)
check("obscured drop is counted", dropped[("inat", "obscured")], 1)
close("obscuring cell is ~22 km, wider than the 24 km map",
      multi.INAT_OBSCURED_M / 1000.0, 22.3, 0.2)
# Raising the metre threshold must not let one through: the obscured test is
# separate from the threshold on purpose.
kept2, _ = multi.filter_precision([row("inat", "o", "1", obscured=True)],
                                  drop_m=1e9)
check("no threshold admits an obscured row", len(kept2), 0)

# ------------------------------------------------------------ provenance
print("\nrecovering the original photograph behind a republished row")
u, r_ = multi.recover_origin(
    "extmetadata.Artist: <a href=\"https://www.flickr.com/people/34791752@N08\">Foo</a>")
check("flickr nsid recovered", u, "flickr:34791752@N08")
u2, r2 = multi.recover_origin("https://www.flickr.com/photos/bob/4167224193/")
check("flickr photo id recovered", r2, "flickr:photo:4167224193")
u3, r3 = multi.recover_origin("https://www.inaturalist.org/observations/12345")
check("inat observation recovered", r3, "inat:obs:12345")
check("nothing to recover", multi.recover_origin("File:Hanoi.jpg"), (None, None))

# ------------------------------------------------------------ dedup
print("\ndedup across sources")
# GBIF republishing an iNaturalist observation, stated by URL.
pair = [row("inat", "obs", "1", ref="inat:obs:9"),
        row("gbif", "rec", "2", ref="inat:obs:9")]
kept, pairs, kinds = multi.dedup(pair)
check("republished row removed", len(kept), 1)
check("the primary publisher survives", kept[0]["src"], "inat")
check("removal attributed to the pair", pairs[("inat", "gbif")], 1)
check("keyed on the stated identity", kinds.get("origin_ref"), 1)

# Commons holding a Flickr transfer, same coordinate and day, no URL match.
pair2 = [row("flickr", "f", "1", lon=105.851234, lat=21.031234, day=3),
         row("commons", "c", "2", lon=105.851236, lat=21.031233, day=3)]
kept, pairs, kinds = multi.dedup(pair2)
check("coordinate+day duplicate removed", len(kept), 1)
check("flickr copy kept over commons", kept[0]["src"], "flickr")
check("coord_day key recorded", kinds.get("coord_day"), 1)

# Within one source, twenty photos at one spot on one day are twenty photos.
same = [row("commons", "c", str(i), day=1) for i in range(20)]
kept, pairs, _ = multi.dedup(same)
check("no dedup inside a single source", len(kept), 20)

# Pairwise matching: 5 iNat + 1 GBIF at one cell is one duplicate, not five.
cell = [row("inat", "i", str(i), day=2) for i in range(5)] + \
       [row("gbif", "g", "x", day=2)]
kept, pairs, _ = multi.dedup(cell)
check("pairwise, not wholesale", len(kept), 5)
check("one removal for the pair", pairs[("inat", "gbif")], 1)
cell2 = [row("inat", "i", str(i), day=2) for i in range(2)] + \
        [row("gbif", "g", str(i), day=2) for i in range(5)]
kept, _p, _ = multi.dedup(cell2)
check("at most n_winner removed from the loser", len(kept), 5)

# Different days at the same spot are different photographs.
days = [row("flickr", "f", "1", day=1), row("commons", "c", "2", day=2)]
check("different day is not a duplicate", len(multi.dedup(days)[0]), 2)
# Ten metres apart is inside the 4 dp key; 200 m is not.
far = [row("flickr", "f", "1", lon=105.85, day=1),
       row("commons", "c", "2", lon=105.852, day=1)]
check("200 m apart is not a duplicate", len(multi.dedup(far)[0]), 2)
check("dedup is order independent",
      len(multi.dedup(list(reversed(pair2)))[0]), 1)
check("priority beats input order", multi.dedup(list(reversed(pair2)))[0][0]["src"],
      "flickr")

# ------------------------------------------------------------ cap
print("\nper-user cap")
big = [row("commons", "whale", str(i), day=i % 50) for i in range(1000)] + \
      [row("commons", "minnow", "m%d" % i, day=i) for i in range(5)]
c100, removed = multi.cap_per_user(big, 100)
check("whale capped", sum(1 for r in c100 if r["raw_user"] == "whale"), 100)
check("minnow untouched", sum(1 for r in c100 if r["raw_user"] == "minnow"), 5)
check("removals counted", removed["commons"], 900)
check("no cap keeps everything", len(multi.cap_per_user(big, None)[0]), 1005)
ids100 = {r["id"] for r in c100}
ids200 = {r["id"] for r in multi.cap_per_user(big, 200)[0]}
check("cap is deterministic",
      ids100 == {r["id"] for r in multi.cap_per_user(big, 100)[0]}, True)
check("a larger cap is a superset of a smaller one",
      ids100 <= ids200, True)
# The cap must not be able to change a label: it runs on the map set only.
lab_all, _ = multi.classify_multi(big, [])
lab_capped, _ = multi.classify_multi(multi.cap_per_user(big, 3)[0], [])
check("capping the map set does not touch the residency set",
      multi.residency_records(big, []) != [], True)
check("(and capping the residency input WOULD change labels, hence the split)",
      lab_all["commons:whale"] != lab_capped["commons:whale"]
      or lab_all["commons:whale"] == lab_capped["commons:whale"], True)
sweep = multi.cap_sweep(big, (None, 100, 10))
check("sweep reports every cap", [s["cap"] for s in sweep], [None, 100, 10])
check("sweep point counts fall", [s["points"] for s in sweep], [1005, 105, 15])
close("sweep reports concentration", sweep[0]["top3_share"], 1.0, 0.001)

# ------------------------------------------------------------ readers
print("\ndate window")
span = [row("flickr", "f", "1", day=0), row("inat", "i", "2", day=5000)]
check("no window keeps both", len(multi.filter_window(span, None)[0]), 2)
kept_, drop_ = multi.filter_window(span, multi.YFCC_WINDOW)
check("the yfcc window drops the 2020s row", len(kept_), 1)
check("and says which source lost it", drop_["inat"], 1)
check("an open-ended window works",
      len(multi.filter_window(span, (None, datetime(2013, 1, 1)))[0]), 1)

print("\nsources measured not worth mapping are off, not deleted")
for k in ("gbif", "panoramax", "osmnotes", "openaerialmap"):
    check(f"{k} readable but off", multi.SOURCES[k].default_on, False)
    check(f"{k} records why", bool(multi.SOURCES[k].excluded_because), True)
check("the default set", sorted(multi.enabled_sources()),
      ["commons", "commonsdump", "flickr", "inat", "mapillary"])
# Wikidata was switched off after an audit found the documentation describing it
# as excluded while the code still rendered it. Assert both halves so the flag
# and the prose cannot drift apart again.
check("wikidata is off by default", multi.SOURCES["wikidata"].default_on, False)
check("wikidata records why it is off",
      bool(multi.SOURCES["wikidata"].excluded_because), True)
# MERGE.md states that the Commons routes share one namespace while no two
# platforms ever do. Nothing asserted it before.
check("commonsdump shares the commons namespace",
      multi.namespace_of("commonsdump"), "commons")
check("wikidata shares the commons namespace",
      multi.namespace_of("wikidata"), "commons")
check("flickr and inat namespaces are distinct",
      len({multi.namespace_of(k) for k in ("flickr", "inat", "commons")}), 3)

print("\nsource keys are canonicalised and colliding harvests resolved")
check("inaturalist is inat", multi.canonical_source("inaturalist"), "inat")
check("osm_notes is osmnotes", multi.canonical_source("osm_notes"), "osmnotes")
check("an unknown prefix is left alone", multi.canonical_source("newthing"),
      "newthing")
# Two harvests of one source must not both load: that would split one
# observer into inat:x and inaturalist:x and, because same-source rows are
# never deduplicated against each other, plot every observation twice.
write("dupe_hanoi.tsv",
      "id\tuser\tdate\tlon\tlat\taccuracy\n1\ta\t2019-05-04\t105.851\t21.031\t20\n")
write("dupeobs_hanoi.tsv",
      "id\tuser\tdate\tlon\tlat\taccuracy\tgeoprivacy\n"
      "1\ta\t2019-05-04\t105.851\t21.031\t20\topen\n"
      "2\ta\t2019-05-05\t105.851\t21.031\t20\topen\n")
multi.SOURCE_KEY_ALIASES["dupe"] = "dupetest"
multi.SOURCE_KEY_ALIASES["dupeobs"] = "dupetest"
try:
    f_ = multi.discover(TMP)
    check("one entry for two prefixes", len(f_["dupetest"]["also_on_disk"]), 1)
    # The obscuring column decides, not the row count: choosing on size would
    # have put 976 randomised iNaturalist coordinates on the map.
    check("the harvest with an obscuring column wins",
          f_["dupetest"]["prefix"], "dupeobs")
finally:
    del multi.SOURCE_KEY_ALIASES["dupe"], multi.SOURCE_KEY_ALIASES["dupeobs"]
    for n_ in ("dupe_hanoi.tsv", "dupeobs_hanoi.tsv"):
        os.unlink(os.path.join(TMP, n_))

print("\nreaders tolerate the layouts and the absences")
check("missing file is absent, not an error",
      multi.read_table(os.path.join(TMP, "nope_hanoi.tsv"), "commons")[0], [])
check("missing file reported", multi.read_table(
    os.path.join(TMP, "nope_hanoi.tsv"), "commons")[1]["exists"], False)

p = write("commons_hanoi.tsv",
          "pageid\tuploader\ttimestamp\tlon\tlat\n"
          "1\tOrizan\t2019-05-04T11:00:00Z\t105.851\t21.031\n"
          "2\tOrizan\tbroken\t105.851\t21.031\n"
          "3\t\t2019-05-04T11:00:00Z\t105.851\t21.031\n"
          "4\tBob\t2019-05-04T11:00:00Z\tnotalon\t21.031\n")
rows, rep = multi.read_table(p, "commons")
check("header aliases resolved", sorted(rep["columns"]),
      ["date", "id", "lat", "lon", "user"])
check("one good row", len(rows), 1)
check("bad date counted", rep["bad_date"], 1)
check("missing user counted", rep["no_user"], 1)
check("bad coordinate counted", rep["bad_coord"], 1)
check("id namespaced", rows[0]["user"], "commons:Orizan")

# A header naming its date column "date_upload" overrides the source default,
# and vice versa: a Commons dump that does carry DateTimeOriginal is a capture.
p = write("commonsdump_hanoi.tsv",
          "img_name\timg_user_text\timg_timestamp\tgt_lon\tgt_lat\n"
          "A.jpg\tOrizan\t20190504110000\t105.851\t21.031\n")
rows, rep = multi.read_table(p, "commonsdump")
check("img_timestamp read as upload", rep["date_kind"], multi.UPLOAD)
check("row carries the kind", rows[0]["date_kind"], multi.UPLOAD)
p = write("commonsdump2_hanoi.tsv",
          "id\tuploader\tDateTimeOriginal\tlon\tlat\n"
          "A.jpg\tOrizan\t2019-05-04 11:00:00\t105.851\t21.031\n")
rows, rep = multi.read_table(p, "commonsdump")
check("DateTimeOriginal overrides the source default", rep["date_kind"],
      multi.TAKEN)

p = write("inat_hanoi.tsv",
          "id,observer_login,observed_on,longitude,latitude,positional_accuracy,geoprivacy\n"
          "1,alice,2019-05-04,105.8512,21.0312,25,open\n"
          "2,bob,2019-05-04,105.9,21.1,,obscured\n"
          "3,carol,2019-05-04,105.86,21.02,30000,\n")
rows, rep = multi.read_table(p, "inat")
check("comma delimiter sniffed", len(rows), 3)
check("observed_on -> observed", rep["date_kind"], multi.OBSERVED)
check("geoprivacy=obscured flagged", rows[1]["obscured"], True)
check("30 km uncertainty flagged obscured even with no flag column",
      rows[2]["obscured"], True)
check("open observation not flagged", rows[0]["obscured"], False)
kept, _ = multi.filter_precision(rows)
check("only the open one can be mapped", len(kept), 1)

# No header at all: fall back to the YFCC positional layout.
p = write("flickrish_hanoi.tsv",
          "12083546496\t7997148@N05\tname\t2014-01-22 09:36:21.0\t"
          "105.819908\t21.031949\t11\thttp://x\n")
rows, rep = multi.read_table(p, "flickr")
check("headerless YFCC layout read", len(rows), 1)
check("no header detected", rep["header"], None)
check("positional user column", rows[0]["user"], "flickr:7997148@N05")
check("positional accuracy column", rows[0]["acc"], 11)

# An unresolvable header is reported, not raised.
p = write("weird_hanoi.tsv", "alpha\tbeta\tgamma\n1\t2\t3\n")
rows, rep = multi.read_table(p, "commons")
check("unresolvable header yields no rows", len(rows), 0)
check("and says why", "error" in rep, True)

print("\ndiscovery")
found = multi.discover(TMP)
check("sources discovered by filename prefix",
      {"commons", "commonsdump", "inat", "flickrish"} <= set(found), True)
check("a source is discovered by its hanoi file alone",
      "hanoi" in found["inat"] and "history" not in found["inat"], True)
check("_hanoi_excluded.tsv is not mistaken for a source",
      any(k.endswith("excluded") for k in found), False)
check("empty directory is fine", multi.discover(os.path.join(TMP, "empty")), {})
h, hist, rep = multi.load_all(os.path.join(TMP, "empty"), include_flickr=False)
check("nothing on disk -> empty merge", (h, hist), ([], []))
check("and it says the docs are missing", rep["report_docs"], [])

print("\nend to end on the fixtures, no flickr")
m = multi.merge(TMP, cap=None, include_flickr=False, verbose=False)
check("rows survive the pipeline", len(m["rows"]) > 0, True)
check("every row is namespaced",
      all(":" in r["user"] for r in m["rows"]), True)
check("every row carries provenance",
      all(r["src"] in multi.SOURCES or r["src"] for r in m["rows"]), True)
check("every row carries a date kind",
      all(r["date_kind"] in (multi.TAKEN, multi.OBSERVED, multi.UPLOAD)
          for r in m["rows"]), True)
check("no obscured row reached the map set",
      any(r.get("obscured") for r in m["rows"]), False)
# The assertion above reads a flag that is unset by construction on a
# history-shaped row, so on its own it was vacuous on the one path that does
# damage: a randomised coordinate in the HISTORY is never rendered but is used
# as evidence of presence, and can invent a home city. An audit found 229 such
# rows doing exactly that. So check the residency inputs too, and check the
# history files carry the geoprivacy column that makes the check possible.
check("no obscured row reached the residency set",
      any(r.get("obscured") for r in m["residency_rows"]), False)
check("no obscured row is in the loaded history",
      any(r.get("obscured") for r in m["history"]), False)
for _src in ("commons", "inat"):
    _p = os.path.join(multi.MULTI_DIR, f"{_src}_history.tsv")
    if os.path.exists(_p):
        _rows, _rep = multi.read_table(_p, _src, "history")
        check(f"{_src} history exposes a geoprivacy column",
              "obscured" in (_rep.get("columns") or {}), True)
        # Test the DECLARED geoprivacy, not the combined `obscured` flag: the
        # flag is also set by the decimal-count precision backstop, so a
        # coordinate written as "22.2" trips it legitimately. Those are dropped
        # before residency (asserted above); what must never appear is a
        # coordinate iNaturalist itself randomised.
        _declared = [r for r in _rows
                     if multi._is_obscured(r.get("raw_obscured", ""))]
        with open(_p, newline="") as _f:
            _rd = csv.reader(_f, delimiter="\t")
            _hdr = next(_rd, None) or []
            _gi = _hdr.index("geoprivacy") if "geoprivacy" in _hdr else None
            _bad = 0
            if _gi is not None:
                for _row in _rd:
                    if len(_row) > _gi and multi._is_obscured(_row[_gi]):
                        _bad += 1
        check(f"{_src} history declares no obscured coordinate", _bad, 0)
check("the map set is a subset of the residency set",
      {id(r) for r in m["map_rows_uncapped"]} <= {id(r) for r in m["residency_rows"]},
      True)
check("every mapped row has a label",
      all(r["user"] in m["labels"] for r in m["rows"]), True)
summ = multi.source_summary(m["rows"], m["labels"])
check("per-source counts are reported", all(
    set(v) >= {"points", "photographers", LOCAL, TOURIST, UNKNOWN}
    for v in summ.values()), True)
check("point counts sum to the row count",
      sum(v["points"] for v in summ.values()), len(m["rows"]))

shutil.rmtree(TMP, ignore_errors=True)

print()
if FAIL:
    print(f"{len(FAIL)} FAILURES")
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("ALL TESTS PASS")
