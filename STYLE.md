# Style spec: Erica Fischer's *Locals and Tourists*, 2010

What the series actually does, what we verified, and what we guessed.

**This file was rewritten after an adversarial review.** Its first version was
derived entirely from the 800×800 published downscale and got several things
badly wrong — it concluded the series draws only dots (it draws connecting
lines too, and in this build they are 63% of the data ink: 74,783 px against
44,518 for points), it described an alpha/gamma
density model (the point marks are opaque), and it said Web Mercator at 800×800
(it is equirectangular at 6137×6137). Measuring a 7.7× downscale of 1-px marks
was the root error. The native original is the authority. That history is kept
here deliberately, because the same trap is easy to fall back into.

## Evidence tiers

- **[V]** verified by measurement on the 6137×6137 original. That file is
  **not redistributed in this repo** (it is Fischer's copyrighted image); only
  the 800×800 downscale `data/ref_london.jpg` ships. To re-run a [V]
  measurement, fetch the native original from the author's Flickr page. The
  800px file is *not* a substitute: measuring it is what produced this
  document's original errors.
- **[Q]** quoted from Fischer's own descriptions
- **[C]** our choice or our arithmetic, not established from the originals
- **[F]** read from a file Fischer published (not from the image)

## Canvas and framing

| | | |
|---|---|---|
| size | 6137 × 6137 | [V] |
| background | `#FFFFFF`, the modal colour at 13.0% of the image | [V] |
| projection | cylindrical equirectangular, scaled so a degree of latitude and a degree of longitude cover equal ground at the box centre — *not* Mercator. The parenthetical *"Maybe I should have used Mercator instead"* is [Q]; the projection description itself is [C], inferred from the bounds file's Δlat/Δlon ratio | [Q]+[C] |
| bounds | Fischer's own precomputed 15-mile city box; Δlat = 0.217705° exactly, Δlon = Δlat / cos(lat) | [F] read from Fischer's bounds file |
| Hanoi box | `20.926386 105.728233 21.144091 105.961466` → 24.235 km N-S × 24.233 km E-W (ratio 0.99993), 3.949 m/px | [C] arithmetic on that file |

Source of the Hanoi box: `flickr-picasa/124` in
[github.com/e-n-f/bounds](https://github.com/e-n-f/bounds) (e-n-f = Erica
Fischer), which contains those four numbers verbatim next to
`Hanoi, Ha Noi, Vietnam`; that repository's README states the `flickr-picasa`
boxes are the ones used for the Geotaggers' World Atlas and Locals and Tourists
sets. Fischer computed a Hanoi box but never published a Hanoi map.

## Colours [V]

| feature | colour | how measured |
|---|---|---|
| background | `#FFFFFF` | modal value, 13.04% of pixels |
| basemap strokes | `#AAAA00` | integrated channel deficit over near-data-free tiles, reproducible with `tools/measure_style.py`: **0.353 : 0.369 : 1.000** over 70 32×32 tiles, against #AAAA00's predicted 0.333 : 0.333 : 1.000. `#FFFF00` (0.000) and `#BBBB00` (0.267) are excluded; this tile set does **not** cleanly separate `#AAAA00` from `#999900` (0.400), though a tighter hand-built set (0.344 : 0.351 over 20 tiles) does |
| locals | `#0000FF` | eroded homogeneous interiors (immune to 4:2:0 chroma bleed) |
| tourists | `#FF0000` | same, measured (253.93, 0.15, 0.14) |
| unknown | `#FFFF00` | same, measured (249.99, 248.43, 20.98) |

Naive per-pixel reads of the original lie: it is JPEG q75 4:2:0.

## Marks

| | | |
|---|---|---|
| points | 3 × 3 px **opaque** squares; isolated-dot bounding-box mode is (3,3) in the original | [V] |
| lines | 1 px, joining one photographer's consecutive photos taken within a short time and distance | [Q] |
| line opacity | **0.55**, accumulating as 1−(1−α)^n on overlap | [V] |
| no road-class hierarchy | one hue (the deficit ratio above) and a 1-px modal stroke width, with no width classes | [V] |
| no fills | water bodies appear as bare outlines | [V] |
| draw order | points interleaved across classes and drawn last; lines composited per class, so cross-class line crossings have an order within a render and do not accumulate (3,636 px, measured from the drawing ops) | [C] |

Fischer, on the marks (this quotation and the Mercator one come from the
album and photo descriptions and comments as read during this project's style
research; the London photo's own visible caption carries only "Blue pictures
are by locals. Red pictures are by tourists. Yellow pictures might be by
either.", so treat the longer quotations as reported rather than re-verified
here): *"the individual points are plotted as well as lines
when someone took two pictures within a reasonably short time and distance of
each other. When lines appear thicker, it is just because two lines that were
nearly the same were drawn on top of each other."* [Q] That last clause is
about apparent *thickness*; it is not a statement that density does not
accumulate, and measurement shows it does (below).

**Line opacity, measured with no estimator.** For blue ink over white the red
channel is exactly 255(1−α). So an isolated single segment on a white
background states its own alpha in one pixel, and no calibration is needed.
Select London blue pixels that survive a 15 px directional opening, sit ≥3 px
from any 3×3 core, carry no `#AAAA00` or red ink within 2 px, and are locally
sparse (blue coverage < 3% in a 31×31 window):

    n = 981 clean isolated line pixels, median R = 115  ->  alpha 0.549
    red lines: n = 187, median B = 118                 ->  alpha 0.537
    255 x 0.45 = 114.75

**These figures are mask-sensitive and are reported, not settled to three
decimals.** Three independent implementations of the selection written above
give 0.55/0.54, 0.64/0.53 (`tools/measure_style.py`) and 0.66/0.58. What is
robust across all of them is the pair of conclusions: alpha is near 0.55, well
below the opaque points, and overlap accumulates. Do not quote a third decimal.

**Overlap accumulates.** Stratifying the same pixels by local blue density, the
median core red channel steps 114 → 59 → 50 → 47, against 115 predicted for
one 0.55 line and 52 for two. Coincident segments therefore compose as
1−(1−α)^n, and this renderer reproduces the rungs exactly (measured on its own
output: R = 115, 52, 23, 10 for n = 1..4 over white, and 76, 34, 15, 7 for the
same over a `#AAAA00` road — i.e. 255·0.45ⁿ and 170·0.45ⁿ).

Three earlier answers were wrong. They are recorded because each failed in a
way that is easy to repeat:

1. A line-vs-point **coverage ratio** (0.893) was read as "lines are opaque". A
   coverage ratio between 1-px lines and 3×3 squares conflates geometry with
   opacity and cannot measure per-pixel alpha at all.
2. A perpendicular integrated-deficit estimator gave ~0.55 — the right answer,
   but it was calibrated on synthetic imagery, so it was not trusted.
3. A luma estimator gave 0.896 and was "validated" against this renderer's own
   output, which reproduced a known 0.55 to within 0.004. The validation was
   circular: at that time the renderer composited each class as a single layer,
   so its output was **constant-alpha by construction** and could not exhibit
   the very mixture being measured. 98.5% of that estimator's London mask was
   overlapping bundles, and its 0.896 was the median effective alpha of a
   mixture. Its "opaque control" (3×3 cores at 0.989) was also not
   like-for-like: large solid agglomerations survive JPEG better than 1-px
   features.

The lesson worth keeping: validate an image estimator on a file whose ground
truth you control **and** whose structure matches the target. A constant-alpha
control cannot validate a measurement on an accumulating image.

## Colour mixing in the downscales

The point marks are opaque, so the soft colours of the small published versions
are mostly just area-averaging. For a downscaled block, let `f_b`, `f_r`, `f_y`
be the fractions covered by opaque POINTS, `f_m` the basemap, and `g_b`, `g_r`,
`g_y` the fractions covered by LINES weighted by their effective alpha:

    R/255 = 1 - f_b - g_b - f_m/3
    G/255 = 1 - f_b - f_r - g_b - g_r - f_m/3
    B/255 = 1 - f_r - f_y - g_r - g_y - f_m

Both extra terms are load-bearing. `#AAAA00` removes a third of R and G and all
of B and covers ~7% of the canvas, more than all the data ink together;
omitting it (as an earlier version of this file did) makes the relation wrong
by up to 210/255. Omitting the alpha weighting on the line terms costs up to
113/255 on ~1% of the published downscale — and lines are 63% of the data ink.
Downscale with **box** averaging, which is what the relation describes; Lanczos
ringing sprays grey haloes over pixels that should be white.

A caution about testing this relation: "99.2% of the reference's pixels invert
through it with non-negative ink" sounds like strong support and is nearly
worthless. At zero tolerance the figure is **65.2%**; reaching ~99% needs an
unstated slack of roughly ±8/255 (`tools/measure_style.py` gives 99.16% there;
an independent exact-pixel LP gives 99.94% — the exact percentage depends on
how the constraint is posed, which is itself a sign the test is soft). Worse, all six permutations of the blue/red/yellow ink roles score
**identically**, and a no-model baseline scores **100%** — the test cannot
distinguish which colour means which class, which is the model's whole content.
Do not cite it.

## Classification [Q]

- blue **locals** — photographed in this city over a range of a month or more
- red **tourists** — appear to be a local of a different city, here under a month
- yellow **unknown** — have not photographed anywhere over a span of a month

Mechanism: Fischer precomputed a global set of 15-mile city boxes (~3015 files in
the published bounds repository — a file count, not a quotation) and, per photographer
per box, took the date span; span ≥ one month makes them a resident of that box.
"Unknown" is global, not per-city. Undocumented: the threshold in days, and any
minimum photo count. One stated cleaning rule: photowalks and automated cameras
were excluded from the residency computation but still plotted.

## Still unresolved

- The exact distance/speed caps on connecting lines in the 2010 renderer. The
  15,000 ft and 85 mph values come from Fischer's 2015 code. [C]
- The line-opacity falloff *law*. Sub-unity opacity is measured; whether it
  varies with segment length, and how, is not established. We use a constant.
- Whether points are drawn over lines, or fully interleaved with them. [C]
- The exact OSM feature selection in the original basemap.

The 2010 renderer's source was never published, as Fischer has said.
