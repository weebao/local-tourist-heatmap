# Merging several photo sources

The single-source build is a faithful reproduction of Erica Fischer's *Locals
and Tourists* for Hanoi from one table, YFCC100M. This directory is a separate
experiment: what the same map looks like when every other reachable source of
geotagged photographs is added.

**The merged map is not the reproduction.** It is rendered to `out/multi/` and
never overwrites `out/`. Read `../../README.md` for the faithful one. Live
figures are in `out/multi/stats.json`, which the build emits; figures are
deliberately not copied into this file, because they move whenever a harvest
extends and stale numbers in prose were the single most common defect found in
this project's adversarial reviews.

## Why a separate map at all

Fischer's colours are a residency test. Blue means a photographer's photos
inside the Hanoi box span 30 days or more; red means they look resident in a
15-mile box that does not overlap Hanoi's and were here under a month; yellow
means neither. That test needs each photographer's **worldwide** history over
time. Adding a source that cannot supply one does not add data to this map, it
adds uncoloured dots to a map whose only content is the colour.

So the question for every candidate source was never "does it have photos of
Hanoi" but "does it have a stable photographer id, a date, a coordinate, and a
queryable global history". Most do not.

## Decisions

**Ids are namespaced and never reconciled.** `flickr:7997148@N05`,
`commons:Orizan`, `inat:benjamin`. The same human with accounts on two
platforms is counted as two photographers. Cross-platform identity resolution
is not attempted: it would mean matching people across services on name or
image similarity, which is both unreliable and a privacy problem this map does
not need to create. The three Commons routes (API, SQL dumps, Wikidata) do
share one `commons:` namespace, because there one uploader is one uploader.

**`date_kind` travels with every row**, because a 30-day span measures
different things per source. Flickr gives date *taken*, iNaturalist
*observed*, the Commons SQL dumps *uploaded*. Upload dates are the dangerous
one: a photographer who uploads a five-year backlog in a week shows a one-week
span and is wrongly read as a visitor, while one who drip-feeds the same trip
over years looks resident. `RESIDENCY_DATE_KINDS` therefore restricts which
kinds may participate in the residency test, while all kinds are still
plotted. The Commons API harvest turned out to be 17,168 capture dates against
400 upload dates, so including it costs about 2%; that is the default and the
2% is the reason it is stated here.

**Precision is normalised to metres.** Flickr's 1–16 ordinal, iNaturalist's
`positional_accuracy`, Panoramax's and GBIF's uncertainty fields all land on
one scale, and `precision_level` is written back into the `acc` key so the
existing renderer's accuracy >= 12 line gate keeps working untouched. Missing
accuracy is treated as unknown rather than as either fine or coarse, and
counted separately.

**iNaturalist obscured coordinates are refused outright.** For taxa with
conservation concerns iNaturalist randomises the published position inside
roughly 0.2 degrees, about 22 km. On a 24 km map that is not a coordinate. A
test asserts they cannot reach the render.

**Duplicates are removed across sources and reported per pair.** GBIF
republishes iNaturalist; Commons holds bot-transferred Flickr files, whose
`extmetadata.Artist` can still carry the original Flickr NSID, so the original
id is recovered where possible.

**Per-user capping is exposed, not chosen silently.** Commons is pathologically
concentrated: 432 uploaders in the Hanoi box with the top three holding 46% of
the rows. Uncapped, the merged map is substantially three people's albums.
`cap_sweep` reports what several caps do to the point count, the photographer
count and the top-5 share, and the last of those is the number that says
whether you are looking at a city or an album.

## Sources

Kept:

- **flickr** (YFCC100M) — the baseline. Capture dates, a real ordinal
  precision field, and a 1.1M-row worldwide history.
- **commons** (Wikimedia Commons API) — good capture-date coverage, poor
  photographer diversity, needs capping.
- **inat** (iNaturalist) — the best photographer diversity of anything here,
  2,106 observers in the box, at the cost of a spatial bias toward parks,
  lakes and green space, and a date range overwhelmingly in the 2020s.

Excluded, with the reason recorded in `stats.json`
(`sources_present_but_excluded`) so nobody re-harvests them:

- **gbif** — about half the rows state no uncertainty and the stated ones have
  a p90 of 16.4 km; `recordedBy` is free text, not a stable id, so it cannot
  anchor a residency test; and thousands of its rows are the iNaturalist rows
  we already hold at better fidelity.
- **panoramax** — 320 rows in the box from exactly one contributor. A
  locals-versus-tourists distinction cannot be drawn from a single identity.
- **osmnotes** — not photographs. Harvested because it has the right field
  shape (uid, date, coordinate) and is a real signal of who is active where,
  but it is map notes, not imagery.
- **wikidata**, **openaerialmap** — a few hundred and two rows respectively,
  with no usable contributor identity.

Not attempted, deliberately: Reddit, Instagram, TikTok, X. None exposes a
geotag the photographer attached, and inferring an individual account's home
city from their posting history in order to colour them is profiling of private
individuals. The sources above all use location data the photographer chose to
attach to their own photograph.

## Known gaps

- **Commons worldwide history is being backfilled** by
  `lat/harvest_history.py`, which was written because the original harvest did
  not deliver it. Until it completes, Commons uploaders have no evidence of
  residency elsewhere and fall to "unknown" for lack of data rather than
  because the data says so. The same applies to the iNaturalist observers not
  yet covered. `stats.json` reports `history_rows` per source, and
  `photographers_untestable` counts those with no history at all: read both
  before drawing any conclusion from the merged colour split.
- **The Commons SQL-dump cross-check was never completed.** It was meant to
  give an independent count of geotagged namespace-6 files in the Hanoi box, to
  test whether the API harvest's quadtree geosearch was complete. The API
  harvest's completeness is therefore unverified.
- **The merged map is a 2004–2026 composite, not a 2010 snapshot.** YFCC100M
  stops in 2014 while iNaturalist is overwhelmingly 2020s and Commons straddles
  both. `build_multi.py` takes a date window so a like-for-like 2004–2014
  merged map can be rendered, but the default is all-time and the composite
  should not be compared directly against Fischer's sheets.
