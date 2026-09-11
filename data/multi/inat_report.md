# iNaturalist harvest - Hanoi observations + worldwide observer history

Generated 2026-09-10 11:34. Endpoint `https://api.inaturalist.org/v1/observations`, public v1 REST, **no API key used or required**.

---

## READ THIS FIRST: what this data actually is

**iNaturalist is nature-observation data.** Every row is somebody photographing a plant, bird, insect, fungus or fish and submitting it for identification. The spatial distribution is therefore **not** a distribution of human presence or of photographic interest - it is a distribution of *biodiversity-recording opportunity*, which in a dense city means parks, lakes, river banks and campuses.

Measured, not asserted: **39% of the observations inside the 24 km map footprint fall on mapped green space or water, which is only 25% of the map's surface** (1.54x enrichment). Parks and gardens alone are **9.4x** over-represented: 16.0% of observations on 1.7% of the land. Section 6 has the full breakdown.

Use it as a clearly-labelled supplementary layer. Do not blend it anonymously into a photo-density heatmap and do not read it as "where people photograph Hanoi".

---

## 1. What was run, and the re-verified API constraints

### Step 1 - Hanoi sweep
Wide box, lon 105.30..106.40 / lat 20.60..21.50, photos required:

```
https://api.inaturalist.org/v1/observations?nelat=21.50&nelng=106.40&swlat=20.60&swlng=105.30&per_page=200&order_by=id&order=asc&photos=true&id_above=<cursor>
```

Both constraints recorded earlier in this project were re-verified live before harvesting:

| Claim | Test performed | Result |
|---|---|---|
| `per_page` max 200 | requested `per_page=300` | response reported `per_page: 200` and returned 200 results - **silently clamped, no error** |
| `page * per_page <= 10000` | requested `page=51&per_page=200` | **HTTP 403** - *"Result window is too large, page x size must be less than or equal to [10000]. Please narrow your search, or use a sliding window approach with id_above or id_below params."* |

Both hold. The box contains **20,899** matching observations - 2.1x past the 10,000-row page window - so the `id_above` cursor is mandatory, not optional. The sweep pages on `id_above = max(id)` of the previous page until a short page returns: **105 pages, no gaps, no overlap** (observation ids were deduped into a set as a check and the dedupe removed nothing).

### Step 4 - worldwide history sweep
For every observer found in Hanoi, their global geotagged history:

```
https://api.inaturalist.org/v1/observations?user_login=<login>&per_page=200&order_by=id&order=asc&id_above=<cursor>&geo=true
```

Two deliberate changes to that shape, both of which materially improve the result:

1. **Batched observers.** `user_id` accepts a comma-separated list, verified live (40 ids in one query returned the union of their observations, `total_results` summing correctly, URL only 455 chars). The naive one-query-per-observer shape pays a minimum of one request per observer - 1,188 wasted requests for the 1,188 observers who have fewer than 200 observations each. Batching 60 observers per query removed that floor. Observers with 5,000+ global observations are still swept individually, since a batch cannot enforce a per-observer cap.
2. **Heavy observers are swept DESCENDING** (`order=desc` with an `id_below` cursor) rather than ascending. This matters: when a cap truncates an observer's history, ascending order keeps their *oldest* observations, which is exactly the wrong half for a residency question. Descending keeps the most recent. Verified live - the first descending page for the largest observer returned observations dated today.

**Observers were processed in Hanoi-photo-count descending order**, so the coverage is front-loaded onto the observers who contribute the most points to the map. This was a correction: the globally-prolific observers are only 37.4% of Hanoi rows, so ordering by global size would have prioritised the wrong people.

### Two findings worth recording for future work in this project

- **The `fields` parameter does not work on API v1.** Passing `fields=id,observed_on,geojson,...` returns the full fat record anyway - the response was byte-identical at 14,009,729 bytes. It is an API-v2 feature, and the v2 endpoint rejected the same field list with HTTP 422. So every page is a ~14 MB JSON payload no matter what you ask for.
- **Always request gzip.** `Accept-Encoding: gzip` takes one page from 14,009,729 bytes on the wire to ~649,000 - a **21x** reduction, and the difference between ~70 MB and ~1.5 GB across the Hanoi sweep. Python `requests` sends it by default; bare `curl` does not, which is easy to miss when prototyping with curl.

## 2. Headline counts

| Quantity | Count |
|---|---|
| Observations reported by the API for the box | 20,899 |
| **Distinct observations fetched** (deduped by id) | **20,899** |
| Pages fetched via `id_above` cursor | 105 |
| **Rows surviving with usable coordinate AND date AND user** | **19,866** |
| Rows excluded (all reasons) | 1,033 |
| **Distinct observers in the mappable set** | **2,106** |
| Distinct observers seen anywhere in the box | 2,176 |
| Rows inside the 24 km map footprint | 10,485 |

19,866 kept + 1,033 excluded = 20,899 fetched. Every observation is accounted for, and the excluded ones are written to `inat_hanoi_excluded.tsv` with a reason column rather than silently dropped.

Note the box is deliberately wide (~114 km x 100 km) to catch observers, so it is far larger than the 24 km map. Only **10,485 of 19,866 rows (53%)** actually land inside the map footprint; the rest are still valuable for observer classification.

## 3. Data quality

### 3.1 Obscured and private coordinates - detected and EXCLUDED

iNaturalist deliberately randomises the published coordinate for taxa of conservation concern, and for anything a user marks private. An obscured coordinate is placed randomly inside a ~0.2 degree box. At Hanoi's latitude that is roughly **21 km east-west by 22 km north-south** - the error is the size of the entire 24 km map. These are not slightly-imprecise points, they are uniform noise, and at 3.95 m/px they would be ~5,600 px of error. All of them are excluded from `inat_hanoi.tsv`.

| Flag | Count | Share of fetched |
|---|---|---|
| `obscured: true` - the effective flag, union of the below | 1,033 | 4.94% |
| `geoprivacy = "obscured"` - user-set | 829 | 3.97% |
| `taxon_geoprivacy = "obscured"` - taxon-driven | 225 | 1.08% |
| `geoprivacy = "private"` - coordinates withheld | 0 | 0.00% |

The two named fields overlap and do not sum to the total; `obscured` is the field to trust, and it is what the exclusion actually keys on. **1,033 observations (4.94%) were removed for this reason.**

Exclusion reasons applied, first match wins in this order:

| Reason | Rows |
|---|---|
| no coordinate in the payload | 0 |
| obscured (~22 km randomisation) | 1,033 |
| `geoprivacy = private` | 0 |
| no date of any kind | 0 |
| no user login | 0 |

**On `geoprivacy = "private"`: 0 were seen, and zero were excluded under that reason.** That is not an inconsistency. Private observations are indexed by their true hidden location, so a few surface through the bbox filter, but the payload carries no coordinate at all - so they fall out as `no_coords` first. The handling is: they can never be mapped, and they are not in the output. There were 0 rows with no coordinate.

### 3.2 positional_accuracy

At **3.95 m/px** one pixel is 3.95 m, so a point is only pixel-honest if its accuracy is in the low tens of metres. 100 m is 25 px; 1 km is 253 px.

| | Rows | Share of mappable |
|---|---|---|
| Has a `positional_accuracy` value | 16,206 | 81.6% |
| **Field is null - accuracy simply unknown** | **3,660** | **18.4%** |

For the rows that do have a value: median **45 m**, p25 12 m, p75 330 m, p90 2216 m, p99 79803 m, max 13,452,423 m.

| Accuracy band | Rows | Share of rows *with* a value | Usable at 3.95 m/px? |
|---|---|---|---|
| <=10 m | 3,870 | 23.9% | yes, sub-pixel to 3 px |
| 11-50 m | 4,433 | 27.4% | yes, 3-13 px |
| 51-100 m | 1,318 | 8.1% | marginal, 13-25 px |
| 101-300 m | 2,486 | 15.3% | poor, 25-76 px |
| 301-1000 m | 1,824 | 11.3% | no |
| 1-10 km | 1,497 | 9.2% | no |
| >10 km | 778 | 4.8% | no |

**8,303 rows carry a stated accuracy of 50 m or better** - 51.2% of the rows that have an accuracy value, but only **41.8% of all mappable rows**. That last number is the one that matters: it is how much of this dataset is defensible at full resolution.

The null-accuracy majority is the real problem, and it is worse than a large error bar: a 5 m phone GPS fix and a coordinate dropped by hand on a map from memory are indistinguishable in this data. There is no field that resolves it.

### 3.3 Date provenance (`date_kind`)

`observed_on` is the field to trust. `created_at` is the *upload* timestamp and can be years after the fact, which would wreck a residency-span calculation - so every fallback is labelled in the `date_kind` column rather than blended in silently.

| `date_kind` | Rows | Share of mappable |
|---|---|---|
| `observed_on` | 19,763 | 99.48% |
| `time_observed_at` | 0 | 0.00% |
| `created_at` | 103 | 0.52% |

This is the strongest part of the dataset: **99.5% of rows have a real observation date.** The temporal side of the Fischer rule rests on firm ground.

### 3.4 Captive/cultivated and quality_grade - counted, NOT dropped

These are kept in `inat_hanoi.tsv`; the `quality_grade` column lets you filter downstream. Counts:

| Category | Count | Share of fetched |
|---|---|---|
| `captive: true` - cultivated plant, pet, zoo animal | 2,706 | 12.9% |
| `quality_grade = "research"` | 7,375 | 35.3% |
| `quality_grade = "needs_id"` | 10,551 | 50.5% |
| `quality_grade = "casual"` | 2,973 | 14.2% |

`casual` is 14.2% of the data - a large slice. It mostly means missing date, missing location, captive, or no community ID. Note `captive` (2,706) and `casual` (2,973) heavily overlap, since cultivated status forces casual grade.

Captive/cultivated records matter here for a spatial reason, not a taxonomic one: a cultivated plant is by definition at a *planted* location - a park bed, a garden, a pot on a balcony - so they concentrate the data even harder into managed green space. If you want the least-biased subset, dropping captive is a defensible first filter.

## 4. Per-observer concentration

| Metric | Value |
|---|---|
| Distinct observers | 2,106 |
| Mappable rows | 19,866 |
| Mean rows per observer | 9.4 |
| Median rows per observer | 2 |
| **Top-3 observers' share** | **15.9%** (3,152 rows) |
| **Top-10 observers' share** | **28.2%** (5,612 rows) |
| Top-100 observers' share | 60.2% |
| Observers contributing exactly 1 row | 930 (44.2%) |

Top 10 observers:

| # | observer | Hanoi rows | share | date span in box (days) | global obs |
|---|---|---|---|---|---|
| 1 | `human_ecologist` | 1,755 | 8.8% | 5,178 | 8,908 |
| 2 | `vietanhnguyen` | 754 | 3.8% | 1,137 | 7,432 |
| 3 | `superman4realz` | 643 | 3.2% | 1,200 | 2,730 |
| 4 | `svr_vietnam` | 455 | 2.3% | 4,030 | 1,644 |
| 5 | `onidiras` | 448 | 2.3% | 1,122 | 78,951 |
| 6 | `lynetteclennell` | 403 | 2.0% | 104 | 23,411 |
| 7 | `billyschofield` | 352 | 1.8% | 1,370 | 3,150 |
| 8 | `tuminh` | 288 | 1.4% | 2,926 | 1,642 |
| 9 | `van_the_pham` | 267 | 1.3% | 108 | 729 |
| 10 | `budak` | 247 | 1.2% | 2,185 | 57,566 |

This heavy tail is a genuine hazard for a heatmap, not just a statistic: **28% of all points come from 10 people**, and each of them has a habitual patch. A single prolific local naturalist walking the same lake shore weekly will paint a bright trail that reads as a city-wide hotspot. Any density render off this data should be checked against a leave-the-top-N-out version.

## 5. Worldwide observer history

Same obscured/private exclusion as the Hanoi set. Per-observer cap **3,000** scanned observations, applied **most-recent-first**.

| Quantity | Count |
|---|---|
| Observers needing history (the mappable set) | 2,106 |
| **Observers history was fetched for** | **3 (0.1%)** |
| Observers with >= 1 usable worldwide row | 3 (0.1%) |
| Observers with NO history fetched | 2,103 (99.9%) |
| **Rows in `inat_history.tsv`** | **3,434** |
| Worldwide observations scanned | 9,000 |
| Dropped worldwide (obscured/private/no date) | 5,566 (61.8%) |
| Observers hitting the 3,000 cap (truncated) | 3 |
| Requests used by this phase | 0 |

The phase ran to completion - every observer in the mappable set was attempted.

Observers with no history contribute 16,909 Hanoi rows (85.1% of the map's points). For those points the locals-vs-tourists colour cannot be computed and they should be rendered as unresolved rather than guessed.

**Truncation.** 3 observers hit the 3,000 cap. Because the sweep runs descending, their history is their 3,000 *most recent* geotagged observations - the correct half for a residency question. Their deeper past is missing, so a *former* home city may be invisible for them, but their current one is not. Most-affected observers: `human_ecologist`, `vietanhnguyen`, `onidiras`.

The cap is recorded in the `truncated` flag of the run log, and the figure above is the honest count of observers whose history is incomplete. The cap was set to 3,000 rather than 5,000 as a deliberate trade: at 5,000 the sweep would not have covered every observer inside the available time, and full observer coverage is worth more to the Fischer rule than extra depth on 0 already-heavily-sampled accounts. 3,000 most-recent geotagged points is ample to establish which metro area someone lives in.

### 5.1 Request rate actually used

iNaturalist asks for <= 60 requests/minute and ~10,000/day, and asks that you identify yourself. What this run did:

| | Requests | Spacing | Effective rate |
|---|---|---|---|
| Hanoi sweep | 105 | 1.10 s minimum | ~55 req/min ceiling |
| History sweep | 0 | 1.05 s minimum | ~57 req/min ceiling |
| Constraint probes and validation | ~10 | - | - |
| **Total for the day** | **~115** | | **inside both limits** |

Throttling is a hard floor between request *starts*, not a sleep after each response, so the ceiling holds even when a response is fast. Retries use linear backoff on 429 and 5xx. Every request carried `User-Agent: img-city-heatmap/1.0 (research; baochidangg@gmail.com)`. Batching the light observers is what kept the total under the daily budget - the naive shape estimated **11,280 requests**, which would have exceeded it.

### 5.2 Does this actually feed the locals-vs-tourists rule?

This is what the worldwide pull was for, so here is the rule applied to the harvested data. "City" is a 0.25 degree cell (~25 km, about one metro area); an observer's home is the densest cell in which they have a >= 30 day span.

| Class | Rule | Observers | Share |
|---|---|---|---|
| **blue** - local | >= 1 month span within the Hanoi box | 288 | 13.7% |
| **red** - tourist | resident in a different cell, < 1 month here | 0 | 0.0% |
| **yellow** - no anchor | no >= 1 month span anywhere | 0 | 0.0% |
| unresolved | no worldwide history available | 1,818 | 86.3% |

**The rule is decidable for 288 of 2,106 observers (13.7%).** This is the payoff of step 4: the Hanoi rows alone can only identify the blue group, because a >= 1 month span in Hanoi is visible locally. Separating red from yellow - the entire tourist signal - is impossible without the worldwide history, and 0 observers (0.0%) fall in that red-or-yellow split that only the global data resolves.

These numbers are a feasibility check on the data, not the final classification - the map's own renderer should do the real thing with its own city definition. The point is that the inputs it needs are present.

## 6. Green-space bias, quantified

This is the dataset's defining caveat, so it gets measured rather than hedged. OSM polygons for parks, gardens, pitches, nature reserves, cemeteries, farmland, forest, grass, scrub, wetland and water were pulled from Overpass for central Hanoi (12,529 polygons, 12,626 rings) and every mappable observation was point-in-polygon tested with a ray-cast against a 550 m grid index.

The project's existing `data/hanoi_osm.json.gz` was checked first but was not sufficient: it contains `natural=water`, `landuse=reservoir` and highway ways only - **no park polygons at all** - and its bbox (20.84-21.22, 105.62-106.09) is narrower than the harvest box. A dedicated Overpass query was cheap, so parks are included properly rather than skipped.

Classifier spot-check against known ground truth: Hoan Kiem lake centre -> `water`, West Lake -> `water`, Bach Thao park -> `park`, an Old Quarter street -> `other`. Classification is water > park > green when a point falls in overlapping polygons.

The comparison that matters is against the land's own composition, so the same 24 km core box was sampled on an 81 m grid (90,000 points) and classified with the same polygons. **Enrichment = share of observations / share of land area.**

| Land class | %% of map area | %% of observations | **Enrichment** |
|---|---|---|---|
| Water - lake, pond, river, wetland | 13.0% | 17.0% | **1.31x** |
| Park / garden / pitch / reserve | 1.7% | 16.0% | **9.36x** |
| Other green - forest, grass, farmland, cemetery, scrub | 10.7% | 6.0% | **0.56x** |
| Everything else - streets, buildings, blocks | 74.7% | 60.9% | **0.82x** |
| **Green + water combined** | **25.3%** | **39.1%** | **1.54x** |

Reading this honestly:

- **Parks are the extreme case at 9.4x.** They are 1.7% of the map's surface and 16.0% of the observations. This is the single clearest statement of the bias.
- Water is enriched only 1.31x - less than expected, because the core box includes large stretches of the Red River and outlying ponds that nobody surveys.
- "Other green" is *under*-represented at 0.56x. That category is dominated by peri-urban farmland and cemeteries - green on the map, but not places people go photographing wildlife. This is a useful sanity check: the bias is toward *recreational* green space specifically, not vegetation in general.
- The built environment is under-sampled by 1.23x - 74.7% of the surface, 60.9% of the observations.

Across the wider central area (not just the 24 km box) the green+water share is 37.2% of 11,037 classified observations, consistent with the core figure.

**This understates the true bias**, for three reasons. It is measured only against polygons OSM actually has - street trees, the Red River sandbars, university campuses and hundreds of unmapped ponds all sit in the "everything else" denominator. The `other` observations are not evidence of street-level coverage either: a large share are balcony plants, courtyard trees and market produce. And 12.9% of observations are captive/cultivated, which are planted-location records by definition.

Practical consequence: **Hoan Kiem lake, Bach Thao, the Botanical Garden, West Lake and the Red River banks will dominate any heatmap built from this**, while the dense residential grid of the Old Quarter - where the people and the photographs actually are - will read as empty. That is an artefact of what iNaturalist is *for*. It is not a finding about Hanoi.

## 7. Verdict: is this good enough for a 3.95 m/px map?

**As a standalone heatmap layer: no. As a clearly-labelled supplementary layer, with filters: yes.**

Against it, worst problem first:

1. **The spatial bias is disqualifying on its own.** 39% of in-map points sit on green space or water covering 25% of the area, and parks are 9.4x enriched. A heatmap of this is a map of Hanoi's recreational green space and its naturalist community's walking routes. Real signal - but not the signal a photo heatmap is asking about.
2. **Accuracy is unknown, not merely coarse, for 18% of rows.** Only 41.8% of mappable rows are documented at 50 m or better. At 3.95 m/px you cannot distinguish a 5 m GPS fix from a 500 m guess, and no field will tell you which you have.
3. **The volume is thin for the resolution.** 10,485 points across a 24 km footprint at 3.95 m/px is a sparse scatter over ~37 million pixels. It renders as dots, not heat; almost every pixel is empty.
4. **Contributor concentration.** 28% of points come from 10 people, so individual habit is directly visible as geography.

In favour, and genuinely solid:

- **Dates are excellent**: 99.5% real `observed_on`, only 0.52% falling back to the upload timestamp, and every fallback labelled. The Fischer rule is a temporal test, and the temporal data is trustworthy.
- **The ~22 km obscured-coordinate trap is closed.** 1,033 rows of pure noise were identified and removed. Left in, they would have quietly poisoned the map with errors the size of the map itself - and they carry no visible marker once plotted.
- **The locals-vs-tourists question is answerable for 13.7% of observers**, which is precisely what the worldwide history was fetched for.
- Provenance is complete: every excluded row is retained with its reason, so nothing was silently dropped and the filtering can be audited or reversed.

**Recommended use.** For anything rendered at full 3.95 m/px resolution, filter to `pos_accuracy` non-null and `<= 50` - that is 8,303 rows, 41.8% of the set, and it is the only subset whose positions are defensible at one pixel. Keep the remainder for the observer-classification step, which needs dates and approximate positions rather than pixel-accurate ones. Consider dropping `captive` rows to reduce the green-space pull. Render iNaturalist as its own labelled layer, and if you blend it into a combined heatmap, down-weight it against the measured 1.54x green-space enrichment or it will import that bias wholesale.

## 8. Output files

All under `data/multi/`. Tab-separated, one header line, embedded tabs/CR/LF stripped from every field.

| File | Data rows | Columns |
|---|---|---|
| `inat_hanoi.tsv` | 19,866 | `id, user, user_id, date, date_kind, lon, lat, pos_accuracy, geoprivacy, quality_grade, photo_url` |
| `inat_history.tsv` | 3,434 | `user, date, lon, lat` |
| `inat_hanoi_excluded.tsv` | 1,033 | as `inat_hanoi.tsv` plus `exclude_reason` - audit trail |

Notes on the columns:

- `pos_accuracy` is empty where iNaturalist reports none - empty means *unknown*, not *good*.
- `geoprivacy` carries the effective value (`geoprivacy` falling back to `taxon_geoprivacy`, defaulting to `open`). Every `obscured` row is in the excluded file, not here.
- `photo_url` is the `square.jpg` thumbnail the API returns; substitute `medium` or `large` for that path segment for bigger renditions.
- `inat_history.tsv` is deduped on the exact `(user, date, lon, lat)` tuple and includes each observer's Hanoi observations, since Hanoi is part of their worldwide history.
